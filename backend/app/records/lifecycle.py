"""The lifecycle of a transaction record, as an explicit state machine.

    processing ──> awaiting_review ──> approved ──> completed
        │  ▲             │  │  ▲          │
        ▼  │             │  │  └─reopen───┤
      failed             │  ▼             ▼
        │                │ rejected    cancelled
        ▼                ▼
      draft ──────> approved, cancelled

- processing: the conversation is being read.
- awaiting_review: read, and waiting for a person. Nothing is authoritative until they approve it.
- draft: entered by hand, or started by hand after reading failed.
- approved: a person confirmed it. Its figures are frozen; changing them means reopening it.
- completed: approved and recorded as fully paid. Terminal.
- rejected: the proposal was wrong or wasn't a transaction. Terminal.
- cancelled: the deal was called off. Terminal.
- failed: reading the conversation failed. It can be tried again or entered by hand.
"""

from __future__ import annotations

from enum import Enum


class State(str, Enum):
    DRAFT = "draft"
    PROCESSING = "processing"
    AWAITING_REVIEW = "awaiting_review"
    APPROVED = "approved"
    COMPLETED = "completed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    FAILED = "failed"


_TRANSITIONS: dict[State, frozenset[State]] = {
    State.PROCESSING: frozenset({State.AWAITING_REVIEW, State.FAILED}),
    State.FAILED: frozenset({State.PROCESSING, State.DRAFT}),
    State.DRAFT: frozenset({State.APPROVED, State.CANCELLED}),
    State.AWAITING_REVIEW: frozenset({State.APPROVED, State.REJECTED, State.CANCELLED}),
    State.APPROVED: frozenset({State.COMPLETED, State.CANCELLED, State.AWAITING_REVIEW}),
    State.COMPLETED: frozenset(),
    State.REJECTED: frozenset(),
    State.CANCELLED: frozenset(),
}

EDITABLE_STATES = frozenset({State.DRAFT, State.AWAITING_REVIEW})
RECEIPT_STATES = frozenset({State.APPROVED, State.COMPLETED})
TERMINAL_STATES = frozenset(state for state, targets in _TRANSITIONS.items() if not targets)


class InvalidTransition(Exception):
    def __init__(self, current: State, target: State) -> None:
        super().__init__(f"{current.value} -> {target.value}")
        self.current = current
        self.target = target


def can_transition(current: State, target: State) -> bool:
    return target in _TRANSITIONS[current]


def require_transition(current: State, target: State) -> None:
    if not can_transition(current, target):
        raise InvalidTransition(current, target)


def available_actions(state: State) -> list[str]:
    """What a person can do next, for clients deciding which controls to show."""
    actions: list[str] = []
    if state in EDITABLE_STATES:
        actions += ["review", "approve"]
    if can_transition(state, State.REJECTED):
        actions.append("reject")
    if can_transition(state, State.CANCELLED):
        actions.append("cancel")
    if state == State.APPROVED:
        actions += ["complete", "reopen"]
    if state in RECEIPT_STATES:
        actions.append("issue_receipt")
    if state == State.FAILED:
        actions += ["retry", "enter_manually"]
    return actions
