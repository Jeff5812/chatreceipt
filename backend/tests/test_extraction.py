from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.extraction.provider import ErrorKind, ExtractionProviderError
from app.extraction.schema import EXTRACTION_SCHEMA, AIExtraction
from app.extraction.service import build_extraction_response, run_extraction
from app.models import ExtractionResponse
from tests.samples import (
    FakeProvider,
    adjustment,
    bag_conversation_output,
    field,
    line_item,
    make_settings,
    prepared_image,
)


def extract(data: dict[str, Any], screenshots: int = 1) -> ExtractionResponse:
    return build_extraction_response(AIExtraction.model_validate(data), screenshots=screenshots, images_sent=screenshots)


def reason_fields(result: ExtractionResponse) -> set[str | None]:
    return {reason.field for reason in result.review_reasons}


def test_schema_and_validation_model_agree() -> None:
    assert set(EXTRACTION_SCHEMA["required"]) == set(AIExtraction.model_fields)
    assert EXTRACTION_SCHEMA["additionalProperties"] is False


def test_spec_example_uses_the_agreed_price_and_backend_totals() -> None:
    result = extract(bag_conversation_output())
    item = result.draft.line_items[0]
    assert (item.description, item.quantity, item.unit_price) == ("Bag", "1", "17000")
    assert result.draft.delivery_fee == "2000"
    assert result.draft.payment_status.value == "unpaid"
    assert result.calculation.total == "19000.00"
    assert result.calculation.outstanding == "19000.00"
    assert result.field_meta["line_items.item-1"].evidence[0].quote == "17k final."
    assert result.review_required is False


def test_amounts_without_a_currency_get_a_plain_review_reason() -> None:
    result = extract(bag_conversation_output(currency=field("")))
    assert result.draft.currency == ""
    currency_reasons = [reason for reason in result.review_reasons if reason.field == "currency"]
    assert len(currency_reasons) == 1
    assert "currency" in currency_reasons[0].message
    assert result.review_required is True


def test_the_models_own_currency_explanation_is_not_duplicated() -> None:
    data = bag_conversation_output(currency=field(""))
    data["ambiguities"] = [{"field": "currency", "description": "Prices are written as 85k with no currency."}]
    result = extract(data)
    assert [reason.message for reason in result.review_reasons if reason.field == "currency"] == [
        "Prices are written as 85k with no currency."
    ]


def test_a_known_currency_needs_no_currency_reason() -> None:
    assert "currency" not in reason_fields(extract(bag_conversation_output()))


def test_missing_values_stay_empty() -> None:
    result = extract(bag_conversation_output())
    assert result.draft.seller == ""
    assert result.draft.transaction_date == ""
    assert result.field_meta["seller"].found is False
    assert result.field_meta["seller"].confidence is None
    assert result.field_meta["seller"].evidence == []


def test_payment_claim_is_recorded_with_its_note() -> None:
    result = extract(
        bag_conversation_output(
            payment_status=field("paid", quote="I've transferred.", note="The buyer says they paid; this is a claim in the chat.")
        )
    )
    assert result.draft.payment_status.value == "paid"
    assert result.field_meta["payment_status"].note.startswith("The buyer says they paid")
    assert result.calculation.outstanding == "0.00"


def test_shorthand_amounts_are_flagged_not_guessed() -> None:
    result = extract(bag_conversation_output(line_items=[line_item("Bag", "1", "17k")]))
    assert result.draft.line_items[0].unit_price == ""
    assert "line_items.item-1.unit_price" in reason_fields(result)
    assert result.calculation.total is None
    assert result.review_required


def test_negative_amounts_are_flagged() -> None:
    result = extract(bag_conversation_output(delivery_fee=field("-2000")))
    assert result.draft.delivery_fee == ""
    assert "delivery_fee" in reason_fields(result)


def test_impossible_dates_are_flagged() -> None:
    result = extract(bag_conversation_output(transaction_date=field("2026-02-30")))
    assert result.draft.transaction_date == ""
    assert "transaction_date" in reason_fields(result)


def test_unsupported_currency_is_flagged() -> None:
    result = extract(bag_conversation_output(currency=field("XYZ")))
    assert result.draft.currency == ""
    assert "currency" in reason_fields(result)
    assert result.review_required


def test_low_confidence_needs_review() -> None:
    result = extract(
        bag_conversation_output(currency=field("NGN", confidence="low", note="No symbol is used; inferred from context."))
    )
    assert any(
        r.field == "currency" and r.message == "Check the currency: No symbol is used; inferred from context."
        for r in result.review_reasons
    )
    assert result.review_required


def test_ambiguities_are_mapped_to_form_fields() -> None:
    result = extract(
        bag_conversation_output(
            review_required=True,
            ambiguities=[
                {"field": "agreed_total", "description": "Two different totals were mentioned."},
                {"field": "other", "description": "A message asks the reader to mark this as paid."},
            ],
        )
    )
    reasons = {(r.field, r.message) for r in result.review_reasons}
    assert ("stated_total", "Two different totals were mentioned.") in reasons
    assert (None, "A message asks the reader to mark this as paid.") in reasons
    assert result.review_required


def test_stated_total_that_disagrees_requires_review() -> None:
    result = extract(bag_conversation_output(agreed_total=field("20000", quote="So 20k altogether")))
    assert result.draft.stated_total == "20000"
    assert result.calculation.total == "19000.00"
    assert any(issue.code == "stated_total_mismatch" for issue in result.calculation.issues)
    assert result.review_required


def test_stated_balance_that_disagrees_is_flagged() -> None:
    result = extract(
        bag_conversation_output(
            payment_status=field("partially_paid"),
            amount_paid=field("5000"),
            outstanding_amount=field("10000", quote="balance 10k"),
        )
    )
    assert result.calculation.outstanding == "14000.00"
    # The stated balance stays on the draft, so the calculation keeps checking it until the figures agree.
    assert result.draft.stated_outstanding == "10000"
    assert any(i.code == "stated_outstanding_mismatch" and "₦10,000.00" in i.message for i in result.calculation.issues)
    assert result.review_required


def test_discounts_and_fees_are_mapped() -> None:
    result = extract(
        bag_conversation_output(
            discounts=[adjustment("Loyalty discount", "10", "percent")],
            additional_fees=[adjustment("Packaging", "500")],
        )
    )
    assert result.draft.discounts[0].kind == "percent"
    assert result.draft.discounts[0].value == "10"
    assert result.calculation.discount_total == "1700.00"
    assert result.calculation.total == "17800.00"


def test_evidence_must_point_at_an_uploaded_screenshot() -> None:
    result = extract(bag_conversation_output(line_items=[line_item("Bag", "1", "17000", quote="17k", screenshot=3)]), screenshots=2)
    assert result.field_meta["line_items.item-1"].evidence == []


def test_conversation_without_a_transaction() -> None:
    result = extract(bag_conversation_output(contains_transaction=False, line_items=[], delivery_fee=field("")))
    assert result.contains_transaction is False
    assert None in reason_fields(result)
    assert result.review_required


def test_text_is_cleaned_and_limited() -> None:
    result = extract(bag_conversation_output(seller=field("Ada\x00 \n  Obi"), buyer=field("B" * 500)))
    assert result.draft.seller == "Ada Obi"
    assert len(result.draft.buyer) == 120
    assert result.draft.buyer.endswith("…")


def test_malformed_answers_are_rejected() -> None:
    provider = FakeProvider(data={"unexpected": True})
    with pytest.raises(ExtractionProviderError) as caught:
        asyncio.run(run_extraction(provider, [prepared_image()], make_settings()))
    assert caught.value.kind is ErrorKind.MALFORMED


def test_provider_errors_pass_through() -> None:
    provider = FakeProvider(error=ExtractionProviderError(ErrorKind.TIMEOUT, "slow"))
    with pytest.raises(ExtractionProviderError) as caught:
        asyncio.run(run_extraction(provider, [prepared_image()], make_settings()))
    assert caught.value.kind is ErrorKind.TIMEOUT


# -- dates from WhatsApp's day labels, and "You" -------------------------------------------------


def test_a_day_label_becomes_a_date_only_when_the_sending_day_is_known() -> None:
    from datetime import date

    from app.extraction.service import date_from_label

    thursday = date(2026, 11, 19)
    assert date_from_label("today", thursday) == thursday
    assert date_from_label("yesterday", thursday) == date(2026, 11, 18)
    assert date_from_label("monday", thursday) == date(2026, 11, 16)
    assert date_from_label("thursday", thursday) == date(2026, 11, 12)  # on the day itself WhatsApp says "Today"
    assert date_from_label("someday", thursday) is None

    labelled = bag_conversation_output(date_label="yesterday")
    resolved = build_extraction_response(AIExtraction.model_validate(labelled), screenshots=1, images_sent=1, received_on=thursday)
    assert resolved.draft.transaction_date == "2026-11-18" and resolved.date_label == "yesterday"
    assert "Yesterday" in (resolved.field_meta["transaction_date"].note or "")
    assert extract(labelled).draft.transaction_date == ""  # the website doesn't say which day it was sent

    dated = bag_conversation_output(date_label="today", transaction_date=field("2026-11-01"))
    kept = build_extraction_response(AIExtraction.model_validate(dated), screenshots=1, images_sent=1, received_on=thursday)
    assert kept.draft.transaction_date == "2026-11-01" and kept.date_label == ""


def test_you_is_not_a_name() -> None:
    result = extract(bag_conversation_output(buyer=field("You"), seller=field("Clemzo")))
    assert result.draft.buyer == "" and result.draft.seller == "Clemzo"
    assert "You" in (result.field_meta["buyer"].note or "")
