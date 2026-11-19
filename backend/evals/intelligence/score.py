"""Score the engine's proposed records against a labelled case. Pure functions: no AI, no database.

Only the labels a case gives are checked. Each check compares one value of the record with the
label and keeps the field state the engine gave that value (confirmed, inferred, needs review...),
the model's confidence and whether approval waits on it. That is what makes the safety measures
possible: a wrong figure on a record that could be approved without anyone being asked is an
"unflagged financial error", the most harmful kind of mistake.

Matching rules:
- Items are found by a word their description must contain (case, spaces and hyphens ignored).
- Amounts are compared as numbers. An empty label (null) means the value must be left empty; a
  delivery fee, discount, fee or amount paid of 0 also matches an empty value, since both mean no
  charge.
- Totals are the backend's own calculation from the record, never a figure the model wrote.
- Names must match exactly, ignoring case and spacing.
- A case with several expected deals is matched to the records by the pairing that agrees most
  (items, then quantities, prices, totals and payment status).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import permutations
from decimal import Decimal, InvalidOperation
from typing import Any

from app.calculations import compute
from app.intelligence.interpretation import Interpretation
from app.intelligence.proposal import Proposal
from evals.intelligence.dataset import Case, expected_items, number

FINANCIAL_CHECKS = frozenset({"currency", "item.quantity", "item.price", "delivery", "discount", "fees", "total", "payment", "paid"})
NO_RECORD = "no record"
_FILLER = re.compile(r"[\s\-_]+")


@dataclass(frozen=True)
class Check:
    name: str
    expected: str
    actual: str
    passed: bool
    deal: int = 0  # 0 for a single-deal case, else the expected deal's number
    status: str | None = None  # the field's state, when the value has one
    confidence: str | None = None  # the model's confidence label for the value, when it gave one
    blocking: bool | None = None  # approval waits until a person resolves this field
    record_review: bool | None = None  # the record this value belongs to needs review before approval

    @property
    def financial(self) -> bool:
        return self.name in FINANCIAL_CHECKS

    def describe(self) -> str:
        where = f"deal {self.deal}, " if self.deal else ""
        return f"{where}{self.name}: expected {self.expected}, got {self.actual}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "expected": self.expected, "actual": self.actual, "passed": self.passed,
            "deal": self.deal, "status": self.status, "confidence": self.confidence, "blocking": self.blocking,
            "record_review": self.record_review,
        }


@dataclass(frozen=True)
class CaseScore:
    case_id: str
    category: str
    split: str
    checks: tuple[Check, ...]
    records: int = 0  # records the engine proposed
    records_needing_review: int = 0
    error: str | None = None  # the run failed before a record could be made

    @property
    def passed(self) -> bool:
        return self.error is None and all(check.passed for check in self.checks)

    @property
    def failures(self) -> list[Check]:
        return [check for check in self.checks if not check.passed]

    @property
    def unflagged_financial_errors(self) -> list[Check]:
        """Wrong money values on a record that could be approved without anyone being asked."""
        return [check for check in self.failures if check.financial and check.record_review is False]

    @property
    def confident_financial_errors(self) -> list[Check]:
        """Wrong money values the record presents as confirmed."""
        return [check for check in self.failures if check.financial and check.status == "confirmed"]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.case_id, "category": self.category, "split": self.split, "passed": self.passed,
            "error": self.error, "records": self.records, "records_needing_review": self.records_needing_review,
            "problems": [check.describe() for check in self.failures],
            "checks": [check.as_dict() for check in self.checks],
        }


def _norm(text: str) -> str:
    return _FILLER.sub("", text.casefold())


def _name(text: str) -> str:
    return " ".join(text.casefold().split())


def _value(text: str) -> Decimal | None:
    cleaned = (text or "").replace(",", "").strip()
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _show(amount: Decimal | None, empty: str = "empty") -> str:
    return empty if amount is None else format(amount.normalize(), "f")


def _same(actual: Decimal | None, expected: Decimal | None, *, zero_is_empty: bool = False) -> bool:
    if expected is None:
        return actual is None
    if zero_is_empty and expected == 0:
        return actual is None or actual == 0
    return actual is not None and actual == expected


def _affinity(expected: dict[str, Any], proposal: Proposal) -> int:
    """How much of an expected deal a record agrees with: items by word, then quantity and price."""
    draft = proposal.draft
    score = 0
    if "items" in expected:
        for item in expected_items(expected["items"], ""):
            rows = [row for row in draft.line_items if _norm(item.word) in _norm(row.description)]
            score += max(
                (1 + _same(_value(row.quantity), item.quantity) + _same(_value(row.unit_price), item.unit_price) for row in rows),
                default=0,
            )
    if "total" in expected:
        score += _same(compute(draft).total, number(expected["total"]))
    if "payment" in expected:
        score += draft.payment_status.value == expected["payment"]
    return score


def match_deals(expected: list[dict[str, Any]], proposals: list[Proposal]) -> list[tuple[dict[str, Any], Proposal | None]]:
    """Pair expected deals with records so that the pairs agree as much as possible overall.

    Every assignment is tried (cases hold a handful of deals); on a tie, earlier records go to
    earlier deals. A deal gets no record only when there are fewer records than deals.
    """
    if not expected:
        return []
    if len(expected) > 4 or len(proposals) > 6:
        # Too many to try every pairing: take the best remaining record for each deal in turn.
        unused = list(proposals)
        pairs: list[tuple[dict[str, Any], Proposal | None]] = []
        for deal in expected:
            pick = max(unused, key=lambda p: (_affinity(deal, p), -unused.index(p))) if unused else None
            if pick is not None:
                unused.remove(pick)
            pairs.append((deal, pick))
        return pairs
    slots = len(proposals) + len(expected)  # indices past the records mean "no record"
    best: tuple[int, ...] | None = None
    best_score = -1
    for assignment in permutations(range(slots), len(expected)):
        total = sum(_affinity(deal, proposals[index]) for deal, index in zip(expected, assignment) if index < len(proposals))
        filled = sum(index < len(proposals) for index in assignment)
        if filled == min(len(expected), len(proposals)) and total > best_score:
            best, best_score = assignment, total
    assert best is not None
    return [(deal, proposals[index] if index < len(proposals) else None) for deal, index in zip(expected, best)]


class _Deal:
    def __init__(self, expected: dict[str, Any], proposal: Proposal | None, deal: int) -> None:
        self.expected = expected
        self.proposal = proposal
        self.deal = deal
        self.checks: list[Check] = []

    def add(self, name: str, expected: str, actual: str, passed: bool, path: str | None = None) -> None:
        proposal = self.proposal
        state = proposal.field_states.get(path) if proposal is not None and path else None
        self.checks.append(
            Check(
                name=name, expected=expected, actual=actual, passed=passed, deal=self.deal,
                status=state.status.value if state else None,
                confidence=state.confidence.value if state and state.confidence else None,
                blocking=state.blocking if state else None,
                record_review=proposal.review_required if proposal is not None else None,
            )
        )

    def missing(self) -> list[Check]:
        """Every labelled value fails when no record was made for this deal."""
        e = self.expected
        for key in ("currency", "seller", "buyer"):
            if key in e:
                self.add(key, str(e[key]), NO_RECORD, False)
        if "items" in e:
            wanted = expected_items(e["items"], "")
            self.add("item.count", str(len(wanted)), NO_RECORD, False)
            for item in wanted:
                self.add("item.product", item.word, NO_RECORD, False)
                self.add("item.quantity", _show(item.quantity), NO_RECORD, False)
                self.add("item.price", _show(item.unit_price), NO_RECORD, False)
        for key in ("delivery", "discount", "fees", "total", "paid"):
            if key in e:
                self.add(key, _show(number(e[key]), "none"), NO_RECORD, False)
        for key in ("payment", "agreement", "review"):
            if key in e:
                self.add(key, str(e[key]).lower(), NO_RECORD, False)
        return self.checks

    def score(self) -> list[Check]:
        if self.proposal is None:
            return self.missing()
        e, proposal = self.expected, self.proposal
        draft = proposal.draft
        calc = compute(draft)
        if "currency" in e:
            self.add("currency", e["currency"], draft.currency or "empty", draft.currency == e["currency"], "currency")
        for role in ("seller", "buyer"):
            if role in e:
                actual = getattr(draft, role)
                self.add(role, e[role], actual or "empty", _name(actual) == _name(e[role]), role)
        if "items" in e:
            self._items()
        if "delivery" in e:
            want = number(e["delivery"])
            got = _value(draft.delivery_fee)
            passed = not draft.delivery_fee.strip() if want is None else _same(got, want, zero_is_empty=True)
            self.add("delivery", _show(want, "none"), draft.delivery_fee or "none", passed, "delivery_fee")
        for key, total, group in (("discount", calc.discount_total, "discounts"), ("fees", calc.fee_total, "additional_fees")):
            if key in e:
                rows = draft.discounts if group == "discounts" else draft.additional_fees
                path = f"{group}.{rows[0].id}.value" if len(rows) == 1 else None
                want = number(e[key])
                self.add(key, _show(want, "none"), _show(total, "none"), _same(total, want, zero_is_empty=True), path)
        if "total" in e:
            want = number(e["total"])
            self.add("total", _show(want, "can't be calculated"), _show(calc.total, "can't be calculated"), _same(calc.total, want))
        if "payment" in e:
            actual = draft.payment_status.value
            self.add("payment", e["payment"], actual, actual == e["payment"], "payment_status")
        if "paid" in e:
            want = number(e["paid"])
            got = _value(draft.amount_paid)
            self.add("paid", _show(want, "none"), draft.amount_paid or "none", _same(got, want, zero_is_empty=True), "amount_paid")
        if "agreement" in e:
            actual = proposal.agreement_status.value
            self.add("agreement", e["agreement"], actual, actual == e["agreement"], "agreement")
        if "review" in e:
            actual = proposal.review_required
            self.add("review", str(e["review"]).lower(), str(actual).lower(), actual == e["review"])
        return self.checks

    def _items(self) -> None:
        assert self.proposal is not None
        rows = list(self.proposal.draft.line_items)
        wanted = expected_items(self.expected["items"], "")
        self.add("item.count", str(len(wanted)), str(len(rows)), len(rows) == len(wanted))
        used: set[str] = set()
        for item in wanted:
            row = next((r for r in rows if r.id not in used and _norm(item.word) in _norm(r.description)), None)
            if row is None:
                listed = ", ".join(r.description for r in rows) or "no items"
                self.add("item.product", item.word, listed, False)
                self.add("item.quantity", _show(item.quantity), "item missing", False)
                self.add("item.price", _show(item.unit_price), "item missing", False)
                continue
            used.add(row.id)
            self.add("item.product", item.word, row.description, True)
            base = f"line_items.{row.id}"
            self.add("item.quantity", _show(item.quantity), row.quantity or "empty", _same(_value(row.quantity), item.quantity), f"{base}.quantity")
            self.add("item.price", _show(item.unit_price), row.unit_price or "empty", _same(_value(row.unit_price), item.unit_price), f"{base}.unit_price")


def score_case(
    case: Case, interpretation: Interpretation | None, proposals: list[Proposal], *, error: str | None = None
) -> CaseScore:
    if error is not None:
        return CaseScore(case.id, case.category, case.split, (), error=error)
    found = bool(interpretation is not None and interpretation.contains_transaction and proposals)
    checks = [Check("contains_transaction", str(case.contains_transaction).lower(), str(found).lower(), found == case.contains_transaction)]
    expected = case.deals()
    multi = "deals" in case.expect
    if expected and (proposals or multi):
        checks.append(Check("records", str(len(expected)), str(len(proposals)), len(proposals) == len(expected)))
    for number_, (deal, proposal) in enumerate(match_deals(expected, proposals), start=1):
        checks.extend(_Deal(deal, proposal, number_ if multi else 0).score())
    return CaseScore(
        case_id=case.id, category=case.category, split=case.split, checks=tuple(checks), records=len(proposals),
        records_needing_review=sum(proposal.review_required for proposal in proposals),
    )
