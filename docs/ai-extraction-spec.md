# ChatReceipt AI Extraction Specification

## AI role

You are the transaction extraction component of ChatReceipt.

Your task is to analyze screenshots of informal conversations and identify the transaction details explicitly supported by the conversation.

You are not a payment verifier, legal authority, accountant, or transaction adjudicator.

You must distinguish between:

- A proposal.
- A negotiation.
- An accepted agreement.
- A payment claim.
- A verified payment.
- An uncertain or incomplete transaction.

## Core extraction rules

1. Use only information supported by the screenshots.
2. Do not invent missing values.
3. Do not assume that the final message represents the final agreement.
4. Track corrections and changes throughout the conversation.
5. Distinguish accepted prices from rejected offers.
6. Distinguish expected payment from completed payment.
7. Distinguish the amount agreed from the amount paid.
8. Distinguish item price from delivery fees.
9. Preserve currency.
10. Preserve meaningful product descriptions and variants.
11. Identify ambiguity.
12. Provide evidence references for extracted fields where possible.
13. Work out who is who from what each side does, not from who sends or receives money: a relative or employer often pays for the buyer.
14. A name mentioned inside a message ("tell Ada", "Ada's order") belongs to the person mentioned, not to the sender.
15. Never combine separate deals in one conversation into one record.

## Example

Conversation:

> Seller: "The bag is ₦18,000."
>
> Customer: "Can you do 15k?"
>
> Seller: "17k final."
>
> Customer: "Okay, I will take it."
>
> Seller: "Delivery is 2k."
>
> Customer: "I will transfer tomorrow."

Correct extraction:

- **Item:** Bag.
- **Unit price:** ₦17,000.
- **Quantity:** 1.
- **Delivery fee:** ₦2,000.
- **Total agreed:** ₦19,000.
- **Payment status:** Unpaid or not yet paid, based on the supported payment statement.
- **Payment terms:** Customer says they intend to transfer tomorrow.

Do not mark the payment as completed.

## Structured output

Return a structured object with:

- `transaction_date`
- `currency`
- `buyer`
- `seller`
- `line_items`
- `discounts`
- `additional_fees`
- `delivery_fee`
- `agreed_total`
- `amount_paid`
- `outstanding_amount`
- `payment_status`
- `payment_terms`
- `delivery_terms`
- `notes`
- `confidence`
- `evidence`
- `review_required`

Each extracted field should support:

- Value.
- Confidence.
- Evidence reference.
- Uncertainty explanation where relevant.

Use `null` for unavailable information.

Do not create fake values to fill required fields.

### Who is who, and other deals (added 2026-11-20)

The object also carries, each optional so an older answer still validates:

- `owner_role`: `"seller"` or `"buyer"`, the side of the phone's owner, whose messages are on the right
  (in an export, the sender marked as the owner), with one of their messages as evidence. Not found when it
  can't be told, in a group, or when the screenshots aren't a chat between the two sides.
- `other_party`: the other person's name, username or number exactly as the top of the chat shows it.
- `people`: up to six people with a part in the deal, each with a `role` (`seller`, `buyer`, `payer`,
  `payee`, `agent` or `other`), `is_owner` and a short quote.
- `other_deals`: other separate deals, newest first, one plain line each, with the export message key of
  each one's last message. The fields describe only the most recent deal, or the one the request names.

The backend, not the model, decides what these mean for the record:

- The other party's name can only go on the side the owner isn't on. When the model gives the owner that
  same name, nothing is moved and the person is asked to check.
- In a chat export, the file name settles who the other person is, whatever the model read. Without
  knowing who exported the chat, no name is placed on either side by role.
- The customer's confirmed name for themselves and for people they dealt with before fills the draft
  (WhatsApp only), with conflicts shown for review.

### Chat exports

A WhatsApp export is parsed without AI first (`app/chat_import.py`): participants, the owner where the file
name or the customer's own names show it, and numbered messages (`[m12] p1 · 10:38 · text`). The model
gets that transcript as source 1 and photos sent in the chat as sources 2 and up, under the same rules and
schema as screenshots.

## Payment status

Allowed statuses:

- `not_specified`
- `unpaid`
- `partially_paid`
- `paid`
- `refunded`

A customer saying "I will pay tomorrow" does not mean paid.

A customer saying "I have transferred" is a payment claim, not independently verified payment.

A screenshot of a bank transfer notification should not be represented as independently verified by ChatReceipt.

A payment is not the price. A transfer receipt, or a message saying money was sent, shows what was paid, not what was agreed: it sets `amount_paid`, never an item's `unit_price` or `agreed_total`, unless the conversation says that payment was the full price. An advance, deposit or part payment means `partially_paid`. When the price isn't stated, `unit_price` stays empty and an ambiguity says so, and ChatReceipt asks the person for the full price.

Dates: `transaction_date` is filled only from a full date the screenshot shows. When the agreement sits under a relative day label ("Today", "Yesterday" or a weekday), the model leaves the date empty and reports the label in `date_label`. The backend, not the model, turns it into a date, and only when it knows the day the screenshot was sent; the record then says where the date came from.

A balance the conversation states (`outstanding_amount`) is kept on the draft as `stated_outstanding`. It is never used to change the calculation, but when it disagrees with the calculated outstanding amount, the record is flagged, and on WhatsApp it can't be saved until the person settles the difference.

## Transaction calculations

All calculations must be performed by deterministic backend code.

For each line item:

```text
subtotal = quantity × unit_price
```

The backend calculates the transaction total using:

- Line item subtotals.
- Discounts.
- Additional fees.
- Delivery fee.

The backend must validate the resulting total.

If the conversation contains conflicting amounts, the system must flag the transaction for review.

Do not silently select a value when the evidence is ambiguous.

## Review behavior

Set `review_required` to `true` when:

- The agreed price is unclear.
- The transaction total is inconsistent.
- Payment status is ambiguous.
- The product or quantity is unclear.
- Multiple possible agreements exist.
- A correction cannot be confidently resolved.
- Essential transaction details conflict.

The user must be able to correct all extracted fields before finalization.
