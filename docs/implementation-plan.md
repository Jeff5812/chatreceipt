# Implementation plan

## Architecture

```text
Browser ── Next.js app (localhost:3000)
  ├─ /         landing page
  └─ /create   Upload → Extract → Review → Preview → Export
        │  JSON + multipart over HTTP (CORS, no secrets in the browser)
        ▼
FastAPI backend (localhost:8000)
  ├─ uploads        validate + normalise images in memory (Pillow); never written to storage
  ├─ extraction     provider interface → Claude or Gemini provider (JSON-schema output)
  │                 → strict validation → editable draft + per-field evidence + review flags
  ├─ calculations   deterministic Decimal arithmetic, currency precision, validation issues
  ├─ documents      one Jinja2 template → preview HTML, and headless Chromium → PDF / PNG
  └─ guards         file limits, request-size limit, per-IP rate limits, logs without content
```

Decisions:

- **The backend owns every number.** The frontend never adds anything up: the review form asks
  `POST /api/transactions/calculate` for totals as the user types. Exports recalculate from scratch.
- **One document template.** The preview shown to the user and the exported PDF/PNG come from the
  same HTML, so what you see is what you export.
- **No database in milestone 1.** Nothing is saved server-side; screenshots live only in the
  browser and in the memory of the request that reads them. Accounts and saved history are
  milestone 3.
- **Replaceable AI provider.** `extraction/provider.py` defines the interface; Claude
  (`claude.py`) and Gemini (`gemini.py`) implement it with the same instructions and JSON schema.
  `EXTRACTION_PROVIDER` picks one (`auto` uses whichever key is set, preferring Anthropic). This is a
  switch, not a router: one provider per running app. Tests use a fake provider.

## Project structure

```text
chatreceipt/
  CLAUDE.md, README.md
  docs/                      specs and this plan
  backend/
    app/
      main.py                app factory, CORS, error handling
      config.py              settings from environment
      currencies.py          currency configuration (code, symbol, decimals)
      models.py              API schemas (draft, calculation, extraction result)
      calculations.py        money arithmetic + validation
      uploads.py             image validation and preparation
      rate_limit.py, errors.py, observability.py
      extraction/            provider interface, Claude provider, prompt + schema, service
      documents/             template, fonts, renderer (HTML, PDF, PNG)
      api/                   routes
    tests/
  frontend/
    app/                     layout, landing page, /create
    components/              landing/, workflow/, ui/
    lib/                     API client, types, formatting, draft state
```

## Phases

1. **Milestone 1: core workflow, no accounts** (this milestone).
2. **Milestone 2: accuracy and polish.** An evaluation set of real-world conversations (tricky
   negotiations, overlapping screenshots, part payments), prompt tuning against it, mobile and
   accessibility passes, landing page refinement.
3. **Milestone 3: accounts and saved records.** Supabase Auth, Postgres with row-level security
   (`transactions`, `transaction_items`), history, a deletion path.
4. **Milestone 4: production.** Deployment (Vercel for the frontend, a container with Chromium for
   the API), shared rate limiting, observability dashboards, usage limits.

## Milestone 1 acceptance criteria

- Upload accepts only JPEG, PNG and WebP, judged by file content rather than extension: up to 10
  files, 10 MB each. Users can reorder and remove files and see clear errors.
- Extraction sends screenshots in the user's order, validates the AI response against a strict
  schema, keeps missing values empty, and shows confidence and an evidence quote for each
  extracted field. Provider failures, timeouts and malformed responses show a plain-language error
  and offer manual entry.
- All arithmetic runs in the backend with `Decimal` and per-currency precision. Invalid values
  block export, and the CLAUDE.md §14 calculation cases are covered by tests.
- Every field is editable; items, discounts and fees can be added and removed. The form shows
  which values came from the chat, which were edited and which were added. Totals update live
  from the backend.
- The preview uses the same template as the PDF and PNG exports. Documents state that payments
  are recorded from the conversation, not verified.
- The landing page follows CLAUDE.md §9.
- Layouts work at 375 px wide with no horizontal scrolling.
- Backend tests, TypeScript type-check and the production build all run and pass.

## Risks and limits

- Real extraction has been verified end to end with Gemini (`gemini-3.5-flash`, free tier). The
  Claude provider is covered by tests but hasn't run against the live API, because no Anthropic key
  is configured.
- Accuracy is measured with `backend/evals`: ten synthetic cases covering the CLAUDE.md §5 rules. It
  is a start, not a benchmark; real screenshots (dark mode, overlapping crops, voice notes, images)
  should be added.
- Gemini's free tier has low request limits; a production deployment needs a paid plan for either
  provider.
- PDF/PNG export needs Chromium. Development uses the installed Microsoft Edge; production needs a
  container image with Chromium.
- Rate limits are in memory: they reset on restart and don't span multiple server instances.
- Cost per extraction depends on the provider and model (configurable). Logged cost estimates use
  the price settings for the active provider.
