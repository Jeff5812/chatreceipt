"""Turn case scores into the evaluation's measures, as JSON and as a Markdown report."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from statistics import median
from typing import Any

from evals.intelligence.score import NO_RECORD, CaseScore, Check

# Measures named in the evaluation brief, each built from one or more kinds of check.
MEASURES: list[tuple[str, tuple[str, ...], str]] = [
    ("Transaction detection", ("contains_transaction",), "Says whether the chat holds a transaction at all"),
    ("Number of records", ("records",), "One record per transaction, no more and no fewer"),
    ("Product", ("item.product",), "Each agreed item is on the record"),
    ("Item count", ("item.count",), "No extra or missing rows"),
    ("Quantity", ("item.quantity",), "Quantity of each agreed item"),
    ("Unit price", ("item.price",), "The agreed price, not an earlier offer"),
    ("Currency", ("currency",), ""),
    ("Speaker attribution", ("seller", "buyer"), "Who sold and who bought"),
    ("Delivery fee", ("delivery",), ""),
    ("Discount", ("discount",), "Total discount, as the backend calculates it"),
    ("Additional fees", ("fees",), ""),
    ("Total", ("total",), "The backend's own calculation from the record"),
    ("Payment classification", ("payment",), "not specified / unpaid / partially paid / paid / refunded"),
    ("Amount paid", ("paid",), ""),
    ("Agreement detection", ("agreement",), "enquiry, quotation, negotiation, agreed, unclear, cancelled"),
    ("Transaction validation", ("review",), "Whether the record must be reviewed before approval"),
]
NEGOTIATION_CATEGORIES = ("price_negotiation", "multiple_counteroffers")
STATUS_ORDER = ("confirmed", "inferred", "needs_review", "contradictory", "missing", "invalid")


def _rate(correct: int, total: int) -> float | None:
    return round(correct / total, 4) if total else None


def _pct(rate: float | None) -> str:
    return "–" if rate is None else f"{rate * 100:.0f}%"


def _checks(scores: Iterable[CaseScore], *names: str) -> list[Check]:
    return [check for score in scores for check in score.checks if check.name in names]


def _accuracy(checks: list[Check]) -> dict[str, Any]:
    correct = sum(check.passed for check in checks)
    return {"checked": len(checks), "correct": correct, "accuracy": _rate(correct, len(checks))}


def _confusion(checks: list[Check]) -> dict[str, dict[str, int]]:
    table: dict[str, Counter[str]] = defaultdict(Counter)
    for check in checks:
        table[check.expected][check.actual] += 1
    return {expected: dict(row) for expected, row in sorted(table.items())}


def review_measures(scores: list[CaseScore]) -> dict[str, Any]:
    """Treat "needs review" as the positive class: precision, recall and the effort it costs."""
    checks = _checks(scores, "review")
    tp = sum(c.expected == "true" and c.actual == "true" for c in checks)
    fp = sum(c.expected == "false" and c.actual == "true" for c in checks)
    fn = sum(c.expected == "true" and c.actual == "false" for c in checks)
    tn = sum(c.expected == "false" and c.actual == "false" for c in checks)
    records = sum(score.records for score in scores)
    flagged = sum(score.records_needing_review for score in scores)
    return {
        "labelled": len(checks),
        "true_positive": tp, "false_positive": fp, "false_negative": fn, "true_negative": tn,
        "no_record": sum(c.actual == NO_RECORD for c in checks),
        "precision": _rate(tp, tp + fp),
        "recall": _rate(tp, tp + fn),
        "records": records,
        "records_needing_review": flagged,
        "review_rate": _rate(flagged, records),
    }


def calibration(scores: list[CaseScore]) -> dict[str, Any]:
    """How often values are right, by the state and confidence the engine gave them."""
    checks = [check for score in scores for check in score.checks if check.status is not None]
    by_status: dict[str, list[Check]] = defaultdict(list)
    by_confidence: dict[str, list[Check]] = defaultdict(list)
    for check in checks:
        by_status[check.status or ""].append(check)
        by_confidence[check.confidence or "none given"].append(check)
    return {
        "by_status": {status: _accuracy(by_status[status]) for status in STATUS_ORDER if status in by_status},
        "by_confidence": {level: _accuracy(by_confidence[level]) for level in ("high", "medium", "low", "none given") if level in by_confidence},
    }


def summarise(scores: list[CaseScore], *, selected: int) -> dict[str, Any]:
    scored = [score for score in scores if score.error is None]
    errors = [score for score in scores if score.error is not None]
    categories: dict[str, list[CaseScore]] = defaultdict(list)
    for score in scores:
        categories[score.category].append(score)
    negotiation_prices = [c for s in scored if s.category in NEGOTIATION_CATEGORIES for c in s.checks if c.name == "item.price"]
    detection = _checks(scored, "contains_transaction")
    return {
        "cases": {
            "selected": selected,
            "run": len(scores),
            "not_run": selected - len(scores),
            "fully_correct": sum(score.passed for score in scores),
            "errors": len(errors),
        },
        "checks": _accuracy([check for score in scored for check in score.checks]),
        "measures": {
            title: {**_accuracy(_checks(scored, *names)), "note": note} for title, names, note in MEASURES
        },
        "negotiated_price": _accuracy(negotiation_prices),
        "transaction_detection": {
            "transactions_found": _accuracy([c for c in detection if c.expected == "true"]),
            "non_transactions_rejected": _accuracy([c for c in detection if c.expected == "false"]),
        },
        "agreement_confusion": _confusion(_checks(scored, "agreement")),
        "payment_confusion": _confusion(_checks(scored, "payment")),
        "review": review_measures(scored),
        "unflagged_financial_errors": [
            {"case": score.case_id, "problem": check.describe()} for score in scored for check in score.unflagged_financial_errors
        ],
        "confident_financial_errors": [
            {"case": score.case_id, "problem": check.describe()} for score in scored for check in score.confident_financial_errors
        ],
        "calibration": calibration(scored),
        "categories": {
            name: {"run": len(rows), "fully_correct": sum(row.passed for row in rows)} for name, rows in sorted(categories.items())
        },
        "errors": [{"case": score.case_id, "error": score.error} for score in errors],
        "failures": {score.case_id: [check.describe() for check in score.failures] for score in scored if score.failures},
    }


def usage(stages: list[dict[str, Any]]) -> dict[str, Any]:
    durations = sorted(stage["duration_ms"] for stage in stages if stage.get("duration_ms"))
    p90 = durations[max(0, round(0.9 * len(durations)) - 1)] if durations else None
    return {
        "requests": len(stages),
        "input_tokens": sum(stage.get("input_tokens", 0) for stage in stages),
        "output_tokens": sum(stage.get("output_tokens", 0) for stage in stages),
        "median_ms": round(median(durations)) if durations else None,
        "p90_ms": p90,
    }


def _table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    lines += ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
    return lines


def _matrix(title: str, confusion: dict[str, dict[str, int]]) -> list[str]:
    if not confusion:
        return []
    actual = sorted({label for row in confusion.values() for label in row})
    rows = [[f"**{expected}**", *[row.get(label, "") for label in actual]] for expected, row in confusion.items()]
    return [f"### {title}", "", "Rows are the labels, columns what the engine recorded.", "", *_table(["labelled \\ recorded", *actual], rows), ""]


def markdown(summary: dict[str, Any], meta: dict[str, Any], strict: dict[str, Any] | None = None) -> str:
    cases, review = summary["cases"], summary["review"]
    lines = [
        f"# Intelligence evaluation: {meta['split_name']}",
        "",
        f"- **Model:** `{meta['model']}` (no fallback model), run {meta['date']}",
        f"- **Engine:** {meta['versions']['engine']}; prompts {meta['versions']['transcription_prompt']}, "
        f"{meta['versions']['interpretation_prompt']}; schema {meta['versions']['interpretation_schema']}",
        f"- **Cases:** {cases['run']} of {cases['selected']} selected were run"
        + (f" ({cases['not_run']} not run yet: the daily request allowance ran out or the run was capped)" if cases["not_run"] else ""),
        f"- **AI requests behind these results:** {meta['usage']['requests']} "
        f"(median {meta['usage']['median_ms']} ms, 90th percentile {meta['usage']['p90_ms']} ms; "
        f"{meta['usage']['input_tokens']:,} input and {meta['usage']['output_tokens']:,} output tokens)",
        "",
        "The conversations are synthetic: written and labelled by Claude for this evaluation, not taken from "
        "real customers, so these figures show how the engine handles the situations in the dataset, not "
        "how often each situation happens in real chats. Only the labels a case gives are scored. "
        "See `evals/intelligence/README.md` for the method.",
        "",
        "## Results",
        "",
        f"- **Cases fully correct:** {cases['fully_correct']} of {cases['run']} ({_pct(_rate(cases['fully_correct'], cases['run']))})",
        f"- **Checks passed:** {summary['checks']['correct']} of {summary['checks']['checked']} ({_pct(summary['checks']['accuracy'])})",
        f"- **Unflagged financial errors:** {len(summary['unflagged_financial_errors'])} "
        "(a wrong money value on a record that could be approved without anyone being asked)",
        f"- **Wrong money values marked as confirmed:** {len(summary['confident_financial_errors'])}",
        f"- **Provider errors:** {cases['errors']}",
        "",
        *_table(
            ["Measure", "Correct", "Checked", "Accuracy", "What it checks"],
            [[title, m["correct"], m["checked"], _pct(m["accuracy"]), m["note"]] for title, m in summary["measures"].items() if m["checked"]],
        ),
        "",
        f"Negotiated prices (the final accepted price in negotiation and counter-offer chats): "
        f"{summary['negotiated_price']['correct']} of {summary['negotiated_price']['checked']} "
        f"({_pct(summary['negotiated_price']['accuracy'])}).",
        "",
        "## Review flags",
        "",
        "Positive means the record must be reviewed before approval.",
        "",
        *_table(
            ["", "Labelled needs review", "Labelled clean"],
            [["Engine asked for review", review["true_positive"], review["false_positive"]],
             ["Engine allowed approval", review["false_negative"], review["true_negative"]]],
        ),
        "",
        f"- **Precision:** {_pct(review['precision'])}; **recall:** {_pct(review['recall'])}"
        + (f"; {review['no_record']} labelled records weren't produced at all" if review["no_record"] else ""),
        f"- **Review effort:** {review['records_needing_review']} of {review['records']} proposed records needed review ({_pct(review['review_rate'])})",
        "",
        *_matrix("Agreement status", summary["agreement_confusion"]),
        *_matrix("Payment status", summary["payment_confusion"]),
        "## Confidence calibration",
        "",
        "How often a value was right, by the state the engine gave it. A useful state is right much more "
        "often when it says confirmed than when it says needs review.",
        "",
        *_table(
            ["Field state", "Right", "Values", "Accuracy"],
            [[status, m["correct"], m["checked"], _pct(m["accuracy"])] for status, m in summary["calibration"]["by_status"].items()],
        ),
        "",
        *_table(
            ["Model confidence", "Right", "Values", "Accuracy"],
            [[level, m["correct"], m["checked"], _pct(m["accuracy"])] for level, m in summary["calibration"]["by_confidence"].items()],
        ),
        "",
    ]
    if strict is not None:
        default_review, strict_review = summary["review"], strict["review"]
        lines += [
            "## Review policy comparison",
            "",
            "The same model answers, re-scored under the stricter policy that sends medium-confidence money values to review "
            "(`REVIEW_MEDIUM_CONFIDENCE_FINANCIAL=needs_review`). No extra AI requests.",
            "",
            *_table(
                ["Policy", "Records needing review", "Review precision", "Review recall", "Unflagged financial errors"],
                [
                    ["Default (medium confidence shown as inferred)", _pct(default_review["review_rate"]), _pct(default_review["precision"]),
                     _pct(default_review["recall"]), len(summary["unflagged_financial_errors"])],
                    ["Strict (medium confidence needs review)", _pct(strict_review["review_rate"]), _pct(strict_review["precision"]),
                     _pct(strict_review["recall"]), len(strict["unflagged_financial_errors"])],
                ],
            ),
            "",
        ]
    lines += [
        "## By category",
        "",
        *_table(["Category", "Fully correct", "Run"], [[name, row["fully_correct"], row["run"]] for name, row in summary["categories"].items()]),
        "",
    ]
    if summary["unflagged_financial_errors"]:
        lines += ["## Unflagged financial errors", "", *[f"- `{row['case']}`: {row['problem']}" for row in summary["unflagged_financial_errors"]], ""]
    if summary["failures"]:
        lines += ["## Every failed check", ""]
        for case_id, problems in summary["failures"].items():
            lines.append(f"- `{case_id}`: " + "; ".join(problems))
        lines.append("")
    if summary["errors"]:
        lines += ["## Provider errors", "", *[f"- `{row['case']}`: {row['error']}" for row in summary["errors"]], ""]
    return "\n".join(lines)
