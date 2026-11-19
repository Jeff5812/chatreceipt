"""Measure the transaction intelligence engine on the labelled conversations.

usage (from backend/):
    python -m evals.run_intelligence --split dev --model gemini-3.1-flash-lite
    python -m evals.run_intelligence --split test --model gemini-3.1-flash-lite --budget 40
    python -m evals.run_intelligence --split dev --model gemini-3.1-flash-lite --report-only

Each case goes through the engine's own stages: the chat text is parsed (or split into messages by
the AI when it has no recognisable structure), the AI interprets it, and ChatReceipt's rules build
the proposed records and decide what needs review. The model's raw answers are saved, so scoring
again after a rules change costs no requests. A case goes back to the AI only when the request
itself would change: its text, the model, or a prompt or schema.

Every request counts against the provider's daily allowance. On the free Gemini tier that is about
20 requests a day per model, and the WhatsApp channel needs its own models' allowance, so this
refuses those models unless --allow-live-model is given. --budget caps the requests in one run;
when the allowance runs out the run stops, keeps what it has and reports how many cases are left.
Run it again the next day to continue. Reports go to evals/intelligence/reports/ and the full
per-case results to evals/results/intelligence/ (not committed).

Develop against --split dev only. Run --split test to measure, not to tune: see
evals/intelligence/README.md.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import Settings, get_settings
from app.extraction.provider import ErrorKind, ExtractionProviderError, StructuredModel, StructuredRequest, TextPart
from app.extraction.providers import create_provider
from app.intelligence.conversation import Conversation
from app.intelligence.fields import FieldStatus
from app.intelligence.interpretation import Interpretation, interpretation_request, parse_interpretation
from app.intelligence.pipeline import EmptyConversationError, StageRecord, conversation_from_text
from app.intelligence.proposal import Proposal, build_proposals
from app.intelligence.review_policy import DEFAULT_POLICY, ReviewPolicy
from app.intelligence.text_import import parse_text_conversation
from app.intelligence.transcription import parse_transcription, segmented_conversation, text_segmentation_request
from app.intelligence.versions import (
    ENGINE_VERSION,
    INTERPRETATION_PROMPT_VERSION,
    INTERPRETATION_SCHEMA_VERSION,
    TRANSCRIPTION_PROMPT_VERSION,
)
from evals.intelligence.dataset import Case, load_cases, select
from evals.intelligence.report import markdown, summarise, usage
from evals.intelligence.score import CaseScore, score_case

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results" / "intelligence"
REPORTS = HERE / "intelligence" / "reports"
STRICT_POLICY = ReviewPolicy(medium_financial=FieldStatus.NEEDS_REVIEW)
_TRANSIENT = (ErrorKind.TIMEOUT, ErrorKind.UNAVAILABLE)
SPLIT_NAMES = {"dev": "development split", "test": "held-out test split", "all": "all cases"}


def versions() -> dict[str, str]:
    return {
        "engine": ENGINE_VERSION,
        "transcription_prompt": TRANSCRIPTION_PROMPT_VERSION,
        "interpretation_prompt": INTERPRETATION_PROMPT_VERSION,
        "interpretation_schema": INTERPRETATION_SCHEMA_VERSION,
    }


def _fingerprint(request: StructuredRequest) -> dict[str, Any]:
    return {
        "task": request.task,
        "system": request.system,
        "schema": request.schema,
        "parts": [part.text if isinstance(part, TextPart) else part.media_type for part in request.parts],
        "max_output_tokens": request.max_output_tokens,
    }


def cache_key(model: str, chat: str) -> str:
    """Everything the AI is sent for this chat, so a changed prompt or schema can't reuse an old answer."""
    parsed = parse_text_conversation(chat)
    if parsed is not None and parsed.messages:
        sent = [_fingerprint(interpretation_request(parsed))]
    else:
        # The interpretation request depends on the AI's own segmentation; key on that request and
        # on the interpretation instructions it will be followed by.
        empty = Conversation(source_type="text", participants=[], messages=[])
        probe = interpretation_request(empty)
        sent = [_fingerprint(text_segmentation_request(chat)), {"system": probe.system, "schema": probe.schema}]
    blob = json.dumps({"model": model, "sent": sent}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class AnswerStore:
    """The model's raw answers, one file per case and model."""

    def __init__(self, root: Path, model: str) -> None:
        self.folder = root / "answers" / re.sub(r"[^A-Za-z0-9._-]", "_", model)

    def _path(self, case: Case) -> Path:
        return self.folder / f"{case.id}.json"

    def get(self, case: Case, key: str) -> dict[str, Any] | None:
        path = self._path(case)
        if not path.exists():
            return None
        entry = json.loads(path.read_text(encoding="utf-8"))
        return entry if entry.get("key") == key else None

    def put(self, case: Case, entry: dict[str, Any]) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        self._path(case).write_text(json.dumps(entry, indent=1, ensure_ascii=False), encoding="utf-8")


def rebuild(case: Case, entry: dict[str, Any], policy: ReviewPolicy) -> tuple[Interpretation | None, list[Proposal], str | None]:
    """Re-run the deterministic stages on a saved answer."""
    if entry.get("error"):
        return None, [], f"{entry['error']['kind']}: {entry['error']['detail']}"
    try:
        if entry.get("segmentation") is not None:
            conversation = segmented_conversation(parse_transcription(entry["segmentation"], [1])[0])
        else:
            parsed = parse_text_conversation(case.chat)
            assert parsed is not None, "a case without a saved segmentation must parse as text"
            conversation = parsed
        interpretation = parse_interpretation(entry["interpretation"], conversation)
    except ExtractionProviderError as exc:
        return None, [], f"{exc.kind.value}: {exc.detail}"
    return interpretation, build_proposals(conversation, interpretation, policy=policy), None


async def ask(model: StructuredModel, case: Case) -> dict[str, Any]:
    """Send one case through the AI stages and keep the raw answers.

    The answer is checked against the schema here, as the engine's jobs do, so a malformed answer
    raises and gets the same single retry.
    """
    stages: list[dict[str, Any]] = []
    conversation, segmentation, stage = await conversation_from_text(model, case.chat)
    if stage is not None:
        stages.append(asdict(stage))
    response = await model.generate(interpretation_request(conversation))
    stages.append(asdict(StageRecord.of("interpretation", model.name, response)))
    parse_interpretation(response.data, conversation)
    return {"segmentation": segmentation, "interpretation": response.data, "error": None, "stages": stages}


def requests_needed(case: Case) -> int:
    parsed = parse_text_conversation(case.chat)
    return 1 if parsed is not None and parsed.messages else 2


def eval_settings(base: Settings, model: str) -> Settings:
    # One model only: no fallback to another model's answers, and no light model for any request.
    return base.model_copy(
        update={"extraction_provider": "gemini", "gemini_model": model, "gemini_fallback_model": "", "gemini_light_model": ""}
    )


def live_channel_models(settings: Settings) -> set[str]:
    return {name for name in (settings.gemini_model, settings.gemini_fallback_model, settings.gemini_light_model) if name}


async def collect(
    cases: list[Case], store: AnswerStore, model_name: str, model: StructuredModel, *, budget: int, delay: float, retry_errors: bool
) -> tuple[int, str | None]:
    """Ask the AI about every case without a saved answer, within the budget. Returns (requests, stop reason)."""
    spent, transient_in_a_row = 0, 0
    for case in cases:
        key = cache_key(model_name, case.chat)
        saved = store.get(case, key)
        if saved is not None and not (retry_errors and saved.get("error")):
            continue
        needed = requests_needed(case)
        if spent + needed > budget:
            return spent, f"the budget of {budget} requests was reached"
        if spent and delay:
            await asyncio.sleep(delay)
        attempts = 0
        while True:
            attempts += 1
            spent += needed
            try:
                entry = await ask(model, case)
                break
            except EmptyConversationError as exc:
                entry = {"segmentation": None, "interpretation": None, "stages": [], "error": {"kind": "empty_conversation", "detail": str(exc)}}
                break
            except ExtractionProviderError as exc:
                if exc.kind == ErrorKind.RATE_LIMITED:
                    return spent, "the provider's rate limit or daily allowance was reached"
                if exc.kind == ErrorKind.NOT_CONFIGURED:
                    return spent, f"the provider isn't usable: {exc.detail}"
                if exc.kind in _TRANSIENT:
                    transient_in_a_row += 1
                    print(f"  {case.id}: {exc.kind.value} ({exc.detail}); not saved, it will be tried again next run")
                    if transient_in_a_row >= 3:
                        return spent, "the provider failed three times in a row"
                    entry = None
                    break
                if exc.kind == ErrorKind.MALFORMED and attempts == 1 and spent + needed <= budget:
                    continue  # the engine's own jobs retry a malformed answer too
                entry = {"segmentation": None, "interpretation": None, "stages": [], "error": {"kind": exc.kind.value, "detail": exc.detail}}
                break
        if entry is None:
            continue
        transient_in_a_row = 0
        entry.update({"case": case.id, "key": key, "model": model_name, "versions": versions(), "recorded_at": datetime.now(UTC).isoformat(timespec="seconds")})
        store.put(case, entry)
        print(f"  {case.id}: answered" + (f" ({entry['error']['kind']})" if entry["error"] else ""))
    return spent, None


def score_saved(cases: list[Case], store: AnswerStore, model_name: str, policy: ReviewPolicy) -> tuple[list[CaseScore], list[dict[str, Any]]]:
    scores, stages = [], []
    for case in cases:
        entry = store.get(case, cache_key(model_name, case.chat))
        if entry is None:
            continue
        interpretation, proposals, error = rebuild(case, entry, policy)
        scores.append(score_case(case, interpretation, proposals, error=error))
        stages.extend(entry.get("stages", []))
    return scores, stages


async def main_async(args: argparse.Namespace) -> int:
    base = get_settings()
    model_name = args.model or base.gemini_model
    if model_name in live_channel_models(base) and not args.allow_live_model and not args.report_only:
        print(
            f"{model_name} is one of the models the WhatsApp channel uses, and every request here spends its "
            "daily allowance. Choose another model with --model, or pass --allow-live-model."
        )
        return 2
    cases = select(load_cases(), args.split, args.case)
    store = AnswerStore(RESULTS, model_name)
    stop_reason = None
    if not args.report_only:
        settings = eval_settings(base, model_name)
        model = create_provider(settings)
        if not model.configured:
            print("No Gemini API key is configured (GEMINI_API_KEY).")
            return 2
        print(f"Asking {model_name} about up to {len(cases)} cases ({args.split} split), at most {args.budget} requests")
        try:
            spent, stop_reason = await collect(
                cases, store, model_name, model, budget=args.budget, delay=args.delay, retry_errors=args.retry_errors
            )
        finally:
            await model.aclose()
        print(f"{spent} requests sent" + (f"; stopped because {stop_reason}" if stop_reason else ""))

    scores, stages = score_saved(cases, store, model_name, DEFAULT_POLICY)
    if not scores:
        print("No saved answers to score yet.")
        return 1
    strict_scores, _ = score_saved(cases, store, model_name, STRICT_POLICY)
    summary = summarise(scores, selected=len(cases))
    strict = summarise(strict_scores, selected=len(cases))
    custom = bool(args.case)
    meta = {
        "model": model_name,
        "split": args.split,
        "split_name": f"{SPLIT_NAMES[args.split]}{' (selected cases)' if custom else ''}, {len(cases)} cases",
        "date": datetime.now(UTC).date().isoformat(),
        "versions": versions(),
        "usage": usage(stages),
        "stopped_because": stop_reason,
    }
    name = f"{args.split}{'-selected' if custom else ''}-{re.sub(r'[^A-Za-z0-9._-]', '_', model_name)}"
    RESULTS.mkdir(parents=True, exist_ok=True)
    REPORTS.mkdir(parents=True, exist_ok=True)
    details = {"meta": meta, "summary": summary, "strict_policy": strict, "cases": [score.as_dict() for score in scores]}
    (RESULTS / f"{name}.json").write_text(json.dumps(details, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    (REPORTS / f"{name}.md").write_text(markdown(summary, meta, strict), encoding="utf-8")

    c = summary["cases"]
    print(f"\n{c['fully_correct']}/{c['run']} cases fully correct ({c['not_run']} of {c['selected']} not run yet, {c['errors']} provider errors)")
    for title, measure in summary["measures"].items():
        if measure["checked"]:
            print(f"  {title:<24} {measure['correct']:>3}/{measure['checked']:<3}")
    print(f"  unflagged financial errors: {len(summary['unflagged_financial_errors'])}")
    print(f"Report: {REPORTS / (name + '.md')}")
    return 0


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--split", choices=("dev", "test", "all"), default="dev")
    parser.add_argument("--case", action="append", default=[], help="Only this case id within the split (repeatable).")
    parser.add_argument("--model", help="Gemini model to evaluate (default: GEMINI_MODEL).")
    parser.add_argument("--budget", type=int, default=20, help="Most AI requests to send in this run (default 20).")
    parser.add_argument("--delay", type=float, default=4.0, help="Seconds between requests (default 4).")
    parser.add_argument("--report-only", action="store_true", help="Score saved answers without sending any request.")
    parser.add_argument("--retry-errors", action="store_true", help="Ask again about cases whose saved answer was an error.")
    parser.add_argument("--allow-live-model", action="store_true", help="Allow a model the WhatsApp channel uses.")
    raise SystemExit(asyncio.run(main_async(parser.parse_args())))


if __name__ == "__main__":
    main()
