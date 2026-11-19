"""What was agreed, worked out from the conversation's events by ChatReceipt's own rules.

The model labels each message (offer, counter-offer, acceptance...). These rules, not the model,
decide which price and quantity were agreed:

- An offer is agreed only when the other party accepts it. A first price, a counter-offer, a
  rejected offer and a "final price" nobody accepted are not agreements.
- A party's new offer replaces their own earlier offer on the same item.
- An acceptance applies to the offer it replies to, or else to the other party's latest open offer.
  Accepting an offer that had already been replaced or withdrawn makes the agreement unclear.
- A new price after an agreement, or a withdrawal by either side, makes the agreement unclear
  until someone accepts again. The earlier agreement is never silently kept or replaced: the record
  leaves the price empty and lists both candidates for the person to choose.
- Quantities come from what was stated ("make it 3"); a single item that was agreed with no
  quantity stated is quantity 1, marked as inferred.
- A delivery fee the seller states and nobody disputes is part of the deal; competing fees with no
  acceptance are unclear.
- The model's own reading of the final terms is compared with these results; a disagreement is
  a contradiction for a person to resolve.

Every figure is also checked against the text of the message it came from ("grounded").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.calculations import to_plain
from app.intelligence.conversation import Conversation
from app.intelligence.fields import Candidate
from app.intelligence.interpretation import (
    DELIVERY_ITEM,
    AgreementStatus,
    Deal,
    Event,
    EventType,
    Interpretation,
    Role,
)
from app.intelligence.money import counts_in, mentions_amount, mentions_percent, mentions_quantity
from app.models import Confidence

_RANK = {Confidence.LOW: 0, Confidence.MEDIUM: 1, Confidence.HIGH: 2}
_OFFERS = (EventType.OFFER, EventType.COUNTER_OFFER)


def weakest(*levels: Confidence | None) -> Confidence | None:
    known = [level for level in levels if level is not None]
    return min(known, key=_RANK.__getitem__) if known else None


class ItemStatus(str, Enum):
    AGREED = "agreed"
    OFFERED = "offered"  # one price given, never accepted
    NEGOTIATING = "negotiating"  # offers exchanged, none accepted
    UNCLEAR = "unclear"  # an agreement was changed, withdrawn or contradicted
    REMOVED = "removed"
    ENQUIRED = "enquired"  # asked about, no price


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Conflict(_Model):
    code: str
    # What it concerns: "item:<key>:unit_price", "item:<key>:quantity", "delivery_fee", "discounts",
    # "additional_fees", "payment_status", "amount_paid", "agreement".
    subject: str
    message: str
    messages: tuple[str, ...] = ()
    candidates: tuple[Candidate, ...] = ()


class PricePoint(_Model):
    message: str
    actor: str | None
    type: str
    unit_price: str | None = None
    amount: str | None = None
    quantity: str | None = None
    outcome: str  # accepted, rejected, superseded, withdrawn, open


class ItemResolution(_Model):
    item: str
    description: str
    unit: str
    status: ItemStatus
    quantity: Decimal | None = None
    quantity_basis: Literal["stated", "implicit", "none"] = "none"
    quantity_messages: tuple[str, ...] = ()
    quantity_grounded: bool | None = None
    unit_price: Decimal | None = None
    # One price for several units ("3 for 15k"): the record shows one line for the bundle.
    bundle: bool = False
    price_messages: tuple[str, ...] = ()
    price_grounded: bool | None = None
    confidence: Confidence | None = None
    condition: str = ""
    history: tuple[PricePoint, ...] = ()


class Charge(_Model):
    """A delivery fee, discount or additional fee."""

    kind: Literal["amount", "percent"]
    value: Decimal
    label: str = ""
    messages: tuple[str, ...] = ()
    confidence: Confidence = Confidence.LOW
    grounded: bool | None = None
    basis: Literal["agreed", "stated"] = "stated"


class NegotiationResult(_Model):
    items: tuple[ItemResolution, ...]
    delivery_fee: Charge | None
    discounts: tuple[Charge, ...]
    additional_fees: tuple[Charge, ...]
    cancelled: bool
    delivered: bool
    conflicts: tuple[Conflict, ...]
    notes: tuple[str, ...]


@dataclass
class _Offer:
    event: Event
    price: Decimal | None  # price of one unit
    bundle: Decimal | None  # one price for several units
    outcome: str = "open"

    @property
    def open(self) -> bool:
        return self.outcome == "open"

    @property
    def actor(self) -> str | None:
        return self.event.actor

    def same_terms(self, other: _Offer) -> bool:
        return self.price == other.price and self.bundle == other.bundle

    def value_text(self) -> str:
        value = self.price if self.price is not None else self.bundle
        return to_plain(value) if value is not None else ""


@dataclass
class _ItemTrack:
    key: str
    offers: list[_Offer] = field(default_factory=list)
    agreement: tuple[_Offer, Event] | None = None
    changed_after: list[_Offer] = field(default_factory=list)
    withdrawn_after: Event | None = None
    quantity: tuple[Decimal, Event] | None = None
    removed: bool = False
    conflicts: list[Conflict] = field(default_factory=list)


def _offer_terms(event: Event) -> tuple[Decimal | None, Decimal | None]:
    if event.type == EventType.DELIVERY_FEE:
        return event.amount if event.amount is not None else event.unit_price, None
    if event.unit_price is not None:
        return event.unit_price, None
    if event.amount is not None:
        # A price for several units together is a bundle; for one unit it's simply the price.
        if event.quantity is not None and event.quantity != 1:
            return None, event.amount
        return event.amount, None
    return None, None


def _target(offers: list[_Offer], event: Event) -> _Offer | None:
    """The offer an acceptance or rejection applies to."""
    others = [offer for offer in offers if event.actor is None or offer.actor != event.actor]
    if event.responds_to:
        replied = [offer for offer in others if offer.event.message == event.responds_to]
        if replied:
            return replied[-1]
    open_offers = [offer for offer in others if offer.open]
    return open_offers[-1] if open_offers else None


def _track(key: str, events: list[Event], *, explicit_item: bool) -> _ItemTrack:
    track = _ItemTrack(key)
    kinds_by_message: dict[str, set[EventType]] = {}
    for event in events:
        kinds_by_message.setdefault(event.message, set()).add(event.type)
    for event in events:
        kind = event.type
        # "I'll take the jacket for ₦21,000": asking for an item at a price is an offer at that price.
        priced_request = kind in (EventType.REQUEST, EventType.ITEM_ADDED) and _offer_terms(event) != (None, None)
        if kind == EventType.ITEM_ADDED:
            track.removed = False
        if kind in (EventType.REQUEST, EventType.ITEM_ADDED) and not priced_request:
            if event.quantity is not None:
                track.quantity = (event.quantity, event)
        elif kind in _OFFERS or kind == EventType.DELIVERY_FEE or priced_request:
            price, bundle = _offer_terms(event)
            if price is None and bundle is None:
                if event.quantity is not None:
                    track.quantity = (event.quantity, event)
                continue
            for offer in track.offers:
                if offer.open and event.actor is not None and offer.actor == event.actor:
                    offer.outcome = "superseded"
            offer = _Offer(event, price, bundle)
            track.offers.append(offer)
            if event.quantity is not None:
                track.quantity = (event.quantity, event)
            if track.agreement is not None and not offer.same_terms(track.agreement[0]):
                track.changed_after.append(offer)
        elif kind == EventType.ACCEPTANCE:
            target = _target(track.offers, event)
            if target is None and _offer_terms(event) != (None, None) and track.agreement is None:
                # "Okay, deal at ₦14,000" with no earlier offer in view: keep the price as this
                # side's offer, still open, so it stays on the record for a person to confirm.
                price, bundle = _offer_terms(event)
                track.offers.append(_Offer(event, price, bundle))
                if event.quantity is not None:
                    track.quantity = (event.quantity, event)
                continue
            if target is None:
                answered = kinds_by_message.get(event.responds_to or "", set())
                # "Okay Tolu, it's yours" confirms an acceptance already made; "Okay" after
                # "cancel it please" acknowledges the cancellation. Neither accepts a new price.
                confirms = track.agreement is not None and (
                    event.responds_to == track.agreement[1].message or EventType.ACCEPTANCE in answered
                )
                acknowledges = bool(answered & {EventType.CANCELLATION, EventType.WITHDRAWAL, EventType.REJECTION})
                if explicit_item and event.item is not None and not confirms and not acknowledges:
                    track.conflicts.append(
                        Conflict(
                            code="acceptance_without_offer",
                            subject=f"item:{key}:unit_price",
                            message=f"Message {event.message} accepts something, but no matching offer was found before it.",
                            messages=(event.message,),
                        )
                    )
                continue
            if target.outcome in ("superseded", "withdrawn"):
                track.conflicts.append(
                    Conflict(
                        code="accepted_replaced_offer",
                        subject=f"item:{key}:unit_price",
                        message=f"Message {event.message} accepts an offer that had already been replaced or withdrawn.",
                        messages=(target.event.message, event.message),
                    )
                )
            if event.unit_price is not None and target.price is not None and event.unit_price != target.price:
                track.conflicts.append(
                    Conflict(
                        code="accepted_price_mismatch",
                        subject=f"item:{key}:unit_price",
                        message=f"The price accepted in message {event.message} differs from the offer it replies to.",
                        messages=(target.event.message, event.message),
                        candidates=(
                            Candidate(value=to_plain(target.price), source="offer", messages=(target.event.message,)),
                            Candidate(value=to_plain(event.unit_price), source="acceptance", messages=(event.message,)),
                        ),
                    )
                )
            track.agreement = (target, event)
            target.outcome = "accepted"
            track.changed_after = []
            track.withdrawn_after = None
            if event.quantity is not None:
                track.quantity = (event.quantity, event)
        elif kind == EventType.REJECTION:
            target = _target(track.offers, event)
            if target is None:
                continue
            if track.agreement is not None and target is track.agreement[0]:
                track.withdrawn_after = event  # backing out of an accepted offer
            elif target.open:
                target.outcome = "rejected"
        elif kind == EventType.WITHDRAWAL:
            for offer in track.offers:
                if offer.open and offer.actor == event.actor:
                    offer.outcome = "withdrawn"
            if track.agreement is not None and event.actor in (track.agreement[0].actor, track.agreement[1].actor):
                track.withdrawn_after = event
        elif kind == EventType.QUANTITY_CHANGE:
            if event.quantity is not None:
                track.quantity = (event.quantity, event)
        elif kind == EventType.ITEM_REMOVED:
            track.removed = True
    return track


def _grounded_price(event: Event, value: Decimal, conversation: Conversation) -> bool:
    message = conversation.message(event.message)
    if message is None:
        return False
    text = f"{message.text}\n{message.quoted}"
    if value == 0:
        return "free" in text.lower() or mentions_amount(text, value)
    return mentions_amount(text, value)


def _grounded_quantity(event: Event, value: Decimal, conversation: Conversation) -> bool:
    message = conversation.message(event.message)
    return message is not None and mentions_quantity(f"{message.text}\n{message.quoted}", value)


def _counts_nothing(event: Event, conversation: Conversation) -> bool:
    message = conversation.message(event.message)
    return message is not None and not counts_in(f"{message.text}\n{message.quoted}")


def _history(track: _ItemTrack) -> tuple[PricePoint, ...]:
    return tuple(
        PricePoint(
            message=offer.event.message,
            actor=offer.actor,
            type=offer.event.type.value,
            unit_price=to_plain(offer.price) if offer.price is not None else None,
            amount=to_plain(offer.bundle) if offer.bundle is not None else None,
            quantity=to_plain(offer.event.quantity) if offer.event.quantity is not None else None,
            outcome=offer.outcome,
        )
        for offer in track.offers
    )


def _resolve_item(
    key: str,
    description: str,
    unit: str,
    events: list[Event],
    conversation: Conversation,
    conflicts: list[Conflict],
) -> ItemResolution:
    track = _track(key, events, explicit_item=True)
    conflicts.extend(track.conflicts)
    price_subject = f"item:{key}:unit_price"
    has_price_conflict = any(c.subject == price_subject for c in track.conflicts)

    status: ItemStatus
    offer: _Offer | None = None
    acceptance: Event | None = None
    if track.removed:
        status = ItemStatus.REMOVED
    elif track.agreement is not None and not track.changed_after and track.withdrawn_after is None:
        offer, acceptance = track.agreement
        status = ItemStatus.UNCLEAR if has_price_conflict else ItemStatus.AGREED
    elif track.agreement is not None:
        status = ItemStatus.UNCLEAR
        agreed_offer, agreed_by = track.agreement
        candidates = [
            Candidate(
                value=agreed_offer.value_text(),
                source="accepted_offer",
                messages=(agreed_offer.event.message, agreed_by.message),
                note="accepted earlier",
            )
        ]
        for later in track.changed_after:
            candidates.append(
                Candidate(value=later.value_text(), source="later_offer", messages=(later.event.message,), note="not accepted")
            )
        changed = track.withdrawn_after or (track.changed_after[0].event if track.changed_after else None)
        conflicts.append(
            Conflict(
                code="changed_after_agreement",
                subject=price_subject,
                message=(
                    f"The price was accepted in message {agreed_by.message}, then changed or withdrawn"
                    f"{f' in message {changed.message}' if changed else ''} without a new acceptance."
                ),
                messages=tuple(dict.fromkeys([agreed_by.message, *(c.messages[0] for c in candidates[1:])])),
                candidates=tuple(candidates),
            )
        )
    elif track.offers:
        # A single open offer is a quotation; anything else (counter-offers, a rejected or
        # withdrawn offer) is a negotiation that didn't end in an accepted price.
        single_open = len(track.offers) == 1 and track.offers[0].open
        status = ItemStatus.OFFERED if single_open else ItemStatus.NEGOTIATING
        if status == ItemStatus.OFFERED:
            offer = track.offers[0]
    else:
        status = ItemStatus.ENQUIRED

    unit_price = bundle_price = None
    price_messages: tuple[str, ...] = ()
    price_grounded: bool | None = None
    confidence: Confidence | None = None
    condition = ""
    if offer is not None:
        unit_price, bundle_price = offer.price, offer.bundle
        value = unit_price if unit_price is not None else bundle_price
        price_messages = tuple(dict.fromkeys([offer.event.message, *([acceptance.message] if acceptance else [])]))
        price_grounded = _grounded_price(offer.event, value, conversation) if value is not None else None
        confidence = weakest(offer.event.confidence, acceptance.confidence if acceptance else None)
        if offer.event.conditional:
            condition = offer.event.condition or "a condition stated in the conversation"
    if status == ItemStatus.NEGOTIATING:
        latest_by_actor: dict[str | None, _Offer] = {}
        for candidate in track.offers:
            latest_by_actor[candidate.actor] = candidate
        conflicts.append(
            Conflict(
                code="no_accepted_price",
                subject=price_subject,
                message="Prices were exchanged for this item but none was accepted in the conversation.",
                messages=tuple(o.event.message for o in latest_by_actor.values()),
                candidates=tuple(
                    Candidate(value=o.value_text(), source="offer", messages=(o.event.message,), note="not accepted")
                    for o in latest_by_actor.values()
                ),
            )
        )

    quantity: Decimal | None = None
    quantity_basis: Literal["stated", "implicit", "none"] = "none"
    quantity_messages: tuple[str, ...] = ()
    quantity_grounded: bool | None = None
    if track.quantity is not None:
        quantity, quantity_event = track.quantity
        quantity_basis = "stated"
        quantity_messages = (quantity_event.message,)
        quantity_grounded = _grounded_quantity(quantity_event, quantity, conversation)
        if quantity == 1 and quantity_grounded is False and _counts_nothing(quantity_event, conversation):
            # "I'll take it", "the blue gown": the model wrote down the 1 these rules would imply
            # anyway. A message that counts something else ("2 or 3 of them") stays flagged.
            quantity_basis, quantity_grounded = "implicit", None
    elif status in (ItemStatus.AGREED, ItemStatus.OFFERED) and bundle_price is None:
        quantity, quantity_basis = Decimal(1), "implicit"

    is_bundle = bundle_price is not None
    if is_bundle:
        # "3 for 15k" becomes one line, "3 × shirt", at 15000: prices are never divided.
        bundle_units = offer.event.quantity if offer is not None else None
        description = f"{to_plain(bundle_units)} × {description}" if bundle_units else description
        quantity, quantity_basis, unit_price = Decimal(1), "stated", bundle_price
        quantity_messages, quantity_grounded = price_messages, None
    return ItemResolution(
        item=key,
        description=description,
        unit=unit,
        status=status,
        quantity=quantity,
        quantity_basis=quantity_basis,
        quantity_messages=quantity_messages,
        quantity_grounded=quantity_grounded,
        unit_price=unit_price,
        bundle=is_bundle,
        price_messages=price_messages,
        price_grounded=price_grounded,
        confidence=confidence,
        condition=condition,
        history=_history(track),
    )


def _resolve_delivery(
    deal: Deal, conversation: Conversation, conflicts: list[Conflict], notes: list[str]
) -> Charge | None:
    events = [
        e
        for e in deal.events
        if e.item == DELIVERY_ITEM or e.type == EventType.DELIVERY_FEE or (e.item is None and e.type == EventType.ACCEPTANCE)
    ]
    if not any(e.type == EventType.DELIVERY_FEE or e.item == DELIVERY_ITEM for e in events):
        return None
    unpriced = [e.message for e in events if e.type == EventType.DELIVERY_FEE and e.amount is None and e.unit_price is None]
    if unpriced:
        notes.append(f"Delivery is mentioned in message {unpriced[0]}, but no delivery fee is stated.")
    normalised = [e.model_copy(update={"item": DELIVERY_ITEM}) if e.type == EventType.DELIVERY_FEE else e for e in events]
    track = _track(DELIVERY_ITEM, normalised, explicit_item=False)
    if track.agreement is not None and not track.changed_after and track.withdrawn_after is None:
        offer, acceptance = track.agreement
        basis: Literal["agreed", "stated"] = "agreed"
        messages = (offer.event.message, acceptance.message)
        confidence = weakest(offer.event.confidence, acceptance.confidence) or Confidence.LOW
    else:
        distinct = {o.price for o in track.offers if o.price is not None}
        disputed = any(o.outcome in ("rejected",) for o in track.offers) or track.withdrawn_after is not None
        if len(distinct) == 1 and not disputed and track.agreement is None:
            offer = track.offers[-1]
            basis, messages, confidence = "stated", (offer.event.message,), offer.event.confidence
        else:
            if track.offers:
                conflicts.append(
                    Conflict(
                        code="delivery_fee_unclear",
                        subject="delivery_fee",
                        message="The conversation gives more than one delivery fee and doesn't settle on one.",
                        messages=tuple(o.event.message for o in track.offers),
                        candidates=tuple(
                            Candidate(value=o.value_text(), source="stated", messages=(o.event.message,), note=o.outcome)
                            for o in track.offers
                        ),
                    )
                )
            return None
    if offer.price is None:
        return None
    return Charge(
        kind="amount",
        value=offer.price,
        label="Delivery",
        messages=tuple(dict.fromkeys(messages)),
        confidence=confidence,
        grounded=_grounded_price(offer.event, offer.price, conversation),
        basis=basis,
    )


def _resolve_charges(
    deal: Deal,
    interpretation: Interpretation,
    conversation: Conversation,
    kind: EventType,
    group: str,
    conflicts: list[Conflict],
    notes: list[str],
) -> tuple[Charge, ...]:
    noun = "discount" if kind == EventType.DISCOUNT else "fee"
    accepted: dict[str, Charge] = {}
    for event in deal.events:
        if event.type != kind:
            continue
        value = event.amount if event.amount is not None else event.unit_price
        if value is None:
            continue
        role = interpretation.role_of(event.actor)
        agreed_later = any(
            later.type == EventType.ACCEPTANCE and later.responds_to == event.message and later.actor != event.actor
            for later in deal.events
        )
        if role == Role.BUYER and not agreed_later:
            notes.append(f"The buyer asked for a {noun} in message {event.message}, but the conversation doesn't show the seller agreeing to it.")
            continue
        message = conversation.message(event.message)
        text = f"{message.text}\n{message.quoted}" if message else ""
        charge_kind: Literal["amount", "percent"] = "percent" if event.kind == "percent" else "amount"
        grounded = mentions_percent(text, value) if charge_kind == "percent" else mentions_amount(text, value)
        charge = Charge(
            kind=charge_kind,
            value=value,
            label=event.label,
            messages=(event.message,),
            confidence=event.confidence,
            grounded=grounded,
        )
        slot = " ".join(event.label.casefold().split())
        previous = accepted.get(slot)
        if previous is not None and (previous.kind, previous.value) != (charge.kind, charge.value):
            conflicts.append(
                Conflict(
                    code=f"{noun}_changed",
                    subject=group,
                    message=f"The conversation gives two different {noun}s ({_charge_text(previous)} and {_charge_text(charge)}).",
                    messages=(*previous.messages, *charge.messages),
                    candidates=(
                        Candidate(value=_charge_text(previous), source="stated", messages=previous.messages),
                        Candidate(value=_charge_text(charge), source="stated", messages=charge.messages),
                    ),
                )
            )
        if previous is None or (previous.kind, previous.value) != (charge.kind, charge.value):
            accepted[slot] = charge
    return tuple(accepted.values())


def _charge_text(charge: Charge) -> str:
    return f"{to_plain(charge.value)}%" if charge.kind == "percent" else to_plain(charge.value)


def resolve_negotiation(deal: Deal, interpretation: Interpretation, conversation: Conversation) -> NegotiationResult:
    conflicts: list[Conflict] = []
    notes: list[str] = []
    items: list[ItemResolution] = []
    for item in deal.items:
        events = [e for e in deal.events if e.item == item.key or (e.item is None and e.type in (EventType.ACCEPTANCE, EventType.REJECTION))]
        items.append(_resolve_item(item.key, item.description, item.unit, events, conversation, conflicts))

    # The model's own reading of the final terms is a cross-check, never the answer.
    for reading in deal.agreed_items:
        resolution = next((r for r in items if r.item == reading.item), None)
        if resolution is None or resolution.status != ItemStatus.AGREED:
            continue
        subject = f"item:{reading.item}"
        if reading.unit_price is not None and resolution.unit_price is not None and reading.unit_price != resolution.unit_price:
            conflicts.append(
                Conflict(
                    code="reading_differs",
                    subject=f"{subject}:unit_price",
                    message="The accepted offer and the analysis's reading of the final price disagree.",
                    messages=resolution.price_messages,
                    candidates=(
                        Candidate(value=to_plain(resolution.unit_price), source="accepted_offer", messages=resolution.price_messages),
                        Candidate(value=to_plain(reading.unit_price), source="model_reading", note=reading.note),
                    ),
                )
            )
        if (
            reading.quantity is not None
            and resolution.quantity is not None
            and not resolution.bundle
            and reading.quantity != resolution.quantity
        ):
            conflicts.append(
                Conflict(
                    code="reading_differs",
                    subject=f"{subject}:quantity",
                    message="The quantities in the conversation and the analysis's reading of the final quantity disagree.",
                    messages=resolution.quantity_messages,
                    candidates=(
                        Candidate(value=to_plain(resolution.quantity), source=resolution.quantity_basis, messages=resolution.quantity_messages),
                        Candidate(value=to_plain(reading.quantity), source="model_reading", note=reading.note),
                    ),
                )
            )

    delivery = _resolve_delivery(deal, conversation, conflicts, notes)
    discounts = _resolve_charges(deal, interpretation, conversation, EventType.DISCOUNT, "discounts", conflicts, notes)
    fees = _resolve_charges(deal, interpretation, conversation, EventType.ADDITIONAL_FEE, "additional_fees", conflicts, notes)

    last_acceptance = max((e.seq for e in deal.events if e.type == EventType.ACCEPTANCE), default=0)
    cancelled = any(e.type == EventType.CANCELLATION and e.seq >= last_acceptance for e in deal.events)
    delivered = any(e.type == EventType.DELIVERY_CONFIRMATION for e in deal.events)
    return NegotiationResult(
        items=tuple(items),
        delivery_fee=delivery,
        discounts=discounts,
        additional_fees=fees,
        cancelled=cancelled,
        delivered=delivered,
        conflicts=tuple(conflicts),
        notes=tuple(dict.fromkeys(notes)),
    )


def agreement_status(
    result: NegotiationResult, *, fully_paid_and_confirmed: bool
) -> AgreementStatus:
    """The state of the deal, from ChatReceipt's rules."""
    if result.cancelled:
        return AgreementStatus.CANCELLED
    active = [item for item in result.items if item.status != ItemStatus.REMOVED]
    if not active:
        return AgreementStatus.CANCELLED if result.items else AgreementStatus.ENQUIRY
    if any(item.status == ItemStatus.UNCLEAR for item in active):
        return AgreementStatus.AGREEMENT_UNCLEAR
    if any(item.status == ItemStatus.AGREED for item in active):
        if fully_paid_and_confirmed and result.delivered:
            return AgreementStatus.COMPLETED
        return AgreementStatus.AGREEMENT_REACHED
    if any(item.status == ItemStatus.NEGOTIATING for item in active):
        return AgreementStatus.NEGOTIATION
    if any(item.status == ItemStatus.OFFERED for item in active):
        return AgreementStatus.QUOTATION
    return AgreementStatus.ENQUIRY
