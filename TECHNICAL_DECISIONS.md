# Technical decisions

Each entry gives the decision, why, and what it costs. Newest considerations are noted with dates.

## Product and AI

**1. The AI interprets; ChatReceipt's code decides.** The model reports events (offers, acceptances,
payment claims…) with the messages they come from. Deterministic rules resolve the agreed price,
quantities, payment status and totals, and the model's own "agreed" reading is only a cross-check.
*Why:* CLAUDE.md §5 forbids relying on AI arithmetic or silently choosing an interpretation; events can be
checked against the text. *Cost:* more rules to maintain, and a misread event still produces a wrong
reading, which the field states and review flags must catch.

**2. A state for every field instead of one confidence score.** Confirmed, inferred, missing,
contradictory, invalid or needs review, with evidence, candidates and reasons. *Why:* a person needs to
know which detail to check, not that the record is "78% sure". Model confidence labels are treated as
ordinal hints, never probabilities.

**3. One review policy object** (`app/intelligence/review_policy.py`) decides which states block
approval, configured by `REVIEW_*` settings. *Why:* thresholds in one place can be changed on evidence;
the evaluation reports what the default and the stricter policy cost in review effort and missed errors.

**4. Every figure must appear in the message it cites.** `money.py` recognises "₦23,000", "23k", "23000"
and number words. *Why:* catches misread digits and invented values cheaply. *2026-11-19:* a quantity of 1
for a message that counts nothing ("I'll take it") is treated as the implied single item instead of an
unsupported figure; a message that counts something else ("2 or 3 of them") is still flagged.

**5. Gemini's free tier, with a provider abstraction.** Main model `gemini-2.5-flash`, fallback
`gemini-3.5-flash` on overload, timeouts and rate limits, `gemini-2.5-flash-lite` for short typed
corrections. Claude is supported by the same interface. *Why:* no budget; evaluations on 2026-11-19 showed
3.5-flash often overloaded or hanging and 2.5-flash reliable. *Cost:* about 20 requests a day per model,
shared by every use, and Google's free-tier terms let it use submitted content to improve its products.

**6. Dates from labels, not from the model's arithmetic.** The model reports "Today", "Yesterday" or a
weekday as shown; the backend resolves it against when the screenshot was sent, in the customer's time
zone (`WHATSAPP_TIMEZONE`). *Why:* the model doesn't know when the chat happened.

## Data and reliability

**7. A modular monolith.** One FastAPI app with separate packages for the website, WhatsApp, the engine
and the pure intelligence code. *Why:* one deployable unit on a free host; the boundaries are packages,
not network calls.

**8. PostgreSQL for everything, including the job queue.** Jobs are rows claimed with `FOR UPDATE SKIP
LOCKED` under leases with fencing tokens; outgoing events use a transactional outbox. *Why:* no Redis or
broker to run or pay for, and results, job completion and events commit together. *Cost:* polling, which
is fine at this scale.

**9. Blocking psycopg 3 through a thread pool.** *Why:* async psycopg can't run on Windows' default
event loop, where development happens, and every query is short. Prepared statements are off so the same
code works through Supabase's transaction-mode pooler.

**10. Tenant isolation enforced by the database.** v1 requests switch to a restricted role with row-level
security per customer. *Why:* a missing `WHERE customer_id = …` in application code can't leak another
customer's records.

**11. Immutable versions and append-only history.** Corrections create versions; receipts record one
version and are voided, never edited. *Why:* a receipt must stay traceable to exactly what was approved.

**12. Optimistic concurrency and idempotency keys.** Changes name the version they were based on;
idempotency keys are stored in the same transaction as the change. *Why:* two tabs or a retried request
can't overwrite or duplicate work.

**13. Opaque session tokens, stored hashed.** *Why:* simple to revoke and expire, nothing to sign or
rotate; the same table serves website sign-ins from WhatsApp and operator-issued API sessions.

## WhatsApp

**14. Store first, work later.** The webhook only verifies, stores and acknowledges; a worker does the
reading and replying, and outgoing messages go through a queue with retries. *Why:* Meta retries slow
webhooks, and AI calls take seconds to a minute.

**15. Deterministic phrases before the AI for typed corrections.** "It's 20k", "the buyer paid 35k",
"balance is 40k" are read by patterns; only other messages go to the AI. *Why:* faster, free, and works
when the AI quota has run out.

**16. A per-number webhook override.** The number's callback is set on the number, not the Meta app,
because the app's own webhook serves another project and must not change.

**16a. Who is who: ask once, at Confirm, and remember** (2026-11-20). A chat never names its owner, so
the customer's own side would always be blank. Asking at the start of every receipt would slow everyone
down; guessing from the WhatsApp profile name would put unconfirmed names on records. Instead the
question comes when they tap Confirm (they're already committed, and it costs one tap), only when a name
would be missing, at most once per receipt, and the answer is remembered. *Why:* the least effort for a
correct record, and the asking stops as soon as ChatReceipt knows.

**16b. Identities match on stable identifiers; names only suggest** (2026-11-20). A customer is Meta's
business-scoped user ID; the people they deal with are remembered by phone number when the chat shows
one, or by the label their phone gives them. A label match only replaces the label itself, and two people
who share a label are never merged. Conflicts with an earlier receipt are shown for review, never settled
silently. *Why:* two different people can share a name, and a wrong merge would put one person's debts on
another's receipts.

**16c. Chat exports are parsed before any AI sees them** (2026-11-20). The `.txt` or `.zip` is read in
memory, deterministically, into the same conversation model the engine uses; the file name settles who
the other person is. The AI then reads numbered lines with the same schema as screenshots. *Why:* who sent
what, and when, is in the file: there's no reason to let a model guess it, and ZIPs need path, size and
compression checks before anything is extracted.

## Documents and hosting

**17. One HTML template printed by Chromium** for HTML, PDF and PNG. *Why:* the record looks identical
everywhere and stays easy to change. *Cost:* Chromium is heavy, the main memory risk on a 512 MB host.

**18. Free hosting: Vercel for the website, Render for the API** (2026-11-19). Vercel's free functions
can't keep workers running or run Chromium; Render runs the API as a normal process but sleeps after 15
idle minutes. See [docs/deployment.md](docs/deployment.md).

**19. Rate limits count the address the proxy reports** (2026-11-19). Behind Render, `CLIENT_IP_HEADER`
names a header set by the proxy (Cloudflare's `CF-Connecting-IP`); otherwise the last `X-Forwarded-For`
entry is used. *Why:* earlier entries can be written by the visitor, which would let anyone bypass the
limits and use up the AI quota.

## Evaluation

**20. A synthetic, hand-labelled dataset with a fixed split.** 119 conversations, every third case in each
category for development and the rest held out, fixed by position so adding cases never moves one across.
Prompts are tested for held-out text. *Why:* no real customer data may be used, and the split must not
leak. *Cost:* synthetic chats are cleaner than real ones; the results show behaviour on these situations,
not real-world frequencies.

**21. Evaluations run on a model the live channels don't use** (`gemini-3.5-flash-lite`), with answers
saved per case. *Why:* the free quota on the production models is needed by real customers, and saved
answers let rule changes be re-scored at no cost. *Cost:* the numbers describe that model, not
`gemini-2.5-flash`.
