"""What each incoming WhatsApp message does.

One message is handled in one database transaction. State changes and the replies that describe
them commit together, so a failure retries the whole message cleanly. Nothing here touches the
network: replies are queued, images are read later by the worker, and a typed correction that
needs the AI to understand it is queued for the worker too (``finish_instruction`` applies it).

A customer can only ever act on their own submissions and records: the customer comes from the
signed webhook, and every lookup is filtered by that customer.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from app.calculations import calculate, compute, to_calculation
from app.config import Settings
from app.currencies import get_currency
from app.db.pool import Connection
from app.models import PaymentStatus, TransactionDraft
from app.whatsapp import corrections, store
from app.whatsapp.messages import ReplyId, button_message, parse_reply_id, receipt_request, reply_id, text_message
from app.whatsapp.receipts import RECORD_GONE
from app.whatsapp.reconcile import accept, proposal, settle_status
from app.whatsapp.store import Row
from app.whatsapp.summary import (
    STATUS_LABELS,
    draft_messages,
    open_reasons,
    payment_line,
    question_messages,
    ready_to_save,
    receipt_caption,
    recent_text,
    saved_text,
    typed_value_needed,
    update_messages,
)
from app.whatsapp.understanding import AICorrection, amount_reply, apply_changes, is_money_path, item_reply, quick_changes

WELCOME = (
    "Hi, I'm *ChatReceipt* 👋\n"
    "Send me screenshots of a chat where you agreed a sale or a job, and I'll turn them into a receipt you can "
    "share. You check every detail before it's saved.\n\n"
    "Type */* to see everything else I can do.\n\n"
    "_Your screenshot is sent to our AI provider to be read, and ChatReceipt doesn't keep the image. "
    "Send *delete my data* any time to remove your records._"
)
FIRST_IMAGE_NOTE = (
    "Hi, I'm *ChatReceipt* 👋 _Your screenshot is sent to our AI provider to be read, and ChatReceipt doesn't keep "
    "the image. You check everything before a record is saved. Send *delete my data* any time to remove your records._"
)
ASK_FOR_IMAGE = "Send me a screenshot of the chat or payment. If it runs across several screenshots, send them together."
ACKNOWLEDGE = "📥 Got it. I'm reading your screenshot now."
READING_AGAIN = "🔄 Reading your screenshot again now."
STILL_READING = "I'm still reading your screenshot. I'll send the details here as soon as they're ready."
WELCOME_BACK = "Hi again 👋 Your draft from earlier isn't saved yet. Do you want to continue it, or start a new one?"
WELCOME_BACK_READING = "Hi again 👋 " + STILL_READING
HELP = (
    "*How ChatReceipt works*\n"
    "1. Send a screenshot of a chat or a payment.\n"
    "2. Check what I found. To fix anything, just tell me, like “the price is 20k” or “he still owes 40k”.\n"
    "3. Tap *Confirm* and I'll send you the receipt.\n\n"
    "*You can also send*\n"
    "• *show* to see your whole draft\n"
    "• *try again* to read the screenshot again\n"
    "• *recent* to see your latest records\n"
    "• *web* to open your records in a browser\n"
    "• *cancel* to stop working on a draft\n"
    "• *delete my data* to remove everything\n\n"
    "ChatReceipt records what a conversation says. It doesn't verify payments."
)
UNSUPPORTED = "I can only read screenshots (JPEG, PNG or WebP) and text messages. Please send a screenshot of the chat or payment."
NOTHING_OPEN = "There's no draft to work on right now. Send a screenshot to start one."
NOTHING_TO_RETRY = "There's nothing to read again right now. Send the screenshot and I'll read it."
DRAFT_GONE = "That draft is no longer available. Send the screenshot again to start a new one."
CANCELLED = "Cancelled. Nothing was saved."
STARTED_OVER = "Okay, I've cleared that draft. Send me the next screenshot."
SLOW_DOWN = "You're sending messages very quickly, so I'll pause for a minute before replying."
IMAGE_LIMIT = "You've reached the limit for screenshots for now. Please try again later."
TOO_MANY_IMAGES = "I can read up to {limit} screenshots at a time. I'll work with the first {limit}."
HELD_PROMPT = "You have a draft waiting. Should these screenshots be added to it, or start a new record?"
HELD_EDIT_WARNING = " Adding them means reading the draft again, so your edits to it will be lost."
REREAD_PROMPT = "Read the screenshot again from the start? Your corrections to this draft will be lost."
DIDNT_CATCH = "I didn't catch what to change. You can say things like “the price is 20k” or “he still owes 40k”."
NOT_UNDERSTOOD_NOW = "I couldn't work that out just now. Tell me again in a moment, or tap *Correct* to choose what to change."
ERASE_PROMPT = (
    "This permanently deletes your saved records and everything ChatReceipt holds for this WhatsApp account. "
    "It can't be undone. Do you want to continue?"
)
ASK_DATE = "📅 *What date was this?* It goes on the receipt.\nTap one, or type a date like *10 Nov*."
ERASED = "Your records and details have been deleted. Send a message any time to start again."
ERASE_KEPT = "Nothing was deleted."

MAX_REMEMBERED = 4
_GREETINGS = frozenset({"hi", "hello", "hey", "hiya", "hola", "greetings", "good", "start", "menu"})
# Typed words, the "/" commands in WhatsApp's menu (the slash is dropped by `normalise`) and the
# suggestions WhatsApp shows on a first chat (PROMPTS) all land here.
_COMMANDS: dict[str, frozenset[str]] = {
    "help": frozenset({"help", "get help", "how does this work", "how does it work", "how does chatreceipt work"}),
    "recent": frozenset(
        {"recent", "recent records", "records", "my records", "history", "transactions", "recent transactions", "show my recent records"}
    ),
    "web": frozenset({"web", "website", "link", "open on web", "sign in", "login", "log in", "open my records on the web"}),
    "scan": frozenset({"scan", "scan a receipt", "receipt", "scan receipt", "turn a chat into a receipt", "make a receipt"}),
    "restart": frozenset({"new", "start again", "start over", "restart", "new scan", "new record", "start afresh", "begin again"}),
    "retry": frozenset(
        {
            "retry", "try again", "can you try again", "please try again", "pls try again", "try it again", "try again please",
            "read it again", "read again", "check again", "scan again", "scan it again", "redo", "reread", "re read",
            "read the screenshot again", "can you read it again",
        }
    ),
    "show": frozenset(
        {
            "show", "show draft", "show my draft", "draft", "my draft", "show record", "show the record", "see draft",
            "view draft", "summary", "show summary", "show details", "show me", "show everything", "see everything",
        }
    ),
    "cancel": frozenset({"cancel", "stop"}),
    "confirm": frozenset({"confirm", "save", "save it", "confirm it", "save record"}),
    "correct": frozenset({"correct", "edit", "fix", "change"}),
    "erase": frozenset({"delete my data", "delete my account", "erase my data"}),
}
# Commands that still work while ChatReceipt is waiting for a typed value.
_ESCAPES = frozenset({"confirm", "retry", "show", "restart", "help", "erase", "recent", "web", "greeting"})
_BACK = frozenset({"back", "keep", "cancel"})
_NO_DATE = frozenset({"skip", "no date", "none", "no", "nope", "not sure", "dont know", "don t know", "i don t know"})
_MONTH_ABBR = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_MONEY_TARGET = re.compile(r"^(line_items\.[^.]+\.unit_price|delivery_fee|amount_paid|(discounts|additional_fees)\.[^.]+\.value|new:.+)$")


def normalise(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.casefold()).split())


def command_of(text: str) -> str:
    cleaned = normalise(text)
    for name, phrases in _COMMANDS.items():
        if cleaned in phrases:
            return name
    first = cleaned.split(" ", 1)[0] if cleaned else ""
    return "greeting" if first in _GREETINGS else ""


def draft_of(submission: Row) -> TransactionDraft:
    return TransactionDraft.model_validate(submission["draft"])


def _merged(*groups: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(path for group in groups for path in group))


class Turn:
    """Handling of one event for one customer, with replies queued under a stable key."""

    def __init__(
        self, conn: Connection, settings: Settings, customer: Row, key: str, *, message_id: str = "", understanding: bool = False
    ) -> None:
        self.conn = conn
        self.settings = settings
        self.customer = customer
        self.key = key
        # The incoming message being handled, and whether an AI is available to read free text.
        self.message_id = message_id
        self.understanding = understanding
        self._sent = 0

    @property
    def today(self) -> date:
        """Today in the customers' time zone, for dates such as “yesterday”."""
        return datetime.now(ZoneInfo(self.settings.whatsapp_timezone)).date()

    def send(self, payload: dict[str, Any]) -> None:
        self._sent += 1
        store.enqueue(self.conn, self.customer, payload, f"{self.key}:{self._sent}")

    def say(self, body: str) -> None:
        self.send(text_message(body))

    # -- menus ------------------------------------------------------------------------------

    def welcome(self) -> None:
        self.send(button_message(WELCOME, self._main_buttons()))

    def _main_buttons(self) -> list[tuple[str, str]]:
        return [
            (reply_id("menu", arg="scan"), "Scan a receipt"),
            (reply_id("menu", arg="recent"), "Recent records"),
            (reply_id("menu", arg="help"), "Get help"),
        ]

    def menu(self) -> None:
        self.send(button_message("What would you like to do?", self._main_buttons()))

    def welcome_back(self, submission: Row) -> None:
        """A greeting while a draft is open: say hello and let them pick up the draft or start over."""
        if submission["status"] != "awaiting_confirmation":
            self.say(WELCOME_BACK_READING)
            return
        self.send(
            button_message(
                WELCOME_BACK,
                [
                    (reply_id("menu", arg="draft"), "Continue the draft"),
                    (reply_id("menu", arg="restart"), "Start a new one"),
                    (reply_id("menu", arg="help"), "Get help"),
                ],
            )
        )

    def recent(self) -> None:
        rows = store.recent_transactions(self.conn, self.customer["id"], limit=5)
        if not rows:
            self.send(button_message("You don't have any saved records yet. Send a screenshot to make your first one.", [(reply_id("menu", arg="scan"), "Scan a receipt"), (reply_id("menu", arg="help"), "Get help")]))
            return
        self.send(button_message(recent_text(rows), [(reply_id("menu", arg="scan"), "Scan a receipt"), (reply_id("menu", arg="web"), "Open on web")]))

    def web_link(self) -> None:
        base = self.settings.web_url
        if not base:
            self.say("Your records can't be opened on the web right now. Send *recent* to see them here.")
            return
        ttl = self.settings.records_sign_in_ttl_minutes
        code = store.issue_sign_in_code(self.conn, self.customer["id"], ttl_minutes=ttl)
        self.say(f"Open your records here:\n{base}/records#code={code}\n\nThis private link works once and expires in {ttl} minutes. Don't share it.")

    # -- drafts -----------------------------------------------------------------------------

    def show_draft(self, submission: Row, *, lead: str = "") -> None:
        messages = draft_messages(
            draft_of(submission),
            submission_id=str(submission["id"]),
            revision=submission["revision"],
            review=submission["review"] or {},
            edited=submission["edited_fields"],
            lead=lead,
        )
        for message in messages:
            self.send(message)

    def show_update(self, submission: Row, notes: Sequence[str], problems: Sequence[str] = (), *, lead: str = "✅ *Updated*") -> None:
        if not notes and not problems:
            self.ask(submission, "✅ Done.")
            return
        messages = update_messages(
            draft_of(submission),
            submission_id=str(submission["id"]),
            revision=submission["revision"],
            review=submission["review"] or {},
            edited=submission["edited_fields"],
            notes=notes,
            problems=problems,
            lead=lead,
        )
        for message in messages:
            self.send(message)

    def ask(self, submission: Row, lead: str) -> None:
        for message in question_messages(draft_of(submission), submission_id=str(submission["id"]), revision=submission["revision"], lead=lead):
            self.send(message)

    def start_submission(self, images: list[dict[str, str]]) -> None:
        store.create_submission(self.conn, self.customer["id"], images, wait_seconds=self.settings.whatsapp_album_wait_seconds)
        self.say(ACKNOWLEDGE)

    def _start_held(self, submission: Row) -> None:
        if submission["held_images"]:
            self.start_submission(submission["held_images"])

    def on_image(self, payload: dict[str, str], message_id: str) -> None:
        image = {"message_id": message_id, "media_id": payload.get("media_id", ""), "mime_type": payload.get("mime_type", ""), "sha256": payload.get("sha256", "")}
        submission = store.open_submission(self.conn, self.customer["id"])
        if submission is None:
            self.start_submission([image])
            return
        limit = self.settings.max_files
        if submission["status"] == "awaiting_confirmation":
            held = submission["held_images"]
            if self._repeats(image, [*submission["images"], *held]) or len(held) >= limit:
                return
            store.update_submission(self.conn, submission["id"], held_images=[*held, image])
            if not held:
                warning = HELD_EDIT_WARNING if submission["edited_fields"] else ""
                self.send(
                    button_message(
                        HELD_PROMPT + warning,
                        [
                            (reply_id("hold", str(submission["id"]), submission["revision"], "add"), "Add to this record"),
                            (reply_id("hold", str(submission["id"]), submission["revision"], "new"), "Start a new record"),
                        ],
                    )
                )
            return
        images = submission["images"]
        if self._repeats(image, images):
            return
        if len(images) >= limit:
            self.say(TOO_MANY_IMAGES.format(limit=limit))
            return
        if submission["status"] == "processing":
            # Already being read: read again with this screenshot included when that attempt ends.
            store.update_submission(self.conn, submission["id"], images=[*images, image], images_version=submission["images_version"] + 1)
        else:
            store.extend_collection(
                self.conn,
                submission["id"],
                [*images, image],
                wait_seconds=self.settings.whatsapp_album_wait_seconds,
                max_wait_seconds=self.settings.whatsapp_album_max_wait_seconds,
            )

    @staticmethod
    def _repeats(image: dict[str, str], images: list[dict[str, str]]) -> bool:
        return any(
            entry.get("media_id") == image["media_id"] or (image["sha256"] and entry.get("sha256") == image["sha256"])
            for entry in images
        )

    def confirm(self, submission: Row, *, without_date: bool = False) -> None:
        draft = draft_of(submission)
        computation = compute(draft)
        if not ready_to_save(draft, calculate(draft)) or computation.total is None:
            self.show_draft(submission, lead="This record can't be saved yet.")
            return
        if not draft.transaction_date.strip() and not without_date:
            self._ask_date(submission)
            return
        record = store.save_transaction(
            self.conn,
            customer=self.customer,
            submission=submission,
            draft=draft.model_dump(mode="json"),
            calculation=to_calculation(draft, computation).model_dump(mode="json"),
            transaction_date=computation.transaction_date,
            total=computation.total,
            amount_paid=computation.amount_paid,
            outstanding=computation.outstanding,
        )
        store.update_submission(self.conn, submission["id"], status="confirmed", pending_edit=None, held_images=[], instruction=None, dialogue=[])
        self.send(receipt_request(str(record["id"]), "png", receipt_caption(record["reference"], draft)))
        self.send(
            button_message(
                saved_text(record["reference"]),
                [(reply_id("pdf", arg=str(record["id"])), "Get PDF"), (reply_id("menu", arg="scan"), "Scan another"), (reply_id("menu", arg="recent"), "Recent records")],
            )
        )
        self._start_held(submission)

    def _ask_date(self, submission: Row) -> None:
        """One question with one-tap answers, instead of saving a receipt with no date."""
        sid, revision, today = str(submission["id"]), submission["revision"], self.today
        yesterday = today - timedelta(days=1)
        store.update_submission(self.conn, submission["id"], pending_edit={"target": "transaction_date", "then": [], "save_after": True})
        self.send(
            button_message(
                ASK_DATE,
                [
                    (reply_id("date", sid, revision, today.isoformat()), f"Today · {today.day} {_MONTH_ABBR[today.month - 1]}"),
                    (reply_id("date", sid, revision, yesterday.isoformat()), f"Yesterday · {yesterday.day} {_MONTH_ABBR[yesterday.month - 1]}"),
                    (reply_id("date", sid, revision, "none"), "No date"),
                ],
            )
        )

    def _date_answer(self, submission: Row, draft: TransactionDraft, arg: str) -> None:
        if arg == "none":
            self.confirm(submission, without_date=True)
            return
        try:
            picked = date.fromisoformat(arg)
        except ValueError:
            self.show_draft(submission)
            return
        updated = self._save(submission, draft.model_copy(update={"transaction_date": picked.isoformat()}), ["transaction_date"], [], explicit_status=False, pending_edit=None)
        self.confirm(updated)

    def send_pdf(self, transaction_id: str) -> None:
        """Queue a PDF copy of a record, which can only ever be this customer's own."""
        try:
            record = store.get_transaction(self.conn, self.customer["id"], UUID(transaction_id))
        except ValueError:
            record = None
        if record is None:
            self.say(RECORD_GONE)
            return
        self.send(receipt_request(str(record["id"]), "pdf", f"*Receipt {record['reference']}*"))

    def cancel(self, submission: Row, *, reply: str = CANCELLED) -> None:
        store.update_submission(
            self.conn, submission["id"], status="cancelled", draft=None, review=None, pending_edit=None, held_images=[],
            lease_token=None, instruction=None, dialogue=[],
        )
        self.say(reply)
        self._start_held(submission)

    def start_again(self, submission: Row | None) -> None:
        if submission is None:
            self.say(ASK_FOR_IMAGE)
        elif submission["status"] in ("collecting", "processing"):
            self.say(STILL_READING)
        else:
            self.cancel(submission, reply=STARTED_OVER)

    def read_again(self, submission: Row | None) -> None:
        """Read the same screenshots again, when the customer asks to retry."""
        if submission is None:
            failed = store.latest_failed_submission(self.conn, self.customer["id"])
            if failed is None or not failed["images"]:
                self.send(button_message(NOTHING_TO_RETRY, self._main_buttons()))
                return
            store.restart_collection(self.conn, failed["id"], failed["images"], wait_seconds=0, revision=failed["revision"] + 1)
            self.say(READING_AGAIN)
            return
        if submission["status"] != "awaiting_confirmation":
            self.say(STILL_READING)
            return
        if submission["edited_fields"]:
            sid, revision = str(submission["id"]), submission["revision"]
            self.send(button_message(REREAD_PROMPT, [(reply_id("reread", sid, revision, "yes"), "Read it again"), (reply_id("reread", sid, revision, "no"), "Keep my draft")]))
            return
        self._reread(submission)

    def _reread(self, submission: Row) -> None:
        store.restart_collection(self.conn, submission["id"], submission["images"], wait_seconds=0, revision=submission["revision"] + 1)
        self.say(READING_AGAIN)

    # -- corrections ---------------------------------------------------------------------------

    def _save(self, submission: Row, draft: TransactionDraft, edited: Sequence[str], notes: list[str], *, explicit_status: bool, **values: Any) -> Row:
        """Store a corrected draft, settling the payment status the amounts imply when nobody chose it."""
        all_edited = _merged(submission["edited_fields"], edited)
        if any(is_money_path(path) for path in edited) and not explicit_status and "payment_status" not in submission["edited_fields"]:
            settled = settle_status(draft)
            if settled.payment_status != draft.payment_status:
                draft = settled
                notes.append(payment_line(settled.payment_status).replace("*Payment:*", "Payment:"))
        return store.update_submission(
            self.conn, submission["id"], draft=draft.model_dump(mode="json"), revision=submission["revision"] + 1, edited_fields=all_edited, **values
        )

    def _remember(self, submission: Row, text: str) -> None:
        dialogue = [*(submission.get("dialogue") or []), " ".join(text.split())[:300]][-MAX_REMEMBERED:]
        store.update_submission(self.conn, submission["id"], dialogue=dialogue)

    def _apply_edit(self, submission: Row, text: str, *, remember: bool = True) -> None:
        pending = submission["pending_edit"]
        target = pending["target"]
        draft = draft_of(submission)
        if pending.get("save_after") and normalise(text) in _NO_DATE:
            self.confirm(store.update_submission(self.conn, submission["id"], pending_edit=None), without_date=True)
            return
        raw = text
        if _MONEY_TARGET.match(target):
            raw = amount_reply(text) or text
        result = corrections.apply_text(draft, target, raw, today=self.today)
        if result.error or result.draft is None:
            if (changes := quick_changes(text)) is not None:
                if remember:
                    self._remember(submission, text)
                self._apply_correction(submission, AICorrection(intent="correct", changes=changes))
                return
            if self.understanding:
                # Not a plain value: let the AI read it, with the question it answers as context.
                self._queue(submission, text, target=target)
                return
            if remember:
                self._remember(submission, text)
            self.say(f"{result.error}\n\n{corrections.prompt_for(draft, target)}")
            return
        if remember:
            self._remember(submission, text)
        queue = [*result.then, *pending.get("then", [])]
        following = {"target": queue[0], "then": queue[1:]} if queue else None
        notes = [_describe_value(result.draft, target)] if not following else []
        updated = self._save(submission, result.draft, [target], notes, explicit_status=False, pending_edit=following)
        if following:
            self.say(corrections.prompt_for(result.draft, following["target"]))
        elif pending.get("save_after"):
            self.confirm(updated, without_date=True)
        else:
            self.show_update(updated, notes)

    def _change(self, submission: Row, draft: TransactionDraft, path: str, *, note: str, pending: dict[str, Any] | None = None) -> Row:
        explicit = path == "payment_status"
        notes = [note]
        updated = self._save(submission, draft, [path], notes, explicit_status=explicit, pending_edit=pending)
        if pending:
            self.say(corrections.prompt_for(draft, pending["target"]))
        else:
            self.show_update(updated, notes)
        return updated

    def _queue(self, submission: Row, text: str, *, target: str = "") -> None:
        """Hand a typed message to the worker, whose AI reading comes back through ``finish_instruction``."""
        store.update_submission(
            self.conn,
            submission["id"],
            instruction={"text": text[:1000], "message_id": self.message_id, "revision": submission["revision"], "target": target},
        )

    def _on_draft_text(self, submission: Row, text: str) -> None:
        """Free text about an open draft: a quick value if it is one, otherwise understood by the AI."""
        draft = draft_of(submission)
        target = typed_value_needed(calculate(draft), draft)
        quick = bool(target) and bool(_MONEY_TARGET.match(target)) and amount_reply(text) is not None
        if not quick and (changes := quick_changes(text)) is not None:
            self._remember(submission, text)
            self._apply_correction(submission, AICorrection(intent="correct", changes=changes))
            return
        if self.understanding and not quick:
            # No pending target: the AI is told the question the draft message itself asked.
            self._queue(submission, text)
        elif target and normalise(text) not in _BACK:
            self._apply_edit({**submission, "pending_edit": {"target": target, "then": []}}, text)
        elif not draft.line_items and (found := item_reply(text)) is not None:
            self._remember(submission, text)
            self._add_item(submission, *found)
        else:
            self._remember(submission, text)
            self.show_draft(submission, lead="You have a draft waiting. Please confirm, correct or cancel it.")

    def _add_item(self, submission: Row, description: str, price: str) -> None:
        added = corrections.apply_text(draft_of(submission), corrections.NEW_ITEM, description)
        if added.draft is None:
            self.show_draft(submission)
            return
        row = added.draft.line_items[-1]
        draft = _replace(added.draft, row.id, quantity="1", unit_price=price)
        notes = [f"Added “{row.description}” for {_money_text(draft, price)}"]
        updated = self._save(submission, draft, [f"line_items.{row.id}"], notes, explicit_status=False)
        self.show_update(updated, notes)

    # -- incoming text ------------------------------------------------------------------------

    def on_text(self, text: str) -> None:
        submission = store.open_submission(self.conn, self.customer["id"])
        command = command_of(text)
        awaiting = submission is not None and submission["status"] == "awaiting_confirmation"
        if awaiting and submission["pending_edit"] and command not in _ESCAPES:  # type: ignore[index]
            if normalise(text) in _BACK:
                updated = store.update_submission(self.conn, submission["id"], pending_edit=None)  # type: ignore[index]
                self.show_draft(updated)
            else:
                self._apply_edit(submission, text)  # type: ignore[arg-type]
            return
        if awaiting and submission["pending_edit"]:  # type: ignore[index]
            # A command takes over from the value we were waiting for.
            asked_date = bool(submission["pending_edit"].get("save_after"))  # type: ignore[index]
            submission = store.update_submission(self.conn, submission["id"], pending_edit=None)  # type: ignore[index]
            if command == "confirm" and asked_date:
                self.confirm(submission, without_date=True)
                return

        if command == "greeting" and submission is not None:
            self.welcome_back(submission)
        elif command == "help":
            self.send(button_message(HELP, self._main_buttons()[:2]))
        elif command == "recent":
            self.recent()
        elif command == "web":
            self.web_link()
        elif command == "scan":
            self.say(ASK_FOR_IMAGE)
        elif command == "restart":
            self.start_again(submission)
        elif command == "retry":
            self.read_again(submission)
        elif command == "erase":
            self.send(button_message(ERASE_PROMPT, [(reply_id("erase", arg="yes"), "Delete everything"), (reply_id("erase", arg="no"), "Keep my data")]))
        elif command == "cancel":
            if submission is None:
                self.say(NOTHING_OPEN)
            else:
                self.cancel(submission)
        elif submission is None:
            if command == "greeting":
                self.menu()
            else:
                self.send(button_message(ASK_FOR_IMAGE, self._main_buttons()))
        elif submission["status"] != "awaiting_confirmation":
            self.say(STILL_READING)
        elif command == "confirm":
            self.confirm(submission)
        elif command == "correct":
            self._open_corrections(submission)
        elif command == "show":
            self.show_draft(submission)
        else:
            self._on_draft_text(submission, text)

    def _open_corrections(self, submission: Row) -> None:
        menus = corrections.Menus(draft_of(submission), str(submission["id"]), submission["revision"])
        self.send(menus.main())

    # -- the AI's reading of a typed correction -------------------------------------------------

    def after_interpretation(self, submission: Row, instruction: dict[str, Any], correction: AICorrection | None) -> None:
        text = str(instruction.get("text") or "")
        self._remember(submission, text)
        submission = {**submission, "dialogue": [*(submission.get("dialogue") or []), text][-MAX_REMEMBERED:]}
        draft = draft_of(submission)
        if correction is None:
            # The AI couldn't be reached: fall back to what can be read without it.
            target = str(instruction.get("target") or "") or typed_value_needed(calculate(draft), draft)
            amount = amount_reply(text)
            if target and _MONEY_TARGET.match(target) and amount is not None:
                self.understanding = False
                self._apply_edit({**submission, "pending_edit": {"target": target, "then": []}}, amount, remember=False)
            elif not draft.line_items and (found := item_reply(text)) is not None:
                self._add_item(submission, *found)
            elif (changes := quick_changes(text)) is not None:
                self._apply_correction(submission, AICorrection(intent="correct", changes=changes))
            else:
                self.ask(submission, NOT_UNDERSTOOD_NOW)
            return

        intent = correction.intent
        if intent == "confirm":
            self.confirm(submission)
        elif intent == "cancel":
            self.cancel(submission)
        elif intent == "read_again":
            self.read_again(submission)
        elif intent == "show_record":
            self.show_draft(submission)
        elif intent == "new_record":
            self.start_again(submission)
        elif intent == "help":
            self.send(button_message(HELP, self._main_buttons()[:2]))
        else:
            self._apply_correction(submission, correction)

    def _apply_correction(self, submission: Row, correction: AICorrection) -> None:
        """Apply the changes a message makes, however they were read, and reply with a short update."""
        applied = apply_changes(draft_of(submission), correction.changes, today=self.today)
        review = dict(submission["review"] or {})
        # The checks were numbered in the order open_reasons lists them; map back to the review's positions.
        listed = [index for index, _ in open_reasons(review, submission["edited_fields"])]
        answered = {listed[number - 1] for number in correction.resolved_checks if 1 <= number <= len(listed)}
        resolved = sorted(set(review.get("resolved") or []) | answered)
        if not applied.edited and len(resolved) == len(review.get("resolved") or []):
            lead = correction.question or DIDNT_CATCH
            self.ask(submission, "\n".join([lead, *[f"⚠️ {problem}" for problem in applied.problems]]))
            return
        review["resolved"] = resolved
        notes = list(applied.notes)
        updated = self._save(submission, applied.draft, applied.edited, notes, explicit_status=applied.status_set, pending_edit=None, review=review)
        if (submission.get("pending_edit") or {}).get("save_after") and applied.draft.transaction_date and not applied.problems:
            self.confirm(updated)
            return
        if not notes and not applied.problems:
            notes = ["Thanks, I've cleared that check."]
        self.show_update(updated, notes, applied.problems)

    # -- taps on buttons and lists ------------------------------------------------------------

    def on_reply(self, raw: str) -> None:
        reply = parse_reply_id(raw)
        if reply is None:
            self.menu()
        elif reply.action == "menu":
            self._on_menu(reply.arg)
        elif reply.action == "erase":
            self._on_erase(reply.arg)
        elif reply.action == "pdf":
            self.send_pdf(reply.arg)
        else:
            self._on_draft_reply(reply)

    def _on_menu(self, choice: str) -> None:
        if choice == "retry":
            self.read_again(store.open_submission(self.conn, self.customer["id"]))
        elif choice == "scan":
            self.say(ASK_FOR_IMAGE)
        elif choice == "recent":
            self.recent()
        elif choice == "web":
            self.web_link()
        elif choice == "help":
            self.send(button_message(HELP, self._main_buttons()[:2]))
        elif choice in ("draft", "restart"):
            submission = store.open_submission(self.conn, self.customer["id"])
            if submission is None:
                self.say(NOTHING_OPEN)
            elif choice == "restart":
                self.start_again(submission)
            elif submission["status"] != "awaiting_confirmation":
                self.say(STILL_READING)
            else:
                self.show_draft(submission)
        else:
            self.menu()

    def _on_erase(self, choice: str) -> None:
        if choice != "yes":
            self.say(ERASE_KEPT)
            return
        store.delete_customer_data(self.conn, self.customer["id"])
        self.say(ERASED)

    def _on_draft_reply(self, reply: ReplyId) -> None:
        submission = store.get_submission(self.conn, self.customer["id"], reply.submission_id) if reply.submission_id else None
        if submission is None:
            self.say(DRAFT_GONE)
            return
        status = submission["status"]
        if status == "confirmed":
            saved = store.saved_for_submission(self.conn, self.customer["id"], submission["id"])
            self.say(f"This record is already saved as *{saved['reference']}*." if saved else DRAFT_GONE)
            return
        if status in ("collecting", "processing"):
            self.say(STILL_READING)
            return
        if status != "awaiting_confirmation":
            self.say(DRAFT_GONE)
            return
        if reply.revision != submission["revision"]:
            self.show_draft(submission, lead="That message is out of date. Here's the latest version.")
            return
        self._dispatch(reply, submission)

    def _dispatch(self, reply: ReplyId, submission: Row) -> None:
        action, arg = reply.action, reply.arg
        if submission["pending_edit"] and action != "ask":
            # Tapping something else abandons the value we were waiting for.
            submission = store.update_submission(self.conn, submission["id"], pending_edit=None)
        draft = draft_of(submission)
        menus = corrections.Menus(draft, str(submission["id"]), submission["revision"])
        if action == "ok":
            self.confirm(submission)
        elif action == "drop":
            self.cancel(submission)
        elif action == "fix":
            self.send(menus.main())
        elif action == "hold":
            self._on_held(submission, arg)
        elif action == "nav":
            self._navigate(submission, menus, arg)
        elif action == "ask":
            updated = store.update_submission(self.conn, submission["id"], pending_edit={"target": arg, "then": []})
            self.say(corrections.prompt_for(draft_of(updated), arg))
        elif action == "rm":
            removed = next((row.description for row in draft.line_items if row.id == arg), "")
            self._change(submission, corrections.remove_item(draft, arg), f"line_items.{arg}", note=f"Removed “{removed or 'the item'}”")
        elif action == "pay" and arg in PaymentStatus._value2member_map_:
            status = PaymentStatus(arg)
            changed = corrections.set_payment_status(draft, status)
            pending = {"target": "amount_paid", "then": []} if status == PaymentStatus.PARTIALLY_PAID else None
            self._change(submission, changed, "payment_status", note=f"Payment: {STATUS_LABELS[status]}", pending=pending)
        elif action == "cur" and get_currency(arg) is not None:
            self._change(submission, draft.model_copy(update={"currency": arg}), "currency", note=f"Currency: {arg}")
        elif action == "settle":
            self._settle(submission, draft, arg)
        elif action == "date":
            self._date_answer(submission, draft, arg)
        elif action == "reread":
            if arg == "yes":
                self._reread(submission)
            else:
                self.ask(submission, "Okay, keeping your draft.")
        else:
            self.show_draft(submission)

    def _settle(self, submission: Row, draft: TransactionDraft, choice: str) -> None:
        """The answer to a question about a stated balance or total that disagrees with the figures."""
        fix = proposal(draft)
        if fix is None:
            self.show_draft(submission)
            return
        if choice == "yes" and fix.changes:
            accepted = accept(draft, fix)
            edited = list(fix.changes)
            notes = [_describe_value(accepted, path) for path in fix.changes if not path.endswith(".quantity")]
            updated = self._save(submission, accepted, edited, notes, explicit_status=False)
            self.show_update(updated, notes)
            return
        # The calculated figure is right: drop the stated one that disagreed with it.
        kept = draft.model_copy(update={fix.keep_clears: ""})
        label = "total" if fix.keep_clears == "stated_total" else "balance"
        updated = store.update_submission(
            self.conn, submission["id"], draft=kept.model_dump(mode="json"), revision=submission["revision"] + 1,
            edited_fields=_merged(submission["edited_fields"], [fix.keep_clears]),
        )
        self.show_update(updated, [f"Kept the calculated {label}"])

    def _navigate(self, submission: Row, menus: corrections.Menus, arg: str) -> None:
        if arg == "items":
            self.send(menus.items())
        elif arg.startswith("items:") and arg.removeprefix("items:").isdigit():
            self.send(menus.items(int(arg.removeprefix("items:"))))
        elif arg.startswith("item:"):
            menu = menus.item(arg.removeprefix("item:"))
            if menu is None:
                self.show_draft(submission)
            else:
                self.send(menu)
        elif arg in ("adj:discounts", "adj:additional_fees"):
            self.send(menus.adjustments(arg.removeprefix("adj:")))
        elif arg == "pay":
            self.send(menus.payment())
        elif arg == "cur":
            self.send(menus.currencies())
        elif arg == "more":
            self.send(menus.more())
        else:
            self.send(menus.main())

    def _on_held(self, submission: Row, choice: str) -> None:
        held = submission["held_images"]
        if not held:
            self.show_draft(submission)
        elif choice == "add":
            images = [*submission["images"], *held][: self.settings.max_files]
            store.restart_collection(
                self.conn, submission["id"], images, wait_seconds=0, revision=submission["revision"] + 1
            )
            self.say(ACKNOWLEDGE)
        else:
            store.update_submission(self.conn, submission["id"], status="cancelled", draft=None, review=None, pending_edit=None, held_images=[])
            self.start_submission(held)

    # -- limits -------------------------------------------------------------------------------

    def over_limit(self, kind: str) -> bool:
        """Stop replying to floods, and stop paying to read images beyond the limits. Warn once."""
        customer_id = self.customer["id"]
        per_minute = self.settings.whatsapp_messages_per_minute
        recent = store.inbound_count(self.conn, customer_id, seconds=60)
        if recent > per_minute:
            if recent == per_minute + 1:
                self.say(SLOW_DOWN)
            return True
        if kind == "image":
            hour = store.inbound_count(self.conn, customer_id, seconds=3600, kind="image")
            day = store.inbound_count(self.conn, customer_id, seconds=86_400, kind="image")
            hour_limit, day_limit = self.settings.whatsapp_images_per_hour, self.settings.whatsapp_images_per_day
            if hour > hour_limit or day > day_limit:
                if hour == hour_limit + 1 or day == day_limit + 1:
                    self.say(IMAGE_LIMIT)
                return True
        return False


def _replace(draft: TransactionDraft, row_id: str, **values: str) -> TransactionDraft:
    rows = [row.model_copy(update=values) if row.id == row_id else row for row in draft.line_items]
    return draft.model_copy(update={"line_items": rows})


def _money_text(draft: TransactionDraft, value: str) -> str:
    from app.whatsapp.summary import money

    return money(value, get_currency(draft.currency))


def _describe_value(draft: TransactionDraft, target: str) -> str:
    """One line saying what a field is now, for the short update after a correction."""
    parts = target.split(".")
    if len(parts) == 3 and parts[0] == "line_items":
        row = next((entry for entry in draft.line_items if entry.id == parts[1]), None)
        if row is None:
            return "Item updated"
        name = f"“{row.description.strip() or 'this item'}”"
        if parts[2] == "unit_price":
            return f"Price of {name}: {_money_text(draft, row.unit_price)}"
        if parts[2] == "quantity":
            return f"Quantity of {name}: {row.quantity}"
        return f"Item: {name}"
    if target == corrections.NEW_ITEM and draft.line_items:
        row = draft.line_items[-1]
        return f"Added “{row.description}” ({row.quantity or '?'} × {_money_text(draft, row.unit_price)})"
    if len(parts) == 3 and parts[0] in ("discounts", "additional_fees"):
        rows = getattr(draft, parts[0])
        row = next((entry for entry in rows if entry.id == parts[1]), None)
        noun = "Discount" if parts[0] == "discounts" else "Fee"
        if row is None:
            return f"{noun} removed"
        return f"{row.label or noun}: {f'{row.value}%' if row.kind == 'percent' else _money_text(draft, row.value)}"
    if target in (corrections.NEW_DISCOUNT, corrections.NEW_FEE):
        return "Discount added" if target == corrections.NEW_DISCOUNT else "Fee added"
    labels = {
        "seller": "Seller", "buyer": "Buyer", "delivery_fee": "Delivery", "amount_paid": "Paid so far",
        "transaction_date": "Date", "due_date": "Due date", "currency": "Currency", "payment_terms": "Payment terms",
        "delivery_terms": "Delivery terms", "notes": "Notes",
    }
    value = getattr(draft, target, "")
    if target in ("delivery_fee", "amount_paid"):
        value = _money_text(draft, value) if value else "none"
    if target in ("transaction_date", "due_date") and value:
        from app.whatsapp.summary import readable_date

        value = readable_date(value)
    return f"{labels.get(target, target)}: {str(value)[:80] or 'none'}"


def handle_inbound(conn: Connection, settings: Settings, row: Row, *, understanding: bool = False) -> float | None:
    """Do what one stored message asks for. A number means: look at it again in that many seconds."""
    customer = store.lock_customer(conn, row["customer_id"])
    if customer is None:
        return None
    if row["kind"] == "unsupported" and customer["welcomed_at"] is not None:
        # Sending several photos together delivers an extra item WhatsApp's API can't describe. Wait
        # for the photos, and stay quiet if they came; otherwise say what ChatReceipt can read.
        if store.image_near(conn, customer["id"], row["sent_at"], seconds=15):
            return None
        if row["attempts"] == 0:
            return settings.whatsapp_album_wait_seconds
    turn = Turn(conn, settings, customer, f"in:{row['message_id']}", message_id=row["message_id"], understanding=understanding)
    kind, payload = row["kind"], row["payload"]
    if turn.over_limit(kind):
        return None
    first_contact = customer["welcomed_at"] is None
    if first_contact:
        store.mark_welcomed(conn, customer["id"])
    if kind == "image":
        if first_contact:
            turn.say(FIRST_IMAGE_NOTE)
        turn.on_image(payload, row["message_id"])
    elif first_contact:
        turn.welcome()
    elif kind == "text":
        turn.on_text(payload.get("text", ""))
    elif kind == "reply":
        turn.on_reply(payload.get("reply_id", ""))
    else:
        turn.say(UNSUPPORTED)
    return None


def finish_instruction(conn: Connection, settings: Settings, claimed: Row, correction: AICorrection | None) -> None:
    """Apply the AI's reading of a typed correction, if this worker still holds it."""
    locked = store.lock_claimed_instruction(conn, claimed["id"], claimed["lease_token"])
    if locked is None:
        return
    customer, submission = locked
    instruction = dict(submission["instruction"] or {})
    key = f"ins:{instruction.get('message_id') or submission['revision']}"
    submission = store.update_submission(conn, submission["id"], instruction=None, lease_token=None, lease_expires_at=None)
    Turn(conn, settings, customer, key).after_interpretation(submission, instruction, correction)
