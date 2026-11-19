"""Load and check the labelled conversations, and split them into development and held-out cases.

The split is fixed by position, not drawn at random: within each category, the 1st, 4th and 7th
cases are for development (tuning prompts and rules, and looking at failures) and every other case
is held out for measurement. Adding a case to the end of a category never moves an existing case
from one split to the other, so a held-out case can't leak into development by re-shuffling.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

import yaml

from app.intelligence.interpretation import AgreementStatus
from app.models import PaymentStatus

DATASET = Path(__file__).resolve().parent / "conversations.yaml"

Split = Literal["dev", "test"]

# The eighteen kinds of conversation the evaluation must cover, and two more it also checks.
REQUIRED_CATEGORIES = (
    "simple_purchase",
    "multiple_products",
    "price_negotiation",
    "multiple_counteroffers",
    "delivery_fees",
    "discounts",
    "partial_payments",
    "unpaid",
    "cancellations",
    "ambiguous_agreements",
    "missing_prices",
    "multiple_speakers",
    "corrections",
    "contradictions",
    "multiple_transactions",
    "poor_ocr",
    "out_of_order",
    "incomplete",
)
EXTRA_CATEGORIES = ("no_transaction", "prompt_injection")

DEAL_KEYS = frozenset(
    {"currency", "seller", "buyer", "items", "delivery", "discount", "fees", "total", "payment", "paid", "agreement", "review"}
)
CASE_KEYS = DEAL_KEYS | {"contains_transaction", "deals"}
_NUMBER_KEYS = ("delivery", "discount", "fees", "total", "paid")
_AGREEMENTS = {status.value for status in AgreementStatus}
_PAYMENTS = {status.value for status in PaymentStatus}


class DatasetError(ValueError):
    """The dataset file breaks one of its own rules."""


@dataclass(frozen=True)
class ExpectedItem:
    word: str  # a word the item's description must contain
    quantity: Decimal | None  # None: the quantity must be left empty
    unit_price: Decimal | None  # None: the price must be left empty


@dataclass(frozen=True)
class Case:
    id: str
    category: str
    position: int  # 0-based position within its category
    chat: str
    expect: dict[str, Any]  # as written in the file; only the keys present are scored

    @property
    def split(self) -> Split:
        return split_for(self.position)

    @property
    def contains_transaction(self) -> bool:
        return bool(self.expect.get("contains_transaction", True))

    def deals(self) -> list[dict[str, Any]]:
        """The expected transactions: the `deals` list, or the case's own keys as one deal."""
        if "deals" in self.expect:
            return list(self.expect["deals"])
        if not self.contains_transaction:
            return []
        return [{key: value for key, value in self.expect.items() if key in DEAL_KEYS}]


def split_for(position: int) -> Split:
    return "dev" if position % 3 == 0 else "test"


def number(value: Any) -> Decimal | None:
    """A label's amount as a Decimal; None stays None."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise DatasetError(f"{value!r} is not an amount")
    try:
        return Decimal(str(value).replace(",", ""))
    except InvalidOperation as exc:
        raise DatasetError(f"{value!r} is not an amount") from exc


def expected_items(raw: Any, where: str) -> list[ExpectedItem]:
    if not isinstance(raw, list) or not raw:
        raise DatasetError(f"{where}: items must be a non-empty list")
    items = []
    for entry in raw:
        if not (isinstance(entry, list) and len(entry) == 3 and isinstance(entry[0], str) and entry[0].strip()):
            raise DatasetError(f"{where}: each item is [word, quantity, unit price], got {entry!r}")
        items.append(ExpectedItem(entry[0].strip(), number(entry[1]), number(entry[2])))
    return items


def _check_deal(deal: Any, where: str) -> None:
    if not isinstance(deal, dict):
        raise DatasetError(f"{where}: a deal must be a mapping")
    unknown = set(deal) - DEAL_KEYS
    if unknown:
        raise DatasetError(f"{where}: unknown keys {sorted(unknown)}")
    if "items" in deal:
        expected_items(deal["items"], where)
    for key in _NUMBER_KEYS:
        if key in deal:
            number(deal[key])
    if "currency" in deal and not (isinstance(deal["currency"], str) and len(deal["currency"]) == 3 and deal["currency"].isupper()):
        raise DatasetError(f"{where}: currency must be a 3-letter code, got {deal['currency']!r}")
    if "payment" in deal and deal["payment"] not in _PAYMENTS:
        raise DatasetError(f"{where}: unknown payment status {deal['payment']!r}")
    if "agreement" in deal and deal["agreement"] not in _AGREEMENTS:
        raise DatasetError(f"{where}: unknown agreement status {deal['agreement']!r}")
    if "review" in deal and not isinstance(deal["review"], bool):
        raise DatasetError(f"{where}: review must be true or false")
    for role in ("seller", "buyer"):
        if role in deal and not (isinstance(deal[role], str) and deal[role].strip()):
            raise DatasetError(f"{where}: {role} must be a name")


def _check_case(case: Case) -> None:
    where = case.id
    if not case.chat.strip():
        raise DatasetError(f"{where}: empty chat")
    unknown = set(case.expect) - CASE_KEYS
    if unknown:
        raise DatasetError(f"{where}: unknown keys {sorted(unknown)}")
    if not case.expect:
        raise DatasetError(f"{where}: nothing to check")
    if "contains_transaction" in case.expect and not isinstance(case.expect["contains_transaction"], bool):
        raise DatasetError(f"{where}: contains_transaction must be true or false")
    if "deals" in case.expect:
        deals = case.expect["deals"]
        # One entry is allowed: it checks that a single order isn't split into several records.
        if not isinstance(deals, list) or not deals:
            raise DatasetError(f"{where}: deals lists the expected transactions")
        if set(case.expect) - {"deals", "contains_transaction"}:
            raise DatasetError(f"{where}: with deals, put every other expectation inside a deal")
        for number_, deal in enumerate(deals, start=1):
            _check_deal(deal, f"{where} deal {number_}")
    else:
        _check_deal({k: v for k, v in case.expect.items() if k in DEAL_KEYS}, where)
    if not case.contains_transaction and set(case.expect) - {"contains_transaction"}:
        raise DatasetError(f"{where}: a chat without a transaction has nothing else to check")


def load_cases(path: Path = DATASET) -> list[Case]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise DatasetError("the dataset must map categories to lists of cases")
    known = set(REQUIRED_CATEGORIES) | set(EXTRA_CATEGORIES)
    cases: list[Case] = []
    seen: set[str] = set()
    for category, entries in data.items():
        if category not in known:
            raise DatasetError(f"unknown category {category!r}")
        if not isinstance(entries, list) or not entries:
            raise DatasetError(f"{category}: no cases")
        for position, entry in enumerate(entries):
            if not isinstance(entry, dict) or set(entry) != {"id", "chat", "expect"}:
                raise DatasetError(f"{category}[{position}]: a case has exactly id, chat and expect")
            case = Case(id=str(entry["id"]), category=category, position=position, chat=str(entry["chat"]), expect=dict(entry["expect"]))
            if case.id in seen:
                raise DatasetError(f"duplicate case id {case.id!r}")
            seen.add(case.id)
            _check_case(case)
            cases.append(case)
    missing = [category for category in REQUIRED_CATEGORIES if category not in data]
    if missing:
        raise DatasetError(f"missing categories: {missing}")
    return cases


def select(cases: list[Case], split: str, ids: list[str] | None = None) -> list[Case]:
    """Cases for a run: one split ("dev", "test" or "all"), optionally narrowed to some ids."""
    if split not in ("dev", "test", "all"):
        raise DatasetError(f"unknown split {split!r}")
    chosen = [case for case in cases if split == "all" or case.split == split]
    if ids:
        # Ids only narrow the split: naming a held-out case can't pull it into a development run.
        outside = set(ids) - {case.id for case in chosen}
        if outside:
            raise DatasetError(f"not in the {split} split: {sorted(outside)}")
        chosen = [case for case in chosen if case.id in ids]
    return chosen
