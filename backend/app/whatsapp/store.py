"""Every SQL statement the WhatsApp channel and the records page use.

Functions take an open connection, so callers decide what commits together. Rows are locked in a
fixed order (customer first, then submission) to rule out deadlocks. Every query that reads a
customer's data filters by that customer's ID.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from app.names import name_key
from app.db.pool import Connection
from app.whatsapp.events import DeliveryFailure, IdentityChange, InboundMessage, Sender

Row = dict[str, Any]


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# -- Customers --------------------------------------------------------------------------------


def resolve_customer(conn: Connection, sender: Sender) -> UUID:
    """The customer behind a signed webhook message, created on first contact.

    The business-scoped user ID is the identity. A phone number alone never grants access to a
    customer who already has a different user ID: phone numbers get reassigned, and the new owner
    must not inherit the previous owner's records.
    """
    if sender.user_id:
        found = conn.execute(
            "SELECT id, whatsapp_phone FROM chatreceipt.customers WHERE whatsapp_user_id = %s FOR NO KEY UPDATE",
            (sender.user_id,),
        ).fetchone()
        if found:
            if sender.phone and found["whatsapp_phone"] != sender.phone:
                _take_phone(conn, found["id"], sender.phone)
            conn.execute("UPDATE chatreceipt.customers SET last_message_at = now() WHERE id = %s", (found["id"],))
            return found["id"]
        if sender.phone:
            holder = conn.execute(
                "SELECT id, whatsapp_user_id FROM chatreceipt.customers WHERE whatsapp_phone = %s FOR NO KEY UPDATE",
                (sender.phone,),
            ).fetchone()
            if holder and holder["whatsapp_user_id"] is None:
                conn.execute(
                    "UPDATE chatreceipt.customers SET whatsapp_user_id = %s, last_message_at = now() WHERE id = %s",
                    (sender.user_id, holder["id"]),
                )
                return holder["id"]
            if holder:
                conn.execute("UPDATE chatreceipt.customers SET whatsapp_phone = NULL WHERE id = %s", (holder["id"],))
        row = conn.execute(
            """
            INSERT INTO chatreceipt.customers (whatsapp_user_id, whatsapp_phone) VALUES (%s, %s)
            ON CONFLICT (whatsapp_user_id) DO UPDATE SET last_message_at = now()
            RETURNING id
            """,
            (sender.user_id, sender.phone),
        ).fetchone()
        return row["id"]  # type: ignore[index]
    row = conn.execute(
        """
        INSERT INTO chatreceipt.customers (whatsapp_phone) VALUES (%s)
        ON CONFLICT (whatsapp_phone) DO UPDATE SET last_message_at = now()
        RETURNING id
        """,
        (sender.phone,),
    ).fetchone()
    return row["id"]  # type: ignore[index]


def _take_phone(conn: Connection, customer_id: UUID, phone: str) -> None:
    conn.execute(
        "UPDATE chatreceipt.customers SET whatsapp_phone = NULL WHERE whatsapp_phone = %s AND id <> %s AND whatsapp_user_id IS NOT NULL",
        (phone, customer_id),
    )
    conn.execute(
        """
        UPDATE chatreceipt.customers SET whatsapp_phone = %s
        WHERE id = %s AND NOT EXISTS (SELECT 1 FROM chatreceipt.customers WHERE whatsapp_phone = %s AND id <> %s)
        """,
        (phone, customer_id, phone, customer_id),
    )


def apply_identity_change(conn: Connection, change: IdentityChange) -> None:
    """Meta reported a changed number: the same person keeps their records under the new identity."""
    found = conn.execute(
        "SELECT id FROM chatreceipt.customers WHERE whatsapp_user_id = %s FOR NO KEY UPDATE", (change.previous_user_id,)
    ).fetchone()
    if found is None:
        return
    taken = conn.execute(
        "SELECT 1 FROM chatreceipt.customers WHERE whatsapp_user_id = %s AND id <> %s", (change.user_id, found["id"])
    ).fetchone()
    if taken:
        return
    conn.execute("UPDATE chatreceipt.customers SET whatsapp_user_id = %s WHERE id = %s", (change.user_id, found["id"]))
    if change.phone:
        _take_phone(conn, found["id"], change.phone)


def lock_customer(conn: Connection, customer_id: UUID) -> Row | None:
    """Serialise everything that happens to one customer until this transaction ends."""
    return conn.execute("SELECT * FROM chatreceipt.customers WHERE id = %s FOR NO KEY UPDATE", (customer_id,)).fetchone()


def mark_welcomed(conn: Connection, customer_id: UUID) -> None:
    conn.execute("UPDATE chatreceipt.customers SET welcomed_at = now() WHERE id = %s", (customer_id,))


def delete_customer_data(conn: Connection, customer_id: UUID) -> None:
    """Delete everything the customer has. Their stored files are removed by the worker straight after."""
    conn.execute("INSERT INTO chatreceipt.storage_erasures (customer_ref) VALUES (%s) ON CONFLICT DO NOTHING", (customer_id,))
    conn.execute("DELETE FROM chatreceipt.whatsapp_outbound WHERE customer_ref = %s", (customer_id,))
    conn.execute("DELETE FROM chatreceipt.customers WHERE id = %s", (customer_id,))


# -- The customer's own name on receipts ---------------------------------------------------------


def note_whatsapp_name(conn: Connection, customer_id: UUID, name: str) -> None:
    """Keep the latest WhatsApp profile name, offered as a one-tap answer when asking for a name."""
    if name:
        conn.execute(
            "UPDATE chatreceipt.customers SET whatsapp_name = %s WHERE id = %s AND whatsapp_name IS DISTINCT FROM %s",
            (name, customer_id, name),
        )


def set_party_name(conn: Connection, customer_id: UUID, name: str) -> None:
    conn.execute("UPDATE chatreceipt.customers SET party_name = %s, name_prompts_skipped = 0 WHERE id = %s", (name, customer_id))


def set_usual_role(conn: Connection, customer_id: UUID, role: str) -> None:
    conn.execute("UPDATE chatreceipt.customers SET usual_role = %s WHERE id = %s", (role, customer_id))


def skip_name_prompt(conn: Connection, customer_id: UUID) -> None:
    conn.execute("UPDATE chatreceipt.customers SET name_prompts_skipped = name_prompts_skipped + 1 WHERE id = %s", (customer_id,))


def note_business_number(conn: Connection, customer_id: UUID, number_id: str) -> None:
    """Which ChatReceipt number (Meta's phone number ID) this customer messages."""
    if number_id:
        conn.execute(
            "UPDATE chatreceipt.customers SET business_number_id = %s WHERE id = %s AND business_number_id IS DISTINCT FROM %s",
            (number_id[:64], customer_id, number_id[:64]),
        )


def owner_names(conn: Connection, customer_id: UUID) -> list[str]:
    """The names a customer goes by: the one they confirmed for receipts, then their WhatsApp name."""
    row = conn.execute("SELECT party_name, whatsapp_name FROM chatreceipt.customers WHERE id = %s", (customer_id,)).fetchone()
    return [name for name in ((row or {}).get("party_name"), (row or {}).get("whatsapp_name")) if name]


# -- The people a customer deals with --------------------------------------------------------------


def find_counterparty(conn: Connection, customer_id: UUID, *, phone: str, label: str) -> tuple[Row, str] | None:
    """Someone this customer confirmed on an earlier receipt, and how they were matched.

    A phone number is a reliable match ("phone"). A label is only the customer's own name for a
    contact ("label"): it is used when exactly one person has it, and only as a suggestion.
    """
    if phone:
        row = conn.execute(
            "SELECT * FROM chatreceipt.counterparties WHERE customer_id = %s AND phone = %s", (customer_id, phone)
        ).fetchone()
        if row:
            return row, "phone"
    key = name_key(label)
    if not key:
        return None
    rows = conn.execute(
        "SELECT * FROM chatreceipt.counterparties WHERE customer_id = %s AND %s = ANY (labels) ORDER BY last_seen_at DESC LIMIT 2",
        (customer_id, key),
    ).fetchall()
    return (rows[0], "label") if len(rows) == 1 else None


def remember_counterparty(
    conn: Connection, customer_id: UUID, *, name: str, phone: str, label: str, role: str, counterparty_id: UUID | None = None
) -> None:
    """After a receipt is saved: the other person's name as the customer confirmed it.

    By phone number when there is one. Otherwise only with a label to recognise them by, and never
    by merging two people who merely have the same name.
    """
    key = name_key(label)
    labels = [key] if key else []
    last_role = role if role in ("seller", "buyer") else None
    if phone:
        conn.execute(
            """
            INSERT INTO chatreceipt.counterparties (customer_id, name, phone, labels, last_role, receipts)
            VALUES (%s, %s, %s, %s, %s, 1)
            ON CONFLICT (customer_id, phone) WHERE phone IS NOT NULL DO UPDATE SET
                name = EXCLUDED.name,
                labels = ARRAY(SELECT DISTINCT label FROM unnest(chatreceipt.counterparties.labels || EXCLUDED.labels) AS label LIMIT 10),
                last_role = COALESCE(EXCLUDED.last_role, chatreceipt.counterparties.last_role),
                receipts = chatreceipt.counterparties.receipts + 1,
                last_seen_at = now()
            """,
            (customer_id, name, phone, labels, last_role),
        )
        return
    if not key:
        return  # nothing reliable to recognise this person by next time
    if counterparty_id is None:
        found = find_counterparty(conn, customer_id, phone="", label=label)
        # Someone with the same label but another name may be a different person: they get their own
        # entry, and from then on that label is shared, so it suggests nobody.
        same = found is not None and found[0]["phone"] is None and name_key(found[0]["name"]) == name_key(name)
        counterparty_id = found[0]["id"] if same else None  # type: ignore[index]
    if counterparty_id is not None:
        conn.execute(
            """
            UPDATE chatreceipt.counterparties
            SET name = %s, last_role = COALESCE(%s, last_role), receipts = receipts + 1, last_seen_at = now()
            WHERE id = %s AND customer_id = %s
            """,
            (name, last_role, counterparty_id, customer_id),
        )
        return
    conn.execute(
        "INSERT INTO chatreceipt.counterparties (customer_id, name, labels, last_role, receipts) VALUES (%s, %s, %s, %s, 1)",
        (customer_id, name, labels, last_role),
    )


def clear_history(conn: Connection, customer_id: UUID) -> bool:
    """Forget the conversation: open drafts, what was remembered of the chat, waiting requests.

    Closed drafts (kept for a day so a deal can be read again) are emptied too. Saved records, the
    customer's name for receipts and the people they confirmed stay. Returns whether a draft was open.
    """
    submission = open_submission(conn, customer_id)
    if submission is not None:
        update_submission(
            conn, submission["id"], status="cancelled", draft=None, review=None, pending_edit=None, held_images=[],
            lease_token=None, instruction=None, dialogue=[],
        )
    conn.execute(
        """
        UPDATE chatreceipt.whatsapp_submissions
        SET draft = NULL, review = NULL, pending_edit = NULL, images = '[]', held_images = '[]', instruction = NULL,
            dialogue = '[]', updated_at = now()
        WHERE customer_id = %s AND status IN ('confirmed', 'cancelled', 'failed')
          AND (draft IS NOT NULL OR review IS NOT NULL OR images <> '[]'::jsonb OR dialogue <> '[]'::jsonb)
        """,
        (customer_id,),
    )
    conn.execute(
        "UPDATE chatreceipt.customers SET memory = '[]', request = NULL, request_lease_until = NULL WHERE id = %s", (customer_id,)
    )
    return submission is not None


# -- Inbound messages -------------------------------------------------------------------------


def record_inbound(conn: Connection, message: InboundMessage, *, business_number_id: str = "") -> bool:
    """Store a delivered message once. False means this message ID was already stored."""
    customer_id = resolve_customer(conn, message.sender)
    note_whatsapp_name(conn, customer_id, message.sender.profile_name)
    note_business_number(conn, customer_id, business_number_id)
    row = conn.execute(
        """
        INSERT INTO chatreceipt.whatsapp_inbound (message_id, customer_id, kind, payload, sent_at)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (message_id) DO NOTHING
        RETURNING message_id
        """,
        (message.message_id, customer_id, message.kind, Jsonb(message.payload()), message.sent_at),
    ).fetchone()
    return row is not None


def record_delivery_failure(conn: Connection, failure: DeliveryFailure) -> None:
    conn.execute(
        "UPDATE chatreceipt.whatsapp_outbound SET status = 'failed', last_error_code = %s WHERE provider_message_id = %s",
        (str(failure.code) if failure.code is not None else "delivery_failed", failure.message_id),
    )


def claim_inbound(conn: Connection) -> Row | None:
    """The next message to handle: the oldest waiting one for a customer, locked for this transaction."""
    return conn.execute(
        """
        SELECT i.* FROM chatreceipt.whatsapp_inbound i
        WHERE i.status = 'pending' AND i.next_attempt_at <= now()
          AND NOT EXISTS (
              SELECT 1 FROM chatreceipt.whatsapp_inbound earlier
              WHERE earlier.customer_id = i.customer_id AND earlier.status = 'pending'
                AND (earlier.sent_at, earlier.seq) < (i.sent_at, i.seq)
          )
          -- A typed correction is still being understood: its result comes before anything newer.
          AND NOT EXISTS (
              SELECT 1 FROM chatreceipt.whatsapp_submissions s
              WHERE s.customer_id = i.customer_id AND s.status = 'awaiting_confirmation' AND s.instruction IS NOT NULL
          )
          -- So is a free-text request.
          AND NOT EXISTS (SELECT 1 FROM chatreceipt.customers c WHERE c.id = i.customer_id AND c.request IS NOT NULL)
        ORDER BY i.next_attempt_at, i.seq
        LIMIT 1
        FOR UPDATE OF i SKIP LOCKED
        """
    ).fetchone()


def finish_inbound(conn: Connection, message_id: str) -> None:
    conn.execute(
        "UPDATE chatreceipt.whatsapp_inbound SET status = 'handled', payload = '{}', handled_at = now() WHERE message_id = %s",
        (message_id,),
    )


def retry_inbound(conn: Connection, message_id: str, *, delay_seconds: float, give_up: bool) -> Row | None:
    return conn.execute(
        """
        UPDATE chatreceipt.whatsapp_inbound
        SET attempts = attempts + 1,
            next_attempt_at = now() + make_interval(secs => %s),
            status = CASE WHEN %s THEN 'failed' ELSE status END,
            payload = CASE WHEN %s THEN '{}'::jsonb ELSE payload END
        WHERE message_id = %s
        RETURNING customer_id, attempts
        """,
        (delay_seconds, give_up, give_up, message_id),
    ).fetchone()


def postpone_inbound(conn: Connection, message_id: str, *, seconds: float) -> None:
    """Look at a message again shortly, counting the look, without treating it as a failure."""
    conn.execute(
        "UPDATE chatreceipt.whatsapp_inbound SET attempts = attempts + 1, next_attempt_at = now() + make_interval(secs => %s) WHERE message_id = %s",
        (seconds, message_id),
    )


def image_near(conn: Connection, customer_id: UUID, sent_at: datetime, *, seconds: int) -> bool:
    """Whether the customer sent a photo within this many seconds of a moment."""
    row = conn.execute(
        """
        SELECT 1 FROM chatreceipt.whatsapp_inbound
        WHERE customer_id = %s AND kind = 'image' AND sent_at BETWEEN %s - make_interval(secs => %s) AND %s + make_interval(secs => %s)
        LIMIT 1
        """,
        (customer_id, sent_at, seconds, sent_at, seconds),
    ).fetchone()
    return row is not None


def inbound_count(conn: Connection, customer_id: UUID, *, seconds: int, kinds: Sequence[str] = ()) -> int:
    """Messages from a customer in the last few seconds, of any kind or only of these kinds."""
    row = conn.execute(
        """
        SELECT count(*) AS n FROM chatreceipt.whatsapp_inbound
        WHERE customer_id = %s AND created_at > now() - make_interval(secs => %s)
          AND (cardinality(%s::text[]) = 0 OR kind = ANY (%s::text[]))
        """,
        (customer_id, seconds, list(kinds), list(kinds)),
    ).fetchone()
    return int(row["n"])  # type: ignore[index]


# -- Submissions ------------------------------------------------------------------------------

_JSON_COLUMNS = frozenset({"images", "held_images", "draft", "review", "edited_fields", "pending_edit", "instruction", "dialogue"})
_SUBMISSION_COLUMNS = _JSON_COLUMNS | frozenset(
    {"status", "images_version", "revision", "attempts", "lease_token", "lease_expires_at", "delay_notified", "failure_code"}
)


def open_submission(conn: Connection, customer_id: UUID) -> Row | None:
    return conn.execute(
        """
        SELECT * FROM chatreceipt.whatsapp_submissions
        WHERE customer_id = %s AND status IN ('collecting', 'processing', 'awaiting_confirmation')
        FOR NO KEY UPDATE
        """,
        (customer_id,),
    ).fetchone()


def get_submission(conn: Connection, customer_id: UUID, submission_id: str) -> Row | None:
    return conn.execute(
        "SELECT * FROM chatreceipt.whatsapp_submissions WHERE id = %s AND customer_id = %s FOR NO KEY UPDATE",
        (submission_id, customer_id),
    ).fetchone()


def create_submission(conn: Connection, customer_id: UUID, images: list[dict[str, str]], *, wait_seconds: float) -> Row:
    row = conn.execute(
        """
        INSERT INTO chatreceipt.whatsapp_submissions (customer_id, status, images, run_after)
        VALUES (%s, 'collecting', %s, now() + make_interval(secs => %s))
        RETURNING *
        """,
        (customer_id, Jsonb(images), wait_seconds),
    ).fetchone()
    return row  # type: ignore[return-value]


def update_submission(conn: Connection, submission_id: UUID, **values: Any) -> Row:
    unknown = set(values) - _SUBMISSION_COLUMNS
    if unknown:
        raise ValueError(f"Unknown submission columns: {sorted(unknown)}")
    assignments = ", ".join(f"{column} = %s" for column in values)
    parameters = [Jsonb(value) if column in _JSON_COLUMNS and value is not None else value for column, value in values.items()]
    row = conn.execute(
        f"UPDATE chatreceipt.whatsapp_submissions SET {assignments}, updated_at = now() WHERE id = %s RETURNING *",  # noqa: S608
        [*parameters, submission_id],
    ).fetchone()
    return row  # type: ignore[return-value]


def extend_collection(conn: Connection, submission_id: UUID, images: list[dict[str, str]], *, wait_seconds: float, max_wait_seconds: float) -> None:
    """Another screenshot arrived: wait a little longer for the rest, within the limit."""
    conn.execute(
        """
        UPDATE chatreceipt.whatsapp_submissions
        SET images = %s,
            run_after = LEAST(now() + make_interval(secs => %s), first_image_at + make_interval(secs => %s)),
            updated_at = now()
        WHERE id = %s
        """,
        (Jsonb(images), wait_seconds, max_wait_seconds, submission_id),
    )


def restart_collection(conn: Connection, submission_id: UUID, images: list[dict[str, str]], *, wait_seconds: float, revision: int) -> None:
    """Read the screenshots again from scratch, because the set of screenshots changed."""
    conn.execute(
        """
        UPDATE chatreceipt.whatsapp_submissions
        SET status = 'collecting', images = %s, held_images = '[]', draft = NULL, review = NULL, pending_edit = NULL,
            edited_fields = '[]', images_version = images_version + 1, revision = %s, attempts = 0,
            lease_token = NULL, lease_expires_at = NULL, first_image_at = now(), delay_notified = false,
            failure_code = NULL, instruction = NULL, dialogue = '[]',
            run_after = now() + make_interval(secs => %s), updated_at = now()
        WHERE id = %s
        """,
        (Jsonb(images), revision, wait_seconds, submission_id),
    )


def latest_export_submission(conn: Connection, customer_id: UUID) -> Row | None:
    """The customer's latest chat export that listed other deals, while it's still kept (a day after it closed)."""
    return conn.execute(
        """
        SELECT * FROM chatreceipt.whatsapp_submissions
        WHERE customer_id = %s AND status IN ('confirmed', 'cancelled', 'awaiting_confirmation')
          AND images @> '[{"kind": "chat_export"}]'::jsonb AND jsonb_array_length(COALESCE(review -> 'other_deals', '[]'::jsonb)) > 0
        ORDER BY updated_at DESC LIMIT 1
        """,
        (customer_id,),
    ).fetchone()


def latest_failed_submission(conn: Connection, customer_id: UUID, *, within_hours: int = 24) -> Row | None:
    """The customer's most recent screenshots that couldn't be read, to read again on request."""
    return conn.execute(
        """
        SELECT * FROM chatreceipt.whatsapp_submissions
        WHERE customer_id = %s AND status = 'failed' AND updated_at > now() - make_interval(hours => %s)
        ORDER BY updated_at DESC LIMIT 1
        FOR NO KEY UPDATE
        """,
        (customer_id, within_hours),
    ).fetchone()


def claim_instruction(conn: Connection, *, lease_seconds: float) -> Row | None:
    """Take the next typed correction waiting to be understood, counting the attempt."""
    return conn.execute(
        """
        UPDATE chatreceipt.whatsapp_submissions s
        SET lease_token = gen_random_uuid(), lease_expires_at = now() + make_interval(secs => %s),
            instruction = s.instruction || jsonb_build_object('attempts', COALESCE((s.instruction ->> 'attempts')::int, 0) + 1),
            updated_at = now()
        WHERE s.id = (
            SELECT id FROM chatreceipt.whatsapp_submissions
            WHERE status = 'awaiting_confirmation' AND instruction IS NOT NULL
              AND (lease_expires_at IS NULL OR lease_expires_at < now())
            ORDER BY updated_at
            LIMIT 1
            FOR UPDATE SKIP LOCKED
        )
        RETURNING s.*
        """,
        (lease_seconds,),
    ).fetchone()


def lock_claimed_instruction(conn: Connection, submission_id: UUID, lease_token: UUID) -> tuple[Row, Row] | None:
    """The customer and draft, if this worker still holds the correction. None means discard the result."""
    owner = conn.execute("SELECT customer_id FROM chatreceipt.whatsapp_submissions WHERE id = %s", (submission_id,)).fetchone()
    if owner is None:
        return None
    customer = lock_customer(conn, owner["customer_id"])
    submission = conn.execute(
        "SELECT * FROM chatreceipt.whatsapp_submissions WHERE id = %s FOR NO KEY UPDATE", (submission_id,)
    ).fetchone()
    if (
        customer is None
        or submission is None
        or submission["status"] != "awaiting_confirmation"
        or submission["instruction"] is None
        or submission["lease_token"] != lease_token
    ):
        return None
    return customer, submission


def claim_submission(conn: Connection, *, lease_seconds: float) -> Row | None:
    """Take the next submission that is due, or one whose worker stopped before finishing it."""
    return conn.execute(
        """
        UPDATE chatreceipt.whatsapp_submissions s
        SET status = 'processing', attempts = s.attempts + 1, lease_token = gen_random_uuid(),
            lease_expires_at = now() + make_interval(secs => %s), updated_at = now()
        WHERE s.id = (
            SELECT id FROM chatreceipt.whatsapp_submissions
            WHERE (status = 'collecting' AND run_after <= now()) OR (status = 'processing' AND lease_expires_at < now())
            ORDER BY run_after
            LIMIT 1
            FOR UPDATE SKIP LOCKED
        )
        RETURNING s.*
        """,
        (lease_seconds,),
    ).fetchone()


def lock_claimed_submission(conn: Connection, submission_id: UUID, lease_token: UUID) -> tuple[Row, Row] | None:
    """The customer and submission, if this worker still holds the lease. None means discard the result."""
    owner = conn.execute("SELECT customer_id FROM chatreceipt.whatsapp_submissions WHERE id = %s", (submission_id,)).fetchone()
    if owner is None:
        return None
    customer = lock_customer(conn, owner["customer_id"])
    submission = conn.execute(
        "SELECT * FROM chatreceipt.whatsapp_submissions WHERE id = %s FOR NO KEY UPDATE", (submission_id,)
    ).fetchone()
    if customer is None or submission is None or submission["status"] != "processing" or submission["lease_token"] != lease_token:
        return None
    return customer, submission


def schedule_submission_retry(conn: Connection, submission_id: UUID, *, delay_seconds: float, delay_notified: bool) -> None:
    conn.execute(
        """
        UPDATE chatreceipt.whatsapp_submissions
        SET status = 'collecting', run_after = now() + make_interval(secs => %s), lease_token = NULL,
            lease_expires_at = NULL, delay_notified = %s, updated_at = now()
        WHERE id = %s
        """,
        (delay_seconds, delay_notified, submission_id),
    )


# -- Saved records ----------------------------------------------------------------------------


def save_transaction(
    conn: Connection,
    *,
    customer: Row,
    submission: Row,
    draft: dict[str, Any],
    calculation: dict[str, Any],
    transaction_date: date | None,
    total: Decimal,
    amount_paid: Decimal | None,
    outstanding: Decimal | None,
    owner_role: str | None = None,
) -> Row:
    """Save a confirmed record. Saving the same submission twice returns the first record.

    ``owner_role`` is the side the customer was on ("seller" or "buyer"), when it was known.
    """
    number = customer["next_record_no"]
    hashes = [image["sha256"] for image in submission["images"] if image.get("sha256")]
    row = conn.execute(
        """
        INSERT INTO chatreceipt.transactions (
            customer_id, record_no, reference, submission_id, draft, calculation, currency, seller, buyer,
            transaction_date, total, amount_paid, outstanding, payment_status, edited_fields, image_hashes, owner_role
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (submission_id) DO NOTHING
        RETURNING *
        """,
        (
            customer["id"], number, f"CR-{number:06d}", submission["id"], Jsonb(draft), Jsonb(calculation),
            draft["currency"], draft["seller"].strip(), draft["buyer"].strip(), transaction_date, total, amount_paid,
            outstanding, draft["payment_status"], len(submission["edited_fields"]), hashes,
            owner_role if owner_role in ("seller", "buyer") else None,
        ),
    ).fetchone()
    if row is None:
        return saved_for_submission(conn, customer["id"], submission["id"])  # type: ignore[return-value]
    conn.execute("UPDATE chatreceipt.customers SET next_record_no = %s WHERE id = %s", (number + 1, customer["id"]))
    return row


def saved_for_submission(conn: Connection, customer_id: UUID, submission_id: UUID) -> Row | None:
    return conn.execute(
        "SELECT * FROM chatreceipt.transactions WHERE customer_id = %s AND submission_id = %s", (customer_id, submission_id)
    ).fetchone()


def find_saved_with_images(conn: Connection, customer_id: UUID, hashes: Sequence[str]) -> str | None:
    """The reference of an earlier record made from any of these exact screenshots."""
    if not hashes:
        return None
    row = conn.execute(
        """
        SELECT reference FROM chatreceipt.transactions
        WHERE customer_id = %s AND image_hashes && %s::text[]
        ORDER BY created_at DESC LIMIT 1
        """,
        (customer_id, list(hashes)),
    ).fetchone()
    return row["reference"] if row else None


_RECORD_COLUMNS = (
    "id, reference, source, currency, seller, buyer, transaction_date, total, amount_paid, outstanding, payment_status, created_at,"
    " owner_role"
)


def recent_transactions(
    conn: Connection, customer_id: UUID, *, limit: int, before: tuple[datetime, UUID] | None = None
) -> list[Row]:
    if before is None:
        return conn.execute(
            f"""
            SELECT {_RECORD_COLUMNS} FROM chatreceipt.transactions
            WHERE customer_id = %s AND voided_at IS NULL ORDER BY created_at DESC, id DESC LIMIT %s
            """,  # noqa: S608
            (customer_id, limit),
        ).fetchall()
    return conn.execute(
        f"""
        SELECT {_RECORD_COLUMNS} FROM chatreceipt.transactions
        WHERE customer_id = %s AND voided_at IS NULL AND (created_at, id) < (%s, %s)
        ORDER BY created_at DESC, id DESC LIMIT %s
        """,  # noqa: S608
        (customer_id, before[0], before[1], limit),
    ).fetchall()


def get_transaction(conn: Connection, customer_id: UUID, transaction_id: UUID) -> Row | None:
    return conn.execute(
        "SELECT * FROM chatreceipt.transactions WHERE id = %s AND customer_id = %s AND voided_at IS NULL", (transaction_id, customer_id)
    ).fetchone()


# -- Outbound messages ------------------------------------------------------------------------


def enqueue(conn: Connection, customer: Row, payload: dict[str, Any], dedupe_key: str) -> None:
    conn.execute(
        """
        INSERT INTO chatreceipt.whatsapp_outbound (customer_ref, to_user_id, to_phone, dedupe_key, payload)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (dedupe_key) DO NOTHING
        """,
        (customer["id"], customer["whatsapp_user_id"], customer["whatsapp_phone"], dedupe_key, Jsonb(payload)),
    )


def claim_outbound(conn: Connection, *, hold_seconds: float) -> Row | None:
    """The next reply to send. It stays hidden while being sent, and reappears if the sender stops."""
    return conn.execute(
        """
        UPDATE chatreceipt.whatsapp_outbound o
        SET attempts = o.attempts + 1, next_attempt_at = now() + make_interval(secs => %s)
        WHERE o.id = (
            SELECT c.id FROM chatreceipt.whatsapp_outbound c
            WHERE c.status = 'pending' AND c.next_attempt_at <= now()
              AND NOT EXISTS (
                  SELECT 1 FROM chatreceipt.whatsapp_outbound earlier
                  WHERE earlier.customer_ref = c.customer_ref AND earlier.status = 'pending' AND earlier.id < c.id
              )
            ORDER BY c.next_attempt_at, c.id
            LIMIT 1
            FOR UPDATE SKIP LOCKED
        )
        RETURNING o.*
        """,
        (hold_seconds,),
    ).fetchone()


def mark_outbound_sent(conn: Connection, outbound_id: int, provider_message_id: str) -> None:
    conn.execute(
        """
        UPDATE chatreceipt.whatsapp_outbound
        SET status = 'sent', sent_at = now(), provider_message_id = %s, payload = '{}', to_user_id = NULL, to_phone = NULL
        WHERE id = %s
        """,
        (provider_message_id or None, outbound_id),
    )


def mark_outbound_failed(conn: Connection, outbound_id: int, error_code: str) -> None:
    conn.execute(
        """
        UPDATE chatreceipt.whatsapp_outbound
        SET status = 'failed', last_error_code = %s, payload = '{}', to_user_id = NULL, to_phone = NULL
        WHERE id = %s
        """,
        (error_code, outbound_id),
    )


def schedule_outbound_retry(conn: Connection, outbound_id: int, *, delay_seconds: float, error_code: str) -> None:
    conn.execute(
        "UPDATE chatreceipt.whatsapp_outbound SET next_attempt_at = now() + make_interval(secs => %s), last_error_code = %s WHERE id = %s",
        (delay_seconds, error_code, outbound_id),
    )


# -- Web sign-in ------------------------------------------------------------------------------


def issue_sign_in_code(conn: Connection, customer_id: UUID, *, ttl_minutes: int) -> str:
    code = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO chatreceipt.web_sign_in_codes (code_hash, customer_id, expires_at) VALUES (%s, %s, now() + make_interval(mins => %s))",
        (_hash(code), customer_id, ttl_minutes),
    )
    return code


def exchange_sign_in_code(conn: Connection, code: str, *, session_ttl_hours: int) -> tuple[str, datetime] | None:
    """Swap a valid, unused code for a session. Each code works exactly once."""
    used = conn.execute(
        """
        UPDATE chatreceipt.web_sign_in_codes SET used_at = now()
        WHERE code_hash = %s AND used_at IS NULL AND expires_at > now()
        RETURNING customer_id
        """,
        (_hash(code),),
    ).fetchone()
    if used is None:
        return None
    token = secrets.token_urlsafe(32)
    session = conn.execute(
        """
        INSERT INTO chatreceipt.web_sessions (token_hash, customer_id, expires_at)
        VALUES (%s, %s, now() + make_interval(hours => %s))
        RETURNING expires_at
        """,
        (_hash(token), used["customer_id"], session_ttl_hours),
    ).fetchone()
    return token, session["expires_at"]  # type: ignore[index]


def session_customer(conn: Connection, token: str) -> UUID | None:
    row = conn.execute(
        "SELECT customer_id FROM chatreceipt.web_sessions WHERE token_hash = %s AND revoked_at IS NULL AND expires_at > now()",
        (_hash(token),),
    ).fetchone()
    return row["customer_id"] if row else None


def revoke_session(conn: Connection, token: str) -> None:
    conn.execute("UPDATE chatreceipt.web_sessions SET revoked_at = now() WHERE token_hash = %s", (_hash(token),))


# -- Housekeeping -----------------------------------------------------------------------------


def purge_expired(conn: Connection, *, draft_ttl_hours: int) -> None:
    """Delete what's no longer needed: unconfirmed drafts, finished queues and expired sign-ins."""
    statements: Iterable[tuple[str, tuple[Any, ...]]] = (
        (
            """
            UPDATE chatreceipt.whatsapp_submissions
            SET status = 'cancelled', failure_code = 'expired', draft = NULL, review = NULL, pending_edit = NULL,
                images = '[]', held_images = '[]', instruction = NULL, dialogue = '[]', updated_at = now()
            WHERE status IN ('collecting', 'awaiting_confirmation') AND updated_at < now() - make_interval(hours => %s)
            """,
            (draft_ttl_hours,),
        ),
        (
            """
            UPDATE chatreceipt.whatsapp_submissions
            SET draft = NULL, review = NULL, pending_edit = NULL, images = '[]', held_images = '[]',
                instruction = NULL, dialogue = '[]'
            WHERE status IN ('confirmed', 'cancelled', 'failed') AND updated_at < now() - interval '1 day'
              AND (draft IS NOT NULL OR images <> '[]'::jsonb OR dialogue <> '[]'::jsonb)
            """,
            (),
        ),
        ("DELETE FROM chatreceipt.whatsapp_inbound WHERE status <> 'pending' AND created_at < now() - interval '30 days'", ()),
        ("DELETE FROM chatreceipt.whatsapp_outbound WHERE status <> 'pending' AND created_at < now() - interval '30 days'", ()),
        ("DELETE FROM chatreceipt.web_sign_in_codes WHERE expires_at < now() - interval '1 day'", ()),
        # The assistant's memory of a conversation lasts a day without messages.
        ("UPDATE chatreceipt.customers SET memory = '[]' WHERE memory <> '[]'::jsonb AND last_message_at < now() - interval '1 day'", ()),
        ("DELETE FROM chatreceipt.web_sessions WHERE expires_at < now() - interval '1 day'", ()),
    )
    for sql, parameters in statements:
        conn.execute(sql, parameters)


# -- Free-text requests (assistant.py) --------------------------------------------------------


def queue_request(
    conn: Connection, customer_id: UUID, text: str, message_id: str, *, has_draft: bool, earlier: Sequence[str] = ()
) -> None:
    """Hold a message for the AI to read, with the conversation before it. Answered by ``conversation.finish_request``."""
    request = {"text": text[:1000], "message_id": message_id, "has_draft": has_draft, "earlier": list(earlier)[-8:], "attempts": 0}
    conn.execute(
        "UPDATE chatreceipt.customers SET request = %s, request_lease_until = NULL WHERE id = %s",
        (Jsonb(request), customer_id),
    )


def claim_request(conn: Connection, *, lease_seconds: float) -> Row | None:
    """Take the next waiting request, counting the attempt."""
    return conn.execute(
        """
        UPDATE chatreceipt.customers c
        SET request_lease_until = now() + make_interval(secs => %s),
            request = c.request || jsonb_build_object('attempts', COALESCE((c.request ->> 'attempts')::int, 0) + 1)
        WHERE c.id = (
            SELECT id FROM chatreceipt.customers
            WHERE request IS NOT NULL AND (request_lease_until IS NULL OR request_lease_until < now())
            ORDER BY last_message_at
            LIMIT 1
            FOR UPDATE SKIP LOCKED
        )
        RETURNING c.id, c.request, c.request_lease_until
        """,
        (lease_seconds,),
    ).fetchone()


def lock_claimed_request(conn: Connection, customer_id: UUID, lease_until: datetime) -> Row | None:
    """The customer, with the request cleared, if this worker still holds it. None means discard the result."""
    customer = lock_customer(conn, customer_id)
    if customer is None or customer["request"] is None or customer["request_lease_until"] != lease_until:
        return None
    conn.execute("UPDATE chatreceipt.customers SET request = NULL, request_lease_until = NULL WHERE id = %s", (customer_id,))
    return customer


def record_counts(conn: Connection, customer_id: UUID) -> Row:
    """How many records a customer has, and how many still have money owed on them."""
    return conn.execute(
        """
        SELECT count(*) AS records, count(*) FILTER (WHERE outstanding > 0) AS owed
        FROM chatreceipt.transactions WHERE customer_id = %s AND voided_at IS NULL
        """,
        (customer_id,),
    ).fetchone()  # type: ignore[return-value]


def search_transactions(
    conn: Connection,
    customer_id: UUID,
    *,
    person: str = "",
    reference: str = "",
    date_from: date | None = None,
    date_to: date | None = None,
    status: str = "any",
    timezone: str = "UTC",
    limit: int = 10,
) -> list[Row]:
    """A customer's own records matching the filters, newest first. The filters are values, never SQL."""
    conditions = ["customer_id = %s", "voided_at IS NULL"]
    parameters: list[Any] = [customer_id]
    if reference:
        conditions.append("reference = %s")
        parameters.append(reference)
    if person:
        # A name is matched as text: LIKE wildcards in it are escaped.
        pattern = "%" + person.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        conditions.append("(seller ILIKE %s OR buyer ILIKE %s)")
        parameters += [pattern, pattern]
    day = "COALESCE(transaction_date, (created_at AT TIME ZONE %s)::date)"
    if date_from is not None:
        conditions.append(f"{day} >= %s")
        parameters += [timezone, date_from]
    if date_to is not None:
        conditions.append(f"{day} <= %s")
        parameters += [timezone, date_to]
    if status == "owed":
        conditions.append("outstanding > 0")
    elif status in ("unpaid", "partially_paid", "paid"):
        conditions.append("payment_status = %s")
        parameters.append(status)
    parameters += [timezone, limit]
    return conn.execute(
        f"""
        SELECT {_RECORD_COLUMNS} FROM chatreceipt.transactions
        WHERE {" AND ".join(conditions)}
        ORDER BY COALESCE(transaction_date, (created_at AT TIME ZONE %s)::date) DESC, created_at DESC
        LIMIT %s
        """,  # noqa: S608 - only fixed column names are interpolated
        parameters,
    ).fetchall()


# -- Conversation memory -------------------------------------------------------------------------

MEMORY_LINES = 8


def remember(conn: Connection, customer_id: UUID, speaker: str, text: str) -> None:
    """Keep a line of the conversation ("You" or "ChatReceipt"), dropping the oldest beyond MEMORY_LINES."""
    line = " ".join(text.split())[:240]
    if not line:
        return
    conn.execute(
        """
        UPDATE chatreceipt.customers
        SET memory = (
            SELECT COALESCE(jsonb_agg(entry ORDER BY position), '[]'::jsonb)
            FROM (
                SELECT entry, position FROM jsonb_array_elements(memory || %s::jsonb) WITH ORDINALITY AS lines (entry, position)
                ORDER BY position DESC
                LIMIT %s
            ) recent
        )
        WHERE id = %s
        """,
        (Jsonb([{"who": speaker, "text": line, "at": datetime.now(UTC).isoformat(timespec="seconds")}]), MEMORY_LINES, customer_id),
    )


def recent_memory(customer: Row, *, within_minutes: int = 360) -> list[dict[str, str]]:
    """The conversation's recent lines, oldest first; older ones belong to another conversation."""
    cutoff = datetime.now(UTC).timestamp() - within_minutes * 60
    lines = []
    for entry in customer.get("memory") or []:
        try:
            at = datetime.fromisoformat(str(entry.get("at"))).timestamp()
        except ValueError:
            continue
        if at >= cutoff and entry.get("text"):
            lines.append({"who": str(entry.get("who") or ""), "text": str(entry["text"]), "at": str(entry.get("at"))})
    return lines
