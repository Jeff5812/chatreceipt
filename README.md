# ChatReceipt

Turn screenshots of a chat into a proper transaction record: what was agreed, what was paid and what
is still owed. Upload screenshots, review what ChatReceipt finds, and download a PDF or image.

- `frontend/`: Next.js app (landing page and the `/create` workflow)
- `backend/`: FastAPI service (upload checks, AI extraction, calculations, documents, WhatsApp, the
  transaction engine and its `/api/v1` API)
- `docs/`: product and technical specs; start with `CLAUDE.md` and `docs/implementation-plan.md`
- [ARCHITECTURE.md](ARCHITECTURE.md), [API_DOCUMENTATION.md](API_DOCUMENTATION.md),
  [TECHNICAL_DECISIONS.md](TECHNICAL_DECISIONS.md), [EDGE_CASES.md](EDGE_CASES.md),
  [TESTING.md](TESTING.md) and [ENGINEERING_PROGRESS.md](ENGINEERING_PROGRESS.md)

## Requirements

- Python 3.12 or newer (developed on 3.14)
- Node.js 20 or newer
- Microsoft Edge or Chrome for PDF/PNG export, or Playwright's Chromium
- An API key for reading screenshots: Google Gemini or Anthropic. Without one, everything else
  works and people enter the details by hand.

## Backend

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements-dev.txt
copy .env.example .env            # macOS/Linux: cp .env.example .env
uvicorn app.main:app --reload --reload-dir app --port 8000
```

`--reload-dir app` restarts the server only when application code changes, not when tests or eval
files are added. On Windows, uvicorn's reloader occasionally stalls after "Reloading..."; if the API
stops reflecting your changes, stop the server and start it again.

Set the AI key as an environment variable rather than in a file, for example on Windows
`setx GEMINI_API_KEY "..."` (then open a new terminal). `EXTRACTION_PROVIDER=auto` uses Anthropic
when `ANTHROPIC_API_KEY` is set and Gemini when `GEMINI_API_KEY` is set. `GET /api/config` reports
whether reading screenshots is available.

To use Playwright's own Chromium for exports instead of Edge, run
`python -m playwright install chromium` and leave `BROWSER_CHANNEL` empty.

## Frontend

```bash
cd frontend
npm install
copy .env.example .env.local      # NEXT_PUBLIC_API_URL points at the backend
npm run dev                       # http://localhost:3000
```

## Checks

```bash
cd backend && python -m pytest            # unit and API tests; one test prints a real PDF and PNG
cd frontend && npx tsc --noEmit && npm run lint && npm run build
```

Extraction accuracy is measured separately against the real AI provider (it uses quota):

```bash
cd backend
python -m evals.render    # draw the synthetic chat screenshots
python -m evals.run       # score every case; see backend/evals/README.md
python -m evals.run_intelligence --split dev --model gemini-3.5-flash-lite   # the engine; see TESTING.md
```

## Deploying

The website runs on Vercel's free plan and the API on Render's free plan, with Supabase and Gemini's
free tiers; no domain or card is needed. Steps and the limits of free hosting are in
[docs/deployment.md](docs/deployment.md).

## WhatsApp

Customers can also send screenshots to a WhatsApp number and confirm the record in the chat. This is
optional and off until it is configured. Setup, testing without Meta credentials and limitations are
in [docs/whatsapp-integration.md](docs/whatsapp-integration.md).

## Privacy

Screenshots are checked and re-encoded in memory, sent to the configured AI provider and then
discarded. The website's upload workflow stores nothing server-side. On WhatsApp, the records a customer
confirms are stored in PostgreSQL until they delete them, and unconfirmed drafts are deleted after 72
hours. Logs record counts, durations and token usage, never conversation content. Payment statuses are
recorded from the conversation and are never verified.
