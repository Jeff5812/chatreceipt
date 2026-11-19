"""Check how the real AI reads typed WhatsApp corrections.

usage (from backend/): python -m evals.whatsapp_corrections [--delay SECONDS]

Each case is a draft, the question ChatReceipt last asked, and what the person typed, taken from the
first real phone test and the corrections people commonly send. The AI's answer goes through the same
parsing as the live channel, and a case passes when it reports every expected change and the right
intent. It calls the configured AI provider and uses quota (one request per case).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from typing import Any

from app.calculations import calculate
from app.config import get_settings
from app.extraction.provider import AIProviderError
from app.extraction.providers import create_provider
from app.models import TransactionDraft
from app.whatsapp.corrections import read_amount
from app.whatsapp.understanding import understand

TRANSFER = {
    "currency": "NGN", "seller": "Bola Studio", "buyer": "Emeka Daniel Obi", "transaction_date": "2026-11-10",
    "line_items": [{"id": "item-1", "description": "Transfer", "quantity": "1", "unit_price": "20000"}],
    "amount_paid": "20000", "payment_status": "paid",
}
ADVANCE = {**TRANSFER, "line_items": [{"id": "item-1", "description": "Advance work", "quantity": "1", "unit_price": ""}], "payment_status": "partially_paid"}
TWO_ITEMS = {
    **TRANSFER, "amount_paid": "", "payment_status": "not_specified",
    "line_items": [{"id": "item-1", "description": "Bag", "quantity": "1", "unit_price": "15000"}, {"id": "item-2", "description": "Belt", "quantity": "1", "unit_price": ""}],
}
PRICE_QUESTION = "What's the price of one “Transfer”?"
FULL_PRICE_QUESTION = "What was the full price of “Advance work”? ₦20,000.00 has been paid so far."
CONFIRM = "Is this correct?"

# (name, draft, question, earlier messages, message, expected intent, expected changes)
# An expected change is (field, value): a value is matched as an amount when it's a number, and
# as a case-insensitive part of the text otherwise. A field written "a|b" accepts either.
CASES: list[tuple[str, dict[str, Any], str, list[str], str, str, list[tuple[str, str]]]] = [
    ("advance and remaining", TRANSFER, CONFIRM, [], "It's an advance work. 40k was still remaining", "correct", [("item_description", "advance"), ("balance", "40000")]),
    ("20k for advance work", TRANSFER, PRICE_QUESTION, [], "It's a 20k for advance work", "correct", [("item_description", "advance")]),
    ("it's 20k", TRANSFER, PRICE_QUESTION, [], "It's 20k", "correct", [("item_price|total", "20000")]),
    ("20k is the price", TRANSFER, PRICE_QUESTION, [], "20k is the price", "correct", [("item_price|total", "20000")]),
    ("three figures", ADVANCE, FULL_PRICE_QUESTION, [], "The full work was 60k. He paid 20k and still owes 40k.", "correct", [("total|item_price", "60000"), ("amount_paid", "20000"), ("balance", "40000")]),
    ("he paid", TWO_ITEMS, CONFIRM, [], "He paid 20k.", "correct", [("amount_paid", "20000")]),
    ("still owes", ADVANCE, FULL_PRICE_QUESTION, [], "He still owes me 40k.", "correct", [("balance", "40000")]),
    ("that was an advance", TRANSFER, CONFIRM, [], "That was an advance.", "correct", [("payment_status", "partially_paid")]),
    ("that's the total", ADVANCE, FULL_PRICE_QUESTION, ["60k"], "That's the total amount.", "correct", [("total|item_price", "60000")]),
    ("change the amount", TRANSFER, CONFIRM, [], "Change the amount to 30k.", "correct", [("item_price|total", "30000")]),
    ("remove that item", TWO_ITEMS, "What's the price of one “Belt”?", [], "Remove that item.", "correct", [("remove_item", "item-2")]),
    ("several at once", TRANSFER, CONFIRM, [], "Change the buyer to Emeka, the total to 60k, and mark 20k as paid.", "correct", [("buyer", "emeka"), ("total|item_price", "60000"), ("amount_paid", "20000")]),
    ("pidgin balance", ADVANCE, FULL_PRICE_QUESTION, [], "Na 60k be the full money, him don pay 20k remain 40k", "correct", [("total|item_price", "60000"), ("balance", "40000")]),
    ("try again", TRANSFER, CONFIRM, [], "can you try again", "read_again", []),
    ("check am again", TRANSFER, CONFIRM, [], "abeg check am again", "read_again", []),
    ("show everything", TRANSFER, CONFIRM, [], "show me the whole record", "show_record", []),
]


def _matches(change: Any, field: str, value: str) -> bool:
    if change.field not in field.split("|"):
        return False
    if field == "remove_item":
        return change.item_id == value
    wanted = read_amount(value) if value.replace(".", "").isdigit() else None
    if wanted is not None:
        return read_amount(change.value) == wanted or read_amount(change.price) == wanted
    return value.lower() in change.value.lower()


async def run(delay: float) -> int:
    settings = get_settings()
    model = create_provider(settings)
    if not model.configured:
        print("No AI provider is configured.")
        return 2
    used = (settings.gemini_light_model or settings.gemini_model) if model.name == "gemini" else settings.extraction_model
    print(f"Checking {len(CASES)} typed corrections with {model.name} ({used})\n")
    passed = 0
    try:
        for name, values, question, earlier, message, intent, expected in CASES:
            draft = TransactionDraft.model_validate(values)
            calculate(draft)
            started = time.perf_counter()
            try:
                answer = await understand(model, draft, checks=[], question=question, earlier=earlier, message=message)
            except AIProviderError as exc:
                print(f"FAIL  {name:24} provider error: {exc.kind.value}")
                continue
            seconds = time.perf_counter() - started
            problems = [] if answer.intent == intent else [f"intent {answer.intent}, expected {intent}"]
            problems += [f"missing {field} = {value}" for field, value in expected if not any(_matches(c, field, value) for c in answer.changes)]
            got = ", ".join(f"{c.field}={c.value or c.item_id}" for c in answer.changes) or "no changes"
            passed += not problems
            print(f"{'PASS' if not problems else 'FAIL'}  {name:24} {seconds:4.1f}s  {message!r}\n        -> {answer.intent}: {got}")
            for problem in problems:
                print(f"        - {problem}")
            await asyncio.sleep(delay)
    finally:
        await model.aclose()
    print(f"\n{passed}/{len(CASES)} corrections understood")
    return 0 if passed == len(CASES) else 1


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--delay", type=float, default=2.0, help="Seconds between requests, to stay under rate limits.")
    raise SystemExit(asyncio.run(run(parser.parse_args().delay)))


if __name__ == "__main__":
    main()
