"""Human review: corrections and confirmations applied to a draft.

Operations are all-or-nothing. A value is refused here when the value itself is unusable (not a
number, negative, too many decimals, not a real date). Problems between fields, such as a status of
"Paid" with a part payment entered, are allowed through and block approval until they are fixed, so
a person can correct several fields in any order.

Every operation produces a ``Decision`` recording the value originally extracted, the value before
this change and the new value. The original extraction is never overwritten.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.calculations import compute, parse_number, to_plain
from app.currencies import get_currency
from app.intelligence.fields import (
    DUPLICATE_CHECK,
    LABELS,
    PROCESS_PATHS,
    FieldSource,
    FieldState,
    FieldStatus,
    field_class,
)
from app.models import AdjustmentDraft, LineItemDraft, PaymentStatus, TransactionDraft

Group = Literal["discounts", "additional_fees"]

MAX_ITEMS = 50
MAX_ADJUSTMENTS = 10
_TEXT_LIMITS = {"seller": 120, "buyer": 120, "payment_terms": 1000, "delivery_terms": 1000, "notes": 2000}
_MONEY = frozenset({"delivery_fee", "amount_paid", "stated_total"})
_DATES = frozenset({"transaction_date", "due_date"})
_VALUE_CODES = frozenset(
    {
        "invalid_number", "too_many_decimals", "negative_value", "value_too_large", "non_positive_quantity",
        "invalid_percent", "invalid_date", "date_out_of_range",
    }
)


class _Operation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="", max_length=300)


class SetField(_Operation):
    op: Literal["set"]
    path: str = Field(min_length=1, max_length=120)
    value: str = Field(max_length=2000)


class AddItem(_Operation):
    op: Literal["add_item"]
    description: str = Field(min_length=1, max_length=200)
    quantity: str = Field(min_length=1, max_length=32)
    unit_price: str = Field(min_length=1, max_length=32)


class RemoveItem(_Operation):
    op: Literal["remove_item"]
    id: str = Field(min_length=1, max_length=40)


class AddAdjustment(_Operation):
    op: Literal["add_adjustment"]
    group: Group
    label: str = Field(default="", max_length=120)
    kind: Literal["amount", "percent"] = "amount"
    value: str = Field(min_length=1, max_length=32)


class RemoveAdjustment(_Operation):
    op: Literal["remove_adjustment"]
    group: Group
    id: str = Field(min_length=1, max_length=40)


class Confirm(_Operation):
    """Accept a flagged value as it stands."""

    op: Literal["confirm"]
    path: str = Field(min_length=1, max_length=120)


Operation = Annotated[
    SetField | AddItem | RemoveItem | AddAdjustment | RemoveAdjustment | Confirm, Field(discriminator="op")
]


@dataclass(frozen=True)
class Decision:
    operation: str
    path: str
    extracted_value: Any
    previous_value: Any
    new_value: Any
    reason: str


@dataclass(frozen=True)
class Problem:
    index: int
    path: str
    code: str
    message: str


class ReviewRejected(Exception):
    def __init__(self, problems: list[Problem]) -> None:
        super().__init__(f"{len(problems)} problem(s)")
        self.problems = problems


@dataclass(frozen=True)
class ReviewOutcome:
    draft: TransactionDraft
    states: dict[str, FieldState]
    decisions: list[Decision]


def read_value(draft: TransactionDraft, path: str) -> str | None:
    parts = path.split(".")
    if len(parts) == 1:
        value = getattr(draft, path, None) if path in TransactionDraft.model_fields else None
        if isinstance(value, PaymentStatus):
            return value.value
        return value if isinstance(value, str) else None
    if len(parts) == 3 and parts[0] in ("line_items", "discounts", "additional_fees"):
        row = next((entry for entry in getattr(draft, parts[0]) if entry.id == parts[1]), None)
        value = getattr(row, parts[2], None) if row is not None and parts[2] != "id" else None
        return value if isinstance(value, str) else None
    return None


def _next_id(prefix: str, rows: list[Any]) -> str:
    numbers = [int(match[1]) for row in rows if (match := re.fullmatch(rf"{prefix}-(\d+)", row.id))]
    return f"{prefix}-{max(numbers, default=0) + 1}"


def _user_state(path: str, label: str | None = None) -> FieldState:
    return FieldState(
        path=path,
        label=label or LABELS.get(path, path),
        field_class=field_class(path),
        status=FieldStatus.CONFIRMED,
        source=FieldSource.USER,
        resolved=True,
    )


def _plain_number(text: str) -> str | None:
    try:
        value = parse_number(text)
    except ValueError:
        return None
    return to_plain(value) if value is not None else None


class _Review:
    def __init__(self, draft: TransactionDraft, states: dict[str, FieldState], extracted: dict[str, Any]) -> None:
        self.draft = draft
        self.states = dict(states)
        self.extracted = extracted
        self.decisions: list[Decision] = []

    def _problem(self, candidate: TransactionDraft, path: str) -> str:
        for issue in compute(candidate).issues:
            if issue.severity == "error" and issue.field == path and issue.code in _VALUE_CODES:
                return issue.message
        return ""

    def _decide(self, op: str, path: str, previous: Any, new: Any, reason: str) -> None:
        self.decisions.append(Decision(op, path, self.extracted.get(path), previous, new, reason))

    def _row_update(self, group: str, row_id: str, attribute: str, value: str) -> TransactionDraft:
        rows = [row.model_copy(update={attribute: value}) if row.id == row_id else row for row in getattr(self.draft, group)]
        return self.draft.model_copy(update={group: rows})

    def _candidate_for(self, index: int, path: str, raw: str) -> tuple[TransactionDraft, str]:
        """The draft with this value applied, and the value as stored. Raises for an unusable value."""
        text = raw.strip()

        def refuse(code: str, message: str) -> ReviewRejected:
            return ReviewRejected([Problem(index, path, code, message)])

        parts = path.split(".")
        if len(parts) == 1:
            if path in _TEXT_LIMITS:
                if len(text) > _TEXT_LIMITS[path]:
                    raise refuse("too_long", f"Keep this under {_TEXT_LIMITS[path]} characters.")
                stored = text
            elif path in _MONEY:
                stored = _plain_number(text) if text else ""
                if stored is None:
                    raise refuse("invalid_number", "Enter this as a number, for example 17000 or 17,000.50.")
            elif path in _DATES:
                stored = text
            elif path == "currency":
                currency = get_currency(text)
                if currency is None:
                    raise refuse("unsupported_currency", "Choose a supported currency code, such as NGN or USD.")
                stored = currency.code
            elif path == "payment_status":
                if text not in PaymentStatus._value2member_map_:
                    raise refuse("invalid_status", "Choose a valid payment status.")
                return self.draft.model_copy(update={"payment_status": PaymentStatus(text)}), text
            else:
                raise refuse("unknown_field", "This field can't be changed.")
            candidate = self.draft.model_copy(update={path: stored})
        elif len(parts) == 3 and parts[0] == "line_items" and parts[2] in ("description", "quantity", "unit_price"):
            if not any(row.id == parts[1] for row in self.draft.line_items):
                raise refuse("unknown_row", "That item isn't on this record.")
            if not text:
                raise refuse("value_required", "Enter a value for this field.")
            stored = text[:200] if parts[2] == "description" else _plain_number(text)  # type: ignore[assignment]
            if stored is None:
                raise refuse("invalid_number", "Enter this as a number, for example 17000 or 17,000.50.")
            candidate = self._row_update("line_items", parts[1], parts[2], stored)
        elif len(parts) == 3 and parts[0] in ("discounts", "additional_fees") and parts[2] in ("label", "kind", "value"):
            if not any(row.id == parts[1] for row in getattr(self.draft, parts[0])):
                raise refuse("unknown_row", "That entry isn't on this record.")
            if parts[2] == "kind" and text not in ("amount", "percent"):
                raise refuse("invalid_kind", "Choose either amount or percent.")
            if parts[2] == "value":
                if not text:
                    raise refuse("value_required", "Enter a value for this field.")
                stored = _plain_number(text)  # type: ignore[assignment]
                if stored is None:
                    raise refuse("invalid_number", "Enter this as a number, for example 1500 or 10.")
            else:
                stored = text[:120]
            candidate = self._row_update(parts[0], parts[1], parts[2], stored)
        else:
            raise refuse("unknown_field", "This field can't be changed.")

        problem = self._problem(candidate, path)
        if problem:
            raise refuse("invalid_value", problem)
        return candidate, stored

    def set_field(self, index: int, op: SetField) -> None:
        previous = read_value(self.draft, op.path)
        self.draft, stored = self._candidate_for(index, op.path, op.value)
        label = self.states[op.path].label if op.path in self.states else None
        self.states[op.path] = _user_state(op.path, label)
        self._decide("set", op.path, previous, stored, op.reason)

    def add_item(self, index: int, op: AddItem) -> None:
        if len(self.draft.line_items) >= MAX_ITEMS:
            raise ReviewRejected([Problem(index, "line_items", "too_many_items", f"A record can have up to {MAX_ITEMS} items.")])
        row_id = _next_id("item", self.draft.line_items)
        row = LineItemDraft(id=row_id, description=op.description.strip(), quantity=op.quantity.strip(), unit_price=op.unit_price.strip())
        self.draft = self.draft.model_copy(update={"line_items": [*self.draft.line_items, row]})
        for attribute in ("quantity", "unit_price"):
            path = f"line_items.{row_id}.{attribute}"
            stored = _plain_number(getattr(row, attribute))
            if stored is None:
                raise ReviewRejected([Problem(index, path, "invalid_number", "Enter this as a number, for example 17000.")])
            self.draft = self._row_update("line_items", row_id, attribute, stored)
            problem = self._problem(self.draft, path)
            if problem:
                raise ReviewRejected([Problem(index, path, "invalid_value", problem)])
        for attribute in ("description", "quantity", "unit_price"):
            self.states[f"line_items.{row_id}.{attribute}"] = _user_state(f"line_items.{row_id}.{attribute}")
        added = next(entry for entry in self.draft.line_items if entry.id == row_id)
        self._decide("add_item", f"line_items.{row_id}", None, added.model_dump(), op.reason)

    def remove_item(self, index: int, op: RemoveItem) -> None:
        row = next((entry for entry in self.draft.line_items if entry.id == op.id), None)
        if row is None:
            raise ReviewRejected([Problem(index, f"line_items.{op.id}", "unknown_row", "That item isn't on this record.")])
        self.draft = self.draft.model_copy(update={"line_items": [entry for entry in self.draft.line_items if entry.id != op.id]})
        self._forget(f"line_items.{op.id}.")
        self._decide("remove_item", f"line_items.{op.id}", row.model_dump(), None, op.reason)

    def add_adjustment(self, index: int, op: AddAdjustment) -> None:
        rows: list[AdjustmentDraft] = list(getattr(self.draft, op.group))
        if len(rows) >= MAX_ADJUSTMENTS:
            raise ReviewRejected([Problem(index, op.group, "too_many_rows", f"A record can have up to {MAX_ADJUSTMENTS} of these.")])
        noun = "discount" if op.group == "discounts" else "fee"
        row_id = _next_id(noun, rows)
        path = f"{op.group}.{row_id}.value"
        stored = _plain_number(op.value)
        if stored is None:
            raise ReviewRejected([Problem(index, path, "invalid_number", "Enter this as a number, for example 1500 or 10.")])
        row = AdjustmentDraft(id=row_id, label=op.label.strip(), kind=op.kind, value=stored)
        candidate = self.draft.model_copy(update={op.group: [*rows, row]})
        problem = self._problem(candidate, path)
        if problem:
            raise ReviewRejected([Problem(index, path, "invalid_value", problem)])
        self.draft = candidate
        self.states[path] = _user_state(path, f"the {noun}")
        self._decide("add_adjustment", f"{op.group}.{row_id}", None, row.model_dump(), op.reason)

    def remove_adjustment(self, index: int, op: RemoveAdjustment) -> None:
        rows: list[AdjustmentDraft] = list(getattr(self.draft, op.group))
        row = next((entry for entry in rows if entry.id == op.id), None)
        if row is None:
            raise ReviewRejected([Problem(index, f"{op.group}.{op.id}", "unknown_row", "That entry isn't on this record.")])
        self.draft = self.draft.model_copy(update={op.group: [entry for entry in rows if entry.id != op.id]})
        self._forget(f"{op.group}.{op.id}.")
        self._decide("remove_adjustment", f"{op.group}.{op.id}", row.model_dump(), None, op.reason)

    def confirm(self, index: int, op: Confirm) -> None:
        state = self.states.get(op.path)
        if state is None and op.path == DUPLICATE_CHECK:
            state = _user_state(DUPLICATE_CHECK)
        if state is None:
            raise ReviewRejected([Problem(index, op.path, "nothing_to_confirm", "This field isn't waiting for confirmation.")])
        value = read_value(self.draft, op.path)
        if op.path not in PROCESS_PATHS and not value:
            raise ReviewRejected([Problem(index, op.path, "value_required", "Enter a value for this field instead of confirming it.")])
        self.states[op.path] = state.model_copy(update={"status": FieldStatus.CONFIRMED, "resolved": True, "blocking": False})
        self._decide("confirm", op.path, value, value, op.reason)

    def _forget(self, prefix: str) -> None:
        self.states = {path: state for path, state in self.states.items() if not path.startswith(prefix)}


def apply_operations(
    draft: TransactionDraft,
    states: dict[str, FieldState],
    extracted: dict[str, Any],
    operations: list[SetField | AddItem | RemoveItem | AddAdjustment | RemoveAdjustment | Confirm],
) -> ReviewOutcome:
    """Apply every operation, or raise ReviewRejected and change nothing."""
    review = _Review(draft, states, extracted)
    handlers = {
        "set": review.set_field,
        "add_item": review.add_item,
        "remove_item": review.remove_item,
        "add_adjustment": review.add_adjustment,
        "remove_adjustment": review.remove_adjustment,
        "confirm": review.confirm,
    }
    for index, operation in enumerate(operations):
        handlers[operation.op](index, operation)  # type: ignore[operator]
    return ReviewOutcome(review.draft, review.states, review.decisions)
