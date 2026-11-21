"""Who is who on a WhatsApp receipt: the customer's own side, and the person they dealt with.

A chat never shows its owner's name, so without help the customer's side of every receipt is
blank. The extraction reports which side the phone's owner is on (``Parties.owner_role``) and the
name at the top of the chat (``other_party``); this module decides what to fill and what to ask.

- The customer's own name is asked once, when they first confirm a receipt, and remembered.
- Their role is asked only when the screenshots don't show it, with their usual role first.
- Nothing is asked when both names are known, and asking stops after they skip it twice.

The rules work on the draft and the ``parties`` entry of a submission's review, so they can be
tested without the conversation around them.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from app.models import TransactionDraft
from app.names import clean_name, phone_in_label, same_name

ROLES = ("seller", "buyer")
ROLE_LABELS = {"seller": "I'm the seller", "buyer": "I'm the buyer"}
# After two "Not now" answers ChatReceipt stops asking; "my name is …" still sets the name.
MAX_SKIPS = 2
BUTTON_TITLE_LIMIT = 20

NAME_HELP = "Please type just the name for your receipts, like *Ada's Bags*, or tap *Not now*."
NAME_SET = "Saved. Your receipts will show you as *{name}*."
NAME_REMEMBERED = "Your receipts will show you as *{name}* from now on. Send *my name is …* to change it."

SKIP_WORDS = frozenset({"not now", "skip", "no", "nope", "later", "none", "no name", "leave it", "leave it blank", "blank", "nothing"})
_ROLE_WORDS = {
    "seller": frozenset({
        "seller", "the seller", "i m the seller", "im the seller", "i am the seller", "i m selling", "i am selling",
        "im selling", "i sold", "i sold it", "i sell", "me seller", "i be the seller", "na me be the seller",
    }),
    "buyer": frozenset({
        "buyer", "the buyer", "i m the buyer", "im the buyer", "i am the buyer", "i m buying", "i am buying",
        "im buying", "i bought", "i bought it", "i buy", "me buyer", "i be the buyer", "na me be the buyer",
    }),
    "neither": frozenset({"neither", "none of them", "neither of them", "i m neither", "im neither", "not me"}),
}
_STATED_NAME = re.compile(
    r"^\s*(?:my (?:business |shop |store |brand |company )?name is|set my name to|change my name to|"
    r"use (?P<inner>.+?) as my name|put (?P<inner2>.+?) as my name)\s*(?P<name>.*?)\s*[.!]*\s*$",
    re.IGNORECASE,
)
# "My name is not on the receipt" is a complaint, not a name.
_NOT_A_NAME_START = frozenset({"not", "wrong", "missing", "correct", "incorrect", "showing", "spelt", "spelled", "written", "still", "blank", "empty"})


def _plain(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.casefold()).split())


def opposite(role: str) -> str:
    return "buyer" if role == "seller" else "seller"


def state_of(review: Mapping[str, Any] | None) -> dict[str, Any]:
    """The ``parties`` entry of a submission's review: owner_role, other_party, you, asked, neither."""
    return dict((review or {}).get("parties") or {})


def looks_like_name(name: str) -> bool:
    """A short name with a letter in it, not a sentence or a question."""
    return 0 < len(name) <= 120 and any(character.isalpha() for character in name) and len(name.split()) <= 6 and "?" not in name


def stated_name(text: str) -> str | None:
    """The name in "my name is …", "call me …" or "use … as my name", if the message is one of those."""
    found = _STATED_NAME.match(text)
    if found is None:
        return None
    name = clean_name(found.group("inner") or found.group("inner2") or found.group("name") or "")
    first = _plain(name).split(" ", 1)[0] if name else ""
    return name if looks_like_name(name) and first not in _NOT_A_NAME_START else None


_LEAD_IN = re.compile(r"^\s*(?:it'?s|it is|its|i'?m|i am|im|this is|na|name is|use|put)\s+", re.IGNORECASE)


def answered_name(text: str) -> str:
    """The name in a typed answer to "What name should go on your receipts?", or "" if it isn't one.

    "my name is …", "it's …" and "I'm …" lead-ins are dropped, so only the name is saved.
    """
    straightened = text.replace("’", "'")
    stated = stated_name(straightened)
    if stated:
        return stated
    name = clean_name(_LEAD_IN.sub("", straightened, count=1))
    return name if looks_like_name(name) else ""


def role_in(text: str) -> str | None:
    """"seller", "buyer" or "neither" when a typed message answers "Who are you in this chat?"."""
    plain = _plain(text)
    return next((role for role, words in _ROLE_WORDS.items() if plain in words), None)


def fill_saved_name(
    draft: TransactionDraft, state: dict[str, Any], saved_name: str | None, *, owner_label: str = ""
) -> tuple[TransactionDraft, dict[str, Any]]:
    """When a chat is read: the customer's confirmed name on their side, if the chat shows which side that is.

    Their side is filled when it's empty, or when it holds only their own label from a chat export
    (their WhatsApp name as their phone shows it), which their confirmed receipt name replaces.
    """
    role = state.get("owner_role") or ""
    if role not in ROLES or not saved_name:
        return draft, state
    current = getattr(draft, role).strip()
    if not current or (owner_label and same_name(current, owner_label)):
        return draft.model_copy(update={role: saved_name}), {**state, "you": role}
    if same_name(current, saved_name):
        return draft, {**state, "you": role}
    return draft, state


def recall_counterparty(
    draft: TransactionDraft, state: dict[str, Any], found: tuple[Mapping[str, Any], str] | None
) -> tuple[TransactionDraft, dict[str, Any]]:
    """The other side's name as the customer confirmed it on an earlier receipt.

    A phone-number match is reliable, so the confirmed name is used; if the chat gave them another
    name, that name is kept in the state for a check. A label match (the name at the top of a chat,
    an export's sender label) is only a suggestion: it replaces the label itself, never a name the
    conversation gave some other way, which is kept with a check instead. Every name filled from an
    earlier receipt is marked on the draft.
    """
    role = state.get("owner_role") or ""
    if role not in ROLES or found is None:
        return draft, state
    row, how = found
    theirs = opposite(role)
    current, label = getattr(draft, theirs).strip(), state.get("other_party") or ""
    remembered = {"id": str(row["id"]), "match": how, "name": row["name"]}
    if same_name(current, row["name"]):
        return draft, {**state, "counterpart": remembered}
    if not current or same_name(current, label) or how == "phone":
        return draft.model_copy(update={theirs: row["name"]}), {**state, "counterpart": {**remembered, "was": current}}
    # Only the label matched, and the conversation names them differently: keep what it shows.
    return draft, {**state, "known_as": remembered}


def identity_checks(state: Mapping[str, Any]) -> list[dict[str, str]]:
    """Review notes for names that disagree with the customer's earlier receipts. Nothing is overwritten silently."""
    role = state.get("owner_role") or ""
    if role not in ROLES:
        return []
    theirs, label = opposite(role), state.get("other_party") or ""
    counterpart, known = state.get("counterpart") or {}, state.get("known_as") or {}
    was = str(counterpart.get("was") or "")
    if counterpart and was and not same_name(was, label):
        return [{
            "field": theirs,
            "message": f"Your earlier receipts name this {theirs} “{counterpart['name']}”, but the chat says “{was}”. Change it if that's wrong.",
        }]
    if known:
        return [{
            "field": theirs,
            "message": f"Your earlier receipts name someone shown as “{label}” “{known['name']}”. If it's the same person, say “the {theirs} is {known['name']}”.",
        }]
    return []


def counterpart_to_remember(draft: TransactionDraft, state: Mapping[str, Any]) -> dict[str, Any] | None:
    """After saving: the other person's confirmed name and how to recognise them next time."""
    role = state.get("owner_role") or ""
    if role not in ROLES:
        return None
    theirs = opposite(role)
    name = getattr(draft, theirs).strip()
    if not name:
        return None
    counterpart = state.get("counterpart") or {}
    return {
        "name": name, "phone": state.get("other_phone") or "", "label": state.get("other_party") or "", "role": theirs,
        "counterparty_id": counterpart.get("id"),
    }


def marker(state: Mapping[str, Any], side: str, edited: bool) -> str:
    """Where a name on the draft came from, when ChatReceipt filled it rather than the chat."""
    if edited:
        return ""
    if state.get("you") == side:
        return " _(you)_"
    counterpart = state.get("counterpart") or {}
    if counterpart and counterpart.get("was") is not None and opposite(state.get("owner_role") or "") == side:
        return " _(from your earlier receipts)_"
    return ""


def apply_role(draft: TransactionDraft, state: dict[str, Any], role: str) -> tuple[TransactionDraft, dict[str, Any], list[str]]:
    """The customer said which side they're on: the name at the top of the chat goes on the other side.

    Returns the draft, the new state, and the fields that moved because the names had been read the
    wrong way round, which count as the customer's corrections.
    """
    other = state.get("other_party") or ""
    theirs = opposite(role)
    moved: list[str] = []
    if other and same_name(getattr(draft, role), other):
        # The other person's name was on the customer's side: the two sides were swapped.
        draft = draft.model_copy(update={role: getattr(draft, theirs), theirs: getattr(draft, role)})
        moved = [role, theirs]
    elif other and not getattr(draft, theirs).strip():
        draft = draft.model_copy(update={theirs: other})
    return draft, {**state, "owner_role": role}, moved


def question_needed(draft: TransactionDraft, state: Mapping[str, Any], *, saved_name: str | None, skipped: int) -> str:
    """What to ask before saving: "role", "name" or "" for nothing.

    Asked at most once per receipt, and only when a name is missing that the answer can fill.
    """
    if state.get("asked") or state.get("neither"):
        return ""
    if all(getattr(draft, side).strip() for side in ROLES):
        return ""
    can_name = bool(saved_name) or skipped < MAX_SKIPS
    role = state.get("owner_role") or ""
    if role not in ROLES:
        other = state.get("other_party") or ""
        unplaced = bool(other) and not any(same_name(getattr(draft, side), other) for side in ROLES)
        return "role" if can_name or unplaced else ""
    if getattr(draft, role).strip() or saved_name:
        # Their side is named, or they cleared their saved name from it on purpose. The other side
        # can still be corrected, but isn't worth a question.
        return ""
    return "name" if skipped < MAX_SKIPS else ""


def role_question(other_party: str) -> str:
    lines = ["*Who are you in this chat?*", "Your name goes on your side of the receipt."]
    if other_party:
        lines.append(f"The other person is shown as “{other_party}”.")
    return "\n".join(lines)


def name_question(role: str, *, suggested: str) -> str:
    lines = ["*What name should go on your receipts?*", f"You're the {role} on this one. I'll remember the name for your next receipts."]
    lines.append("Tap your WhatsApp name, or type another one." if suggested else "Type it, or tap *Not now*.")
    return "\n".join(lines)


def suggested_name(whatsapp_name: str | None, state: Mapping[str, Any]) -> str:
    """The one-tap answer to the name question: their WhatsApp name, or their own name in a chat export.

    Either is only offered, never put on a receipt until the customer taps it.
    """
    return (whatsapp_name or "").strip() or str(state.get("owner_label") or "").strip()


def button_title(name: str) -> str:
    return name if len(name) <= BUTTON_TITLE_LIMIT else name[: BUTTON_TITLE_LIMIT - 1].rstrip() + "…"


def role_order(usual_role: str | None) -> list[str]:
    """The customer's usual role first: the likely answer is the easiest to tap."""
    return [usual_role, opposite(usual_role)] if usual_role in ROLES else list(ROLES)
