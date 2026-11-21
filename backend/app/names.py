"""Comparing and cleaning people's names, the same way in every channel.

Names are only compared to recognise the same label (the name at the top of a chat, an export's
sender label) or to spot a contradiction. Two different people can share a name, so a match on a
name alone never merges records: stable identifiers (phone numbers, WhatsApp IDs) do that.
"""

from __future__ import annotations

import re

_PHONE_LABEL = re.compile(r"^\+?[\d\s\-().]{7,22}$")


def name_key(name: str) -> str:
    """A name reduced to its letters and digits, in lower case."""
    return "".join(character for character in name.casefold() if character.isalnum())


def same_name(first: str, second: str) -> bool:
    """Two names that differ only in case, spacing, punctuation or emoji."""
    key = name_key(first)
    return bool(key) and key == name_key(second)


def clean_name(text: str, limit: int = 120) -> str:
    """One line, no control characters, no surrounding quotes or WhatsApp formatting marks."""
    name = " ".join("".join(character for character in text if character.isprintable()).split())
    return name.strip(" \"'“”‘’*_~").strip()[:limit]


def phone_in_label(label: str) -> str:
    """The digits of a label that is a phone number ("+234 803 123 4567"), otherwise ""."""
    if not _PHONE_LABEL.match(label.strip()):
        return ""
    digits = re.sub(r"\D", "", label)
    return digits if 7 <= len(digits) <= 15 else ""
