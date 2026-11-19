"""The engine end to end: submission, processing, review, approval, receipts and history."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest

from app.engine import jobs, outbox
from app.engine.failures import Failure
from app.extraction.provider import ErrorKind, ExtractionProviderError
from app.intelligence.fields import AGREEMENT, DUPLICATE_CHECK
from tests.engine_harness import CHAT, Engine, ScriptedModel, chat_answer, open_engine, transcript_answer
from tests.intel import answer, deal, event
from tests.samples import FakeRenderer, image_bytes, valid_draft


@pytest.fixture
def build(clean_database: str, tmp_path: Path) -> Iterator[Any]:
    opened: list[Any] = []

    def make(*interpretations: Any, transcriptions: list[Any] | None = None, **settings: Any) -> Engine:
        client, engine = open_engine(clean_database, ScriptedModel(*interpretations, transcriptions=transcriptions), tmp_path / "files", **settings)
        opened.append(client)
        return engine

    yield make
    for client in opened:
        client.__exit__(None, None, None)


def error_code(response: httpx.Response) -> str:
    return response.json()["error"]["code"]


# -- the whole journey --------------------------------------------------------------------------


def test_a_conversation_becomes_a_reviewed_record_with_a_receipt_and_full_history(build: Any) -> None:
    engine = build()
    alice = engine.sign_in()

    accepted = engine.submit(alice, title="Tote order")
    assert accepted["job"]["status"] == "queued"
    placeholder = engine.get(alice, accepted["transaction_id"])
    assert placeholder["state"] == "processing"

    engine.work()
    job = engine.client.get(f"/api/v1/jobs/{accepted['job']['id']}", headers=alice).json()
    assert job["status"] == "completed" and job["attempts"] == 1

    # Read correctly, with the backend's own totals and evidence for each value.
    record = engine.get(alice, accepted["transaction_id"])
    assert record["state"] == "awaiting_review" and record["agreement_status"] == "agreement_reached"
    assert (record["seller"], record["buyer"]) == ("Tola Bags", "Ada")
    assert record["draft"]["line_items"][0]["unit_price"] == "11000"
    assert record["calculation"]["total"] == "24000.00" and record["total"] == "24000"
    assert record["payment_status"] == "unpaid"
    assert "doesn't verify payments" in record["payment_notice"]
    price = record["fields"]["line_items.item-1.unit_price"]
    assert price["status"] == "confirmed" and price["grounded"] is True
    assert [e["message"] for e in price["evidence"]] == ["m4", "m5"]
    assert "₦11,000" in price["evidence"][0]["quote"]
    history = record["analysis"]["negotiation"]["items"][0]["history"]
    assert [(p["unit_price"], p["outcome"]) for p in history] == [("12000", "superseded"), ("10000", "rejected"), ("11000", "accepted")]

    # The conversation as it was read, with speakers.
    read = engine.client.get(f"/api/v1/conversations/{accepted['conversation_id']}/messages", headers=alice).json()
    assert [(p["name"], p["role"]) for p in read["participants"]] == [("Ada", "buyer"), ("Tola Bags", "seller")]
    assert len(read["messages"]) == 5

    # A person corrects a detail, approves, and a receipt is issued.
    corrected = engine.act(alice, record, "review", operations=[{"op": "set", "path": "delivery_fee", "value": "2500", "reason": "Seller confirmed by phone"}])
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["calculation"]["total"] == "24500.00" and corrected.json()["version"] == record["version"] + 1
    approved = engine.act(alice, corrected.json(), "approve")
    assert approved.status_code == 200, approved.text
    assert approved.json()["state"] == "approved"

    issued = engine.client.post(f"/api/v1/transactions/{record['id']}/receipts", headers=alice)
    assert issued.status_code == 201, issued.text
    receipt = issued.json()
    assert receipt["reference"] == "RCT-000001" and receipt["document_status"] == "pending"
    engine.work()
    document = engine.client.get(f"/api/v1/receipts/{receipt['id']}/document", headers=alice)
    assert document.status_code == 200 and document.content.startswith(b"%PDF")
    assert engine.client.get(f"/api/v1/receipts/{receipt['id']}", headers=alice).json()["document_status"] == "ready"

    # The audit trail keeps what the AI proposed, what changed, who changed it and why.
    audit = engine.client.get(f"/api/v1/transactions/{record['id']}/history", headers=alice).json()
    assert [e["event_type"] for e in audit["events"]] == ["processing_started", "extracted", "reviewed", "approved", "receipt_issued"]
    decision = audit["decisions"][0]
    assert (decision["field_path"], decision["extracted_value"], decision["previous_value"], decision["new_value"]) == ("delivery_fee", "2000", "2000", "2500")
    assert decision["reason"] == "Seller confirmed by phone" and decision["actor_id"] == str(engine.customer_id())
    first = engine.client.get(f"/api/v1/transactions/{record['id']}/versions/2", headers=alice).json()
    assert first["snapshot"]["draft"]["delivery_fee"] == "2000"

    # Approved records appear on the customer's records page.
    records_page = engine.client.get("/api/records", headers=alice).json()["records"]
    assert [(r["source"], r["total"]) for r in records_page] == [("engine", "24500")]


def test_screenshots_are_read_then_interpreted_and_the_reading_is_kept(build: Any) -> None:
    transcription = transcript_answer(
        ("outgoing", "10:02", "I need two of the black tote bags"),
        ("incoming", "10:03", "They are ₦12,000 each"),
        ("outgoing", "10:04", "Can you do ₦10,000?"),
        ("incoming", "10:05", "₦11,000 is my last price. Delivery to Yaba is ₦2,000"),
        ("outgoing", "10:06", "Okay, deal. I'll transfer tomorrow morning"),
    )
    events = [
        event("m1", "request", "p1", quantity="2"),
        event("m2", "offer", "p2", unit_price="12000"),
        event("m3", "counter_offer", "p1", unit_price="10000"),
        event("m4", "offer", "p2", unit_price="11000"),
        event("m4", "delivery_fee", "p2", item="delivery", amount="2000"),
        event("m5", "acceptance", "p1", item="", responds_to="m4"),
        event("m5", "payment_intention", "p1", item=""),
    ]
    engine = build(answer(deal(events, payment_status="unpaid"), roles={"p1": "buyer", "p2": "seller"}), transcriptions=[transcription])
    alice = engine.sign_in()
    uploaded = engine.client.post("/api/v1/uploads", files=[("files", ("shot.png", image_bytes(), "image/png"))], headers=alice)
    assert uploaded.status_code == 201, uploaded.text
    upload = uploaded.json()["uploads"][0]
    assert (upload["kind"], upload["content_type"], upload["width"]) == ("image", "image/png", 400)

    accepted = engine.client.post("/api/v1/conversations", json={"upload_ids": [upload["id"]]}, headers=alice).json()
    engine.work()
    record = engine.get(alice, accepted["transaction_id"])
    assert engine.model.calls == ["transcription", "interpretation"]
    assert record["calculation"]["total"] == "24000.00"
    assert record["seller"] == "Tola Bags"  # the chat header name, for the contact's side
    assert record["fields"]["line_items.item-1.unit_price"]["evidence"][0]["screenshot"] == 1
    assert engine.rows("SELECT count(*) AS n FROM chatreceipt.ocr_results") == [{"n": 1}]


def test_a_retry_doesnt_pay_to_read_the_screenshots_again(build: Any) -> None:
    transcription = transcript_answer(("incoming", "", "Bag is ₦5,000"), ("outgoing", "", "Ok deal"))
    events = [event("m1", "offer", "p1", unit_price="5000"), event("m2", "acceptance", "p2", responds_to="m1")]
    good = answer(deal(events))
    engine = build(ExtractionProviderError(ErrorKind.UNAVAILABLE), good, transcriptions=[transcription])
    alice = engine.sign_in()
    upload = engine.client.post("/api/v1/uploads", files=[("files", ("s.png", image_bytes(), "image/png"))], headers=alice).json()["uploads"][0]
    engine.client.post("/api/v1/conversations", json={"upload_ids": [upload["id"]]}, headers=alice)
    engine.work()
    assert engine.model.calls == ["transcription", "interpretation", "interpretation"]


def test_a_conversation_with_two_deals_becomes_two_transactions(build: Any) -> None:
    first = deal([event("m1", "offer", "p2", unit_price="5000"), event("m2", "acceptance", "p1", responds_to="m1")], key="t1", items=[("i1", "Cap")])
    second = deal([event("m3", "offer", "p2", unit_price="7000"), event("m4", "acceptance", "p1", responds_to="m3")], key="t2", items=[("i1", "Belt")])
    engine = build(answer(first, second, roles={"p1": "buyer", "p2": "seller"}))
    alice = engine.sign_in()
    text = "Ada: How much is the cap\nTola: ₦5,000\nAda: Ok deal"
    text = "Tola: Cap is ₦5,000\nAda: Ok\nTola: Belt is ₦7,000\nAda: Ok deal"
    # In this text p1 is Tola, so the scripted roles are swapped to match.
    swapped_first = deal([event("m1", "offer", "p1", unit_price="5000"), event("m2", "acceptance", "p2", responds_to="m1")], key="t1", items=[("i1", "Cap")])
    swapped_second = deal([event("m3", "offer", "p1", unit_price="7000"), event("m4", "acceptance", "p2", responds_to="m3")], key="t2", items=[("i1", "Belt")])
    engine.model.interpretations = [answer(swapped_first, swapped_second, roles={"p1": "seller", "p2": "buyer"})]
    engine.submit(alice, text)
    engine.work()
    listed = engine.client.get("/api/v1/transactions", headers=alice).json()["transactions"]
    assert sorted(t["total"] for t in listed) == ["5000", "7000"]
    assert {t["state"] for t in listed} == {"awaiting_review"}


def test_a_conversation_without_a_transaction_is_explained(build: Any) -> None:
    engine = build(answer(contains_transaction=False))
    alice = engine.sign_in()
    accepted = engine.submit(alice, "Ada: Are you coming tomorrow?\nTola: Yes, by 10")
    engine.work()
    record = engine.get(alice, accepted["transaction_id"])
    assert record["state"] == "failed" and "No transaction was found" in record["state_reason"]
    assert "enter_manually" in record["available_actions"]
    moved = engine.act(alice, record, "enter-manually")
    assert moved.status_code == 200 and moved.json()["state"] == "draft"


# -- review rules -------------------------------------------------------------------------------


def unclear_answer() -> dict[str, Any]:
    events = [event("m1", "offer", "p2", unit_price="12000"), event("m3", "counter_offer", "p1", unit_price="10000", responds_to="m2")]
    return answer(deal(events, items=[("i1", "Tote")], status="negotiation"), roles={"p1": "buyer", "p2": "seller"})


def test_approval_is_refused_until_everything_flagged_is_resolved(build: Any) -> None:
    engine = build(unclear_answer())
    alice = engine.sign_in()
    record = engine.ready(alice)
    assert record["validation"]["can_approve"] is False
    assert record["fields"]["line_items.item-1.unit_price"]["status"] == "contradictory"

    refused = engine.act(alice, record, "approve")
    assert refused.status_code == 422 and error_code(refused) == "review_incomplete"
    paths = {field["path"] for field in refused.json()["error"]["details"]["fields"]}
    assert {"line_items.item-1.unit_price", AGREEMENT} <= paths

    # An empty contradictory price can't simply be confirmed: a person must supply it.
    empty = engine.act(alice, record, "review", operations=[{"op": "confirm", "path": "line_items.item-1.unit_price"}])
    assert empty.status_code == 422 and empty.json()["error"]["details"]["problems"][0]["code"] == "value_required"

    fixed = engine.act(
        alice,
        record,
        "review",
        operations=[
            {"op": "set", "path": "line_items.item-1.unit_price", "value": "11000"},
            {"op": "set", "path": "line_items.item-1.quantity", "value": "2"},
            {"op": "confirm", "path": AGREEMENT, "reason": "Agreed by phone"},
        ],
    )
    assert fixed.status_code == 200, fixed.text
    assert fixed.json()["validation"]["can_approve"] is True
    assert engine.act(alice, fixed.json(), "approve").status_code == 200


def test_invalid_corrections_change_nothing(build: Any) -> None:
    engine = build()
    alice = engine.sign_in()
    record = engine.ready(alice)
    response = engine.act(alice, record, "review", operations=[{"op": "set", "path": "seller", "value": "New"}, {"op": "set", "path": "delivery_fee", "value": "-100"}])
    assert response.status_code == 422 and error_code(response) == "invalid_correction"
    assert response.json()["error"]["details"]["problems"][0]["index"] == 1
    again = engine.get(alice, record["id"])
    assert again["seller"] == "Tola Bags" and again["version"] == record["version"]


def test_a_stale_version_is_refused(build: Any) -> None:
    engine = build()
    alice = engine.sign_in()
    record = engine.ready(alice)
    assert engine.act(alice, record, "review", operations=[{"op": "set", "path": "notes", "value": "One"}]).status_code == 200
    stale = engine.act(alice, record, "review", operations=[{"op": "set", "path": "notes", "value": "Two"}])
    assert stale.status_code == 409 and error_code(stale) == "version_conflict"
    assert engine.act(alice, record, "approve").status_code == 409
    assert engine.get(alice, record["id"])["draft"]["notes"] == "One"


def test_approved_figures_are_frozen_until_reopened(build: Any) -> None:
    engine = build()
    alice = engine.sign_in()
    approved = engine.approved(alice)
    frozen = engine.act(alice, approved, "review", operations=[{"op": "set", "path": "delivery_fee", "value": "1"}])
    assert frozen.status_code == 409 and error_code(frozen) == "not_editable"

    reopened = engine.act(alice, approved, "reopen", reason="The delivery fee was wrong")
    assert reopened.status_code == 200 and reopened.json()["state"] == "awaiting_review"
    fixed = engine.act(alice, reopened.json(), "review", operations=[{"op": "set", "path": "delivery_fee", "value": "1500"}])
    assert fixed.status_code == 200
    assert engine.act(alice, fixed.json(), "approve").json()["calculation"]["total"] == "23500.00"
    pages = engine.client.get("/api/records", headers=alice).json()["records"]
    assert [r["total"] for r in pages] == ["23500"]  # one record, updated, not two


@pytest.mark.parametrize(
    ("setup", "action", "body", "code"),
    [
        ("rejected", "approve", {}, "invalid_state"),
        ("awaiting", "complete", {}, "invalid_state"),
        ("approved", "reject", {"reason": "x"}, "invalid_state"),
        ("cancelled", "reopen", {"reason": "x"}, "invalid_state"),
    ],
)
def test_forbidden_transitions_are_refused(build: Any, setup: str, action: str, body: dict[str, Any], code: str) -> None:
    engine = build()
    alice = engine.sign_in()
    record = engine.ready(alice)
    if setup == "rejected":
        record = engine.act(alice, record, "reject", reason="Not a sale").json()
    elif setup == "approved":
        record = engine.act(alice, record, "approve").json()
    elif setup == "cancelled":
        record = engine.act(alice, record, "cancel", reason="Called off").json()
    response = engine.act(alice, record, action, **body)
    assert response.status_code == 409 and error_code(response) == code


def test_only_a_fully_paid_record_can_be_completed(build: Any) -> None:
    engine = build()
    alice = engine.sign_in()
    approved = engine.approved(alice)
    refused = engine.act(alice, approved, "complete")
    assert refused.status_code == 422 and error_code(refused) == "not_fully_paid"

    reopened = engine.act(alice, approved, "reopen", reason="Paid since").json()
    paid = engine.act(alice, reopened, "review", operations=[{"op": "set", "path": "payment_status", "value": "paid"}]).json()
    again = engine.act(alice, paid, "approve").json()
    assert again["outstanding"] == "0"
    completed = engine.act(alice, again, "complete")
    assert completed.status_code == 200 and completed.json()["state"] == "completed"


def test_a_manual_transaction_follows_the_same_rules(build: Any) -> None:
    engine = build()
    alice = engine.sign_in()
    created = engine.client.post("/api/v1/transactions", json={"draft": valid_draft()}, headers=alice)
    assert created.status_code == 201, created.text
    record = created.json()
    assert (record["state"], record["origin"], record["calculation"]["total"]) == ("draft", "manual", "19000.00")
    assert engine.act(alice, record, "approve").status_code == 200

    broken = engine.client.post("/api/v1/transactions", json={"draft": valid_draft(currency="")}, headers=alice).json()
    refused = engine.act(alice, broken, "approve")
    assert refused.status_code == 422 and "currency_valid" in refused.json()["error"]["details"]["failed_rules"]


def test_two_simultaneous_approvals_produce_one_result(build: Any) -> None:
    import concurrent.futures

    engine = build()
    alice = engine.sign_in()
    record = engine.ready(alice)
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        statuses = sorted(future.result().status_code for future in [pool.submit(engine.act, alice, record, "approve") for _ in range(6)])
    assert statuses.count(200) == 1 and set(statuses) == {200, 409}
    assert engine.rows("SELECT count(*) AS n FROM chatreceipt.transactions") == [{"n": 1}]
    events = engine.rows("SELECT count(*) AS n FROM chatreceipt.transaction_events WHERE event_type = 'approved'")
    assert events == [{"n": 1}]


# -- receipts -----------------------------------------------------------------------------------


def test_a_receipt_needs_approval_and_repeat_requests_return_the_same_one(build: Any) -> None:
    engine = build()
    alice = engine.sign_in()
    record = engine.ready(alice)
    early = engine.client.post(f"/api/v1/transactions/{record['id']}/receipts", headers=alice)
    assert early.status_code == 409 and error_code(early) == "not_approved"

    engine.act(alice, record, "approve")
    first = engine.client.post(f"/api/v1/transactions/{record['id']}/receipts", headers=alice).json()
    second = engine.client.post(f"/api/v1/transactions/{record['id']}/receipts", headers=alice).json()
    assert first["id"] == second["id"]
    assert engine.rows("SELECT count(*) AS n FROM chatreceipt.receipts") == [{"n": 1}]
    assert engine.rows("SELECT count(*) AS n FROM chatreceipt.processing_jobs WHERE kind = 'render_receipt'") == [{"n": 1}]


def test_a_correction_replaces_the_receipt_without_changing_the_issued_one(build: Any) -> None:
    renderer = FakeRenderer()
    engine = build(renderer=renderer)
    alice = engine.sign_in()
    approved = engine.approved(alice)
    first = engine.client.post(f"/api/v1/transactions/{approved['id']}/receipts", headers=alice).json()
    engine.work()
    original = engine.client.get(f"/api/v1/receipts/{first['id']}/document", headers=alice).content
    digest = engine.client.get(f"/api/v1/receipts/{first['id']}", headers=alice).json()["document_sha256"]

    reopened = engine.act(alice, approved, "reopen", reason="Wrong delivery fee").json()
    fixed = engine.act(alice, reopened, "review", operations=[{"op": "set", "path": "delivery_fee", "value": "3000"}]).json()
    engine.act(alice, fixed, "approve")
    second = engine.client.post(f"/api/v1/transactions/{approved['id']}/receipts", headers=alice).json()
    engine.work()

    assert second["reference"] == "RCT-000002" and second["replaces_receipt_id"] == first["id"]
    old = engine.client.get(f"/api/v1/receipts/{first['id']}", headers=alice).json()
    assert old["status"] == "superseded" and old["document_sha256"] == digest
    assert engine.client.get(f"/api/v1/receipts/{first['id']}/document", headers=alice).content == original
    # Each document was printed from its own version's figures.
    assert "₦24,000.00" in renderer.calls[0][1] and "₦25,000.00" in renderer.calls[1][1]
    assert "RCT-000002" in renderer.calls[1][1]


def test_cancelling_voids_receipts_and_removes_the_record(build: Any) -> None:
    engine = build()
    alice = engine.sign_in()
    approved = engine.approved(alice)
    receipt = engine.client.post(f"/api/v1/transactions/{approved['id']}/receipts", headers=alice).json()
    cancelled = engine.act(alice, approved, "cancel", reason="The buyer withdrew")
    assert cancelled.status_code == 200 and cancelled.json()["state"] == "cancelled"
    voided = engine.client.get(f"/api/v1/receipts/{receipt['id']}", headers=alice).json()
    assert (voided["status"], voided["void_reason"]) == ("voided", "The buyer withdrew")
    assert engine.client.get("/api/records", headers=alice).json()["records"] == []


def test_a_failed_render_is_retried_then_reported(build: Any) -> None:
    from app.documents.browser import DocumentRenderError

    engine = build(renderer=FakeRenderer(error=DocumentRenderError("unavailable", "no browser")), engine_job_max_attempts=2)
    alice = engine.sign_in()
    approved = engine.approved(alice)
    receipt = engine.client.post(f"/api/v1/transactions/{approved['id']}/receipts", headers=alice).json()
    engine.work()
    current = engine.client.get(f"/api/v1/receipts/{receipt['id']}", headers=alice).json()
    assert current["document_status"] == "failed"
    pending = engine.client.get(f"/api/v1/receipts/{receipt['id']}/document", headers=alice)
    assert pending.status_code == 409 and error_code(pending) == "document_not_ready"
    assert engine.rows("SELECT status, attempts FROM chatreceipt.processing_jobs WHERE kind = 'render_receipt'") == [{"status": "failed", "attempts": 2}]


# -- jobs and failures ---------------------------------------------------------------------------


def test_temporary_ai_failures_are_retried(build: Any) -> None:
    engine = build(ExtractionProviderError(ErrorKind.RATE_LIMITED), ExtractionProviderError(ErrorKind.TIMEOUT), chat_answer())
    alice = engine.sign_in()
    accepted = engine.submit(alice)
    engine.work()
    job = engine.client.get(f"/api/v1/jobs/{accepted['job']['id']}", headers=alice).json()
    assert (job["status"], job["attempts"]) == ("completed", 3)
    outcomes = engine.rows("SELECT outcome, error_code FROM chatreceipt.job_attempts ORDER BY attempt")
    assert outcomes == [
        {"outcome": "retrying", "error_code": "ai_rate_limited"},
        {"outcome": "retrying", "error_code": "ai_timeout"},
        {"outcome": "completed", "error_code": None},
    ]


def test_a_malformed_ai_answer_is_retried(build: Any) -> None:
    engine = build({"nonsense": True}, chat_answer())
    alice = engine.sign_in()
    accepted = engine.submit(alice)
    engine.work()
    assert engine.get(alice, accepted["transaction_id"])["state"] == "awaiting_review"
    assert engine.model.calls == ["interpretation", "interpretation"]


def test_permanent_failures_stop_at_once_and_can_be_retried_by_a_person(build: Any) -> None:
    engine = build(ExtractionProviderError(ErrorKind.REFUSED), chat_answer())
    alice = engine.sign_in()
    accepted = engine.submit(alice)
    engine.work()
    job = engine.client.get(f"/api/v1/jobs/{accepted['job']['id']}", headers=alice).json()
    assert (job["status"], job["attempts"], job["error_code"]) == ("failed", 1, "ai_refused")
    record = engine.get(alice, accepted["transaction_id"])
    assert record["state"] == "failed" and "couldn't be processed" in record["state_reason"]

    retried = engine.act(alice, record, "retry")
    assert retried.status_code == 202, retried.text
    engine.work()
    assert engine.get(alice, record["id"])["state"] == "awaiting_review"


def test_retries_are_bounded(build: Any) -> None:
    engine = build(ExtractionProviderError(ErrorKind.UNAVAILABLE), engine_job_max_attempts=3)
    alice = engine.sign_in()
    accepted = engine.submit(alice)
    engine.work()
    assert engine.model.calls == ["interpretation"] * 3
    assert engine.get(alice, accepted["transaction_id"])["state"] == "failed"


def test_a_job_can_be_cancelled_before_it_starts(build: Any) -> None:
    engine = build()
    alice = engine.sign_in()
    accepted = engine.submit(alice)
    cancelled = engine.client.post(f"/api/v1/jobs/{accepted['job']['id']}/cancel", headers=alice)
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    engine.work()
    assert engine.model.calls == []
    assert engine.get(alice, accepted["transaction_id"])["state"] == "failed"
    again = engine.client.post(f"/api/v1/jobs/{accepted['job']['id']}/cancel", headers=alice)
    assert again.status_code == 409


def test_a_crashed_workers_job_is_taken_over_and_its_late_result_discarded(build: Any) -> None:
    engine = build()
    alice = engine.sign_in()
    accepted = engine.submit(alice)
    stale = engine.worker._claim()  # this worker claims the job, then dies
    assert stale is not None
    engine.execute("UPDATE chatreceipt.processing_jobs SET lease_expires_at = now() - interval '1 second'")

    engine.work()  # another worker takes over
    job = engine.client.get(f"/api/v1/jobs/{accepted['job']['id']}", headers=alice).json()
    assert (job["status"], job["attempts"]) == ("completed", 2)
    assert engine.get(alice, accepted["transaction_id"])["version"] == 2

    # The first worker wakes up and tries to finish: its lease is gone, so nothing changes.
    customer = engine.customer_id()
    with pytest.raises(jobs.LeaseLost), engine.database.tenant(customer) as conn:
        jobs.complete(conn, stale, {"late": True})
    with engine.database.tenant(customer) as conn:
        assert jobs.fail_attempt(conn, stale, Failure("x", "late", True), engine.client.app.state.settings) == "lease_lost"  # type: ignore[attr-defined]
    assert engine.client.get(f"/api/v1/jobs/{accepted['job']['id']}", headers=alice).json()["status"] == "completed"


def test_a_job_that_keeps_crashing_workers_is_stopped(build: Any) -> None:
    engine = build(engine_job_max_attempts=2)
    alice = engine.sign_in()
    accepted = engine.submit(alice)
    for _ in range(2):
        assert engine.worker._claim() is not None
        engine.execute("UPDATE chatreceipt.processing_jobs SET lease_expires_at = now() - interval '1 second'")
    engine.work()
    assert engine.model.calls == []
    assert engine.client.get(f"/api/v1/jobs/{accepted['job']['id']}", headers=alice).json()["error_code"] == "too_many_attempts"
    assert engine.get(alice, accepted["transaction_id"])["state"] == "failed"


def test_one_subject_can_only_have_one_active_job(build: Any) -> None:
    engine = build()
    alice = engine.sign_in()
    accepted = engine.submit(alice)
    customer = engine.customer_id()
    with engine.database.tenant(customer) as conn:
        again = jobs.enqueue(conn, customer, jobs.PROCESS_CONVERSATION, accepted["conversation_id"], max_attempts=4)
    assert str(again["id"]) == accepted["job"]["id"]


# -- idempotency and duplicates ------------------------------------------------------------------


def test_a_repeated_request_with_the_same_key_is_answered_once(build: Any) -> None:
    engine = build()
    alice = engine.sign_in()
    headers = {**alice, "Idempotency-Key": "submit-conversation-001"}
    first = engine.client.post("/api/v1/conversations", json={"text": CHAT}, headers=headers)
    second = engine.client.post("/api/v1/conversations", json={"text": CHAT}, headers=headers)
    assert first.status_code == second.status_code == 202
    assert first.json() == second.json() and second.headers["idempotency-replayed"] == "true"
    assert engine.rows("SELECT count(*) AS n FROM chatreceipt.conversations") == [{"n": 1}]

    reused = engine.client.post("/api/v1/conversations", json={"text": "Ada: Something else\nTola: Yes"}, headers=headers)
    assert reused.status_code == 422 and error_code(reused) == "idempotency_key_reused"
    bad = engine.client.post("/api/v1/conversations", json={"text": CHAT}, headers={**alice, "Idempotency-Key": "no spaces allowed"})
    assert bad.status_code == 400


def test_a_retried_approval_gets_the_original_answer(build: Any) -> None:
    engine = build()
    alice = engine.sign_in()
    record = engine.ready(alice)
    url = f"/api/v1/transactions/{record['id']}/approve"
    headers = {**alice, "Idempotency-Key": "approve-attempt-0001"}
    body = {"expected_version": record["version"]}
    first = engine.client.post(url, json=body, headers=headers)
    second = engine.client.post(url, json=body, headers=headers)
    assert first.status_code == second.status_code == 200 and first.json()["version"] == second.json()["version"]


def test_a_failed_request_isnt_remembered_so_it_can_be_retried(build: Any) -> None:
    engine = build(unclear_answer())
    alice = engine.sign_in()
    record = engine.ready(alice)
    url = f"/api/v1/transactions/{record['id']}/approve"
    headers = {**alice, "Idempotency-Key": "approve-attempt-0002"}
    assert engine.client.post(url, json={"expected_version": record["version"]}, headers=headers).status_code == 422
    assert engine.rows("SELECT count(*) AS n FROM chatreceipt.idempotency_keys") == [{"n": 0}]


def test_the_same_file_uploaded_twice_is_stored_once(build: Any) -> None:
    engine = build()
    alice = engine.sign_in()
    data = image_bytes()
    first = engine.client.post("/api/v1/uploads", files=[("files", ("a.png", data, "image/png"))], headers=alice).json()["uploads"][0]
    second = engine.client.post("/api/v1/uploads", files=[("files", ("renamed.png", data, "image/png"))], headers=alice).json()["uploads"][0]
    assert first["id"] == second["id"] and first["duplicate"] is False and second["duplicate"] is True
    assert len([path for path in engine.storage_root.rglob("*") if path.is_file()]) == 1


def test_the_same_conversation_submitted_again_is_flagged_not_blocked_outright(build: Any) -> None:
    engine = build()
    alice = engine.sign_in()
    first = engine.approved(alice)
    second = engine.ready(alice)
    flag = second["fields"][DUPLICATE_CHECK]
    assert flag["blocking"] and first["id"] in flag["reasons"][0] and "same uploaded file" in flag["reasons"][0]

    refused = engine.act(alice, second, "approve")
    assert refused.status_code == 409 and error_code(refused) == "possible_duplicate"
    assert refused.json()["error"]["details"]["matches"][0]["transaction_id"] == first["id"]
    acknowledged = engine.act(alice, second, "review", operations=[{"op": "confirm", "path": DUPLICATE_CHECK, "reason": "A separate order"}])
    assert engine.act(alice, acknowledged.json(), "approve").status_code == 200


def test_a_similar_new_transaction_is_flagged_at_approval(build: Any) -> None:
    engine = build()
    alice = engine.sign_in()
    engine.approved(alice)
    manual = engine.client.post(
        "/api/v1/transactions",
        json={"draft": valid_draft(seller="Tola Bags", buyer="Ada", delivery_fee="2000", transaction_date="", payment_status="unpaid", line_items=[{"id": "item-1", "description": "Black tote bag", "quantity": "2", "unit_price": "11000"}])},
        headers=alice,
    ).json()
    refused = engine.act(alice, manual, "approve")
    assert refused.status_code == 409 and error_code(refused) == "possible_duplicate"
    confirmed = engine.act(alice, manual, "review", operations=[{"op": "confirm", "path": DUPLICATE_CHECK}])
    assert engine.act(alice, confirmed.json(), "approve").status_code == 200


# -- integration events --------------------------------------------------------------------------


def test_events_are_signed_delivered_once_and_carry_no_personal_details(build: Any) -> None:
    engine = build(webhook_url="https://hooks.example/chatreceipt", webhook_secret="whsec-test-secret")
    alice = engine.sign_in()
    approved = engine.approved(alice)
    engine.work()
    assert len(engine.webhooks) == 1
    delivery = engine.webhooks[0]
    assert outbox.signature_is_valid("whsec-test-secret", delivery.headers[outbox.SIGNATURE_HEADER], delivery.content)
    assert not outbox.signature_is_valid("wrong-secret", delivery.headers[outbox.SIGNATURE_HEADER], delivery.content)
    assert delivery.headers["x-chatreceipt-event-type"] == outbox.TRANSACTION_APPROVED
    assert approved["id"].encode() in delivery.content and b"Ada" not in delivery.content and b"Tola" not in delivery.content
    assert engine.rows("SELECT status, attempts FROM chatreceipt.outbox_events") == [{"status": "delivered", "attempts": 1}]


def test_an_old_signature_is_refused() -> None:
    header = outbox.signature("secret", 1_000_000, b"{}")
    assert outbox.signature_is_valid("secret", header, b"{}", now=1_000_100)
    assert not outbox.signature_is_valid("secret", header, b"{}", now=1_009_999)
    assert not outbox.signature_is_valid("secret", "garbage", b"{}")


def test_an_unavailable_receiver_is_retried_and_never_blocks_the_app(build: Any) -> None:
    answers = [httpx.Response(500), httpx.Response(503), httpx.Response(200)]
    engine = build(webhook_url="https://hooks.example/x", webhook_secret="s3cret", webhook_handler=lambda request: answers.pop(0))
    alice = engine.sign_in()
    engine.approved(alice)
    engine.work()
    engine.execute("UPDATE chatreceipt.outbox_events SET next_attempt_at = now()")
    engine.work()
    engine.execute("UPDATE chatreceipt.outbox_events SET next_attempt_at = now()")
    engine.work()
    assert engine.rows("SELECT status, attempts FROM chatreceipt.outbox_events") == [{"status": "delivered", "attempts": 3}]


def test_delivery_gives_up_after_the_limit_and_can_be_inspected(build: Any) -> None:
    engine = build(webhook_url="https://hooks.example/x", webhook_secret="s3cret", webhook_max_attempts=2, webhook_handler=lambda request: httpx.Response(500))
    alice = engine.sign_in()
    engine.approved(alice)
    engine.work()
    engine.execute("UPDATE chatreceipt.outbox_events SET next_attempt_at = now()")
    engine.work()
    assert engine.rows("SELECT status, last_error FROM chatreceipt.outbox_events") == [{"status": "failed", "last_error": "status_500"}]


def test_without_a_webhook_events_are_recorded_but_not_sent(build: Any) -> None:
    engine = build()
    engine.approved(engine.sign_in())
    engine.work()
    assert engine.webhooks == []
    assert engine.rows("SELECT event_type, status FROM chatreceipt.outbox_events") == [{"event_type": "transaction.approved", "status": "skipped"}]


def test_a_failure_while_saving_a_change_leaves_no_trace(build: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = build()
    alice = engine.sign_in()
    record = engine.ready(alice)

    def explode(*args: Any, **kwargs: Any) -> None:
        raise psycopg.errors.UniqueViolation("simulated")

    monkeypatch.setattr("app.engine.transactions.publish_record", explode)
    failed = engine.act(alice, record, "approve")
    assert failed.status_code == 500 and error_code(failed) == "internal_error"
    assert "simulated" not in failed.text
    monkeypatch.undo()
    unchanged = engine.get(alice, record["id"])
    assert unchanged["state"] == "awaiting_review" and unchanged["version"] == record["version"]
    assert engine.rows("SELECT count(*) AS n FROM chatreceipt.outbox_events") == [{"n": 0}]
    assert engine.act(alice, unchanged, "approve").status_code == 200
