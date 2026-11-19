"""Which uncertainties a person must resolve before a record can be approved.

All thresholds live here, in one policy object built from settings, instead of being scattered
through the code. The evaluation report (``evals/run_intelligence.py``) measures how often each
confidence level is wrong and what each policy choice costs in review effort, so the defaults can
be changed on evidence. Model confidence labels are treated as ordinal hints, not probabilities.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from app.intelligence.fields import FieldClass, FieldStatus
from app.models import Confidence

_ALWAYS_BLOCKING = frozenset({FieldStatus.CONTRADICTORY, FieldStatus.INVALID})


def _default_blocking() -> dict[FieldClass, frozenset[FieldStatus]]:
    return {
        FieldClass.FINANCIAL: _ALWAYS_BLOCKING | {FieldStatus.NEEDS_REVIEW},
        FieldClass.PROCESS: _ALWAYS_BLOCKING | {FieldStatus.NEEDS_REVIEW},
        FieldClass.PARTY: _ALWAYS_BLOCKING,
        FieldClass.DATE: _ALWAYS_BLOCKING,
        FieldClass.DESCRIPTION: _ALWAYS_BLOCKING,
    }


@dataclass(frozen=True)
class ReviewPolicy:
    # What a medium-confidence money value becomes: inferred (shown, not blocking) or needs_review.
    medium_financial: FieldStatus = FieldStatus.INFERRED
    blocking: Mapping[FieldClass, frozenset[FieldStatus]] = field(default_factory=_default_blocking)
    # A possible duplicate of an existing record must be acknowledged before approval.
    block_possible_duplicates: bool = True

    def status_for(
        self,
        field_class: FieldClass,
        *,
        has_value: bool,
        confidence: Confidence | None,
        grounded: bool | None = None,
        implicit: bool = False,
        conflict: bool = False,
        invalid: bool = False,
        conditional: bool = False,
    ) -> FieldStatus:
        if invalid:
            return FieldStatus.INVALID
        if conflict:
            return FieldStatus.CONTRADICTORY
        if not has_value:
            return FieldStatus.MISSING
        if grounded is False or conditional or confidence == Confidence.LOW:
            return FieldStatus.NEEDS_REVIEW
        if implicit:
            return FieldStatus.INFERRED
        if confidence == Confidence.MEDIUM:
            return self.medium_financial if field_class == FieldClass.FINANCIAL else FieldStatus.INFERRED
        return FieldStatus.CONFIRMED

    def is_blocking(self, field_class: FieldClass, status: FieldStatus) -> bool:
        return status in self.blocking.get(field_class, _ALWAYS_BLOCKING)


DEFAULT_POLICY = ReviewPolicy()


def policy_from_settings(settings: object) -> ReviewPolicy:
    medium = getattr(settings, "review_medium_confidence_financial", "inferred")
    return ReviewPolicy(
        medium_financial=FieldStatus.NEEDS_REVIEW if medium == "needs_review" else FieldStatus.INFERRED,
        block_possible_duplicates=bool(getattr(settings, "review_block_possible_duplicates", True)),
    )
