# Testing ChatReceipt

## Backend tests

From `backend/`, with the virtual environment active:

```bash
python -m pytest -o addopts="" -q
```

The database tests start a private PostgreSQL on a free port from the installed `initdb` and `pg_ctl`
(or use `TEST_DATABASE_URL`) and never touch a real database. No test calls a real AI provider, Meta or
any other external service: providers, WhatsApp and webhook receivers are replaced by scripted fakes.

Latest full run, 2026-11-21: **785 passed** in 3 min 20 s. The slowest step is starting the private
database (about 10 s); one test prints a real PDF and PNG with Chromium.

| File | What it covers |
| --- | --- |
| `test_calculations.py` | Totals, discounts, fees, percentages, outstanding amounts, currency precision, invalid values |
| `test_extraction.py`, `test_gemini_provider.py`, `test_claude_provider.py` | Single-step extraction: schema, mapping, dates, provider errors, fallback and timeouts |
| `test_uploads.py`, `test_documents.py`, `test_api.py` | File checks by content, the record template, PDF/PNG, website endpoints, rate limits |
| `test_conversations.py` | Text and export parsing, screenshot merging, ordering, day/month ambiguity, AI-split text |
| `test_negotiation_rules.py` | Offers, counter-offers, acceptances, payments, figures and their states |
| `test_records_rules.py` | Lifecycle transitions and review operations |
| `test_engine_pipeline.py` | The engine end to end: jobs, retries, crashes, review, approval, receipts, duplicates, outbox |
| `test_engine_security.py` | Tenant isolation in the database, authentication, upload attacks, injection, erasure |
| `test_whatsapp_*.py` | The WhatsApp channel against a fake Cloud API: conversation, corrections, receipts, privacy |
| `test_whatsapp_identity.py`, `test_parties.py` | Who is who: the customer's name and side, remembered contacts, conflicts, payers, money owed by side; chat exports sent as documents, "deal N", clear history, yes/no in context |
| `test_whatsapp_trust.py` | Payment checks, reused transfer receipts, the other person's confirmation link, WhatsApp usernames, messages per receipt |
| `test_chat_import.py`, `test_chat_export_api.py` | WhatsApp chat exports: Android and iPhone formats, owners, photos in ZIPs, unsafe archives, `POST /api/extractions/chat` |
| `test_client_address.py`, `test_api_session_tool.py` | Rate-limit addresses behind a proxy; the operator session tool |
| `test_intelligence_eval.py` | The evaluation itself: dataset rules, the split, scoring, reports, the runner, prompt leakage |

## Frontend checks

From `frontend/`: `npx tsc --noEmit`, `npm run lint` and `npm run build` (the same build Vercel runs).

## Evaluations with the real AI

These call the AI provider and spend its daily allowance, so they are run on purpose, not in CI.

| Command (from `backend/`) | What it measures |
| --- | --- |
| `python -m evals.run` | Single-step screenshot extraction on 12 rendered chat screenshots (`evals/README.md`), including which side the phone's owner is on and the other person's name. On 2026-11-21, 3 chosen cases passed on `gemini-2.5-flash` (`--case negotiated-price --case promise-to-pay-pidgin --case usd-freelance-deposit`) |
| `python -m evals.whatsapp_corrections` | How typed WhatsApp corrections are understood (16 messages) |
| `python -m evals.whatsapp_transcript` | Replays the first real phone test end to end |
| `python -m evals.run_intelligence` | The transaction intelligence engine on 119 labelled conversations (below) |

### The intelligence evaluation

**Data.** `evals/intelligence/conversations.yaml`: 119 synthetic conversations in 20 categories — the 18
the brief requires (simple purchases, several products, negotiation, counter-offers, delivery fees,
discounts, partial payments, unpaid, cancellations, ambiguous agreements, missing prices, several
speakers, corrections, contradictions, several transactions, poor OCR, out-of-order messages, incomplete
chats) plus chats with no transaction and prompt-injection attempts. They were written and labelled by
Claude, not taken from customers. Each case labels only what a careful person would record; only the
labelled keys are scored.

**Split.** Within each category the 1st, 4th and 7th cases are for development (42) and the rest are held
out (77). The split is fixed by position, and a test fails if any held-out message appears in a prompt.
Rules are tuned on development failures only; the held-out split is run to measure.

**Scoring** (`evals/intelligence/score.py`). Items are matched by a word in their description, amounts as
numbers, names exactly. Totals are ChatReceipt's own calculation. Each checked value keeps the field state
the engine gave it, which gives:

- *unflagged financial errors*: a wrong money value on a record that could be approved without review;
- *review precision and recall*: whether the engine asks for review exactly when a person should look;
- *calibration*: how often values are right for each field state and model confidence;
- a *policy comparison*: the same answers re-scored under the stricter review policy, at no AI cost.

**Running it.** `--split dev|test|all`, `--model` (a model the WhatsApp channel doesn't use; live models
are refused without `--allow-live-model`), `--budget` (requests per run, default 20). Answers are saved
under `evals/results/intelligence/answers/` and reused until the request would change, so a run can
continue the next day and rule changes are re-scored for free with `--report-only`. Reports are written to
`evals/intelligence/reports/`.

### Results on 2026-11-19

Model `gemini-3.5-flash-lite` (no fallback), engine `2026.11.19-1`. The development figures are after
tuning on them; the held-out figures were run once, after the rules were frozen.

| Measure | Development (42) | Held out (77) |
| --- | --- | --- |
| Cases fully correct | 36 (86%) | 57 (74%) |
| Checks passed | 343 of 359 (96%) | 589 of 622 (95%) |
| Transaction detection | 42 of 42 | 75 of 77 |
| Unit price | 33 of 36 | 57 of 61 |
| Negotiated final price | 5 of 5 | 8 of 8 |
| Quantity | 32 of 36 | 54 of 61 |
| Total | 23 of 25 | 37 of 40 |
| Payment classification | 10 of 10 | 12 of 14 |
| Agreement detection | 20 of 22 | 40 of 44 |
| Speaker attribution | 13 of 14 | 29 of 30 |
| Review precision / recall | 94% / 100% | 90% / 88% |
| Records needing review | 56% | 49% |
| Values marked confirmed that were right | 109 of 109 | 178 of 178 |
| Unflagged financial errors | 0 | 1 |

The one unflagged error (`unpaid-05`, "₦52,000, pay on delivery") is a payment status recorded as "not
specified" instead of "unpaid"; no amount is wrong. It follows from the rule added that day that treats
the model's "unpaid" as agreeing with "not specified" when the chat has no payment messages: "pay on
delivery" is a payment term the rule doesn't yet consider.

Before that day's rule changes, the same development answers scored 16 of 39 fully correct, with review
precision of 50% (most clean records were flagged, mainly for that payment wording and for an implied
quantity of 1).

**What these numbers don't show.** The chats are synthetic and cleaner than real ones, and they measure
`gemini-3.5-flash-lite`, not `gemini-2.5-flash`, which the live channels use; the free quota on the live
models is kept for real customers. Labels were written by the same assistant that wrote the rules, which
can share blind spots; a person labelling real (consented) chats would be the stronger test.
