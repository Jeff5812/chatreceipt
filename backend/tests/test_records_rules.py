"""The record rules: lifecycle, review operations and duplicate detection."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from app.intelligence.fields import AGREEMENT, DUPLICATE_CHECK, FieldClass, FieldSource, FieldState, FieldStatus
from app.intelligence.proposal import evaluate
from app.models import PaymentStatus, TransactionDraft
from app.records.duplicates import Fingerprint, find_match, item_overlap
from app.records.lifecycle import (
    EDITABLE_STATES,
    InvalidTransition,
    State,
    TERMINAL_STATES,
    available_actions,
    can_transition,
    require_transition,
)
from app.records.review import Operation, ReviewRejected, apply_operations, read_value
from tests.samples import valid_draft

ops = TypeAdapter(list[Operation])


def draft(**overrides: Any) -> TransactionDraft:
    return TransactionDraft.model_validate(valid_draft(**overrides))


def flagged(path: str, status: FieldStatus = FieldStatus.CONTRADICTORY, cls: FieldClass = FieldClass.FINANCIAL) -> FieldState:
    return FieldState(path=path, label=path, field_class=cls, status=status, source=FieldSource.EXTRACTED, blocking=True)


def apply(current: TransactionDraft, *operations: dict[str, Any], states: dict[str, FieldState] | None = None, extracted: dict[str, Any] | None = None) -> Any:
    return apply_operations(current, states or {}, extracted or {}, ops.validate_python(list(operations)))


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (State.PROCESSING, State.AWAITING_REVIEW), (State.PROCESSING, State.FAILED), (State.FAILED, State.PROCESSING),
        (State.FAILED, State.DRAFT), (State.DRAFT, State.APPROVED), (State.AWAITING_REVIEW, State.APPROVED),
        (State.AWAITING_REVIEW, State.REJECTED), (State.AWAITING_REVIEW, State.CANCELLED), (State.APPROVED, State.COMPLETED),
        (State.APPROVED, State.CANCELLED), (State.APPROVED, State.AWAITING_REVIEW),
    ],
)
def test_allowed_transitions(current: State, target: State) -> None:
    require_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (State.REJECTED, State.APPROVED), (State.AWAITING_REVIEW, State.COMPLETED), (State.DRAFT, State.COMPLETED),
        (State.FAILED, State.APPROVED), (State.PROCESSING, State.APPROVED), (State.COMPLETED, State.AWAITING_REVIEW),
        (State.CANCELLED, State.APPROVED), (State.COMPLETED, State.CANCELLED),
    ],
)
def test_forbidden_transitions(current: State, target: State) -> None:
    assert not can_transition(current, target)
    with pytest.raises(InvalidTransition):
        require_transition(current, target)


def test_terminal_states_have_no_way_out() -> None:
    assert TERMINAL_STATES == {State.COMPLETED, State.REJECTED, State.CANCELLED}
    assert EDITABLE_STATES == {State.DRAFT, State.AWAITING_REVIEW}
    assert available_actions(State.COMPLETED) == ["issue_receipt"]
    assert "approve" in available_actions(State.AWAITING_REVIEW) and "approve" not in available_actions(State.APPROVED)


def test_a_correction_records_what_was_extracted_and_what_it_replaced() -> None:
    outcome = apply(draft(), {"op": "set", "path": "line_items.item-1.unit_price", "value": "17,500", "reason": "Seller confirmed"}, extracted={"line_items.item-1.unit_price": "17000"})
    assert outcome.draft.line_items[0].unit_price == "17500"
    decision = outcome.decisions[0]
    assert (decision.extracted_value, decision.previous_value, decision.new_value, decision.reason) == ("17000", "17000", "17500", "Seller confirmed")
    state = outcome.states["line_items.item-1.unit_price"]
    assert state.status == FieldStatus.CONFIRMED and state.source == FieldSource.USER and state.resolved


@pytest.mark.parametrize(
    ("path", "value", "code"),
    [
        ("line_items.item-1.unit_price", "abc", "invalid_number"),
        ("line_items.item-1.unit_price", "-5", "invalid_value"),
        ("line_items.item-1.unit_price", "10.555", "invalid_value"),
        ("line_items.item-1.quantity", "0", "invalid_value"),
        ("line_items.item-9.quantity", "1", "unknown_row"),
        ("line_items.item-1.description", "  ", "value_required"),
        ("delivery_fee", "free", "invalid_number"),
        ("currency", "XXX", "unsupported_currency"),
        ("payment_status", "verified", "invalid_status"),
        ("transaction_date", "2026-02-31", "invalid_value"),
        ("id", "x", "unknown_field"),
        ("line_items.item-1.id", "x", "unknown_field"),
    ],
)
def test_unusable_values_are_refused(path: str, value: str, code: str) -> None:
    with pytest.raises(ReviewRejected) as raised:
        apply(draft(), {"op": "set", "path": path, "value": value})
    assert raised.value.problems[0].code == code and raised.value.problems[0].path == path


def test_operations_are_all_or_nothing() -> None:
    current = draft()
    with pytest.raises(ReviewRejected) as raised:
        apply(current, {"op": "set", "path": "seller", "value": "New name"}, {"op": "set", "path": "delivery_fee", "value": "nope"})
    assert raised.value.problems[0].index == 1
    assert current.seller == "Tola Bags"


def test_problems_between_fields_are_allowed_and_cleared_by_later_fixes() -> None:
    outcome = apply(draft(payment_status="unpaid"), {"op": "set", "path": "amount_paid", "value": "5000"})
    _, validation = evaluate(outcome.draft, outcome.states)
    assert not validation.can_approve and "payment_consistent" in validation.failed
    fixed = apply(outcome.draft, {"op": "set", "path": "payment_status", "value": "partially_paid"}, states=outcome.states)
    _, validation = evaluate(fixed.draft, fixed.states)
    assert validation.can_approve


def test_items_and_adjustments_can_be_added_and_removed() -> None:
    outcome = apply(
        draft(),
        {"op": "add_item", "description": "Belt", "quantity": "2", "unit_price": "5,000"},
        {"op": "add_adjustment", "group": "discounts", "label": "Loyalty", "kind": "percent", "value": "10"},
        {"op": "add_adjustment", "group": "additional_fees", "label": "Packaging", "value": "500"},
    )
    assert [row.id for row in outcome.draft.line_items] == ["item-1", "item-2"]
    assert outcome.draft.line_items[1].unit_price == "5000"
    _, validation = evaluate(outcome.draft, outcome.states)
    assert validation.calculation.total == "26800.00"  # 27,000 items - 2,700 discount + 500 fee + 2,000 delivery

    removed = apply(
        outcome.draft,
        {"op": "remove_item", "id": "item-2"},
        {"op": "remove_adjustment", "group": "discounts", "id": "discount-1"},
        states=outcome.states,
    )
    assert len(removed.draft.line_items) == 1 and removed.draft.discounts == []
    assert "line_items.item-2.unit_price" not in removed.states


def test_invalid_new_rows_are_refused() -> None:
    with pytest.raises(ReviewRejected):
        apply(draft(), {"op": "add_item", "description": "Belt", "quantity": "0", "unit_price": "100"})
    with pytest.raises(ReviewRejected):
        apply(draft(), {"op": "add_adjustment", "group": "discounts", "kind": "percent", "value": "150"})
    with pytest.raises(ReviewRejected):
        apply(draft(), {"op": "remove_item", "id": "item-7"})


def test_unknown_operations_are_refused_by_the_schema() -> None:
    with pytest.raises(ValidationError):
        ops.validate_python([{"op": "approve_everything"}])
    with pytest.raises(ValidationError):
        ops.validate_python([{"op": "set", "path": "seller", "value": "x", "surprise": True}])


def test_confirming_accepts_a_flagged_value() -> None:
    states = {"delivery_fee": flagged("delivery_fee", FieldStatus.NEEDS_REVIEW)}
    outcome = apply(draft(), {"op": "confirm", "path": "delivery_fee"}, states=states)
    state = outcome.states["delivery_fee"]
    assert state.resolved and not state.blocking and state.source == FieldSource.EXTRACTED
    assert outcome.decisions[0].operation == "confirm"


def test_an_empty_value_cannot_be_confirmed() -> None:
    states = {"line_items.item-1.unit_price": flagged("line_items.item-1.unit_price")}
    current = draft(line_items=[{"id": "item-1", "description": "Bag", "quantity": "1", "unit_price": ""}])
    with pytest.raises(ReviewRejected) as raised:
        apply(current, {"op": "confirm", "path": "line_items.item-1.unit_price"}, states=states)
    assert raised.value.problems[0].code == "value_required"


def test_process_checks_can_be_acknowledged_and_unflagged_fields_cannot() -> None:
    states = {AGREEMENT: flagged(AGREEMENT, FieldStatus.NEEDS_REVIEW, FieldClass.PROCESS)}
    assert apply(draft(), {"op": "confirm", "path": AGREEMENT}, states=states).states[AGREEMENT].resolved
    assert apply(draft(), {"op": "confirm", "path": DUPLICATE_CHECK}).states[DUPLICATE_CHECK].resolved
    with pytest.raises(ReviewRejected):
        apply(draft(), {"op": "confirm", "path": "buyer"})


def test_blocking_problems_clear_once_the_cause_is_fixed() -> None:
    current = draft(payment_status="paid", amount_paid="100")
    states, validation = evaluate(current, {})
    assert states["amount_paid"].blocking and not validation.can_approve
    fixed = current.model_copy(update={"amount_paid": ""})
    states, validation = evaluate(fixed, {})
    assert "amount_paid" not in states and validation.can_approve


def test_read_value() -> None:
    current = draft()
    assert read_value(current, "payment_status") == PaymentStatus.UNPAID.value
    assert read_value(current, "line_items.item-1.quantity") == "1"
    assert read_value(current, "line_items.item-9.quantity") is None
    assert read_value(current, "nonsense") is None


def print_of(identifier: str, **overrides: Any) -> Fingerprint:
    values: dict[str, Any] = {
        "seller": "Tola Bags", "buyer": "Ada", "currency": "NGN", "total": Decimal("19000"),
        "transaction_date": date(2026, 11, 1), "descriptions": ("Brown leather bag",),
    }
    values.update(overrides)
    return Fingerprint(id=identifier, **values)


def test_same_amount_and_items_is_a_possible_duplicate() -> None:
    match = find_match(print_of("a"), print_of("b", buyer="Someone else", seller=""), same_source=False)
    assert match is not None and any("items" in reason for reason in match.reasons)


def test_same_amount_and_parties_is_a_possible_duplicate() -> None:
    match = find_match(print_of("a"), print_of("b", descriptions=("Completely different thing",)), same_source=False)
    assert match is not None and any("seller and buyer" in reason for reason in match.reasons)


def test_the_same_uploaded_file_is_always_flagged() -> None:
    match = find_match(print_of("a"), print_of("b", total=Decimal("1"), currency="USD"), same_source=True)
    assert match is not None and "same uploaded file" in match.reasons[0]


@pytest.mark.parametrize(
    "other",
    [
        {"total": Decimal("20000")},
        {"currency": "USD"},
        {"transaction_date": date(2026, 12, 25)},
        {"seller": "Other shop", "buyer": "Bola", "descriptions": ("Laptop charger",)},
        {"total": None},
    ],
)
def test_different_transactions_are_not_flagged(other: dict[str, Any]) -> None:
    assert find_match(print_of("a"), print_of("b", **other), same_source=False) is None


def test_a_record_never_matches_itself() -> None:
    assert find_match(print_of("a"), print_of("a"), same_source=True) is None


def test_item_overlap() -> None:
    assert item_overlap(["Brown leather bag"], ["brown leather BAG"]) == 1
    assert item_overlap(["Bag"], ["Shoes"]) == 0
    assert item_overlap([], ["Shoes"]) == 0
