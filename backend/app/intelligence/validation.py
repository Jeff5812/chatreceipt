"""Deterministic validation of a record: the rules it passes and fails, and what blocks approval.

Arithmetic is not repeated here: ``app.calculations.compute`` remains the only place totals are
calculated, and its issues are grouped into named rules. Field states add the rules that come from
the conversation (contradictions, figures not found in their message, the agreement itself).
Running it twice on the same record gives the same result.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.calculations import compute, to_calculation
from app.intelligence.fields import AGREEMENT, FieldState, FieldStatus
from app.models import Calculation, Issue, TransactionDraft

Outcome = Literal["passed", "failed", "warning"]

RULES: dict[str, str] = {
    "currency_valid": "A supported currency is chosen.",
    "line_items_valid": "There is at least one item, each with a description, a quantity above zero and a unit price.",
    "adjustments_valid": "Discounts and fees are valid amounts or percentages, and discounts don't exceed the items.",
    "delivery_fee_valid": "The delivery fee, if any, is a valid amount.",
    "payment_consistent": "The payment status and the amount paid agree with each other and with the total.",
    "dates_valid": "Dates are real calendar dates, and the due date isn't before the transaction date.",
    "stated_total_matches": "A total stated in the conversation matches the calculated total.",
    "no_duplicate_lines": "No item appears twice.",
    "totals_computable": "The total can be calculated from the details given.",
    "no_contradictions": "No value has competing versions in the conversation that are still unresolved.",
    "values_grounded": "Every figure taken from the conversation appears in the message it was taken from.",
    "agreement_detected": "The conversation shows an agreement, or a person has confirmed it.",
    "review_items_resolved": "Every value a person needs to check has been corrected or confirmed.",
}

# Calculation issue codes that mean the value itself is unusable (as opposed to missing).
INVALID_VALUE_CODES = frozenset(
    {
        "invalid_number", "too_many_decimals", "negative_value", "value_too_large", "non_positive_quantity",
        "invalid_percent", "invalid_date", "date_out_of_range", "unsupported_currency",
    }
)


class RuleResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    rule: str
    description: str
    outcome: Outcome
    fields: tuple[str, ...] = ()
    messages: tuple[str, ...] = ()


class ValidationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    outcome: Literal["valid", "needs_review", "invalid"]
    can_approve: bool
    rules: tuple[RuleResult, ...]
    passed: tuple[str, ...]
    failed: tuple[str, ...]
    warnings: tuple[str, ...]
    missing: tuple[str, ...]  # required values that are empty
    contradictions: tuple[str, ...]  # field paths with unresolved competing values
    review_required: tuple[str, ...]  # field paths a person must correct or confirm
    calculation: Calculation


def _rule_for(issue: Issue) -> str:
    path = issue.field or ""
    if issue.code in ("missing_currency", "unsupported_currency"):
        return "currency_valid"
    if issue.code == "possible_duplicate":
        return "no_duplicate_lines"
    if path.startswith("line_items"):
        return "line_items_valid"
    if path.startswith(("discounts", "additional_fees")):
        return "adjustments_valid"
    if path == "delivery_fee":
        return "delivery_fee_valid"
    if path in ("amount_paid", "payment_status"):
        return "payment_consistent"
    if path in ("transaction_date", "due_date"):
        return "dates_valid"
    if path == "stated_total":
        return "stated_total_matches"
    return "line_items_valid"


def validate(draft: TransactionDraft, field_states: Mapping[str, FieldState]) -> ValidationResult:
    computation = compute(draft)
    calculation = to_calculation(draft, computation)

    grouped: dict[str, list[Issue]] = {rule: [] for rule in RULES}
    for issue in computation.issues:
        if issue.code == "missing_date":
            continue  # a missing date is allowed; the form shows a hint
        grouped[_rule_for(issue)].append(issue)

    open_states = [state for state in field_states.values() if not state.resolved]
    contradictions = [s.path for s in open_states if s.status == FieldStatus.CONTRADICTORY]
    ungrounded = [s.path for s in open_states if s.grounded is False]
    blocking = [s.path for s in open_states if s.blocking]
    agreement = field_states.get(AGREEMENT)
    agreement_open = agreement is not None and not agreement.resolved and agreement.status != FieldStatus.CONFIRMED

    def result(rule: str) -> RuleResult:
        issues = grouped.get(rule, [])
        fields: list[str] = [issue.field for issue in issues if issue.field]
        messages = [issue.message for issue in issues]
        outcome: Outcome = "passed"
        if any(issue.severity == "error" for issue in issues):
            outcome = "failed"
        elif issues:
            outcome = "warning"
        if rule == "totals_computable" and calculation.total is None:
            outcome, messages = "failed", ["The total can't be calculated yet."]
        elif rule == "no_contradictions" and contradictions:
            outcome, fields = "failed", contradictions
        elif rule == "values_grounded" and ungrounded:
            outcome, fields = "warning", ungrounded
        elif rule == "agreement_detected" and agreement_open:
            outcome, fields = "failed" if agreement.blocking else "warning", [AGREEMENT]  # type: ignore[union-attr]
        elif rule == "review_items_resolved" and blocking:
            outcome, fields = "failed", blocking
        return RuleResult(
            rule=rule,
            description=RULES[rule],
            outcome=outcome,
            fields=tuple(dict.fromkeys(fields)),
            messages=tuple(dict.fromkeys(messages)),
        )

    rules = tuple(result(rule) for rule in RULES)
    missing = tuple(
        dict.fromkeys(
            issue.field or "" for issue in computation.issues if issue.code in ("missing_value", "missing_currency", "no_items", "missing_description", "missing_part_payment")
        )
    )
    if computation.has_errors:
        outcome: Literal["valid", "needs_review", "invalid"] = "invalid"
    elif blocking:
        outcome = "needs_review"
    else:
        outcome = "valid"
    return ValidationResult(
        outcome=outcome,
        can_approve=outcome == "valid" and calculation.can_export,
        rules=rules,
        passed=tuple(r.rule for r in rules if r.outcome == "passed"),
        failed=tuple(r.rule for r in rules if r.outcome == "failed"),
        warnings=tuple(r.rule for r in rules if r.outcome == "warning"),
        missing=missing,
        contradictions=tuple(contradictions),
        review_required=tuple(blocking),
        calculation=calculation,
    )
