# WhatsApp integration

Customers can send a chat or receipt screenshot to ChatReceipt's WhatsApp number, check what was read, correct it, and save a record they can later open on the website.

**Status: tested end to end with a real phone on 2026-11-18 and 2026-11-19, through a temporary tunnel to a computer. It isn't hosted anywhere permanent yet, so it only answers while that computer and tunnel are running.**

## How it works

```text
WhatsApp ── signed webhook ──> POST /api/whatsapp/webhook
                                 │ verify signature, store each message once, answer 200
                                 ▼
                     PostgreSQL (inbound messages, drafts, replies waiting to be sent)
                                 ▲
                                 │
                     Background worker
                       ├─ handles each message, in order per customer
                       ├─ downloads the image from WhatsApp
                       ├─ prepare_screenshots()  same checks as the website
                       ├─ run_extraction()       same AI extraction as the website
                       └─ sends replies, retrying temporary failures
```

The channel adds no second extraction engine. It reuses these parts of the website's workflow:

| Existing code | Used for |
| --- | --- |
| `app/uploads.py` `prepare_screenshots` | Checking and preparing every image |
| `app/extraction/service.py` `run_extraction` | Reading the screenshots |
| `app/models.py` `TransactionDraft`, `Calculation` | The draft and saved record format |
| `app/calculations.py` `compute` | Every total, and validating every correction |
| `/api/documents/pdf` | Downloading a saved record as a PDF |

## What customers can do

| They send | What happens |
| --- | --- |
| Any first message | Welcome, privacy note and menu |
| A screenshot, or several together | Acknowledged, read (with the typing indicator showing), and summarised with the one question that matters next |
| A correction in their own words | Understood and applied, such as “it's 20k”, “he paid 20k and still owes 40k” or “change the buyer to Emeka and the total to 60k”. They get a short update, not the whole draft again |
| A bare amount, when a price is asked for | Applied straight away, without the AI: “20k”, “₦20,000”, “#20,000”, “20 thousand”, “20k is the price” |
| Correct | A list of details to change, for anyone who prefers choosing |
| `try again` | The screenshot is read again; after corrections, it asks first. Also works after a failed read |
| `show` | The whole draft again |
| `start again` | The draft is cleared, ready for a new screenshot |
| Confirm | The record is saved with a reference such as `CR-000001`, and the receipt arrives as an image they can forward |
| Get PDF | The same receipt as a PDF they can keep or print |
| `recent` | Their last five saved records |
| `web` | A private link to their records, which works once and expires after 15 minutes |
| `cancel` | The draft is discarded |
| `help` | How it works |
| `delete my data` | After a confirmation, deletes their records and details |

Confirm is only offered when the record can be calculated and every figure the customer or the chat stated agrees with it. When a stated balance or total disagrees, the customer gets one question with the fix, such as “So the full price was ₦60,000 (₦20,000 paid + ₦40,000 still owed)?”, and nothing changes until they answer. Anything the conversation left unclear is listed under "Please check" until the customer corrects that figure, and values the customer changed are marked as edited.

### Dates

WhatsApp labels recent days "Today", "Yesterday" or with a weekday instead of a date. The AI reports that label, and the backend works out the date from the day the screenshot arrived, in `WHATSAPP_TIMEZONE` (default `Africa/Lagos`). The review says where it came from, such as “19 November 2026 (“Today” in the chat)”, so an old screenshot is caught. When no date can be found, tapping Confirm asks once, with **Today**, **Yesterday** and **No date** buttons. A typed answer also works: “yesterday”, “Monday”, “3 days ago”, “10 Nov” or “10/11”. Due dates look forward, so “Friday” is the next Friday.

### How typed corrections are understood

1. Commands (`confirm`, `try again`, `show`, `cancel` …), bare amounts and common one-line corrections (“he paid 20k”, “balance is 40k”, “35k is the final price”, “the buyer is Emeka”) are read without any AI, which also saves the daily AI quota.
2. Anything else is read by the AI provider's light model (`GEMINI_LIGHT_MODEL`, by default `gemini-2.5-flash-lite`), given the draft, the question it answers, the open checks and the customer's last few messages about the draft. It only reports what the customer said, as a list of changes.
3. Each change is validated by the same code as the guided corrections, the backend recalculates everything, and the payment status follows the amounts unless the customer chose it.
4. While a correction is being understood, the customer's next messages wait, so a Confirm tapped meanwhile can't save the old figures.

Check the AI's reading with `python -m evals.whatsapp_corrections` (16 messages from the real test and common phrasings).

## Customer identity

The repository had no accounts, so a customer is identified by the sender on Meta's signed webhook, using Meta's business-scoped user ID.

- A phone number alone never gives access to existing records, because numbers get reassigned. If a different user ID arrives with a known number, that person becomes a new customer.
- When Meta reports that someone changed their number, their records follow them.
- The website has no passwords. Sending `web` issues a one-time link to that WhatsApp account, and the link starts a 12-hour session. Only hashes of codes and session tokens are stored.
- Every database query for a customer's data is filtered by the customer the signature or session proves.

## Reliability

| Situation | Behaviour |
| --- | --- |
| Meta delivers a message twice | The message ID is unique, so it is handled once |
| A customer taps Confirm twice | One record is saved; the second tap says it is already saved |
| An old button is tapped after a correction | It is refused and the latest version is shown |
| The database is down | The webhook answers 503 and Meta delivers again later |
| The AI provider fails temporarily | A second model answers at once (`GEMINI_FALLBACK_MODEL`); otherwise retried with backoff up to 4 times, and the customer is told once if it is slow |
| The main model stalls | After `GEMINI_PRIMARY_TIMEOUT_SECONDS` (60) the fallback model answers instead of waiting out the full timeout |
| The AI can't read a typed correction | The draft is left unchanged and the customer is asked to say it again or tap Correct |
| Several photos sent together | WhatsApp delivers an extra item it can't describe; it waits for the photos and gets no reply |
| The AI provider refuses, or the image is invalid | The customer gets a plain explanation; nothing is saved |
| A worker stops mid-read | Its lease expires and another attempt takes over; a late result is discarded |
| Sending a reply fails temporarily | Retried in order, up to 8 times |
| A reply falls outside WhatsApp's 24-hour window | Marked as undeliverable and not retried |

## Privacy

- ChatReceipt never stores screenshot images. It keeps WhatsApp's media ID, downloads the image when reading it, and discards it. Meta holds the media under its own terms.
- Screenshots are sent to the configured AI provider to be read.
- Receipt images and PDFs are created when they're sent and aren't stored by ChatReceipt. They're uploaded to WhatsApp to be delivered, and Meta holds them under its own terms.
- Confirmed records are stored until the customer deletes them. Unconfirmed drafts are deleted after 72 hours.
- Message text is cleared once handled, and reply content is cleared once sent.
- Typed corrections are sent to the AI provider to be understood. The last four messages about a draft are kept with it as context, and are deleted when the draft is saved, cancelled or expires.
- Logs contain counts, codes and durations, never phone numbers or conversation content.
- All tables are in a private `chatreceipt` schema with row-level security on and no policies, so Supabase's public REST API can't read them.

## Set it up

### 1. Database

Create a PostgreSQL database (a free Supabase project works), then from `backend/`:

```bash
# Put DATABASE_URL in backend/.env first
python -m app.db.migrate
```

### 2. Meta

1. Create a Business app at [developers.facebook.com](https://developers.facebook.com/apps) and add the WhatsApp product.
2. Note the **Phone number ID**, and the **App secret** from App settings > Basic.
3. Create a permanent access token: a system user in Meta Business Settings with the `whatsapp_business_messaging` and `whatsapp_business_management` permissions.
4. Choose a long random **verify token**.

### 3. Backend

Fill these in `backend/.env`, then restart the API:

| Variable | Value |
| --- | --- |
| `DATABASE_URL` | PostgreSQL connection string |
| `WHATSAPP_ACCESS_TOKEN` | The system user's token |
| `WHATSAPP_PHONE_NUMBER_ID` | From the WhatsApp API setup page |
| `WHATSAPP_APP_SECRET` | From App settings > Basic |
| `WHATSAPP_VERIFY_TOKEN` | The value you chose |
| `PUBLIC_WEB_URL` | The website's public address |

### 4. Webhook

The API must be reachable over HTTPS with a valid certificate. In the Meta app, open WhatsApp > Configuration, then:

1. Set the callback URL to `https://YOUR-API-HOST/api/whatsapp/webhook`.
2. Enter the verify token and save.
3. Subscribe to the `messages` field.

### 5. Website

Set `NEXT_PUBLIC_WHATSAPP_NUMBER` in the frontend's environment to the business number, digits only with the country code, and rebuild. Until it is set, the site shows no WhatsApp buttons.

### 6. Profile, welcome message and commands

What people see before they type lives in `backend/app/whatsapp/profile.py`:

- **Profile picture:** Chit and the wordmark on the dark ground, `docs/brand/whatsapp-profile.png`. Draw it
  again with `python docs/brand/render_profile.py`.
- **About line, description and category.**
- **Welcome message:** Meta tells the bot when someone opens the chat for the first time, and the bot
  answers with its welcome.
- **First-chat suggestions:** "Turn a chat into a receipt", "How does ChatReceipt work?", "Show my recent
  records" and "Open my records on the web".
- **The "/" menu:** `help`, `new`, `show`, `retry`, `recent`, `web` and `cancel`.

Apply it, and check what's set, with:

```bash
cd backend
python -m scripts.whatsapp_profile apply --picture ../docs/brand/whatsapp-profile.png
python -m scripts.whatsapp_profile show
```

Add `--website https://<your-site>` once the website is hosted. A test checks that every suggestion and
command reaches a command the bot understands.

The display name is set separately. Meta approves a new name, and it shows once the number is registered
again: in WhatsApp Manager, or through the API with the number's two-step verification PIN.

### 7. Test with a real phone

Send "hi" to the number, then a screenshot, then confirm. Meta's test number can only message phone numbers you add in the API setup page. To serve the public you need a verified business and an approved display name.

## Cost

Meta gives each business number [1,000 delivered service messages a month for free](https://developers.facebook.com/documentation/business-messaging/whatsapp/pricing) and charges from message 1,001. A returning customer's receipt uses at least three replies (acknowledgement, summary and saved confirmation), and each correction adds more, so the free tier covers roughly 300 simple receipts a month.

AI usage is separate. Gemini's free tier allows **20 requests a day per model** (measured on 2026-11-19). Each screenshot uses one request on the main model, and each typed correction that isn't a bare amount uses one on the light model, so the free tier covers roughly 20 screenshots a day, or about 40 with the fallback model. Real use needs a paid Gemini plan.

## Try it locally without Meta

The simulator plays both the customer and the WhatsApp API.

Terminal 1, from `backend/`, with `DATABASE_URL` set and the schema applied:

```powershell
$env:WHATSAPP_ACCESS_TOKEN = "local"
$env:WHATSAPP_PHONE_NUMBER_ID = "15550001111"
$env:WHATSAPP_APP_SECRET = "local-secret"
$env:WHATSAPP_VERIFY_TOKEN = "local-verify"
$env:WHATSAPP_GRAPH_BASE_URL = "http://127.0.0.1:8099"
uvicorn app.main:app --port 8000
```

Terminal 2, from `backend/`, with the same variables set:

```powershell
python -m scripts.whatsapp_sim demo --image evals\screenshots\negotiated-price-1.png   # the whole journey
python -m scripts.whatsapp_sim chat                                                    # type messages yourself
```

Automated tests need no credentials. They start a private PostgreSQL if one is installed, or use `TEST_DATABASE_URL`:

```bash
python -m pytest tests/test_whatsapp_flow.py tests/test_whatsapp_units.py
```

## Known limitations

- Only reachable while the computer running the backend and the tunnel is on; it needs permanent hosting.
- Gemini's free tier limits it to about 20 screenshots a day (see Cost).
- A correction the AI misreads is shown to the customer before anything is saved, but isn't flagged as uncertain.
- Saved records can't be edited after confirmation. The customer can delete all their data, but not one record.
- WhatsApp records and website uploads are separate: the website's upload workflow still saves nothing.
- Rate limits for the sign-in endpoint are kept in memory per server. The per-customer WhatsApp limits are stored in the database.
- Replies more than 24 hours after a customer's last message can't be delivered, because that needs Meta-approved message templates.
