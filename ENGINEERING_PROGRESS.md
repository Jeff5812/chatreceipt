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
| 13 | Deployment and documentation | Deployed, two items open | Live since 2026-11-20 on free plans ([docs/deployment.md](docs/deployment.md)). The demonstration script and seed data are still to do |

## Done on 2026-11-21

Started on 2026-11-20 from the brief "Smart identity resolution, WhatsApp chat import and conversational
intelligence", and finished after that session ran out of usage part-way through.

- **Who is who** (`app/whatsapp/parties.py`, migration 0005):
  - the extraction now reports the phone owner's side, the name at the top of the chat, everyone with a part in the deal (payer, payee, agent) and other deals in the same chat
  - the customer's own name is asked once at Confirm, with their WhatsApp or export name one tap away, and remembered
  - their side is asked only when the chat doesn't show it
  - the people they deal with are remembered by phone number, or suggested by contact label, and conflicts are shown rather than overwritten
  - saved records keep the customer's side, so "who still owes me?" lists money they owe separately
- **WhatsApp chat exports:**
  - `.txt` and `.zip`, Android and iPhone, parsed without AI (`app/chat_import.py`), with ZIP safety checks
  - read on WhatsApp as a document, and on the website through `POST /api/extractions/chat`
  - photos sent near the end of the chat are read with it
  - "deal 1" (or a button on the website) reads the same export for another deal
- **Conversation:**
  - a **Clear history** button and `clear` command (records stay)
  - "yes" and "no" are read against the question just asked
  - typed name answers ("it's Tola's Bags")
- **Website:**
  - the upload step takes a chat export in the same drop area, and swapping between screenshots and an export is asked first
  - the review step shows who is who with a "You" tag, a swap button, the payer, the chat export's participants and its other deals
  - evidence is labelled "Chat" and "Photo 1" for exports
- **Tests:** 749 backend tests pass, 110 of them new: identity and exports end to end (28), party rules (47), chat import (26) and the export endpoint (9). The existing WhatsApp flow tests now give both names by default, so they test what they did before.
- **Checked with the real AI on `gemini-2.5-flash`:**
  - 3 eval cases passed, including the owner's side and the other person's name
  - one chat export was read correctly: two deals kept apart, a third-party payer, and the totals added up by the backend
- **Deployment:**
  - migration 0005 was applied to production on 2026-11-21; it is additive, and the API already running there stays ready with it
  - the code is not deployed yet: it waits for a push to GitHub, then `scripts/render_deploy.py`

## Done on 2026-11-20

- **Deployed on free plans:**
  - website: <https://chatreceipt.vercel.app> (Vercel Hobby). Builds when `frontend/` changes; preview builds need a Vercel login
  - API: <https://chatreceipt-api.onrender.com> (Render free, Frankfurt), now running the WhatsApp worker, which had been off while a computer ran it through a tunnel
  - WhatsApp: the number's webhook moved from the tunnel to Render, and the profile lists the website
  - migration 0004 applied
- **Checked in production:**
  - readiness: database, migrations and worker
  - Meta's signature check
  - a PDF in 10 s and a PNG in 3 s
  - a real screenshot read in 7.5 s, with the negotiated price and delivery added up by the backend
- **WhatsApp questions about saved records** (`app/whatsapp/assistant.py`): "who still owes me?", totals for a period, records by person or reference, a receipt sent again.
  - every figure comes from the database
  - the AI's names, references and wording are used only after checks
  - 23 new tests (18 unit, 5 end to end)
  - `evals.whatsapp_corrections` gained 5 request cases and `--only`; 11 of 11 chosen cases passed on `gemini-2.5-flash-lite`
- **Safety for local runs:** `backend/.env` points at the production database, so it now turns off both in-process workers on this computer.
- **`scripts/render_deploy.py`:** deploys the API, because Render isn't connected to GitHub through its app and a push doesn't deploy it.

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
- **Free-plan sleep:** Render stops the API after 15 minutes without traffic, so the first upload or WhatsApp reply after a quiet spell waits about a minute while it wakes. No keep-alive is set up.
- **No deploy on push for the API:** Render isn't connected through its GitHub app. Run `python -m scripts.render_deploy` after pushing, or connect the repository in the service's settings.
- **Business number stays visible:** WhatsApp always shows a business's number. The display name is "ChatReceipt" since 2026-11-19, applied by registering the number again.

## Deferred

- **Demonstration script:** walks through the whole pipeline and its failure scenarios (brief part 23). The integration tests in `tests/test_engine_pipeline.py` cover the same scenarios, but there's no narrated demo yet.
- **Seed data**, and an analytics API beyond `/api/metrics`.
- **Supabase Storage** behind the storage interface, so engine files persist on free hosting.
- **The website and WhatsApp channel on the engine pipeline**, after evaluating it on the production model.

## Next

1. **Send "hi" and a screenshot from a real phone** to the hosted WhatsApp channel; it has only been checked with signed test requests since the move to Render.
2. **Next round of rules, developed on the development split:**
   - payment terms ("pay on delivery") as a sign of "unpaid"
   - decreasing timestamps as out-of-order messages
   - an implied quantity for negotiated items

   Then write a fresh held-out set to measure them, since the current one has now been seen.
3. **Check the PDF in a real browser** with long names, many items and page overflow.
