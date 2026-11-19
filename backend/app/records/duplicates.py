"""Spotting transactions that may already be on record.

Two real purchases can be identical, so a match is never rejected automatically: it is shown to a
person, who confirms it isn't a duplicate. A record is flagged when it was made from the same
uploaded file, or when the amount matches and either the items or both parties match too, within a
short time of each other.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

DATE_WINDOW_DAYS = 14
MIN_ITEM_OVERLAP = 0.5
_STOP_WORDS = frozenset({"the", "a", "an", "of", "and", "for", "with", "size", "per"})


@dataclass(frozen=True)
class Fingerprint:
    id: str
    seller: str
    buyer: str
    currency: str
    total: Decimal | None
    transaction_date: date | None
    descriptions: tuple[str, ...] = ()


@dataclass(frozen=True)
class Match:
    transaction_id: str
    reasons: tuple[str, ...]


def _name(text: str) -> str:
    return re.sub(r"[^\w]+", "", text.casefold())


def _tokens(descriptions: Iterable[str]) -> set[str]:
    words = re.findall(r"[^\W\d_]+|\d+", " ".join(descriptions).casefold())
    return {word for word in words if word not in _STOP_WORDS}


def item_overlap(first: Iterable[str], second: Iterable[str]) -> float:
    a, b = _tokens(first), _tokens(second)
    return len(a & b) / len(a | b) if a and b else 0.0


def find_match(new: Fingerprint, other: Fingerprint, *, same_source: bool) -> Match | None:
    if new.id == other.id:
        return None
    if same_source:
        return Match(other.id, ("It was made from the same uploaded file.",))
    if new.total is None or other.total is None or new.total != other.total or new.currency != other.currency:
        return None
    if new.transaction_date and other.transaction_date and abs((new.transaction_date - other.transaction_date).days) > DATE_WINDOW_DAYS:
        return None
    reasons = ["The total is the same."]
    same_items = item_overlap(new.descriptions, other.descriptions) >= MIN_ITEM_OVERLAP
    same_parties = bool(new.seller and new.buyer) and _name(new.seller) == _name(other.seller) and _name(new.buyer) == _name(other.buyer)
    if same_items:
        reasons.append("The items are the same or very similar.")
    if same_parties:
        reasons.append("The seller and buyer are the same.")
    return Match(other.id, tuple(reasons)) if same_items or same_parties else None
