from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.calculations import calculate
from app.currencies import CURRENCIES, format_money
from app.models import Calculation, TransactionDraft


def make_draft(items: list[tuple[str, str, str]] | None = None, **fields: object) -> TransactionDraft:
    if items is None:
        items = [("Bag", "1", "17000")]
    data: dict[str, object] = {
        "currency": "NGN",
        "transaction_date": "2026-11-01",
        "line_items": [
            {"id": f"i{n}", "description": d, "quantity": q, "unit_price": p}
            for n, (d, q, p) in enumerate(items, start=1)
        ],
    }
    data.update(fields)
    return TransactionDraft.model_validate(data)


def errors(calc: Calculation) -> set[str]:
    return {issue.code for issue in calc.issues if issue.severity == "error"}


def warnings(calc: Calculation) -> set[str]:
    return {issue.code for issue in calc.issues if issue.severity == "warning"}


def test_spec_example_negotiated_price_with_delivery_unpaid() -> None:
    calc = calculate(make_draft(delivery_fee="2000", payment_status="unpaid"))
    assert calc.items_subtotal == "17000.00"
    assert calc.delivery_fee == "2000.00"
    assert calc.total == "19000.00"
    assert calc.amount_paid == "0.00"
    assert calc.outstanding == "19000.00"
    assert calc.can_export
    assert not errors(calc)


def test_multiple_line_items() -> None:
    calc = calculate(make_draft([("Shirt", "2", "2500"), ("Cap", "3", "1000.50")]))
    assert [line.amount for line in calc.line_items] == ["5000.00", "3001.50"]
    assert calc.total == "8001.50"


def test_fractional_quantities_round_half_up() -> None:
    assert calculate(make_draft([("Fabric (yards)", "2.5", "333.33")])).line_items[0].amount == "833.33"
    assert calculate(make_draft([("Thread", "1.5", "0.01")])).line_items[0].amount == "0.02"


def test_discounts_fees_and_delivery() -> None:
    calc = calculate(
        make_draft(
            [("Phone case", "4", "5000")],
            discounts=[{"id": "d1", "label": "Loyalty", "kind": "amount", "value": "1000"}],
            additional_fees=[{"id": "f1", "label": "Packaging", "kind": "amount", "value": "500"}],
            delivery_fee="1500",
        )
    )
    assert calc.items_subtotal == "20000.00"
    assert calc.discount_total == "1000.00"
    assert calc.fee_total == "500.00"
    assert calc.total == "21000.00"


def test_percent_discount_applies_to_items_subtotal_and_rounds() -> None:
    calc = calculate(make_draft([("Bag", "1", "10000")], discounts=[{"id": "d1", "kind": "percent", "value": "10"}]))
    assert calc.discounts[0].amount == "1000.00"
    assert calc.total == "9000.00"
    calc = calculate(make_draft([("Item", "1", "333.33")], discounts=[{"id": "d1", "kind": "percent", "value": "7.5"}]))
    assert calc.discounts[0].amount == "25.00"  # 24.99975 rounds half-up


def test_percent_fee() -> None:
    calc = calculate(make_draft([("Item", "1", "10000")], additional_fees=[{"id": "f1", "kind": "percent", "value": "7.5"}]))
    assert calc.fee_total == "750.00"
    assert calc.total == "10750.00"


def test_discount_larger_than_items_is_an_error() -> None:
    calc = calculate(make_draft([("Bag", "1", "1000")], discounts=[{"id": "d1", "kind": "amount", "value": "1500"}]))
    assert "negative_total" in errors(calc)
    assert calc.total is None
    assert not calc.can_export


@pytest.mark.parametrize("percent", ["0", "101", "-5", "12.345"])
def test_invalid_percentages(percent: str) -> None:
    calc = calculate(make_draft(discounts=[{"id": "d1", "kind": "percent", "value": percent}]))
    assert errors(calc) & {"invalid_percent", "too_many_decimals"}
    assert calc.total is None


@pytest.mark.parametrize(
    ("currency", "price", "total"),
    [("NGN", "17000.50", "17000.50"), ("USD", "19.99", "19.99"), ("JPY", "1500", "1500"), ("XOF", "25000", "25000"), ("KWD", "1.125", "1.125")],
)
def test_currency_precision(currency: str, price: str, total: str) -> None:
    calc = calculate(make_draft([("Item", "1", price)], currency=currency))
    assert calc.total == total
    assert calc.minor_units == CURRENCIES[currency].minor_units


@pytest.mark.parametrize(("currency", "price"), [("NGN", "100.005"), ("JPY", "1500.5"), ("KWD", "1.1255")])
def test_extra_decimals_are_an_error_not_silently_rounded(currency: str, price: str) -> None:
    calc = calculate(make_draft([("Item", "1", price)], currency=currency))
    assert "too_many_decimals" in errors(calc)
    assert calc.total is None


def test_grouped_numbers_are_accepted_but_ambiguous_formats_are_not() -> None:
    assert calculate(make_draft([("Bag", "1", "17,000.50")])).total == "17000.50"
    assert "invalid_number" in errors(calculate(make_draft([("Bag", "1", "1,5")])))
    assert "invalid_number" in errors(calculate(make_draft([("Bag", "1", "17k")])))


@pytest.mark.parametrize(
    ("quantity", "price", "code"),
    [
        ("-1", "100", "non_positive_quantity"),
        ("0", "100", "non_positive_quantity"),
        ("1.2345", "100", "too_many_decimals"),
        ("", "100", "missing_value"),
        ("1", "-100", "negative_value"),
        ("1", "", "missing_value"),
    ],
)
def test_invalid_line_items(quantity: str, price: str, code: str) -> None:
    calc = calculate(make_draft([("Bag", quantity, price)]))
    assert code in errors(calc)
    assert not calc.can_export


def test_negative_delivery_fee_is_rejected() -> None:
    calc = calculate(make_draft(delivery_fee="-500"))
    assert "negative_value" in errors(calc)
    assert calc.total is None


def test_items_need_a_description() -> None:
    assert "missing_description" in errors(calculate(make_draft([("  ", "1", "100")])))


def test_at_least_one_item_is_required() -> None:
    assert "no_items" in errors(calculate(make_draft([])))


def test_missing_price_leaves_total_unknown_instead_of_partial() -> None:
    calc = calculate(make_draft([("Bag", "1", "17000"), ("Shoes", "1", "")]))
    assert calc.line_items[0].amount == "17000.00"
    assert calc.line_items[1].amount is None
    assert calc.items_subtotal is None
    assert calc.total is None


def test_paid_without_amount_means_fully_paid() -> None:
    calc = calculate(make_draft(payment_status="paid"))
    assert calc.amount_paid == "17000.00"
    assert calc.outstanding == "0.00"


def test_full_payment_with_amount_leaves_zero_outstanding() -> None:
    calc = calculate(make_draft(payment_status="paid", amount_paid="17000"))
    assert calc.outstanding == "0.00"
    assert calc.can_export


def test_partial_payment() -> None:
    calc = calculate(make_draft(payment_status="partially_paid", amount_paid="5000"))
    assert calc.amount_paid == "5000.00"
    assert calc.outstanding == "12000.00"


@pytest.mark.parametrize(
    ("status", "paid", "code"),
    [
        ("paid", "10000", "paid_amount_mismatch"),
        ("unpaid", "5000", "unpaid_with_payment"),
        ("partially_paid", "", "missing_part_payment"),
        ("partially_paid", "17000", "invalid_part_payment"),
        ("partially_paid", "0", "invalid_part_payment"),
        ("not_specified", "20000", "overpaid"),
    ],
)
def test_payment_conflicts_are_errors(status: str, paid: str, code: str) -> None:
    calc = calculate(make_draft(payment_status=status, amount_paid=paid))
    assert code in errors(calc)
    assert not calc.can_export


def test_amount_paid_without_status_asks_for_a_status() -> None:
    calc = calculate(make_draft(amount_paid="5000"))
    assert "status_not_set" in warnings(calc)
    assert calc.outstanding == "12000.00"


def test_unknown_payment_leaves_outstanding_unknown() -> None:
    calc = calculate(make_draft())
    assert calc.amount_paid is None
    assert calc.outstanding is None
    assert calc.can_export


def test_stated_total_is_cross_checked() -> None:
    mismatch = calculate(make_draft(delivery_fee="2000", stated_total="20000"))
    assert "stated_total_mismatch" in warnings(mismatch)
    assert mismatch.total == "19000.00"  # the stated figure never replaces the calculation
    assert "stated_total_mismatch" not in warnings(calculate(make_draft(delivery_fee="2000", stated_total="19000")))


def test_duplicate_items_are_flagged() -> None:
    calc = calculate(make_draft([("Bag", "1", "17000"), ("bag ", "1", "17000")]))
    duplicates = [issue for issue in calc.issues if issue.code == "possible_duplicate"]
    assert [issue.field for issue in duplicates] == ["line_items.i2"]
    assert calc.total == "34000.00"


@pytest.mark.parametrize(("value", "code"), [("2026-02-30", "invalid_date"), ("26/02/2026", "invalid_date"), ("1980-01-01", "date_out_of_range")])
def test_invalid_dates(value: str, code: str) -> None:
    assert code in errors(calculate(make_draft(transaction_date=value)))


def test_date_warnings() -> None:
    assert "missing_date" in warnings(calculate(make_draft(transaction_date="")))
    assert "due_before_transaction" in warnings(calculate(make_draft(due_date="2026-10-01")))


def test_currency_must_be_chosen_and_supported() -> None:
    assert "missing_currency" in errors(calculate(make_draft(currency="")))
    unsupported = calculate(make_draft(currency="XYZ"))
    assert "unsupported_currency" in errors(unsupported)
    assert unsupported.total is None


def test_row_ids_must_be_unique() -> None:
    with pytest.raises(ValidationError):
        TransactionDraft.model_validate(
            {"line_items": [{"id": "a", "description": "x"}], "discounts": [{"id": "a", "value": "1"}]}
        )


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        TransactionDraft.model_validate({"currency": "NGN", "verified": True})


def test_format_money() -> None:
    assert format_money(Decimal("19000"), CURRENCIES["NGN"]) == "₦19,000.00"
    assert format_money(Decimal("2500"), CURRENCIES["KES"]) == "KSh 2,500.00"
    assert format_money(Decimal("1500"), CURRENCIES["JPY"]) == "¥1,500"
    assert format_money(Decimal("1.125"), CURRENCIES["KWD"]) == "KD 1.125"
