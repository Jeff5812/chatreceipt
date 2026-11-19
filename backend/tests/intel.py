"""Builders for intelligence-engine tests: conversations and model answers in the real schema shape."""

from __future__ import annotations

from typing import Any

from app.intelligence.conversation import Conversation, Message, MessageKind, Participant, Side, SourceRef
from app.intelligence.interpretation import Interpretation, parse_interpretation
from app.intelligence.proposal import Proposal, build_proposal
from app.intelligence.review_policy import DEFAULT_POLICY, ReviewPolicy


def conversation(
    *lines: tuple[str, str] | tuple[str, str, str],
    names: dict[str, str] | None = None,
    sides: dict[str, Side] | None = None,
    days: dict[int, str] | None = None,
) -> Conversation:
    """Messages as (participant, text) or (participant, time, text). p1 is the contact, p2 the owner."""
    names = names or {"p1": "Tola Bags", "p2": ""}
    sides = sides or {"p1": Side.CONTACT, "p2": Side.OWNER}
    keys = sorted({line[0] for line in lines} | set(names))
    participants = [Participant(key=k, name=names.get(k, ""), side=sides.get(k, Side.UNKNOWN)) for k in keys if k != "sys"]
    messages = []
    for seq, line in enumerate(lines, start=1):
        who, time, text = (line[0], "", line[1]) if len(line) == 2 else line  # type: ignore[misc]
        messages.append(
            Message(
                key=f"m{seq}",
                seq=seq,
                participant=None if who == "sys" else who,
                kind=MessageKind.SYSTEM if who == "sys" else MessageKind.TEXT,
                text=text,
                original_text=text,
                time=time,
                day=(days or {}).get(seq, ""),
                source=SourceRef(kind="screenshot", position=1, index=seq),
            )
        )
    return Conversation(source_type="screenshots", app="whatsapp", participants=participants, messages=messages)


def value(text: str = "", confidence: str = "high", evidence: list[str] | None = None, note: str = "") -> dict[str, Any]:
    return {"value": text, "confidence": confidence, "evidence": evidence or [], "note": note}


def event(message: str, type: str, actor: str, item: str = "i1", **fields: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "message": message, "type": type, "actor": actor, "item": item, "quantity": "", "unit_price": "",
        "amount": "", "kind": "", "covers": "", "responds_to": "", "conditional": False, "condition": "",
        "label": "", "confidence": "high", "note": "",
    }
    for key, raw in fields.items():
        data[key] = raw
    return data


def deal(
    events: list[dict[str, Any]],
    *,
    items: list[tuple[str, str]] | None = None,
    key: str = "t1",
    currency: str = "NGN",
    agreed_items: list[dict[str, Any]] | None = None,
    delivery: str = "",
    stated_total: str = "",
    amount_paid: str = "",
    payment_status: str = "not_specified",
    status: str = "agreement_reached",
    terms: dict[str, Any] | None = None,
    ambiguities: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    items = items if items is not None else [("i1", "Brown leather bag")]
    base_terms = {name: value() for name in ("transaction_date", "due_date", "payment_terms", "delivery_terms", "notes")}
    base_terms.update(terms or {})
    return {
        "key": key,
        "summary": "A deal.",
        "currency": value(currency, evidence=["m1"]) if currency else value(),
        "items": [{"key": k, "description": d, "unit": "", "evidence": []} for k, d in items],
        "events": events,
        "agreed": {
            "items": agreed_items or [],
            "delivery_fee": value(delivery),
            "stated_total": value(stated_total),
            "amount_paid": value(amount_paid),
            "payment_status": value(payment_status),
        },
        "terms": base_terms,
        "agreement_status": status,
        "ambiguities": ambiguities or [],
    }


def answer(
    *deals: dict[str, Any],
    roles: dict[str, str] | None = None,
    injected: list[str] | None = None,
    contains_transaction: bool = True,
    ambiguities: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    roles = roles or {"p1": "seller", "p2": "buyer"}
    return {
        "contains_transaction": contains_transaction,
        "language": "en",
        "participants": [
            {"participant": k, "role": r, "confidence": "high", "evidence": [], "note": ""} for k, r in roles.items()
        ],
        "instruction_like_messages": injected or [],
        "transactions": list(deals),
        "ambiguities": ambiguities or [],
    }


def interpret(convo: Conversation, data: dict[str, Any]) -> Interpretation:
    return parse_interpretation(data, convo)


def propose(convo: Conversation, data: dict[str, Any], policy: ReviewPolicy = DEFAULT_POLICY) -> Proposal:
    interpretation = interpret(convo, data)
    return build_proposal(convo, interpretation, interpretation.deals[0], policy=policy)
