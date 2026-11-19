"""Find the amounts, percentages and quantities a message actually mentions.

Used to check the AI's figures against the text it cites: an offer of 23000 must come from a
message that says "₦23,000", "23k" or "23000". A figure that isn't in its message is flagged for a
person to check, which catches misread digits, wrong shorthand expansion and invented values.

Shorthand: "k" and "thousand" multiply by 1,000, "bn" and "billion" by 1,000,000,000. "m" is
ambiguous (million, metres, minutes), so both readings count.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

_NUMBER = re.compile(
    r"(?<![\d.,])(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?:\s?(?P<suffix>k|thousand|bn|billion|b|mil|million|m)(?![a-z]))?",
    re.IGNORECASE,
)
_PERCENT = re.compile(r"(?<![\d.,])(\d+(?:\.\d+)?)\s?(?:%|percent|per cent)", re.IGNORECASE)
_WORDS = {
    "a": 1, "an": 1, "one": 1, "single": 1, "two": 2, "pair": 2, "both": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "dozen": 12, "half": Decimal("0.5"),
}
_MULTIPLIERS = {"k": 1_000, "thousand": 1_000, "bn": 10**9, "billion": 10**9, "b": 10**9}
_AMBIGUOUS_MILLION = {"m", "mil", "million"}


def _decimal(text: str) -> Decimal | None:
    try:
        return Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None


def amounts_in(text: str) -> set[Decimal]:
    """Every amount the text could mean, in the currency's main unit."""
    found: set[Decimal] = set()
    for match in _NUMBER.finditer(text or ""):
        value = _decimal(match["num"])
        if value is None:
            continue
        suffix = (match["suffix"] or "").lower()
        if suffix in _MULTIPLIERS:
            found.add(value * _MULTIPLIERS[suffix])
        elif suffix in _AMBIGUOUS_MILLION:
            found.update({value, value * 1_000_000})
        else:
            found.add(value)
    return {value.normalize() for value in found}


def percents_in(text: str) -> set[Decimal]:
    return {Decimal(match[1]).normalize() for match in _PERCENT.finditer(text or "")}


def quantities_in(text: str) -> set[Decimal]:
    """Plain numbers and number words ("two", "a dozen") the text mentions."""
    found = set(amounts_in(text))
    for word in re.findall(r"[a-z]+", (text or "").lower()):
        if word in _WORDS:
            found.add(Decimal(_WORDS[word]))
    return {value.normalize() for value in found}


# Figures that are not counts of items: money, sizes, clock times and dates.
_MONEY = re.compile(
    r"(?:[₦$£€#]|\b(?:NGN|USD|GBP|EUR|N)(?=\s?\d))\s?\d[\d,]*(?:\.\d+)?(?:\s?(?:k|thousand|m|million|bn|billion)\b)?"
    r"|\b\d[\d,]*(?:\.\d+)?\s?(?:k|thousand|naira|dollars?|pounds?|euros?)\b",
    re.IGNORECASE,
)
_NOT_COUNTS = re.compile(r"\bsize\s?\d+\b|\b\d{1,2}[:.]\d{2}\b|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b", re.IGNORECASE)


def counts_in(text: str) -> set[Decimal]:
    """The quantities a message could mean once money amounts, sizes, times and dates are taken out.

    Empty for "I'll take the blue gown for ₦15,000" or "Me, I'll take it": the message names one
    thing without counting it.
    """
    return quantities_in(_NOT_COUNTS.sub(" ", _MONEY.sub(" ", text or "")))


def mentions_amount(text: str, value: Decimal) -> bool:
    return value.normalize() in amounts_in(text)


def mentions_percent(text: str, value: Decimal) -> bool:
    return value.normalize() in percents_in(text) or value.normalize() in amounts_in(text)


def mentions_quantity(text: str, value: Decimal) -> bool:
    return value.normalize() in quantities_in(text)
