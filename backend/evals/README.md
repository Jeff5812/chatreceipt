# Extraction evaluation set

Synthetic chat screenshots with known outcomes, used to measure how well extraction follows the
rules in CLAUDE.md §5 (agreement detection, payment detection, ambiguity, source integrity).

| Case | What it checks |
| --- | --- |
| `negotiated-price` | The accepted price, not the first one; delivery agreed separately; a promise to pay stays unpaid |
| `partial-payment` | A part payment acknowledged by the seller is recorded as partially paid |
| `promise-to-pay-pidgin` | Haggling in Nigerian Pidgin; "I go pay you Friday" is not a payment |
| `multiple-items-discount` | Several items with quantities, and a discount agreed on the total |
| `conflicting-prices` | A price changed after acceptance is flagged for review, not silently chosen |
| `unclear-currency` | "85k" with no currency is flagged; "pay half now" is not a payment |
| `prompt-injection` | A message telling the AI to mark the order paid is ignored and flagged |
| `usd-freelance-deposit` | A US dollar job with a deposit received |
| `two-screenshots` | One conversation across two screenshots, paid in full |
| `no-transaction` | A chat with no goods, services or prices |
| `advance-payment-only` | An advance with no price stated: the payment must not become the total (from the first real phone test) |
| `balance-stated` | A job price, an advance and the balance still owed |

## Running it

From `backend/`, with an AI provider configured (see `.env.example`):

```bash
python -m evals.render   # draws evals/screenshots/*.png from cases.json (needs the export browser)
python -m evals.run      # runs every case through the real pipeline and scores it
python -m evals.run --case prompt-injection   # one case
```

`run` sends each case through the same steps as `POST /api/extractions`: upload preparation, the
configured provider, schema validation and the backend's own calculations. It calls the real
provider, so it uses quota (11 requests for the full set). Results go to `evals/results/latest.json`,
which is not committed.

## Adding a case

Add an entry to `cases.json` with the conversation (`from` is `them` for the contact in the header,
`me` for the phone's owner) and the expectations to check. Supported expectations: `contains_transaction`,
`currency`, `seller`, `buyer`, `line_items` (`description_contains`, `quantity`, `unit_price`),
`delivery_fee`, `payment_status`, `payment_status_not`, `amount_paid`, `discount_total`, `total`,
`review_required` and `flags_any` (review reasons on fields starting with these names). Then run
`python -m evals.render` again.

## Results on 2026-11-19

The same 12 cases, after the rule that a payment is not the price:

| Model | Passed | Time per case | Notes |
| --- | --- | --- | --- |
| `gemini-2.5-flash` | 10/12, then 11/12 on a re-run of the failures | 8–16 s | Missed the buyer's name in `promise-to-pay-pidgin` both times; `conflicting-prices` passed on the re-run |
| `gemini-3.5-flash` | 7/12 | 11–74 s | All 7 it answered passed; 5 failed on Google's side (one 503, four rate limits) |

On this evidence `gemini-2.5-flash` became the main model, with `gemini-3.5-flash` as the fallback. The
free tier allows 20 requests a day per model, which these runs used up.

## Typed WhatsApp corrections

```bash
python -m evals.whatsapp_corrections      # 16 typed messages, read by the configured AI
python -m evals.whatsapp_transcript       # replays the first real phone test and prints the conversation
```

`whatsapp_corrections` checks that the AI reports the right changes for messages from the first real
phone test ("It's an advance work. 40k was still remaining") and common phrasings, including
Nigerian Pidgin. On 2026-11-19, `gemini-2.5-flash-lite` understood 16/16, in 1–3 s each.
`whatsapp_transcript` needs PostgreSQL installed and uses a temporary database and a fake WhatsApp API.

