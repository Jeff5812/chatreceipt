"""Currency configuration.

``minor_units`` is the ISO 4217 exponent: how many decimal places an amount can have. Adding a
currency here makes it available to the API, the extraction schema and the interface.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Currency:
    code: str
    name: str
    symbol: str
    minor_units: int

    @property
    def quantum(self) -> Decimal:
        """The smallest amount the currency can express, e.g. Decimal('0.01') for NGN."""
        return Decimal(1).scaleb(-self.minor_units)


_ALL = (
    Currency("NGN", "Nigerian naira", "₦", 2),
    Currency("USD", "US dollar", "$", 2),
    Currency("GBP", "British pound", "£", 2),
    Currency("EUR", "Euro", "€", 2),
    Currency("GHS", "Ghanaian cedi", "GH₵", 2),
    Currency("KES", "Kenyan shilling", "KSh", 2),
    Currency("ZAR", "South African rand", "R", 2),
    Currency("XOF", "West African CFA franc", "CFA", 0),
    Currency("XAF", "Central African CFA franc", "FCFA", 0),
    Currency("EGP", "Egyptian pound", "E£", 2),
    Currency("CAD", "Canadian dollar", "CA$", 2),
    Currency("AUD", "Australian dollar", "A$", 2),
    Currency("AED", "UAE dirham", "AED", 2),
    Currency("INR", "Indian rupee", "₹", 2),
    Currency("CNY", "Chinese yuan", "CN¥", 2),
    Currency("JPY", "Japanese yen", "¥", 0),
    Currency("KWD", "Kuwaiti dinar", "KD", 3),
)

CURRENCIES: dict[str, Currency] = {currency.code: currency for currency in _ALL}
CURRENCY_CODES: tuple[str, ...] = tuple(CURRENCIES)
DEFAULT_CURRENCY = "NGN"


def get_currency(code: str | None) -> Currency | None:
    if not code:
        return None
    return CURRENCIES.get(code.strip().upper())


def format_money(amount: Decimal, currency: Currency) -> str:
    """Format an amount for people, e.g. ₦17,000.00 or KSh 2,500.00."""
    quantized = amount.quantize(currency.quantum)
    sign = "-" if quantized < 0 else ""
    digits = f"{abs(quantized):,.{currency.minor_units}f}"
    separator = " " if currency.symbol[-1].isalpha() else ""
    return f"{sign}{currency.symbol}{separator}{digits}"
