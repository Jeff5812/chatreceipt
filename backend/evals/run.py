"""Run the evaluation set through the real extraction pipeline and score the results.

usage (from backend/): python -m evals.run [--case ID ...] [--delay SECONDS]
Screenshots come from evals/screenshots (run `python -m evals.render` first). Each case goes
through upload preparation, the configured AI provider, schema validation and the backend's own
calculations, exactly like POST /api/extractions. This calls the real provider and uses quota.
Results are written to evals/results/latest.json.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.extraction.provider import ExtractionProviderError
from app.extraction.providers import create_provider
from app.extraction.service import run_extraction
from app.models import ExtractionResponse
from app.uploads import UploadedFile, prepare_screenshots
from evals.render import SCREENSHOTS, load_cases

HERE = Path(__file__).resolve().parent


def _number(value: Any) -> Decimal | None:
    text = str(value or "").replace(",", "").strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _same_number(actual: Any, expected: str) -> bool:
    if expected == "":
        return str(actual or "").strip() == ""
    got, want = _number(actual), _number(expected)
    return got is not None and want is not None and got == want


def check(expect: dict[str, Any], result: ExtractionResponse) -> list[str]:
    """Return the expectations this result misses, as readable sentences."""
    draft, calc = result.draft, result.calculation
    problems: list[str] = []
    if "contains_transaction" in expect and result.contains_transaction != expect["contains_transaction"]:
        problems.append(f"contains_transaction is {result.contains_transaction}")
    if "currency" in expect and draft.currency != expect["currency"]:
        problems.append(f"currency is {draft.currency or 'empty'}, expected {expect['currency']}")
    for role in ("seller", "buyer"):
        if role in expect and draft.model_dump()[role].strip().lower() != expect[role].lower():
            problems.append(f"{role} is {draft.model_dump()[role]!r}, expected {expect[role]!r}")
    # Whose phone the chat came from: "me" in the synthetic chats.
    if "owner_role" in expect and result.parties.owner_role != expect["owner_role"]:
        problems.append(f"owner role is {result.parties.owner_role or 'unknown'}, expected {expect['owner_role']}")
    if "line_items" in expect:
        items = list(draft.line_items)
        if len(items) != len(expect["line_items"]):
            problems.append(f"{len(items)} items, expected {len(expect['line_items'])}: {[item.description for item in items]}")
        for want in expect["line_items"]:
            match = next((item for item in items if want["description_contains"].lower() in item.description.lower()), None)
            if match is None:
                problems.append(f"no item mentioning {want['description_contains']!r}")
                continue
            if not _same_number(match.quantity, want["quantity"]):
                problems.append(f"{match.description}: quantity {match.quantity!r}, expected {want['quantity']}")
            if not _same_number(match.unit_price, want["unit_price"]):
                problems.append(f"{match.description}: unit price {match.unit_price!r}, expected {want['unit_price']}")
    if "delivery_fee" in expect and not _same_number(draft.delivery_fee, expect["delivery_fee"]):
        problems.append(f"delivery fee {draft.delivery_fee!r}, expected {expect['delivery_fee']!r}")
    if "payment_status" in expect and draft.payment_status.value != expect["payment_status"]:
        problems.append(f"payment status {draft.payment_status.value}, expected {expect['payment_status']}")
    if "payment_status_not" in expect and draft.payment_status.value == expect["payment_status_not"]:
        problems.append(f"payment status must not be {expect['payment_status_not']}")
    if "amount_paid" in expect and not _same_number(draft.amount_paid, expect["amount_paid"]):
        problems.append(f"amount paid {draft.amount_paid!r}, expected {expect['amount_paid']!r}")
    if "discount_total" in expect and not _same_number(calc.discount_total, expect["discount_total"]):
        problems.append(f"discount total {calc.discount_total!r}, expected {expect['discount_total']}")
    if "total" in expect and not _same_number(calc.total, expect["total"]):
        problems.append(f"total {calc.total!r}, expected {expect['total']}")
    if "review_required" in expect and result.review_required != expect["review_required"]:
        problems.append(f"review_required is {result.review_required}")
    if "flags_any" in expect:
        fields = [reason.field or "" for reason in result.review_reasons]
        if not any(field.startswith(prefix) for prefix in expect["flags_any"] for field in fields):
            problems.append(f"expected a review reason on {expect['flags_any']}, got {fields}")
    return problems


async def run_case(case: dict[str, Any], provider: Any, settings: Any) -> dict[str, Any]:
    uploads = [
        UploadedFile(name=path.name, data=path.read_bytes())
        for path in (SCREENSHOTS / f"{case['id']}-{index}.png" for index in range(1, len(case["screenshots"]) + 1))
    ]
    images = prepare_screenshots(
        uploads, max_files=settings.max_files, max_file_bytes=settings.max_file_bytes, max_total_bytes=settings.max_total_bytes
    )
    started = time.perf_counter()
    try:
        result = await run_extraction(provider, images, settings)
    except ExtractionProviderError as exc:
        return {"id": case["id"], "passed": False, "problems": [f"provider error: {exc.kind.value} ({exc.detail})"]}
    problems = check(case["expect"], result)
    return {
        "id": case["id"],
        "passed": not problems,
        "problems": problems,
        "seconds": round(time.perf_counter() - started, 1),
        "review_reasons": [reason.message for reason in result.review_reasons],
        "draft": result.draft.model_dump(mode="json"),
        "total": result.calculation.total,
    }


async def main_async(case_ids: list[str], delay: float) -> int:
    settings = get_settings()
    provider = create_provider(settings)
    if not provider.configured:
        print("No AI provider is configured. Set GEMINI_API_KEY or ANTHROPIC_API_KEY.")
        return 2
    cases = [case for case in load_cases() if not case_ids or case["id"] in case_ids]
    print(f"Running {len(cases)} cases with {provider.name}\n")
    results = []
    try:
        for number, case in enumerate(cases):
            if number and delay:
                await asyncio.sleep(delay)
            outcome = await run_case(case, provider, settings)
            results.append(outcome)
            mark = "PASS" if outcome["passed"] else "FAIL"
            print(f"{mark}  {case['id']:<26} {outcome.get('seconds', '-')}s  {case['title']}")
            for problem in outcome["problems"]:
                print(f"        - {problem}")
    finally:
        await provider.aclose()
    passed = sum(outcome["passed"] for outcome in results)
    print(f"\n{passed}/{len(results)} cases passed")
    (HERE / "results").mkdir(exist_ok=True)
    report = {"provider": provider.name, "passed": passed, "total": len(results), "cases": results}
    (HERE / "results" / "latest.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0 if passed == len(results) else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--case", action="append", default=[], help="Run only this case id (repeatable).")
    parser.add_argument("--delay", type=float, default=6.0, help="Seconds between cases, to stay under rate limits.")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main_async(args.case, args.delay)))


if __name__ == "__main__":
    main()
