# Edge cases

What ChatReceipt does in the situations that most often go wrong, and where each is tested
(`backend/tests/…`). The labelled evaluation (`backend/evals/intelligence`) covers the same ground
against the real AI.

## Agreements and prices

| Situation | Behaviour | Tests |
| --- | --- | --- |
| Several prices before an agreement | The accepted offer wins, not the first or a rejected one | `test_negotiation_rules.py` |
| Many rounds of counter-offers | Each offer supersedes the same person's earlier one; the accepted one is recorded | `test_negotiation_rules.py` |
| Price changed after it was accepted | The price is left empty as contradictory, both values listed | `test_negotiation_rules.py` |
| A buyer names the price ("I'll take the jacket for ₦21,000") and the seller agrees | Treated as the buyer's offer, accepted | `test_negotiation_rules.py` |
| "Okay Tolu, it's yours" after the buyer accepted | A confirmation, not a second acceptance | `test_negotiation_rules.py` |
| "Okay, deal at ₦14,000" with no earlier offer shown | The price is kept as an offer for a person to confirm | `test_negotiation_rules.py` |
| Prices exchanged, nothing accepted | Agreement marked as negotiation; price needs a person | `test_negotiation_rules.py` |
| Bundle price ("3 for ₦2,000") | One line for the bundle, not a unit price | `test_negotiation_rules.py` |
| Cancellation, or a deal reinstated after cancelling | Agreement marked cancelled, or reached again | Evaluation cases `cancel-01` to `cancel-06` |
| Two separate orders in one chat | Two records | `test_engine_pipeline.py` |
| Two separate orders in one screenshot set or chat export | The latest becomes the draft; the others are listed, and an export can be read again for one ("deal 1"), keeping the same numbers | `test_chat_export_api.py`, `test_whatsapp_identity.py` |
| No transaction in the chat | No record, with an explanation | `test_engine_pipeline.py` |

## Who is who

| Situation | Behaviour | Tests |
| --- | --- | --- |
| The customer's own name isn't in their chat (it never is) | Asked once on Confirm, with their WhatsApp name or export name one tap away; remembered after that | `test_whatsapp_identity.py`, `test_parties.py` |
| The chat doesn't show which side the customer is on | One question with their usual role first; "Neither" saves without placing them | `test_whatsapp_identity.py` |
| The customer skips the name question twice | Never asked again; "my name is …" still works | `test_whatsapp_identity.py` |
| The model gives the customer's side the other person's name | Nothing is moved and the names are flagged; saying their side swaps them | `test_parties.py` |
| The model is unsure which side the customer is on | Its guess isn't used; the customer is asked instead | `test_parties.py` |
| Only a phone number at the top of the chat | The number is kept, never replaced by an invented name | `test_whatsapp_identity.py` |
| A returning customer's contact | Their confirmed name is filled by phone number, or suggested by label, and marked | `test_whatsapp_identity.py`, `test_parties.py` |
| The chat names someone differently from an earlier receipt | Shown under "Please check"; nothing is overwritten silently | `test_whatsapp_identity.py`, `test_parties.py` |
| Two different people with the same contact name | Never merged; that label then suggests nobody | `test_whatsapp_identity.py` |
| Someone pays for the buyer (a sister, an employer) | Recorded as payer, shown as *Paid by*; the buyer stays as the chat shows | `test_chat_export_api.py`, `test_whatsapp_identity.py` |
| A name mentioned inside a message ("Ada's order") | Belongs to the person mentioned, not the sender (an AI rule; checked in the evaluation) | `evals/cases.json` |
| The customer was the buyer on a record | "Who still owes me?" lists it as money they owe, not money owed to them | `test_whatsapp_identity.py`, `test_parties.py` |
| Someone hides their number behind a WhatsApp username | Identified by Meta's user ID as before, answered by it, and their username is kept for display only | `test_whatsapp_trust.py` |
| The other person shows as "@ada_closet" in a chat | Recognised by that username on the next receipt, like a phone number; a username is never moved to someone with a different number | `test_whatsapp_trust.py` |

## Payments

| Situation | Behaviour | Tests |
| --- | --- | --- |
| "I'll pay tomorrow" | Unpaid; a promise isn't a payment | `test_negotiation_rules.py`, `test_extraction.py` |
| "Payment sent" | Recorded as the payer's claim | `test_negotiation_rules.py` |
| Payer claims, payee says it hasn't arrived | Payment status contradictory, for a person | `test_negotiation_rules.py` |
| Part payment, balance stated | Partially paid; a stated balance that doesn't match the calculation is flagged | `test_calculations.py`, `test_whatsapp_flow.py` |
| An advance with no price ("I've paid 20k advance") | The advance is not taken as the total | `test_whatsapp_flow.py`, `test_whatsapp_understanding.py`, evaluation case `advance-payment-only` |
| Nobody mentions payment | Not specified; the model calling it "unpaid" is not a conflict | `test_negotiation_rules.py` |
| "Received your payment" before any price appears | Payment flagged: the messages are probably out of order | `test_negotiation_rules.py` |
| Refund | Refunded | Evaluation case `cancel-05` (no unit test yet) |
| Text in the chat telling the AI to mark it paid or approve it | Ignored, and the record is flagged | `test_engine_security.py`, `test_extraction.py` |
| The chat says money was paid to the customer | Saved as a claim, and *Saved* asks whether it reached their account; "Not yet" lists it as not in their account yet until they say it arrived | `test_whatsapp_trust.py` |
| The same transfer receipt reference on a second record | Pointed out under *Please check* as a possible sign of a fake transfer; never called fake | `test_whatsapp_trust.py` |

## Figures

| Situation | Behaviour | Tests |
| --- | --- | --- |
| "23k", "₦23,000", "23000", "two" | All recognised as the same figure | `test_negotiation_rules.py` |
| A figure that isn't in the message it's said to come from | Flagged for a person | `test_negotiation_rules.py` |
| "I'll take it" | Quantity 1, marked inferred | `test_negotiation_rules.py` |
| No currency shown | Left for a person; never assumed silently | `test_extraction.py` |
| Negative, too many decimals, not a number | Refused as invalid | `test_calculations.py`, `test_records_rules.py` |
| Currencies without minor units | Rounded to the currency's precision | `test_calculations.py` |

## Conversations

| Situation | Behaviour | Tests |
| --- | --- | --- |
| Overlapping screenshots | Repeated messages merged once | `test_conversations.py` |
| Screenshots in the wrong order, or with gaps | Order kept as uploaded and flagged | `test_conversations.py` |
| Export dates that could be day/month or month/day | Read by `TEXT_DATES_DAY_FIRST` and flagged unless the chat proves the format | `test_conversations.py` |
| Text with no chat format | Split into messages by the AI and flagged: who said what must be checked | `test_conversations.py` |
| Very long chats | Only the last 600 messages are read, and the record says so | `test_chat_import.py` |
| Android and iPhone chat exports, multi-line messages, system lines | Parsed without AI, with sender labels and timestamps kept | `test_chat_import.py` |
| A chat export ZIP with photos | Photos sent with the latest messages are read with the chat; voice notes and other media are counted, not read | `test_chat_import.py`, `test_chat_export_api.py` |
| A group chat export | Read, but its name never decides who the owner is | `test_chat_import.py` |
| An export without the usual file name | The owner stays unknown unless the customer's own name matches one sender, and no name is placed by role | `test_chat_import.py`, `test_chat_export_api.py` |
| A screenshot sent while a chat export is read, or onto its draft | Never mixed in; the customer chooses a new record or keeps the draft | `test_whatsapp_identity.py` |
| The AI copies the export transcript's keys ("[m3] p1 · 10:41 ·", "p2") into quotes, notes or names | Turned back into the message's words, people's names and "the message at 10:41" | `test_chat_export_api.py` |
| The AI reports a total or balance the chat never states as a doubt | Dropped: ChatReceipt adds totals up. Doubts that quote figures are kept | `test_parties.py` |
| "Today" or "Yesterday" in a screenshot | Resolved against when it was sent, in the customer's time zone | `test_extraction.py`, `test_whatsapp_understanding.py` |

## Records, review and receipts

| Situation | Behaviour | Tests |
| --- | --- | --- |
| Approving with a flagged field unresolved | Refused | `test_engine_pipeline.py` |
| Two tabs editing the same record | The stale one gets `409 version_conflict` | `test_engine_pipeline.py` |
| A request retried after a timeout | Same answer, applied once (idempotency key) | `test_engine_pipeline.py` |
| Correcting an approved record | Reopened as a new version; the issued receipt stays as it was | `test_engine_pipeline.py` |
| The same file or conversation submitted twice | Stored once; the repeat is flagged as a possible duplicate | `test_engine_pipeline.py` |
| A worker crashes mid-job | Another worker takes over; the late result is discarded | `test_engine_pipeline.py` |
| The AI is down or answers in the wrong shape | Retried with backoff, then the record fails with a reason and can be retried or entered by hand | `test_engine_pipeline.py`, `test_gemini_provider.py` |
| PDF rendering fails | Retried, then reported | `test_engine_pipeline.py`, `test_documents.py` |
| The other person opens a receipt's confirmation link | They see that one record and confirm it or say what's wrong; the owner is told and the record shows the answer | `test_whatsapp_trust.py` |
| A disputed record | The note goes to the owner; the record stays as saved, and a new one is saved to fix it | `test_whatsapp_trust.py` |

## Security and privacy

| Situation | Behaviour | Tests |
| --- | --- | --- |
| Another customer's IDs | `404`; the database hides their rows even if the code asked | `test_engine_security.py` |
| A file renamed to look like an image | Identified by its content and refused | `test_uploads.py`, `test_engine_security.py` |
| Oversized files, too many files | Refused with a message saying what to do | `test_uploads.py` |
| HEIC photos | Refused in the browser, with a suggestion to send a screenshot instead | Not covered by a test yet |
| Unsigned or wrongly signed WhatsApp webhooks | Refused | `test_whatsapp_units.py` |
| The same WhatsApp message delivered twice | Handled once | `test_whatsapp_flow.py`, `test_whatsapp_identity.py` |
| The same chat export sent twice | Not read again | `test_whatsapp_identity.py` |
| A ZIP with `../` paths, a drive letter, a password, thousands of files or a huge compression ratio | Refused before anything is extracted; nothing is written to disk | `test_chat_import.py`, `test_chat_export_api.py`, `test_whatsapp_identity.py` |
| A document file name with a path in it | Only the base name is kept | `test_parties.py` |
| "Clear history" | The open draft, closed drafts and the remembered conversation are emptied; saved records stay | `test_whatsapp_identity.py` |
| A wrong or guessed confirmation code | "I couldn't find that record"; codes carry about 50 random bits and show one record | `test_whatsapp_trust.py` |
| Someone who confirmed a record deletes their data | Their answer stays on the owner's record without their name, number or username | `test_whatsapp_trust.py` |
| A visitor forging `X-Forwarded-For` to dodge rate limits | Ignored behind a proxy | `test_client_address.py` |
| Errors and logs | No conversation text, phone numbers or query strings | `test_engine_security.py`, `test_whatsapp_units.py` |
| A customer asks to delete their data | Everything for that customer is erased | `test_engine_security.py`, `test_whatsapp_flow.py` |
