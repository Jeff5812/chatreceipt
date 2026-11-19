"""The engine under test: the real app and database, with a scripted AI and a captured webhook receiver."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from fastapi.testclient import TestClient

from app.db.pool import Database
from app.extraction.provider import ProviderResponse, ProviderUsage, StructuredRequest
from app.main import create_app
from app.storage import LocalStorage
from tests.intel import answer, deal, event, value
from tests.samples import FakeRenderer, make_settings

CHAT = """\
Ada: Good afternoon, I need two of the black tote bags
Tola Bags: Good afternoon! They are ₦12,000 each
Ada: Can you do ₦10,000?
Tola Bags: ₦11,000 is my last price. Delivery to Yaba is ₦2,000
Ada: Okay, deal. I'll transfer tomorrow morning
"""


def chat_answer(**overrides: Any) -> dict[str, Any]:
    """A correct reading of CHAT. In that text, p1 is Ada (buyer) and p2 is Tola Bags (seller)."""
    events = [
        event("m1", "request", "p1", quantity="2"),
        event("m2", "offer", "p2", unit_price="12000"),
        event("m3", "counter_offer", "p1", unit_price="10000", responds_to="m2"),
        event("m4", "rejection", "p2", responds_to="m3"),
        event("m4", "offer", "p2", unit_price="11000"),
        event("m4", "delivery_fee", "p2", item="delivery", amount="2000"),
        event("m5", "acceptance", "p1", item="", responds_to="m4"),
        event("m5", "payment_intention", "p1", item=""),
    ]
    terms = {"payment_terms": value("Buyer says they will transfer tomorrow morning.", evidence=["m5"])}
    fields: dict[str, Any] = {"items": [("i1", "Black tote bag")], "payment_status": "unpaid", "terms": terms}
    fields.update(overrides)
    return answer(deal(events, **fields), roles={"p1": "buyer", "p2": "seller"})


def transcript_answer(*messages: tuple[str, str, str], number: int = 1, header: str = "Tola Bags") -> dict[str, Any]:
    return {
        "screenshots": [
            {
                "screenshot": number,
                "app": "whatsapp",
                "header_name": header,
                "is_group": False,
                "cut_off_top": False,
                "cut_off_bottom": False,
                "messages": [
                    {"side": side, "sender": "", "time": time, "day": "", "kind": "text", "text": text, "quoted": "", "legibility": "clear"}
                    for side, time, text in messages
                ],
            }
        ]
    }


class ScriptedModel:
    """Answers each AI task from its own script. The last scripted answer repeats."""

    name = "scripted"
    configured = True

    def __init__(self, *interpretations: Any, transcriptions: list[Any] | None = None) -> None:
        self.interpretations = list(interpretations) or [chat_answer()]
        self.transcriptions = list(transcriptions or [])
        self.calls: list[str] = []
        self.requests: list[StructuredRequest] = []

    async def generate(self, request: StructuredRequest) -> ProviderResponse:
        self.calls.append(request.task)
        self.requests.append(request)
        script = self.transcriptions if request.task == "transcription" else self.interpretations
        outcome = script.pop(0) if len(script) > 1 else script[0]
        if isinstance(outcome, BaseException):
            raise outcome
        return ProviderResponse(data=outcome, usage=ProviderUsage(model="scripted-1", input_tokens=100, output_tokens=50), duration_ms=7)

    async def extract(self, images: Any) -> ProviderResponse:
        raise AssertionError("The engine must not use the single-step extraction.")

    async def aclose(self) -> None:
        return None


class Engine:
    def __init__(self, client: TestClient, database: Database, model: ScriptedModel, storage_root: Path, webhooks: list[httpx.Request]) -> None:
        self.client = client
        self.database = database
        self.model = model
        self.storage_root = storage_root
        self.webhooks = webhooks

    @property
    def worker(self) -> Any:
        return self.client.app.state.engine_worker  # type: ignore[attr-defined]

    def work(self) -> None:
        self.client.portal.call(self.worker.drain)

    def rows(self, sql: str, *parameters: Any) -> list[dict[str, Any]]:
        with self.database.transaction() as conn:
            return conn.execute(sql, parameters).fetchall()

    def execute(self, sql: str, *parameters: Any) -> None:
        with self.database.transaction() as conn:
            conn.execute(sql, parameters)

    def sign_in(self, name: str = "alice", *, expires_in: str = "1 hour") -> dict[str, str]:
        token = f"test-session-{name}-{expires_in.replace(' ', '')}"
        with self.database.transaction() as conn:
            customer = conn.execute(
                """
                INSERT INTO chatreceipt.customers (external_ref) VALUES (%s)
                ON CONFLICT (external_ref) DO UPDATE SET last_message_at = now()
                RETURNING id
                """,
                (name,),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO chatreceipt.web_sessions (token_hash, customer_id, expires_at)
                VALUES (%s, %s, now() + %s::interval)
                ON CONFLICT (token_hash) DO NOTHING
                """,
                (hashlib.sha256(token.encode()).hexdigest(), customer["id"], expires_in),  # type: ignore[index]
            )
        return {"Authorization": f"Bearer {token}"}

    def customer_id(self, name: str = "alice") -> UUID:
        return self.rows("SELECT id FROM chatreceipt.customers WHERE external_ref = %s", name)[0]["id"]

    def submit(self, headers: dict[str, str], text: str = CHAT, **body: Any) -> dict[str, Any]:
        response = self.client.post("/api/v1/conversations", json={"text": text, **body}, headers=headers)
        assert response.status_code == 202, response.text
        return response.json()

    def ready(self, headers: dict[str, str], text: str = CHAT) -> dict[str, Any]:
        """Submit a conversation, read it, and return the transaction waiting for review."""
        accepted = self.submit(headers, text)
        self.work()
        return self.get(headers, accepted["transaction_id"])

    def get(self, headers: dict[str, str], transaction_id: str) -> dict[str, Any]:
        response = self.client.get(f"/api/v1/transactions/{transaction_id}", headers=headers)
        assert response.status_code == 200, response.text
        return response.json()

    def act(self, headers: dict[str, str], transaction: dict[str, Any], action: str, **body: Any) -> httpx.Response:
        return self.client.post(
            f"/api/v1/transactions/{transaction['id']}/{action}", json={"expected_version": transaction["version"], **body}, headers=headers
        )

    def approved(self, headers: dict[str, str], text: str = CHAT) -> dict[str, Any]:
        response = self.act(headers, self.ready(headers, text), "approve")
        assert response.status_code == 200, response.text
        return response.json()


def open_engine(
    database_url: str,
    model: ScriptedModel,
    storage_root: Path,
    *,
    database_factory: Callable[[str], Database] = Database,
    webhook_handler: Callable[[httpx.Request], httpx.Response] | None = None,
    renderer: FakeRenderer | None = None,
    **settings: Any,
) -> tuple[TestClient, Engine]:
    webhooks: list[httpx.Request] = []

    def receive(request: httpx.Request) -> httpx.Response:
        webhooks.append(request)
        return webhook_handler(request) if webhook_handler else httpx.Response(200)

    values: dict[str, Any] = {
        "database_url": database_url,
        "engine_worker_in_process": False,
        "engine_retry_base_seconds": 0,
        "v1_processing_rate_limit": 500,
        "v1_write_rate_limit": 5000,
        "v1_upload_rate_limit": 500,
    }
    values.update(settings)
    database = database_factory(database_url)
    app = create_app(
        settings=make_settings(**values),
        provider=model,  # type: ignore[arg-type]
        renderer=renderer or FakeRenderer(),  # type: ignore[arg-type]
        database=database,
        storage=LocalStorage(storage_root),
        webhook_transport=httpx.MockTransport(receive),
    )
    client = TestClient(app)
    client.__enter__()
    return client, Engine(client, database, model, storage_root, webhooks)
