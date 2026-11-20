# Deploying ChatReceipt for free

ChatReceipt runs on free plans only. Nothing here needs a domain, a card or a paid service.

**Live since 2026-11-20.** Everything below is set up; the steps stay here for rebuilding it.

| Part | Where | Address |
| --- | --- | --- |
| Website (Next.js) | Vercel, Hobby plan, project `chatreceipt` | <https://chatreceipt.vercel.app> |
| API, WhatsApp channel and PDF printing (FastAPI) | Render, free web service (Docker), `chatreceipt-api` | <https://chatreceipt-api.onrender.com> |
| Database | Supabase, free project (already set up) | — |
| Reading screenshots | Google Gemini API, free tier (already set up) | — |

Vercel can't host the API: its free functions stop after 300 seconds, can't keep the WhatsApp and job
workers running between requests, and can't run the Chromium that prints PDFs. Render runs the API as an
ordinary always-on process while it is awake.

## What the free plans mean in practice

- **Render sleeps after 15 minutes without traffic** and takes up to a minute to wake. The first upload or
  WhatsApp message after a quiet spell is slow. Meta retries webhook deliveries that fail, so a message
  sent while the API is waking arrives late rather than being lost. Background work (WhatsApp replies,
  receipts) pauses while it sleeps and resumes when the next request wakes it. Render's 750 free instance
  hours a month cover one service running all month. Nothing keeps the API awake: a ping every 10
  minutes would, but the hours are shared by every free service in the Render workspace, and running out
  suspends all of them until the next month.
- **Render gives 512 MB of memory and a tenth of a CPU.** Tested locally with the same limits
  (2026-11-19): the API uses about 115 MB at rest and 245 MB after printing. A PDF or PNG takes 10–20
  seconds, and the first one after the API starts about 45 seconds while Chromium starts, which is why
  `render.yaml` sets `DOCUMENT_TIMEOUT_SECONDS` to 90 (the default is 30).
- **Render's disk is temporary.** Files written by the engine's v1 API (uploads and issued receipts under
  `backend/var`) are lost when the service restarts. The website and the WhatsApp channel don't keep files.
- **Gemini's free tier allows about 20 requests a day per model.** ChatReceipt uses `gemini-2.5-flash` for
  screenshots, `gemini-3.5-flash` when that fails or is busy, and `gemini-2.5-flash-lite` for typed
  WhatsApp corrections, so expect roughly 20–40 screenshot readings a day in total. After that, reading
  fails until the quota resets at midnight Pacific time (09:00 in Lagos); people can still enter details
  by hand on the website.
- **Google may use what's sent on the free tier** to improve its products, under the Gemini API terms for
  unpaid services. ChatReceipt's privacy text says screenshots go to "our AI provider" and that
  ChatReceipt doesn't store them, which stays true, but it doesn't promise anything about Google's side.
- **Supabase pauses a free project after a week without activity.** Any use of the site or WhatsApp
  counts as activity. A paused project is restarted from the Supabase dashboard.

## Before you start

1. The code in a GitHub repository (private is fine). `.env` files are ignored by git and must stay out.
2. Accounts: GitHub, Vercel and Render (sign up to Render with GitHub; no card is asked for on the free plan).
3. Your values from `backend/.env`: `DATABASE_URL`, `GEMINI_API_KEY` (or the environment variable it
   comes from), and the four `WHATSAPP_*` values.

## 1. The API on Render

1. In Render, choose **New → Blueprint** and pick the repository. Render reads `render.yaml`.
2. Fill in the values it asks for: `DATABASE_URL`, `GEMINI_API_KEY`, `WHATSAPP_ACCESS_TOKEN`,
   `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_APP_SECRET`, `WHATSAPP_VERIFY_TOKEN`. For `FRONTEND_ORIGINS` and
   `PUBLIC_WEB_URL`, enter `https://chatreceipt.vercel.app` for now; step 2 gives the real address.
3. Apply. The first build installs Chromium and takes several minutes. When it's live, open
   `https://<service>.onrender.com/api/health/ready`: it should say `"status": "ok"`.

The database schema is already applied. When a new file appears in `backend/app/db/migrations`, apply it
from your computer before deploying the code that needs it:

```bash
cd backend
python -m app.db.migrate      # uses DATABASE_URL from backend/.env
```

## 2. The website on Vercel

1. In Vercel, choose **Add New → Project** and import the repository.
2. Set **Root Directory** to `frontend`. Vercel detects Next.js; leave the build settings as they are.
3. Add the environment variables:
   - `NEXT_PUBLIC_API_URL` = the Render address from step 1, with no slash at the end
   - `NEXT_PUBLIC_WHATSAPP_NUMBER` = the business number in digits with the country code
4. Deploy. Note the address Vercel gives the project, for example `https://chatreceipt.vercel.app`.
5. Back in Render, set `FRONTEND_ORIGINS` and `PUBLIC_WEB_URL` to that exact address (no slash at the end)
   and save. Render redeploys. Until then the browser blocks the website's calls to the API.

## 3. WhatsApp

Meta must deliver messages to the new API instead of the temporary ngrok tunnel. The number uses a
webhook override, so the app's own webhook settings stay untouched:

```bash
curl -X POST "https://graph.facebook.com/v26.0/$WHATSAPP_PHONE_NUMBER_ID" \
  -H "Authorization: Bearer $WHATSAPP_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"webhook_configuration":{"override_callback_uri":"https://<service>.onrender.com/api/whatsapp/webhook","verify_token":"<WHATSAPP_VERIFY_TOKEN>"}}'
```

Meta calls the API to check the verify token, so the API must be awake (open `/api/health` first).

Done on 2026-11-20: the number's override points at `https://chatreceipt-api.onrender.com/api/whatsapp/webhook`.
The app-level callback belongs to another number on the same Meta app, so never change it.

The WhatsApp worker runs inside the API (`WHATSAPP_WORKER_IN_PROCESS`), and only one copy may run against
the database. `backend/.env` on the development computer points at the same database, so it turns the
local workers off; turn Render's off before running one anywhere else.

## 4. Check it

- The website loads at the Vercel address and **Create a receipt** opens.
- Upload a screenshot. The first one after a quiet spell waits while Render wakes.
- Download the PDF and the PNG.
- Send "hi" to the WhatsApp number and get the welcome message.

## Updating and rolling back

- **Website:** push to the main branch and Vercel rebuilds it, skipping the build when nothing under
  `frontend/` changed.
- **API:** Render isn't connected to GitHub through Render's GitHub app, so a push doesn't deploy it.
  Push, then run `python -m scripts.render_deploy` from `backend/` (it uses `RENDER_API_KEY` from
  `backend/.env` and waits until the new version is live), or use **Manual Deploy** in the Render
  dashboard. To deploy on every push instead, connect the repository in the service's settings.
- Apply new migrations before deploying the code that needs them (see step 1).
- To undo a bad release, use **Instant Rollback** on the previous deployment in Vercel, and **Rollback**
  on the previous deploy in Render. Database migrations aren't undone by a rollback, so keep them
  backward-compatible.

## What has been checked

In production on 2026-11-20:

- The website's pages load publicly, its WhatsApp buttons use the business number, and the API accepts
  requests from the website's address and refuses other origins.
- Readiness reports the database, all four migrations and the worker as fine.
- A webhook signed with the app secret is accepted, and a forged one is refused.
- A PDF took 10 seconds and a PNG 3 seconds; a real screenshot was read in 7.5 seconds, with the
  negotiated price and the delivery fee added up by the backend.

Not yet checked since the move: a message from a real phone through the hosted channel.

Before that, the Docker image was run on 2026-11-19 with Render's free limits
(`docker run --memory 512m --cpus 0.1`): health, settings, a calculation, and a real PDF and PNG all
worked, without running out of memory.

To run the same image yourself:

```bash
docker build -t chatreceipt-api backend
docker run --rm -p 18080:8000 --memory 512m --cpus 0.1 -e DOCUMENT_TIMEOUT_SECONDS=90 chatreceipt-api
```
