"""What each incoming WhatsApp message does.

One message is handled in one database transaction. State changes and the replies that describe
them commit together, so a failure retries the whole message cleanly. Nothing here touches the
network: replies are queued, images are read later by the worker, and a typed correction that
needs the AI to understand it is queued for the worker too (``finish_instruction`` applies it).

A customer can only ever act on their own submissions and records: the customer comes from the
signed webhook, and every lookup is filtered by that customer.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from app.calculations import calculate, compute, to_calculation
from app.config import Settings
from app.currencies import get_currency
from app.db.pool import Connection
from app.models import PaymentStatus, TransactionDraft
from app.names import clean_name
from app.observability import log_event
from app.whatsapp import assistant, corrections, parties, store
from app.whatsapp.messages import ReplyId, button_message, list_message, parse_reply_id, receipt_request, reply_id, text_message
from app.whatsapp.messages import Row as ChoiceRow
from app.whatsapp.receipts import RECORD_GONE
from app.whatsapp.reconcile import accept, proposal, settle_status
from app.whatsapp.store import Row
from app.whatsapp.summary import (
    CONFIRM_QUESTION,
    STATUS_LABELS,
    draft_messages,
    open_reasons,
    payment_line,
    payment_question,
    question_messages,
    ready_to_save,
    receipt_caption,
    recent_text,
    responder_view,
    saved_text,
    money,
    next_step,
    typed_value_needed,
    update_messages,
)
from app.whatsapp.understanding import (
    DRAFT_INTENTS,
    AICorrection,
    amount_reply,
    apply_changes,
    is_money_path,
    item_reply,
    quick_changes,
    safe_reply,
)

WELCOME = (
    "Hi, I'm *ChatReceipt* 👋\n"
    "Send me screenshots of a chat where you agreed a sale or a job, and I'll turn them into a receipt you can "
    "share. You check every detail before it's saved.\n\n"
    "Type */* to see everything else I can do.\n\n"
    "_Your screenshot is sent to our AI provider to be read, and ChatReceipt doesn't keep the image. "
    "Send *delete my data* any time to remove your records._"
)
FIRST_IMAGE_NOTE = (
    "Hi, I'm *ChatReceipt* 👋 I'm reading your screenshot now.\n\n_Your screenshot is sent to our AI provider to be "
    "read, and ChatReceipt doesn't keep the image. You check everything before a record is saved. Send *delete my data* "
    "any time to remove your records._"
)
ASK_FOR_IMAGE = "Send me a screenshot of the chat or payment. If it runs across several screenshots, send them together."
ACKNOWLEDGE = "📥 Got it. I'm reading your screenshot now."
READING_AGAIN = "🔄 Reading your screenshot again now."
STILL_READING = "I'm still reading your screenshot. I'll send the details here as soon as they're ready."
STILL_READING_EXPORT = "I'm still reading your chat export. I'll send the details here as soon as they're ready."
WELCOME_BACK = "Hi again 👋 Your draft from earlier isn't saved yet. Do you want to continue it, or start a new one?"
HELP = (
    "*How ChatReceipt works*\n"
    "1. Send a screenshot of a chat or a payment, or export the chat and send me the file "
    "(Android: in the chat, tap ⋮ › More › Export chat. iPhone: tap the name at the top › Export Chat).\n"
    "2. Check what I found. To fix anything, just tell me, like “the price is 20k” or “he still owes 40k”.\n"
    "3. Tap *Confirm* and forward the receipt: the other person can confirm it with one tap.\n\n"
    "*You can also send*\n"
    "• *show* to see your whole draft\n"
    "• *try again* to read the screenshot again\n"
    "• *recent* to see your latest records\n"
    "• *web* to open your records in a browser\n"
    "• *cancel* to stop working on a draft\n"
    "• *my name is …* to set the name on your receipts\n"
    "• *clear* to clear this chat's history (your records stay)\n"
    "• *delete my data* to remove everything\n\n"
    "*Or just ask*, for example:\n"
    "• who still owes me?\n"
    "• how much did I sell this week?\n"
    "• send Ada's receipt again\n"
    "• show my records from last month\n\n"
    "ChatReceipt records what a conversation says. It doesn't verify payments."
)
THANKS_REPLY = "You're welcome 🙂 Send a screenshot whenever you need another receipt."
NOT_SURE = "I'm not sure what you need. I can make a receipt from screenshots, find your saved records or tell you who still owes you. What would you like?"
NOT_UNDERSTOOD_REQUEST = "I couldn't work that out just now. Ask me again in a moment, or send *help* to see what I can do."
NO_DRAFT_OPEN = "There's no receipt open to change right now. Send a screenshot of the chat to start one."
NOTHING_FOUND = "I couldn't find a saved record {what}. Send *recent* to see your latest ones."
NOTHING_OWED = "Nothing is owed on your saved records {what}."
WHICH_RECORD = "I found {count} records {what}. Which one should I send?"
ALREADY_DONE = "I've already got that one 👍"
UNSUPPORTED = (
    "I can read screenshots (JPEG, PNG or WebP), WhatsApp chat exports (.txt or .zip) and text messages. "
    "Please send a screenshot of the chat or payment, or export the chat and send the file."
)
EXPORT_ACKNOWLEDGE = "📥 Got your chat export. I'm reading it now."
EXPORT_HELD_PROMPT = "You have a draft waiting. Should I start a new record from this chat instead?"
EXPORT_BUSY = "I'm still reading what you sent before. Send the chat export again when I've finished."
READING_EXPORT = "I'm reading your chat export. Send the screenshot again when I've finished."
READING_EXPORT_AGAIN = "🔄 Reading your chat export again now."
SCREENSHOTS_HELD_PROMPT = "You have a draft from a chat export waiting. Should I start a new record from these screenshots?"
KEEPING_DRAFT = "Okay, keeping this draft."
FIRST_EXPORT_NOTE = (
    "Hi, I'm *ChatReceipt* 👋 I'm reading your chat export now.\n\n_Your chat is sent to our AI provider to be read, "
    "and ChatReceipt doesn't keep the file. You check everything before a record is saved. Send *delete my data* any "
    "time to remove your records._"
)
READING_DEAL = "📥 Reading the chat again for this deal: {deal}"
NO_SUCH_DEAL = "There's no deal {number} in your last chat export."
DEAL_FROM_SCREENSHOTS = "To make a receipt for another deal in these screenshots, send only the screenshots of that deal."
CLEAR_PROMPT = (
    "*Clear this chat's history?*\n"
    "I'll forget your open draft and what I remember from our conversation. Your saved records stay.\n\n"
    "_The messages on your phone stay too: WhatsApp doesn't let me delete them._"
)
CLEARED = "Done. Your open draft and our conversation history are cleared. Your saved records are still here."
CLEARED_NOTHING = "Done. Our conversation history is cleared. Your saved records are still here."
CLEAR_KEPT = "Nothing was cleared."
WHAT_TO_CHANGE = "No problem. Tell me what's wrong in your own words, like “the buyer is Emeka” or “he paid 20k”, or pick from the list."
NOTHING_OPEN = "There's no draft to work on right now. Send a screenshot to start one."
NOTHING_TO_RETRY = "There's nothing to read again right now. Send the screenshot and I'll read it."
DRAFT_GONE = "That draft is no longer available. Send the screenshot or chat export again to start a new one."
CANCELLED = "Cancelled. Nothing was saved."
STARTED_OVER = "Okay, I've cleared that draft. Send me the next screenshot."
SLOW_DOWN = "You're sending messages very quickly, so I'll pause for a minute before replying."
IMAGE_LIMIT = "You've reached the limit for screenshots and chat exports for now. Please try again later."
TOO_MANY_IMAGES = "I can read up to {limit} screenshots at a time. I'll work with the first {limit}."
HELD_PROMPT = "You have a draft waiting. Should these screenshots be added to it, or start a new record?"
HELD_EDIT_WARNING = " Adding them means reading the draft again, so your edits to it will be lost."
REREAD_PROMPT = "Read the screenshot again from the start? Your corrections to this draft will be lost."
REREAD_EXPORT_PROMPT = "Read the chat export again from the start? Your corrections to this draft will be lost."
DIDNT_CATCH = "I didn't catch what to change. You can say things like “the price is 20k” or “he still owes 40k”."
NOT_UNDERSTOOD_NOW = "I couldn't work that out just now. Tell me again in a moment, or tap *Correct* to choose what to change."
ERASE_PROMPT = (
    "This permanently deletes your saved records and everything ChatReceipt holds for this WhatsApp account. "
    "It can't be undone. Do you want to continue?"
)
ASK_DATE = "📅 *What date was this?* It goes on the receipt.\nTap one, or type a date like *10 Nov*."
ERASED = "Your records and details have been deleted. Send a message any time to start again."
ERASE_KEPT = "Nothing was deleted."
# Payment checks: the chat's word that money was paid isn't the money.
PAYMENT_RECEIVED = "✅ Noted: {amount} reached your account for *{reference}*."
PAYMENT_MISSING = (
    "Okay. *{reference}* now shows {amount} as *not in your account yet*. Don't hand anything over until it arrives.\n\n"
    "Tap below when it does."
)
NOTHING_TO_CHECK = "There's no payment to check on that record."
# The other person's confirmation, through the link on a receipt.
CODE_NOT_FOUND = "I couldn't find that record. Check the link you were sent, or ask the person who sent it."
OWN_RECORD = "That's the link for your own record *{reference}*. Forward the receipt to the other person so they can confirm it."
ALREADY_CONFIRMED = "You've already confirmed *{reference}* 👍"
THANKS_CONFIRMED = (
    "✅ Thanks. I've told {owner} you confirmed *{reference}*.\n\n"
    "_You can keep your own records too: send me a screenshot of any deal and I'll make the receipt._"
)
WHATS_WRONG = "What isn't right about *{reference}*? Tell me in one message, like “I paid 15k, not 10k”."
THANKS_DISPUTED = "Thanks. I've sent that to {owner}, so you can sort it out together."
AGREED_NOTICE = "✅ *{who}* confirmed your record *{reference}*."
DISPUTED_NOTICE = (
    "⚠️ *{who}* says something's wrong with *{reference}*:\n“{note}”\n\n"
    "Talk to them. If the record needs changing, send the corrected screenshots and save a new one."
)
# How long a "Something's wrong" tap waits for the message saying what.
DISPUTE_WAIT = timedelta(hours=1)
_CONFIRM_CODE = re.compile(r"^\s*confirm\s+([0-9a-z]{10})\s*[.!]*\s*$", re.IGNORECASE)

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
    "clear": frozenset({
        "clear", "clear history", "clear chat", "clear my history", "clear my chat", "clear conversation",
        "clear the chat", "clear chat history", "clear my chat history", "clear our chat", "clear this chat",
    }),
}
# Commands that still work while ChatReceipt is waiting for a typed value.
_ESCAPES = frozenset({"confirm", "retry", "show", "restart", "help", "erase", "recent", "web", "greeting", "clear"})
# Short answers read against the question just asked: "yes" or "that's correct" to "Is this correct?" confirms.
# A bare "correct" isn't here: it's also the name of the Correct button, and a saved record can't be edited.
_YES = frozenset({
    "yes", "yeah", "yep", "yh", "ya", "yea", "ok", "okay", "oky", "sure", "that s correct", "thats correct",
    "it s correct", "its correct", "right", "that s right", "thats right", "exactly", "perfect", "fine", "all good",
    "yes correct", "yes it is", "yes it s correct", "na so", "e correct", "it is correct", "all correct",
})
_NO = frozenset({
    "no", "nope", "nah", "not correct", "incorrect", "wrong", "that s wrong", "thats wrong", "no it s not", "it s not correct",
    "its not correct", "not right", "no o", "e no correct",
})
_DEAL = re.compile(r"^(?:make |receipt for )?deal\s*(?:no|number)?\s*([1-3])$")
# Inbound kinds read by the AI, which count towards the hourly and daily reading limits.
_READ_BY_AI = ("image", "document")
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


def confirm_code_in(text: str) -> str:
    """The code in "CONFIRM K7Q2M9XWAB", the message a receipt's confirmation link writes, or ""."""
    found = _CONFIRM_CODE.match(text or "")
    return found.group(1).upper() if found else ""


def other_side(draft: TransactionDraft, owner_role: str | None) -> str:
    """The name on the side of a record that isn't the customer's, when their side is known."""
    if owner_role == "seller":
        return draft.buyer.strip()
    if owner_role == "buyer":
        return draft.seller.strip()
    return ""


def owner_name(record: Row) -> str:
    """How the person who saved a record is named to the other person: their side's name, or their own."""
    role = record.get("owner_role")
    if role in parties.ROLES and str(record.get(role) or "").strip():
        return clean_name(str(record[role]), 60)
    return clean_name(str(record.get("owner_party_name") or record.get("owner_whatsapp_name") or ""), 60)


def responder_label(customer: Row) -> str:
    """Who answered a record, as WhatsApp identified them: their profile name and their number or username."""
    name = clean_name(str(customer.get("whatsapp_name") or ""), 60)
    if customer.get("whatsapp_phone"):
        handle = f"+{customer['whatsapp_phone']}"
    elif customer.get("whatsapp_username"):
        handle = f"@{customer['whatsapp_username']}"
    else:
        handle = ""
    if name and handle:
        return f"{name} ({handle})"
    return name or handle or "The other person"


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
        # The conversation before the message being handled, set by on_text.
        self.earlier: list[str] = []
        # This is the customer's first message to ChatReceipt (set by handle_inbound).
        self.first_contact = False
        self._sent = 0

    @property
    def today(self) -> date:
        """Today in the customers' time zone, for dates such as “yesterday”."""
        return datetime.now(ZoneInfo(self.settings.whatsapp_timezone)).date()

    def send(self, payload: dict[str, Any], *, remember: bool = True) -> None:
        self._sent += 1
        store.enqueue(self.conn, self.customer, payload, f"{self.key}:{self._sent}")
        if remember:
            store.remember(self.conn, self.customer["id"], "ChatReceipt", _gist(payload))

    def say(self, body: str, *, remember: bool = True) -> None:
        self.send(text_message(body), remember=remember)

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

    def help(self) -> None:
        self.send(button_message(HELP, [*self._main_buttons()[:2], (reply_id("clear", arg="ask"), "Clear history")]))

    def ask_clear(self) -> None:
        """Clearing the conversation keeps saved records; deleting everything is a separate, confirmed step."""
        self.send(
            button_message(
                CLEAR_PROMPT,
                [(reply_id("clear", arg="yes"), "Clear history"), (reply_id("clear", arg="erase"), "Delete all my data"), (reply_id("clear", arg="no"), "Keep everything")],
            )
        )

    def _on_clear(self, choice: str) -> None:
        if choice == "ask":
            self.ask_clear()
        elif choice == "yes":
            had_draft = store.clear_history(self.conn, self.customer["id"])
            log_event("whatsapp.history_cleared", draft=had_draft)
            # The confirmation isn't remembered either: the conversation starts again from nothing.
            self.say(CLEARED if had_draft else CLEARED_NOTHING, remember=False)
        elif choice == "erase":
            self.ask_erase()
        else:
            self.say(CLEAR_KEPT)

    def greet(self) -> None:
        """A returning customer says hello: welcome them back, with where their records stand."""
        counts = store.record_counts(self.conn, self.customer["id"])
        lines = ["Hi again 👋 Send me screenshots of a chat and I'll make the receipt."]
        if counts["records"]:
            records = f"{counts['records']} saved record{'s' if counts['records'] != 1 else ''}"
            owed = f", and money is still owed on {counts['owed']} of them" if counts["owed"] else ""
            lines.append(f"You have {records}{owed}. Ask me anything about them, like *who still owes me?*")
        self.say("\n\n".join(lines))

    def ask_erase(self) -> None:
        self.send(button_message(ERASE_PROMPT, [(reply_id("erase", arg="yes"), "Delete everything"), (reply_id("erase", arg="no"), "Keep my data")]))

    def _repeated(self, text: str) -> bool:
        """The same request again within a few minutes, already answered: don't read it twice.

        A request that failed ("I couldn't work that out") can always be sent again.
        """
        if len(text.split()) < 2:
            return False
        lines = store.recent_memory(self.customer, within_minutes=3)
        mine = [index for index, line in enumerate(lines) if line["who"] == "You" and normalise(line["text"]) == normalise(text)]
        if not mine:
            return False
        answers = [line["text"] for line in lines[mine[-1] + 1 :] if line["who"] == "ChatReceipt"]
        failed = ("couldn't work that out", "not sure what you need", "didn't catch")
        return bool(answers) and not any(phrase in answer for answer in answers for phrase in failed)

    # -- requests in their own words (assistant.py) ----------------------------------------------

    def after_request(self, text: str, reading: AICorrection | None) -> None:
        """Answer a message read by the AI while no draft was open (or read without it, if it was down)."""
        if reading is None:
            quick = assistant.quick_request(text)
            if quick is not None:
                self.assist(quick)
            else:
                self.say(NOT_UNDERSTOOD_REQUEST)
            return
        intent = reading.intent
        reply = safe_reply(reading.reply, text)
        if intent in DRAFT_INTENTS:
            if intent == "read_again":
                self.read_again(store.open_submission(self.conn, self.customer["id"]))
            elif intent == "new_record":
                self.say(ASK_FOR_IMAGE)
            elif intent == "show_record":
                self.recent()
            else:
                self.say(NO_DRAFT_OPEN)
        elif intent == "unclear":
            self.say(reading.question or NOT_SURE)
        elif intent == "greeting":
            self.greet()
        elif intent == "thanks":
            self.say(reply or THANKS_REPLY)
        elif intent == "help":
            self.help()
        elif intent == "make_receipt":
            self.say("\n\n".join(part for part in (reply, ASK_FOR_IMAGE) if part))
        elif intent == "other":
            self.say(reply or NOT_SURE)
        else:
            self.assist(assistant.request_from(reading, text), lead=reply)

    def assist(self, request: assistant.Request, *, lead: str = "") -> None:
        """Do what a request asks, with every figure taken from the customer's own records."""
        intent = request.intent
        if intent == "greeting":
            self.greet()
        elif intent == "thanks":
            self.say(THANKS_REPLY)
        elif intent == "help":
            self.help()
        elif intent == "make_receipt":
            self.say(ASK_FOR_IMAGE)
        elif intent == "web_link":
            self.web_link()
        elif intent == "delete_data":
            self.ask_erase()
        elif intent == "list_records" and request == assistant.Request("list_records", format=request.format):
            self.recent()
        elif intent in ("list_records", "find_records"):
            self.find_records(request, lead)
        elif intent == "money_owed":
            self.money_owed(request, lead)
        elif intent == "totals":
            self.totals(request, lead)
        elif intent == "resend_receipt":
            self.resend(request, lead)
        else:
            self.say(NOT_SURE)

    def _search(self, request: assistant.Request, *, status: str | None = None, limit: int = 10) -> list[Row]:
        date_from, date_to = assistant.period_range(request.period, self.today)
        return store.search_transactions(
            self.conn, self.customer["id"], person=request.person, reference=request.reference, date_from=date_from,
            date_to=date_to, status=status or request.status, timezone=self.settings.whatsapp_timezone, limit=limit,
        )

    def _say_with(self, lead: str, body: str) -> None:
        self.say(f"{lead}\n\n{body}" if lead else body)

    def find_records(self, request: assistant.Request, lead: str = "") -> None:
        rows = self._search(request, limit=5)
        what = assistant.describe(request)
        if not rows:
            self.say(_tidy(NOTHING_FOUND.format(what=what or "like that")))
            return
        body = recent_text(rows).replace("*Your recent records*", _tidy(f"*Your records {what}*"))
        if lead:
            body = f"{lead}\n\n{body}"
        if len(rows) == 1:
            record = str(rows[0]["id"])
            self.send(button_message(body, [(reply_id("receipt", arg=record), "Send the receipt"), (reply_id("pdf", arg=record), "Send the PDF")]))
        else:
            self.say(body)

    def money_owed(self, request: assistant.Request, lead: str = "") -> None:
        rows = self._search(request, status="owed", limit=30)
        if not rows:
            self.say(_tidy(NOTHING_OWED.format(what=assistant.describe(request))))
            return
        self._say_with(lead, assistant.owed_text(rows, assistant.describe(request)))

    def totals(self, request: assistant.Request, lead: str = "") -> None:
        rows = self._search(request, limit=1000)
        if not rows:
            self.say(_tidy(NOTHING_FOUND.format(what=assistant.describe(request) or "yet")))
            return
        self._say_with(lead, assistant.totals_text(rows, assistant.describe(request)))

    def resend(self, request: assistant.Request, lead: str = "") -> None:
        rows = self._search(request, limit=10)
        what = assistant.describe(request)
        if not rows:
            self.say(_tidy(NOTHING_FOUND.format(what=what or "like that")))
        elif len(rows) == 1:
            if lead:
                self.say(lead)
            self.resend_one(str(rows[0]["id"]), request.format)
        else:
            action = "pdf" if request.format == "pdf" else "receipt"
            choices = [
                ChoiceRow(
                    id=reply_id(action, arg=str(row["id"])),
                    title=row["reference"],
                    description=" · ".join(part for part in (assistant.other_party(row, ""), money(row["total"], get_currency(row["currency"]))) if part),
                )
                for row in rows
            ]
            self.send(list_message(_tidy(WHICH_RECORD.format(count=len(rows), what=what)), "Choose a record", choices))

    def resend_one(self, transaction_id: str, fmt: str) -> None:
        """Send one of this customer's own receipts again, as an image, a PDF or both."""
        try:
            record = store.get_transaction(self.conn, self.customer["id"], UUID(transaction_id))
        except ValueError:
            record = None
        if record is None:
            self.say(RECORD_GONE)
            return
        if fmt in ("image", "both"):
            draft = TransactionDraft.model_validate(record["draft"])
            if not record.get("confirm_code"):
                # Saved before records had codes: it gets one now, so its receipt carries the link too.
                record = {**record, "confirm_code": store.ensure_confirm_code(self.conn, self.customer["id"], record["id"])}
            caption = receipt_caption(
                record["reference"], draft, confirm_link=self._confirm_link(record), other_name=other_side(draft, record.get("owner_role"))
            )
            self.send(receipt_request(str(record["id"]), "png", caption))
        if fmt in ("pdf", "both"):
            self.send_pdf(str(record["id"]))

    def welcome_back(self, submission: Row) -> None:
        """A greeting while a draft is open: say hello and let them pick up the draft or start over."""
        if submission["status"] != "awaiting_confirmation":
            self.say("Hi again 👋 " + _still_reading(submission))
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
        """Start reading: screenshots wait briefly for the rest of an album, a chat export starts at once.

        Only someone who hasn't saved a record yet is told "I'm reading it now". Everyone sees WhatsApp's
        typing indicator while it's read, and since October 2026 every reply costs money, so a customer
        who knows the routine isn't sent a message that only says what the indicator already shows.
        """
        export = next((entry for entry in images if entry.get("kind") == "chat_export"), None)
        if export is not None:
            store.create_submission(self.conn, self.customer["id"], [export], wait_seconds=0)
            if export.get("focus"):
                self.say(READING_DEAL.format(deal=export["focus"]))
            elif self._announce():
                self.say(EXPORT_ACKNOWLEDGE)
            return
        store.create_submission(self.conn, self.customer["id"], images, wait_seconds=self.settings.whatsapp_album_wait_seconds)
        if self._announce():
            self.say(ACKNOWLEDGE)

    def _announce(self) -> bool:
        """Whether to say "I'm reading it now": not on a first contact (its note says so), nor to a regular."""
        if self.first_contact:
            return False
        return not store.record_counts(self.conn, self.customer["id"])["records"]

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
        from_export = _has_export(submission["images"])
        if submission["status"] == "awaiting_confirmation":
            # Held screenshots never join a held chat export: the newer kind replaces the other.
            held = [] if _has_export(submission["held_images"]) else submission["held_images"]
            if self._repeats(image, [*submission["images"], *held]) or len(held) >= limit:
                return
            store.update_submission(self.conn, submission["id"], held_images=[*held, image])
            if not held:
                sid, revision = str(submission["id"]), submission["revision"]
                if from_export:
                    # A draft from a chat export is never mixed with screenshots.
                    buttons = [(reply_id("hold", sid, revision, "new"), "Start a new record"), (reply_id("hold", sid, revision, "drop"), "Keep this draft")]
                    self.send(button_message(SCREENSHOTS_HELD_PROMPT, buttons))
                    return
                warning = HELD_EDIT_WARNING if submission["edited_fields"] else ""
                buttons = [(reply_id("hold", sid, revision, "add"), "Add to this record"), (reply_id("hold", sid, revision, "new"), "Start a new record")]
                self.send(button_message(HELD_PROMPT + warning, buttons))
            return
        if from_export:
            self.say(READING_EXPORT)
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
        """The same file again: WhatsApp's media ID, or the same content (a forwarded copy has a new ID)."""
        return any(
            (entry.get("media_id") == image["media_id"] and not entry.get("focus"))
            or (image["sha256"] and entry.get("sha256") == image["sha256"])
            for entry in images
        )

    def confirm(self, submission: Row, *, without_date: bool = False, note: str = "") -> None:
        draft = draft_of(submission)
        computation = compute(draft)
        if not ready_to_save(draft, calculate(draft)) or computation.total is None:
            self.show_draft(submission, lead="This record can't be saved yet.")
            return
        if self._ask_about_parties(submission, draft):
            return
        if not draft.transaction_date.strip() and not without_date:
            self._ask_date(submission, note=note)
            return
        state = parties.state_of(submission["review"])
        owner_role = state.get("owner_role") or None
        # Money the chat says was paid to the customer is only a claim until they say it arrived.
        paid_to_them = owner_role == "seller" and computation.amount_paid is not None and computation.amount_paid > 0
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
            owner_role=owner_role,
            payment_check="claimed" if paid_to_them else None,
            payment_reference=str((submission["review"] or {}).get("payment_reference") or "") or None,
        )
        store.update_submission(self.conn, submission["id"], status="confirmed", pending_edit=None, held_images=[], instruction=None, dialogue=[])
        self._remember_counterpart(draft, state)
        log_event(
            "whatsapp.parties_saved", seller=bool(draft.seller.strip()), buyer=bool(draft.buyer.strip()),
            owner_role=state.get("owner_role") or "unknown", saved_name_used=bool(state.get("you")),
            corrected=any(path in submission["edited_fields"] for path in parties.ROLES),
        )
        other = other_side(draft, owner_role)
        link = self._confirm_link(record)
        self.send(receipt_request(str(record["id"]), "png", receipt_caption(record["reference"], draft, confirm_link=link, other_name=other)))
        saved = saved_text(record["reference"], other_name=other, confirm_link=bool(link))
        body = f"{note}\n\n{saved}" if note else saved
        record_id = str(record["id"])
        if record.get("payment_check") == "claimed":
            # The question rides on the "Saved" message: one message fewer than asking separately.
            body = f"{body}\n\n{payment_question(money(computation.amount_paid, get_currency(draft.currency)), other)}"
            buttons = [
                (reply_id("pchk", arg=f"{record_id}:received"), "Yes, it's arrived"),
                (reply_id("pchk", arg=f"{record_id}:not_received"), "Not yet"),
                (reply_id("pdf", arg=record_id), "Get PDF"),
            ]
        else:
            buttons = [(reply_id("pdf", arg=record_id), "Get PDF"), (reply_id("menu", arg="scan"), "Scan another"), (reply_id("menu", arg="recent"), "Recent records")]
        self.send(button_message(body, buttons))
        self._start_held(submission)

    def _confirm_link(self, record: Row) -> str:
        """The wa.me link on a receipt that lets the other person confirm it, when ChatReceipt's number is set."""
        number, code = self.settings.whatsapp_link_number, record.get("confirm_code") or ""
        return f"https://wa.me/{number}?text=CONFIRM%20{code}" if number and code else ""

    # -- payment checks ------------------------------------------------------------------------

    def payment_answer(self, arg: str) -> None:
        """The customer's answer to "Has it reached your account?" on one of their own records."""
        record_id, _, state = arg.partition(":")
        try:
            transaction_id = UUID(record_id)
        except ValueError:
            self.say(RECORD_GONE)
            return
        if state not in ("received", "not_received"):
            self.say(RECORD_GONE)
            return
        record = store.set_payment_check(self.conn, self.customer["id"], transaction_id, state)
        if record is None:
            exists = store.get_transaction(self.conn, self.customer["id"], transaction_id) is not None
            self.say(NOTHING_TO_CHECK if exists else RECORD_GONE)
            return
        amount = money(record["amount_paid"], get_currency(record["currency"]))
        if state == "received":
            self.say(PAYMENT_RECEIVED.format(amount=amount, reference=record["reference"]))
        else:
            self.send(button_message(PAYMENT_MISSING.format(amount=amount, reference=record["reference"]), [(reply_id("pchk", arg=f"{record_id}:received"), "It's arrived")]))
        log_event("whatsapp.payment_checked", state=state)

    # -- the other person's confirmation (the link on a receipt) ---------------------------------

    def on_confirm_code(self, code: str) -> None:
        """Someone opened a receipt's confirmation link: show them that record, and only that one."""
        record = store.record_by_code(self.conn, code)
        if record is None:
            self.say(CODE_NOT_FOUND)
            return
        if record["customer_id"] == self.customer["id"]:
            self.say(OWN_RECORD.format(reference=record["reference"]))
            return
        self.send(
            button_message(
                responder_view(record, owner_name(record) or "Someone", first_contact=self.first_contact),
                [(reply_id("ack", arg=f"{code}:agreed"), "Yes, it's right"), (reply_id("ack", arg=f"{code}:disputed"), "Something's wrong")],
            )
        )
        log_event("whatsapp.record_opened", first_contact=self.first_contact)

    def answer_record(self, code: str, response: str) -> None:
        record = store.record_by_code(self.conn, code)
        if record is None:
            self.say(CODE_NOT_FOUND)
            return
        if record["customer_id"] == self.customer["id"]:
            self.say(OWN_RECORD.format(reference=record["reference"]))
            return
        reference = record["reference"]
        if response != "agreed":
            until = (datetime.now(UTC) + DISPUTE_WAIT).isoformat()
            store.set_awaiting(self.conn, self.customer["id"], {"kind": "dispute", "code": code, "until": until})
            self.say(WHATS_WRONG.format(reference=reference))
            return
        if (self.customer.get("awaiting") or {}).get("kind") == "dispute":
            # They tapped "Something's wrong" first and changed their mind: their next message is an ordinary one.
            store.set_awaiting(self.conn, self.customer["id"], None)
        latest = store.latest_response(self.conn, record["id"], responder_id=self.customer["id"])
        if latest is not None and latest["response"] == "agreed":
            self.say(ALREADY_CONFIRMED.format(reference=reference))
            return
        store.add_response(self.conn, record["id"], self.customer, "agreed")
        self.say(THANKS_CONFIRMED.format(owner=owner_name(record) or "them", reference=reference))
        self._tell_owner(record, AGREED_NOTICE.format(who=responder_label(self.customer), reference=reference))
        log_event("whatsapp.record_answered", response="agreed")

    def _dispute_note(self, text: str) -> bool:
        """The message after "Something's wrong": what the person says is wrong, passed to the record's owner."""
        awaiting = self.customer.get("awaiting") or {}
        if awaiting.get("kind") != "dispute":
            return False
        store.set_awaiting(self.conn, self.customer["id"], None)
        try:
            expired = datetime.fromisoformat(str(awaiting.get("until"))) < datetime.now(UTC)
        except ValueError:
            expired = True
        if expired:
            return False
        if normalise(text) in _BACK | {"stop", "never mind", "nevermind", "nothing"}:
            self.say("Okay, I haven't sent anything.")
            return True
        if command_of(text):
            return False  # a command is handled as usual
        record = store.record_by_code(self.conn, str(awaiting.get("code") or ""))
        if record is None or record["customer_id"] == self.customer["id"]:
            return False
        note = " ".join(text.split())[:500]
        store.add_response(self.conn, record["id"], self.customer, "disputed", note=note)
        self.say(THANKS_DISPUTED.format(owner=owner_name(record) or "them"))
        self._tell_owner(
            record, DISPUTED_NOTICE.format(who=responder_label(self.customer), reference=record["reference"], note=note[:300])
        )
        log_event("whatsapp.record_answered", response="disputed")
        return True

    def _tell_owner(self, record: Row, text: str) -> None:
        """A message to the person who saved the record. Without an open chat window WhatsApp may not deliver it;
        the answer is on their record either way."""
        owner = store.customer_contact(self.conn, record["customer_id"])
        if owner is not None:
            store.enqueue(self.conn, owner, text_message(text), f"{self.key}:owner")

    def _ask_date(self, submission: Row, *, note: str = "") -> None:
        """One question with one-tap answers, instead of saving a receipt with no date."""
        sid, revision, today = str(submission["id"]), submission["revision"], self.today
        yesterday = today - timedelta(days=1)
        store.update_submission(self.conn, submission["id"], pending_edit={"target": "transaction_date", "then": [], "save_after": True})
        self.send(
            button_message(
                f"{note}\n\n{ASK_DATE}" if note else ASK_DATE,
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

    # -- who is who (parties.py) ----------------------------------------------------------------

    def _remember_counterpart(self, draft: TransactionDraft, state: dict[str, Any]) -> None:
        """The other person's confirmed name, so their next receipt needs no correction."""
        remembered = parties.counterpart_to_remember(draft, state)
        if remembered is None:
            return
        try:
            counterparty_id = UUID(remembered["counterparty_id"]) if remembered["counterparty_id"] else None
        except ValueError:
            counterparty_id = None
        store.remember_counterparty(
            self.conn, self.customer["id"], name=remembered["name"], phone=remembered["phone"], label=remembered["label"],
            role=remembered["role"], counterparty_id=counterparty_id, username=remembered.get("username") or "",
        )

    def _ask_about_parties(self, submission: Row, draft: TransactionDraft) -> bool:
        """Before saving: ask once who the customer is, or their name, when their side would be blank."""
        review = dict(submission["review"] or {})
        state = parties.state_of(review)
        question = parties.question_needed(
            draft, state, saved_name=self.customer.get("party_name"), skipped=self.customer.get("name_prompts_skipped") or 0
        )
        if question == "role":
            self._ask_role(submission, review, state)
        elif question == "name":
            self._ask_name(submission, review, state, state["owner_role"])
        return bool(question)

    def _ask_role(self, submission: Row, review: dict[str, Any], state: dict[str, Any]) -> None:
        sid, revision = str(submission["id"]), submission["revision"]
        store.update_submission(self.conn, submission["id"], review={**review, "parties": {**state, "asked": "role"}})
        order = parties.role_order(self.customer.get("usual_role"))
        buttons = [(reply_id("role", sid, revision, role), parties.ROLE_LABELS[role]) for role in order]
        buttons.append((reply_id("role", sid, revision, "neither"), "Neither"))
        self.send(button_message(parties.role_question(state.get("other_party") or ""), buttons))
        log_event("whatsapp.party_question", kind="role", usual_role=bool(self.customer.get("usual_role")))

    def _role_answer(self, submission: Row, draft: TransactionDraft, role: str) -> None:
        """The customer said which side they're on: names go where they belong, then saving carries on if it was asked."""
        review = dict(submission["review"] or {})
        state = parties.state_of(review)
        asked = state.get("asked") == "role"
        log_event("whatsapp.party_answer", kind="role", choice=role, asked=asked)
        if role == "neither":
            updated = store.update_submission(self.conn, submission["id"], review={**review, "parties": {**state, "owner_role": "", "neither": True}})
            if asked:
                self.confirm(updated)
            else:
                self.ask(updated, "Okay, you're neither side in this deal.")
            return
        store.set_usual_role(self.conn, self.customer["id"], role)
        changed, state, moved = parties.apply_role(draft, state, role)
        saved_name = self.customer.get("party_name")
        if saved_name and not getattr(changed, role).strip():
            changed, state = changed.model_copy(update={role: saved_name}), {**state, "you": role}
        notes = ["Seller and buyer swapped"] if moved else [f"You're the {role}"]
        updated = self._save(submission, changed, moved, notes, explicit_status=False, review={**review, "parties": state})
        if not asked:
            self.show_update(updated, notes)
        elif not getattr(changed, role).strip() and (self.customer.get("name_prompts_skipped") or 0) < parties.MAX_SKIPS:
            self._ask_name(updated, {**review, "parties": state}, state, role)
        else:
            self.confirm(updated)

    def _ask_name(self, submission: Row, review: dict[str, Any], state: dict[str, Any], role: str) -> None:
        sid, revision = str(submission["id"]), submission["revision"]
        store.update_submission(
            self.conn, submission["id"], review={**review, "parties": {**state, "asked": "name"}},
            pending_edit={"target": role, "then": [], "own_name": True},
        )
        suggested = parties.suggested_name(self.customer.get("whatsapp_name"), state)
        buttons = [(reply_id("name", sid, revision, "profile"), parties.button_title(suggested))] if suggested else []
        buttons.append((reply_id("name", sid, revision, "skip"), "Not now"))
        self.send(button_message(parties.name_question(role, suggested=suggested), buttons))
        log_event("whatsapp.party_question", kind="name", suggested=bool(suggested))

    def _name_answer(self, submission: Row, draft: TransactionDraft, choice: str) -> None:
        state = parties.state_of(submission["review"])
        role = state.get("owner_role") or ""
        suggested = parties.suggested_name(self.customer.get("whatsapp_name"), state)
        if choice != "profile" or role not in parties.ROLES or not suggested:
            self._skip_own_name(submission)
            return
        self._use_own_name(submission, draft, role, suggested, choice="profile")

    def _skip_own_name(self, submission: Row) -> None:
        store.skip_name_prompt(self.conn, self.customer["id"])
        log_event("whatsapp.party_answer", kind="name", choice="skip")
        self.confirm(store.update_submission(self.conn, submission["id"], pending_edit=None))

    def _use_own_name(self, submission: Row, draft: TransactionDraft, role: str, name: str, *, choice: str) -> None:
        """Their name goes on their side of this receipt, and is remembered for the next ones."""
        store.set_party_name(self.conn, self.customer["id"], name)
        log_event("whatsapp.party_answer", kind="name", choice=choice)
        review = dict(submission["review"] or {})
        state = {**parties.state_of(review), "you": role}
        updated = self._save(submission, draft.model_copy(update={role: name}), [], [], explicit_status=False, review={**review, "parties": state}, pending_edit=None)
        self.confirm(updated, note=parties.NAME_REMEMBERED.format(name=name))

    def _typed_own_name(self, submission: Row, draft: TransactionDraft, text: str) -> None:
        """A typed answer to "What name should go on your receipts?"; anything else is handled as usual."""
        if normalise(text) in parties.SKIP_WORDS:
            self._skip_own_name(submission)
            return
        cleared = store.update_submission(self.conn, submission["id"], pending_edit=None)
        if (role := parties.role_in(text)) is not None:
            self._role_answer(cleared, draft, role)
            return
        name = parties.answered_name(text)
        if not name or quick_changes(text) is not None or assistant.quick_request(text, has_draft=True) is not None:
            # A correction or a question, not a name: drop the question and handle it as usual.
            self._on_draft_text(cleared, text)
            return
        role = parties.state_of(submission["review"]).get("owner_role") or str(submission["pending_edit"]["target"])
        self._use_own_name(cleared, draft, role, clean_name(name), choice="typed")

    def set_own_name(self, name: str, submission: Row | None) -> None:
        """"My name is …": remembered for receipts, and put on the open draft if it's clear which side is theirs."""
        store.set_party_name(self.conn, self.customer["id"], name)
        log_event("whatsapp.party_answer", kind="name", choice="stated")
        if submission is not None:
            review = dict(submission["review"] or {})
            state = parties.state_of(review)
            role = state.get("owner_role") or ""
            draft = draft_of(submission)
            if role in parties.ROLES and (not getattr(draft, role).strip() or state.get("you") == role):
                note = f"{role.capitalize()}: {name}"
                updated = self._save(submission, draft.model_copy(update={role: name}), [], [note], explicit_status=False, review={**review, "parties": {**state, "you": role}})
                self.show_update(updated, [note, "I'll use this name on your next receipts too."])
                return
        self.say(parties.NAME_SET.format(name=name))

    # -- other deals in the same chat export ----------------------------------------------------

    def other_deal(self, number: int, submission: Row | None) -> None:
        """"deal 2": read the same chat export again for another deal it holds."""
        open_deals = submission is not None and bool((submission.get("review") or {}).get("other_deals"))
        if open_deals and not _has_export(submission["images"]):  # type: ignore[index]
            self.say(DEAL_FROM_SCREENSHOTS)
            return
        source = submission if open_deals else store.latest_export_submission(self.conn, self.customer["id"])
        deals = ((source or {}).get("review") or {}).get("other_deals") or []
        export = next((image for image in (source or {}).get("images") or [] if image.get("kind") == "chat_export"), None)
        if export is None or not 1 <= number <= len(deals):
            self.say(NO_SUCH_DEAL.format(number=number))
            return
        deal = deals[number - 1]
        # The list from the first reading travels with the export, so the numbers don't change between readings.
        entry = {
            **export, "focus": deal, "deals": deals, "message_id": self.message_id,
            "sha256": _focused_hash(str(export.get("original_sha256") or export.get("sha256") or ""), deal),
            "original_sha256": str(export.get("original_sha256") or export.get("sha256") or ""),
        }
        if submission is not None and submission["status"] == "awaiting_confirmation":
            sid, revision = str(submission["id"]), submission["revision"]
            store.update_submission(self.conn, submission["id"], held_images=[entry])
            self.send(button_message(EXPORT_HELD_PROMPT, [(reply_id("hold", sid, revision, "new"), "Start a new record"), (reply_id("hold", sid, revision, "drop"), "Keep this draft")]))
        elif submission is not None:
            self.say(EXPORT_BUSY)
        else:
            self.start_submission([entry])

    def on_document(self, payload: dict[str, str], message_id: str) -> None:
        """A WhatsApp chat export: read on its own, never mixed into a draft from screenshots."""
        export = {
            "message_id": message_id, "media_id": payload.get("media_id", ""), "mime_type": payload.get("mime_type", ""),
            "sha256": payload.get("sha256", ""), "filename": payload.get("filename", ""), "kind": "chat_export",
        }
        submission = store.open_submission(self.conn, self.customer["id"])
        if submission is None:
            self.start_submission([export])
            return
        if self._repeats(export, [*submission["images"], *submission["held_images"]]):
            self.say(ALREADY_DONE)
            return
        if submission["status"] == "awaiting_confirmation":
            sid, revision = str(submission["id"]), submission["revision"]
            store.update_submission(self.conn, submission["id"], held_images=[export])
            self.send(button_message(EXPORT_HELD_PROMPT, [(reply_id("hold", sid, revision, "new"), "Start a new record"), (reply_id("hold", sid, revision, "drop"), "Keep this draft")]))
            return
        self.say(EXPORT_BUSY)

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
            self.say(_still_reading(submission))
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
            self.say(READING_EXPORT_AGAIN if _has_export(failed["images"]) else READING_AGAIN)
            return
        if submission["status"] != "awaiting_confirmation":
            self.say(_still_reading(submission))
            return
        if submission["edited_fields"]:
            sid, revision = str(submission["id"]), submission["revision"]
            self.send(button_message(REREAD_EXPORT_PROMPT if _has_export(submission["images"]) else REREAD_PROMPT, [(reply_id("reread", sid, revision, "yes"), "Read it again"), (reply_id("reread", sid, revision, "no"), "Keep my draft")]))
            return
        self._reread(submission)

    def _reread(self, submission: Row) -> None:
        store.restart_collection(self.conn, submission["id"], submission["images"], wait_seconds=0, revision=submission["revision"] + 1)
        self.say(READING_EXPORT_AGAIN if _has_export(submission["images"]) else READING_AGAIN)

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
            instruction={
                "text": text[:1000], "message_id": self.message_id, "revision": submission["revision"], "target": target,
                "earlier": getattr(self, "earlier", []),
            },
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
        if self.understanding and not quick and self._repeated(text):
            # The same message again: don't pay to read it twice, or add an item twice.
            self.show_draft(submission, lead=ALREADY_DONE)
        elif self.understanding and not quick:
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
        # The conversation before this message, for reading it in context; then this message joins it.
        self.earlier = [f"{line['who']}: {line['text']}" for line in store.recent_memory(self.customer)]
        store.remember(self.conn, self.customer["id"], "You", text)
        if self._dispute_note(text):
            return
        submission = store.open_submission(self.conn, self.customer["id"])
        command = command_of(text)
        awaiting = submission is not None and submission["status"] == "awaiting_confirmation"
        if awaiting and submission["pending_edit"] and command not in _ESCAPES:  # type: ignore[index]
            if normalise(text) in _BACK:
                updated = store.update_submission(self.conn, submission["id"], pending_edit=None)  # type: ignore[index]
                self.show_draft(updated)
            elif submission["pending_edit"].get("own_name"):  # type: ignore[index]
                self._typed_own_name(submission, draft_of(submission), text)  # type: ignore[arg-type]
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
            self.help()
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
            self.ask_erase()
        elif command == "clear":
            self.ask_clear()
        elif command == "cancel":
            if submission is None:
                self.say(NOTHING_OPEN)
            else:
                self.cancel(submission)
        elif (own := parties.stated_name(text)) is not None:
            self.set_own_name(own, submission if awaiting else None)
        elif (deal := _DEAL.match(normalise(text))) is not None:
            self.other_deal(int(deal[1]), submission)
        elif submission is None:
            if command == "greeting":
                self.greet()
            elif (request := assistant.quick_request(text)) is not None:
                self.assist(request)
            elif not self.understanding:
                self.say(NOT_SURE)
            elif self._repeated(text):
                self.say(ALREADY_DONE)
            else:
                # Read by the AI outside this transaction; answered through finish_request.
                store.queue_request(self.conn, self.customer["id"], text, self.message_id, has_draft=False, earlier=self.earlier)
        elif submission["status"] != "awaiting_confirmation":
            self.say(_still_reading(submission))
        elif self._answer_in_context(submission, text):
            pass
        elif command == "confirm":
            self.confirm(submission)
        elif command == "correct":
            self._open_corrections(submission)
        elif command == "show":
            self.show_draft(submission)
        elif (role := parties.role_in(text)) is not None:
            self._role_answer(submission, draft_of(submission), role)
        elif (request := assistant.quick_request(text, has_draft=True)) is not None:
            self.assist(request)
        else:
            self._on_draft_text(submission, text)

    def _answer_in_context(self, submission: Row, text: str) -> bool:
        """A short "yes", "correct" or "no", read as the answer to the question the draft just asked.

        To "Is this correct?" a yes confirms and a no opens the corrections. To a proposed fix ("So the
        full price was ₦60,000?") a yes accepts it and a no keeps the figures. Anything else is left to
        the usual handling, so "correct" still opens the corrections when no question is waiting.
        """
        plain = normalise(text)
        if plain not in _YES and plain not in _NO:
            return False
        review = dict(submission["review"] or {})
        state = parties.state_of(review)
        if state.get("asked") == "role" and not state.get("owner_role") and not state.get("neither"):
            # "Yes" doesn't answer "Who are you in this chat?": ask again rather than save without an answer.
            self._ask_role(submission, review, state)
            return True
        draft = draft_of(submission)
        question, buttons = next_step(draft, calculate(draft), submission_id=str(submission["id"]), revision=submission["revision"])
        actions = {parsed.action for parsed in (parse_reply_id(button_id) for button_id, _ in buttons) if parsed}
        yes = plain in _YES
        if question == CONFIRM_QUESTION:
            if yes:
                self.confirm(submission)
            else:
                self.say(WHAT_TO_CHANGE)
                self._open_corrections(submission)
            return True
        if "settle" in actions:
            has_accept = any((parse_reply_id(button_id) or ReplyId("")).arg == "yes" for button_id, _ in buttons)
            if yes and not has_accept:
                return False
            self._settle(submission, draft, "yes" if yes else "keep")
            return True
        return False

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
        reply = safe_reply(correction.reply, text)
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
            self.help()
        elif intent == "greeting":
            self.welcome_back(submission)
        elif intent == "thanks":
            self.say(reply or THANKS_REPLY)
        elif intent == "make_receipt":
            self.say(ASK_FOR_IMAGE)
        elif intent == "other":
            self.say(reply or NOT_SURE)
        elif intent in assistant.RECORD_INTENTS:
            # A question about saved records in the middle of a draft: answered, and the draft stays open.
            self.assist(assistant.request_from(correction, text), lead=reply)
        else:
            self._apply_correction(submission, correction, lead=reply)

    def _apply_correction(self, submission: Row, correction: AICorrection, *, lead: str = "") -> None:
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
        self.show_update(updated, notes, applied.problems, lead=f"✅ {lead}" if lead else "✅ *Updated*")

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
        elif reply.action == "receipt":
            self.resend_one(reply.arg, "image")
        elif reply.action == "clear":
            self._on_clear(reply.arg)
        elif reply.action == "pchk":
            self.payment_answer(reply.arg)
        elif reply.action == "ack":
            code, _, response = reply.arg.partition(":")
            self.answer_record(code.upper(), "agreed" if response == "agreed" else "disputed")
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
            self.help()
        elif choice in ("draft", "restart"):
            submission = store.open_submission(self.conn, self.customer["id"])
            if submission is None:
                self.say(NOTHING_OPEN)
            elif choice == "restart":
                self.start_again(submission)
            elif submission["status"] != "awaiting_confirmation":
                self.say(_still_reading(submission))
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
            self.say(_still_reading(submission))
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
        elif action == "role" and arg in (*parties.ROLES, "neither"):
            self._role_answer(submission, draft, arg)
        elif action == "name":
            self._name_answer(submission, draft, arg)
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
        elif choice == "drop":
            updated = store.update_submission(self.conn, submission["id"], held_images=[])
            self.ask(updated, KEEPING_DRAFT)
        elif choice == "add" and _has_export([*submission["images"], *held]):
            # An old "Add to this record" button, from before a chat export took the screenshots' place.
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
        if kind in _READ_BY_AI:
            # Screenshots and chat exports each cost an AI reading, so they share the same limits.
            hour = store.inbound_count(self.conn, customer_id, seconds=3600, kinds=_READ_BY_AI)
            day = store.inbound_count(self.conn, customer_id, seconds=86_400, kinds=_READ_BY_AI)
            hour_limit, day_limit = self.settings.whatsapp_images_per_hour, self.settings.whatsapp_images_per_day
            if hour > hour_limit or day > day_limit:
                if hour == hour_limit + 1 or day == day_limit + 1:
                    self.say(IMAGE_LIMIT)
                return True
        return False


def _has_export(images: Sequence[dict[str, Any]]) -> bool:
    return any(entry.get("kind") == "chat_export" for entry in images)


def _still_reading(submission: Row) -> str:
    """"Still reading", naming what is being read: the screenshots or the chat export."""
    return STILL_READING_EXPORT if _has_export(submission["images"]) else STILL_READING


def _focused_hash(sha256: str, deal: str) -> str:
    """A fingerprint for one deal in an export: a record of another deal isn't reported as a duplicate."""
    return hashlib.sha256(f"{sha256}|{' '.join(deal.split()).casefold()}".encode("utf-8")).hexdigest()


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
    turn.first_contact = first_contact
    if first_contact:
        store.mark_welcomed(conn, customer["id"])
    code = confirm_code_in(payload.get("text", "")) if kind == "text" else ""
    if code:
        # Someone opening a receipt's confirmation link, often their first message to ChatReceipt.
        turn.on_confirm_code(code)
    elif kind == "image":
        if first_contact:
            turn.say(FIRST_IMAGE_NOTE)
        turn.on_image(payload, row["message_id"])
    elif kind == "document":
        if first_contact:
            turn.say(FIRST_EXPORT_NOTE)
        turn.on_document(payload, row["message_id"])
    elif first_contact:
        turn.welcome()
        text = payload.get("text", "") if kind == "text" else ""
        if text and command_of(text) != "greeting":
            turn.on_text(text)
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


def _tidy(text: str) -> str:
    """Tidy a message built from optional parts: no double spaces, no space before a full stop."""
    return " ".join(text.split(" ")).replace("  ", " ").replace(" .", ".").replace(" *", "*")


def _gist(payload: dict[str, Any]) -> str:
    """What a message said, in one line, for the conversation memory."""
    kind = str(payload.get("type") or "")
    section = payload.get(kind) if isinstance(payload.get(kind), dict) else {}
    if kind == "text":
        body = section.get("body", "")
    elif kind == "interactive":
        body = section.get("body", {}).get("text", "")
    else:
        body = section.get("caption", "") or str(payload.get("caption") or "") or "(sent a receipt)"
    return " ".join(str(body).replace("*", "").replace("_", "").split())[:200]


def finish_request(conn: Connection, settings: Settings, claimed: Row, reading: AICorrection | None) -> None:
    """Answer a free-text request the AI has read, if this worker still holds it."""
    customer = store.lock_claimed_request(conn, claimed["id"], claimed["request_lease_until"])
    if customer is None:
        return
    pending = dict(claimed["request"] or {})
    message_id = str(pending.get("message_id") or "")
    key = f"req:{message_id or claimed['request_lease_until'].isoformat()}"
    Turn(conn, settings, customer, key, message_id=message_id).after_request(str(pending.get("text") or ""), reading)
