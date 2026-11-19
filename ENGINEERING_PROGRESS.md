# Engineering progress

The Transaction Intelligence Engine brief (13 phases) and the WhatsApp channel, as of 2026-11-19.
A phase counts as complete only when it works and its tests pass.

## Phases

| # | Phase | Status | Notes |
| --- | --- | --- | --- |
| 1 | Repository audit and architecture | Complete | [ARCHITECTURE.md](ARCHITECTURE.md), [TECHNICAL_DECISIONS.md](TECHNICAL_DECISIONS.md) |
| 2 | Domain model and database integrity | Complete | Migrations 0001–0003 applied to Supabase; constraints, foreign keys, row-level security, append-only history |
| 3 | Ingestion and OCR pipeline | Complete | Files checked by content, SHA-256 fingerprints, private storage, AI transcription with the raw reading kept |
| 4 | Conversation normalisation | Complete | Screenshot merging, ordering and gap notes, speakers, chat-export and pasted-text parsing |
| 5 | AI transaction intelligence | Complete | Event-based interpretation; negotiation and agreement rules; several deals per chat |
| 6 | Validation and confidence engine | Complete | Decimal calculations, contradiction detection, per-field states, one review policy; calibration measured |
| 7 | Human review and lifecycle | Complete | Corrections with history, approval safeguards, optimistic concurrency, state machine |
| 8 | Background processing and reliability | Complete | PostgreSQL job queue with leases and fencing, bounded retries, idempotency keys, crash takeover |
| 9 | Audit history and receipts | Complete, one check open | Versioned receipts, void instead of edit. A real-browser check of very long names, many items and page overflow in the PDF is still to do |
| 10 | API security and authorisation | Complete | Bearer sessions, tenant isolation in the database, upload and injection defences; operator session tool and proxy-safe rate limits added 2026-11-19 |
| 11 | Testing and AI evaluation | Complete | 119 labelled conversations, fixed split, runner, scoring and reports; development and held-out runs on 2026-11-19 ([TESTING.md](TESTING.md)) |
| 12 | Observability and performance | Complete, no load test | Structured events, request IDs, health and readiness, private metrics. No load testing was done |
| 13 | Deployment and documentation | In progress | Documents and free deployment files are done; the deployment itself, the demonstration script and seed data are not |

## Done on 2026-11-19

- **Evaluation framework:** `backend/evals/intelligence/{dataset,score,report}.py` and
  `backend/evals/run_intelligence.py`, with 17 tests in `tests/test_intelligence_eval.py`, including the
  prompt-leakage check.
- **Rules tuned on development failures:**
  - a model-written quantity of 1 for a message that counts nothing
  - "unpaid" against "not specified" when nobody mentions payment
  - a priced request counts as an offer
  - a confirmation of an acceptance needs no offer of its own
  - a priced acceptance with no offer before it is kept for review
  - a payment confirmed before any price is flagged as out of order
  - text the AI had to split into messages is flagged

  Engine version `2026.11.19-1`, with 10 new rule tests.
- **Results:**
  - Development: 16 of 39 fully correct before the changes, 36 of 42 after.
  - Held out, run once: 57 of 77 fully correct, 95% of checks.
- **Production readiness:**
  - Rate limits behind a proxy can no longer be dodged with a forged `X-Forwarded-For`.
  - `backend/Dockerfile`, `render.yaml` and [docs/deployment.md](docs/deployment.md) cover free hosting on Vercel and Render.
  - `.env.example` now lists the engine settings.
  - `scripts/api_session.py` creates API accounts and sessions.
- **WhatsApp profile:** picture, about line, description, category, welcome message, first-chat suggestions and the "/" command menu, from `app/whatsapp/profile.py` via `scripts/whatsapp_profile.py`.
- **Public repository:** <https://github.com/Jeff5812/chatreceipt>. Before publishing, the real names from the first phone test were replaced with fictional ones in the tests and evaluations.
- **Website wording:**
  - slogans and repeated disclaimers removed across the landing page and app
  - the "/ chat → record" tagline removed from the header
- **Documentation:**
  - `ARCHITECTURE.md`
  - `TECHNICAL_DECISIONS.md`
  - `EDGE_CASES.md`
  - `API_DOCUMENTATION.md`
  - `TESTING.md`
  - this file

## Known defects

- **Held-out evaluation failures:**
  - One unflagged payment-status label (`unpaid-05`): "pay on delivery" recorded as "not specified", no amount wrong.
  - Two chats wrongly read as having no transaction.
  - Out-of-order messages with decreasing timestamps aren't detected.
  - Quantities are left empty for items that were discussed but not agreed.
  - Review recall is 88%.
- **Pipeline split:** the website and WhatsApp still use the single-step extraction; only `/api/v1` uses the engine pipeline.
- **Different models:** the evaluation measured `gemini-3.5-flash-lite`, not `gemini-2.5-flash`, which the live channels use.
- **Temporary storage on the free host:** the engine keeps files in local storage, so on Render's free plan uploads and receipts under `/api/v1` don't survive a restart.
- **Docker build unverified:** the image hasn't been built locally, because Docker Desktop wasn't running.
- **Display name:** Meta has approved "ChatReceipt", but it only shows after the number is registered again, which needs WhatsApp Manager or the number's two-step verification PIN.

## Deferred

- **Demonstration script:** walks through the whole pipeline and its failure scenarios (brief part 23). The integration tests in `tests/test_engine_pipeline.py` cover the same scenarios, but there's no narrated demo yet.
- **Seed data**, and an analytics API beyond `/api/metrics`.
- **Supabase Storage** behind the storage interface, so engine files persist on free hosting.
- **The website and WhatsApp channel on the engine pipeline**, after evaluating it on the production model.

## Next

1. **Deploy:**
   - a private GitHub repository
   - the API on Render and the website on Vercel ([docs/deployment.md](docs/deployment.md))
   - point the WhatsApp number at the Render URL
2. **Next round of rules, developed on the development split:**
   - payment terms ("pay on delivery") as a sign of "unpaid"
   - decreasing timestamps as out-of-order messages
   - an implied quantity for negotiated items

   Then write a fresh held-out set to measure them, since the current one has now been seen.
3. **Check the PDF in a real browser** with long names, many items and page overflow.
