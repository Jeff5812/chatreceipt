"""The intelligence evaluation itself: its dataset, split, scoring, reports and runner. No real AI."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from app.extraction import prompt as extraction_prompt
from app.extraction import schema as extraction_schema
from app.extraction.provider import ErrorKind, ExtractionProviderError
from app.intelligence import interpretation, transcription
from app.intelligence.interpretation import parse_interpretation
from app.intelligence.proposal import build_proposals
from app.intelligence.text_import import parse_text_conversation
from app.whatsapp import understanding
from evals import run_intelligence as runner
from evals.intelligence.dataset import (
    REQUIRED_CATEGORIES,
    Case,
    DatasetError,
    load_cases,
    select,
    split_for,
)
from evals.intelligence.report import markdown, summarise
from evals.intelligence.score import NO_RECORD, score_case
from tests.engine_harness import CHAT, ScriptedModel, chat_answer
from tests.intel import answer, deal, event, value

GOOD_EXPECT = {
    "currency": "NGN", "seller": "Tola Bags", "buyer": "Ada", "items": [["tote", 2, 11000]], "delivery": 2000,
    "total": 24000, "payment": "unpaid", "paid": 0, "agreement": "agreement_reached", "review": False,
}


def case(expect: dict, chat: str = CHAT, *, id: str = "sample", category: str = "price_negotiation", position: int = 1) -> Case:
    return Case(id=id, category=category, position=position, chat=chat, expect=expect)


def outcome(chat: str, data: dict):
    convo = parse_text_conversation(chat)
    assert convo is not None
    reading = parse_interpretation(data, convo)
    return reading, build_proposals(convo, reading)


# -- the dataset ---------------------------------------------------------------------------------


def test_dataset_meets_the_brief() -> None:
    cases = load_cases()
    assert len(cases) >= 100
    by_category = {category: [c for c in cases if c.category == category] for category in REQUIRED_CATEGORIES}
    for category, rows in by_category.items():
        assert len(rows) >= 4, category
        assert {c.split for c in rows} == {"dev", "test"}, category  # both splits in every category
    assert sum(c.split == "test" for c in cases) > sum(c.split == "dev" for c in cases)


def test_the_split_is_fixed_by_position() -> None:
    assert [split_for(n) for n in range(7)] == ["dev", "test", "test", "dev", "test", "test", "dev"]
    first = {c.id: c.split for c in load_cases()}
    assert first == {c.id: c.split for c in load_cases()}


def test_naming_a_held_out_case_cannot_pull_it_into_development() -> None:
    cases = load_cases()
    held_out = next(c.id for c in cases if c.split == "test")
    with pytest.raises(DatasetError, match="not in the dev split"):
        select(cases, "dev", [held_out])
    assert [c.id for c in select(cases, "test", [held_out])] == [held_out]


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({"id": "x", "chat": "A: hi\nB: hello", "expect": {"payment": "half_paid"}}, "unknown payment status"),
        ({"id": "x", "chat": "A: hi\nB: hello", "expect": {"items": [["bag", 1]]}}, "each item is"),
        ({"id": "x", "chat": "A: hi\nB: hello", "expect": {"colour": "red"}}, "unknown keys"),
        ({"id": "x", "chat": "A: hi\nB: hello", "expect": {"contains_transaction": False, "total": 5}}, "nothing else to check"),
    ],
)
def test_bad_labels_are_refused(tmp_path: Path, entry: dict, message: str) -> None:
    path = tmp_path / "cases.yaml"
    body = {category: [{"id": f"{category}-1", "chat": "A: hi\nB: hello", "expect": {"review": True}}] for category in REQUIRED_CATEGORIES}
    body["simple_purchase"].append(entry)
    path.write_text(json.dumps(body), encoding="utf-8")  # JSON is valid YAML
    with pytest.raises(DatasetError, match=message):
        load_cases(path)


def _prompt_texts() -> list[str]:
    """Everything sent to the AI as instructions: prompts and the schemas' descriptions."""
    texts = [
        extraction_prompt.SYSTEM_PROMPT,
        interpretation.INTERPRETATION_PROMPT,
        transcription.TRANSCRIPTION_PROMPT,
        transcription.TEXT_SEGMENTATION_PROMPT,
        understanding.CORRECTION_PROMPT,
    ]
    texts += [json.dumps(schema, ensure_ascii=False) for schema in (
        extraction_schema.EXTRACTION_SCHEMA, interpretation.INTERPRETATION_SCHEMA,
        transcription.TRANSCRIPTION_SCHEMA, understanding.CORRECTION_SCHEMA,
    )]
    return [" ".join(text.casefold().split()) for text in texts]


_PREFIX = re.compile(r"^\s*(\[[^\]]*\]\s*)?(\d{1,2}/\d{1,2}/\d{2,4},?\s*\d{1,2}:\d{2}\s*-\s*)?([^:]{1,40}:\s*)?")


def test_no_held_out_conversation_appears_in_any_prompt() -> None:
    prompts = _prompt_texts()
    leaked = []
    for c in load_cases():
        if c.split != "test":
            continue
        for line in c.chat.splitlines():
            text = " ".join(_PREFIX.sub("", line).casefold().split())
            if len(text) >= 16 and any(text in prompt for prompt in prompts):
                leaked.append((c.id, text))
    assert leaked == []


# -- scoring -------------------------------------------------------------------------------------


def test_a_correct_record_passes_every_check() -> None:
    score = score_case(case(GOOD_EXPECT), *outcome(CHAT, chat_answer()))
    assert score.passed, [check.describe() for check in score.failures]
    states = {check.name: check.status for check in score.checks}
    assert states["item.price"] == "confirmed" and states["seller"] == "confirmed"


def test_a_wrong_price_on_an_approvable_record_is_an_unflagged_financial_error() -> None:
    wrong = case({**GOOD_EXPECT, "items": [["tote", 2, 12000]], "total": 26000})
    score = score_case(wrong, *outcome(CHAT, chat_answer()))
    assert not score.passed
    failed = {check.name for check in score.failures}
    assert failed == {"item.price", "total"}
    assert {check.name for check in score.unflagged_financial_errors} == {"item.price", "total"}
    assert [check.name for check in score.confident_financial_errors] == ["item.price"]


def test_no_record_for_a_transaction_fails_every_label() -> None:
    nothing = answer(contains_transaction=False)
    score = score_case(case(GOOD_EXPECT), *outcome(CHAT, nothing))
    assert {check.actual for check in score.checks if check.name != "contains_transaction"} == {NO_RECORD}
    assert not any(check.passed for check in score.checks)
    assert score.unflagged_financial_errors == []  # nothing could be approved


def test_a_chat_without_a_transaction_passes_when_nothing_is_recorded() -> None:
    score = score_case(case({"contains_transaction": False}, category="no_transaction"), *outcome(CHAT, answer(contains_transaction=False)))
    assert score.passed and [check.name for check in score.checks] == ["contains_transaction"]


def test_zero_charges_match_an_empty_field_but_a_missing_price_must_stay_empty() -> None:
    expect = {"delivery": 0, "discount": 0, "items": [["tote", 2, None]]}
    score = score_case(case(expect), *outcome(CHAT, chat_answer()))
    results = {check.name: check.passed for check in score.checks}
    assert results["discount"] is True  # no discount recorded
    assert results["delivery"] is False  # 2000 was recorded, the label says free
    assert results["item.price"] is False  # a price was recorded where the label says none


def test_each_expected_deal_is_matched_to_its_own_record() -> None:
    chat = "Bisi: 2 cakes at ₦15,000 each, paid already\nSweet Tooth: Thanks\nBisi: New order: 1 chocolate cake for ₦20,000\nSweet Tooth: Confirmed"
    first = deal([event("m1", "offer", "p1", quantity="2", unit_price="15000"), event("m2", "acceptance", "p2", responds_to="m1")],
                 items=[("i1", "Cake")], key="t1")
    second = deal([event("m3", "offer", "p1", item="i2", quantity="1", unit_price="20000"), event("m4", "acceptance", "p2", item="i2", responds_to="m3")],
                  items=[("i2", "Chocolate cake")], key="t2")
    for d, evidence in ((first, "m1"), (second, "m3")):
        d["currency"] = value("NGN", evidence=[evidence])
    data = answer(second, first, roles={"p1": "buyer", "p2": "seller"})  # the records come back in the other order
    expect = {"deals": [{"items": [["cake", 2, 15000]]}, {"items": [["chocolate", 1, 20000]]}]}
    score = score_case(case(expect, chat, category="multiple_transactions"), *outcome(chat, data))
    prices = [(check.deal, check.actual) for check in score.checks if check.name == "item.price"]
    assert prices == [(1, "15000"), (2, "20000")]
    assert next(check for check in score.checks if check.name == "records").passed


def test_the_report_counts_review_flags_and_calibration() -> None:
    good = score_case(case(GOOD_EXPECT, id="good"), *outcome(CHAT, chat_answer()))
    wrong = score_case(case({**GOOD_EXPECT, "review": True}, id="missed"), *outcome(CHAT, chat_answer()))
    failed = score_case(case(GOOD_EXPECT, id="broken"), None, [], error="malformed: bad answer")
    summary = summarise([good, wrong, failed], selected=4)
    assert summary["cases"] == {"selected": 4, "run": 3, "not_run": 1, "fully_correct": 1, "errors": 1}
    review = summary["review"]
    assert (review["true_negative"], review["false_negative"], review["recall"]) == (1, 1, 0.0)
    assert summary["calibration"]["by_status"]["confirmed"]["accuracy"] == 1.0
    meta = {"split_name": "test", "model": "scripted", "date": "2026-11-19", "versions": runner.versions(),
            "usage": {"requests": 2, "median_ms": 7, "p90_ms": 7, "input_tokens": 200, "output_tokens": 100}}
    text = markdown(summary, meta, summary)
    for heading in ("## Results", "## Review flags", "## Confidence calibration", "## Review policy comparison", "## Provider errors"):
        assert heading in text
    assert "synthetic" in text and "1 not run yet" in text


# -- the runner ------------------------------------------------------------------------------------


def test_answers_are_saved_and_reused_until_the_request_changes(tmp_path: Path) -> None:
    store = runner.AnswerStore(tmp_path, "scripted-model")
    target = case(GOOD_EXPECT)
    model = ScriptedModel(chat_answer())
    spent, stopped = asyncio.run(runner.collect([target], store, "scripted-model", model, budget=5, delay=0, retry_errors=False))
    assert (spent, stopped, model.calls) == (1, None, ["interpretation"])
    scores, stages = runner.score_saved([target], store, "scripted-model", runner.DEFAULT_POLICY)
    assert scores[0].passed and stages[0]["task"] == "interpretation"

    again = ScriptedModel(chat_answer())
    asyncio.run(runner.collect([target], store, "scripted-model", again, budget=5, delay=0, retry_errors=False))
    assert again.calls == []  # saved answer reused
    other = runner.AnswerStore(tmp_path, "another-model")
    assert other.get(target, runner.cache_key("another-model", target.chat)) is None
    assert runner.cache_key("scripted-model", target.chat) != runner.cache_key("scripted-model", target.chat + "Ada: thanks\n")


def test_a_rate_limit_stops_the_run_without_saving(tmp_path: Path) -> None:
    store = runner.AnswerStore(tmp_path, "m")
    first, second = case(GOOD_EXPECT, id="one"), case(GOOD_EXPECT, id="two")
    model = ScriptedModel(ExtractionProviderError(ErrorKind.RATE_LIMITED, "429"))
    spent, stopped = asyncio.run(runner.collect([first, second], store, "m", model, budget=5, delay=0, retry_errors=False))
    assert spent == 1 and "allowance" in (stopped or "")
    assert store.get(first, runner.cache_key("m", first.chat)) is None


def test_a_malformed_answer_is_retried_once_then_kept_as_an_error(tmp_path: Path) -> None:
    store = runner.AnswerStore(tmp_path, "m")
    target = case(GOOD_EXPECT)
    model = ScriptedModel({"not": "the schema"})
    spent, _ = asyncio.run(runner.collect([target], store, "m", model, budget=5, delay=0, retry_errors=False))
    assert spent == 2
    scores, _ = runner.score_saved([target], store, "m", runner.DEFAULT_POLICY)
    assert scores[0].error and scores[0].error.startswith("malformed")


def test_the_budget_caps_requests(tmp_path: Path) -> None:
    store = runner.AnswerStore(tmp_path, "m")
    cases = [case(GOOD_EXPECT, id=f"c{n}") for n in range(3)]
    model = ScriptedModel(chat_answer())
    spent, stopped = asyncio.run(runner.collect(cases, store, "m", model, budget=2, delay=0, retry_errors=False))
    assert spent == 2 and "budget" in (stopped or "")


def test_the_live_channel_models_are_protected() -> None:
    from tests.samples import make_settings

    settings = make_settings(gemini_model="gemini-2.5-flash", gemini_fallback_model="gemini-3.5-flash", gemini_light_model="gemini-2.5-flash-lite")
    assert runner.live_channel_models(settings) == {"gemini-2.5-flash", "gemini-3.5-flash", "gemini-2.5-flash-lite"}
    pinned = runner.eval_settings(settings, "gemini-3.1-flash-lite")
    assert (pinned.gemini_model, pinned.gemini_fallback_model, pinned.gemini_light_model) == ("gemini-3.1-flash-lite", "", "")
