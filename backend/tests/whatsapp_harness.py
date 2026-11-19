"""A fake WhatsApp Cloud API and signed webhook deliveries, for testing the whole channel."""

from __future__ import annotations

import hashlib
import hmac
import itertools
import json
from collections import deque
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.db.pool import Database
from app.main import create_app
from app.whatsapp.client import WhatsAppClient
from tests.samples import FakeRenderer, make_settings

APP_SECRET = "test-app-secret"
VERIFY_TOKEN = "test-verify-token"
ACCESS_TOKEN = "test-access-token"
PHONE_NUMBER_ID = "15550001111"
MEDIA_HOST = "lookaside.fbsbx.com"

ALICE = {"user_id": "NG.1000000000000000001", "phone": "2348000000001"}
BOLA = {"user_id": "NG.1000000000000000002", "phone": "2348000000002"}


def whatsapp_settings(database_url: str, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": database_url,
        "whatsapp_access_token": ACCESS_TOKEN,
        "whatsapp_phone_number_id": PHONE_NUMBER_ID,
        "whatsapp_app_secret": APP_SECRET,
        "whatsapp_verify_token": VERIFY_TOKEN,
        "whatsapp_worker_in_process": False,
        "engine_worker_in_process": False,
        "whatsapp_album_wait_seconds": 0,
        "whatsapp_album_max_wait_seconds": 0,
        "whatsapp_retry_base_seconds": 0,
        "whatsapp_delay_notice_seconds": 0,
        "public_web_url": "https://chatreceipt.test",
    }
    values.update(overrides)
    return make_settings(**values)


class FakeGraph:
    """Stands in for graph.facebook.com and Meta's media host."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.media: dict[str, bytes] = {}
        self.media_info: dict[str, dict[str, Any]] = {}
        self.send_errors: deque[httpx.Response] = deque()
        self.upload_errors: deque[httpx.Response] = deque()
        self.lookup_errors: deque[httpx.Response] = deque()
        self.uploads: list[bytes] = []
        self.requests: list[httpx.Request] = []
        self._ids = itertools.count(1)

    def add_image(self, media_id: str, data: bytes, **info: Any) -> None:
        self.media[media_id] = data
        self.media_info[media_id] = info

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        parts = urlsplit(str(request.url))
        if parts.hostname == MEDIA_HOST:
            media_id = parts.query.removeprefix("mid=")
            return httpx.Response(200, content=self.media[media_id]) if media_id in self.media else httpx.Response(404)
        if request.method == "POST" and parts.path.endswith("/media"):
            if self.upload_errors:
                return self.upload_errors.popleft()
            self.uploads.append(request.content)
            return httpx.Response(200, json={"id": f"media-out-{next(self._ids)}"})
        if request.method == "POST" and parts.path.endswith("/messages"):
            body = json.loads(request.content)
            if body.get("status") == "read":
                return httpx.Response(200, json={"success": True})
            if self.send_errors:
                return self.send_errors.popleft()
            self.sent.append(body)
            return httpx.Response(200, json={"messages": [{"id": f"wamid.out{next(self._ids)}"}]})
        if self.lookup_errors:
            return self.lookup_errors.popleft()
        media_id = parts.path.rsplit("/", 1)[-1]
        if media_id not in self.media:
            return httpx.Response(404, json={"error": {"code": 100, "message": "Unsupported get request."}})
        info = {
            "messaging_product": "whatsapp",
            "url": f"https://{MEDIA_HOST}/whatsapp_business/attachments/?mid={media_id}",
            "mime_type": "image/png",
            "file_size": len(self.media[media_id]),
            "id": media_id,
        }
        info.update(self.media_info.get(media_id, {}))
        return httpx.Response(200, json=info)

    # -- reading what was sent -----------------------------------------------------------------

    @staticmethod
    def body_of(message: dict[str, Any]) -> str:
        if message["type"] == "text":
            return message["text"]["body"]
        if message["type"] in ("image", "document"):
            return message[message["type"]]["caption"]
        return message["interactive"]["body"]["text"]

    def bodies(self) -> list[str]:
        return [self.body_of(message) for message in self.sent]

    def last_body(self) -> str:
        return self.body_of(self.sent[-1])

    def everything_said(self) -> str:
        return "\n".join(self.bodies())

    @staticmethod
    def options_of(message: dict[str, Any]) -> dict[str, str]:
        """Title -> reply ID for a message's buttons or list rows."""
        if message["type"] != "interactive":
            return {}
        action = message["interactive"]["action"]
        if "buttons" in action:
            return {button["reply"]["title"]: button["reply"]["id"] for button in action["buttons"]}
        return {row["title"]: row["id"] for section in action["sections"] for row in section["rows"]}

    def option(self, title: str) -> str:
        """The reply ID of the most recent message offering this button or row."""
        for message in reversed(self.sent):
            options = self.options_of(message)
            if title in options:
                return options[title]
        raise AssertionError(f"No message offers {title!r}. Sent: {self.bodies()}")

    def offered(self) -> set[str]:
        return set(self.options_of(self.sent[-1]))


class Channel:
    """The app under test with a signed-delivery helper and a way to run its background work."""

    def __init__(self, client: TestClient, graph: FakeGraph, database: Database) -> None:
        self.client = client
        self.graph = graph
        self.database = database
        self._ids = itertools.count(1)
        self._clock = itertools.count(1_790_000_000, 5)

    def post(self, body: bytes, signature: str | None = "auto") -> httpx.Response:
        headers = {"Content-Type": "application/json"}
        if signature == "auto":
            signature = "sha256=" + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
        if signature is not None:
            headers["X-Hub-Signature-256"] = signature
        return self.client.post("/api/whatsapp/webhook", content=body, headers=headers)

    def _message(self, who: dict[str, str], kind: str, message_id: str | None = None, **fields: Any) -> dict[str, Any]:
        message: dict[str, Any] = {
            "id": message_id or f"wamid.in{next(self._ids)}",
            "timestamp": str(next(self._clock)),
            "type": kind,
            **fields,
        }
        if who.get("phone"):
            message["from"] = who["phone"]
        if who.get("user_id"):
            message["from_user_id"] = who["user_id"]
        return message

    def payload(self, *messages: dict[str, Any], phone_number_id: str = PHONE_NUMBER_ID, **value: Any) -> bytes:
        return json.dumps(
            {
                "object": "whatsapp_business_account",
                "entry": [
                    {
                        "id": "1234",
                        "changes": [
                            {
                                "field": "messages",
                                "value": {
                                    "messaging_product": "whatsapp",
                                    "metadata": {"display_phone_number": "15550000000", "phone_number_id": phone_number_id},
                                    "messages": list(messages),
                                    **value,
                                },
                            }
                        ],
                    }
                ],
            }
        ).encode()

    def deliver(self, *messages: dict[str, Any]) -> httpx.Response:
        return self.post(self.payload(*messages))

    def text_event(self, who: dict[str, str], body: str, message_id: str | None = None) -> dict[str, Any]:
        return self._message(who, "text", message_id, text={"body": body})

    def image_event(self, who: dict[str, str], media_id: str, *, message_id: str | None = None, sha256: str | None = None) -> dict[str, Any]:
        return self._message(who, "image", message_id, image={"id": media_id, "mime_type": "image/png", "sha256": sha256 or f"hash-{media_id}"})

    def tap_event(self, who: dict[str, str], option_id: str, message_id: str | None = None) -> dict[str, Any]:
        return self._message(who, "interactive", message_id, interactive={"type": "button_reply", "button_reply": {"id": option_id, "title": "x"}})

    def say(self, who: dict[str, str], body: str) -> None:
        assert self.deliver(self.text_event(who, body)).status_code == 200
        self.work()

    def send_image(self, who: dict[str, str], media_id: str, **fields: Any) -> None:
        assert self.deliver(self.image_event(who, media_id, **fields)).status_code == 200
        self.work()

    def tap(self, who: dict[str, str], title: str) -> None:
        assert self.deliver(self.tap_event(who, self.graph.option(title))).status_code == 200
        self.work()

    def work(self) -> None:
        self.client.portal.call(self.client.app.state.whatsapp_worker.drain)  # type: ignore[union-attr,attr-defined]

    def rows(self, sql: str, *parameters: Any) -> list[dict[str, Any]]:
        with self.database.transaction() as conn:
            return conn.execute(sql, parameters).fetchall()


def open_channel(
    database_url: str,
    provider: Any,
    *,
    graph: FakeGraph | None = None,
    database_factory: Callable[[str], Database] = Database,
    renderer: FakeRenderer | None = None,
    **settings: Any,
) -> tuple[TestClient, Channel]:
    graph = graph or FakeGraph()
    config = whatsapp_settings(database_url, **settings)
    database = database_factory(database_url)
    whatsapp_client = WhatsAppClient(config, transport=httpx.MockTransport(graph))
    app = create_app(settings=config, provider=provider, renderer=renderer or FakeRenderer(), database=database, whatsapp_client=whatsapp_client)
    client = TestClient(app)
    client.__enter__()
    return client, Channel(client, graph, database)
