# Intelligence evaluation: held-out test split, 77 cases

- **Model:** `gemini-3.5-flash-lite` (no fallback model), run 2026-11-19
- **Engine:** 2026.11.19-1; prompts transcribe-v1, interpret-v1; schema interpretation-schema-v1
- **Cases:** 77 of 77 selected were run
- **AI requests behind these results:** 78 (median 10606 ms, 90th percentile 18536 ms; 189,509 input and 103,294 output tokens)

The conversations are synthetic: written and labelled by Claude for this evaluation, not taken from real customers, so these figures show how the engine handles the situations in the dataset, not how often each situation happens in real chats. Only the labels a case gives are scored. See `evals/intelligence/README.md` for the method.

## Results

- **Cases fully correct:** 57 of 77 (74%)
- **Checks passed:** 589 of 622 (95%)
- **Unflagged financial errors:** 1 (a wrong money value on a record that could be approved without anyone being asked)
- **Wrong money values marked as confirmed:** 0
- **Provider errors:** 0

| Measure | Correct | Checked | Accuracy | What it checks |
| --- | --- | --- | --- | --- |
| Transaction detection | 75 | 77 | 97% | Says whether the chat holds a transaction at all |
| Number of records | 72 | 72 | 100% | One record per transaction, no more and no fewer |
| Product | 60 | 61 | 98% | Each agreed item is on the record |
| Item count | 58 | 58 | 100% | No extra or missing rows |
| Quantity | 54 | 61 | 89% | Quantity of each agreed item |
| Unit price | 57 | 61 | 93% | The agreed price, not an earlier offer |
| Currency | 39 | 39 | 100% |  |
| Speaker attribution | 29 | 30 | 97% | Who sold and who bought |
| Delivery fee | 6 | 6 | 100% |  |
| Discount | 4 | 4 | 100% | Total discount, as the backend calculates it |
| Total | 37 | 40 | 92% | The backend's own calculation from the record |
| Payment classification | 12 | 14 | 86% | not specified / unpaid / partially paid / paid / refunded |
| Amount paid | 5 | 5 | 100% |  |
| Agreement detection | 40 | 44 | 91% | enquiry, quotation, negotiation, agreed, unclear, cancelled |
| Transaction validation | 41 | 50 | 82% | Whether the record must be reviewed before approval |

Negotiated prices (the final accepted price in negotiation and counter-offer chats): 8 of 8 (100%).

## Review flags

Positive means the record must be reviewed before approval.

|  | Labelled needs review | Labelled clean |
| --- | --- | --- |
| Engine asked for review | 28 | 3 |
| Engine allowed approval | 4 | 13 |

- **Precision:** 90%; **recall:** 88%; 2 labelled records weren't produced at all
- **Review effort:** 37 of 75 proposed records needed review (49%)

### Agreement status

Rows are the labels, columns what the engine recorded.

| labelled \ recorded | agreement_reached | agreement_unclear | cancelled | enquiry | negotiation | quotation |
| --- | --- | --- | --- | --- | --- | --- |
| **agreement_reached** | 31 |  |  |  |  | 4 |
| **agreement_unclear** |  | 2 |  |  |  |  |
| **cancelled** |  |  | 4 |  |  |  |
| **enquiry** |  |  |  | 1 |  |  |
| **negotiation** |  |  |  |  | 1 |  |
| **quotation** |  |  |  |  |  | 1 |

### Payment status

Rows are the labels, columns what the engine recorded.

| labelled \ recorded | no record | not_specified | paid | partially_paid | refunded | unpaid |
| --- | --- | --- | --- | --- | --- | --- |
| **not_specified** |  | 2 |  |  |  |  |
| **paid** |  |  | 1 |  |  |  |
| **partially_paid** |  |  |  | 4 |  |  |
| **refunded** |  |  |  |  | 1 |  |
| **unpaid** | 1 | 1 |  |  |  | 4 |

## Confidence calibration

How often a value was right, by the state the engine gave it. A useful state is right much more often when it says confirmed than when it says needs review.

| Field state | Right | Values | Accuracy |
| --- | --- | --- | --- |
| confirmed | 178 | 178 | 100% |
| inferred | 36 | 36 | 100% |
| needs_review | 19 | 23 | 83% |
| contradictory | 3 | 6 | 50% |
| missing | 8 | 16 | 50% |

| Model confidence | Right | Values | Accuracy |
| --- | --- | --- | --- |
| high | 189 | 189 | 100% |
| medium | 4 | 4 | 100% |
| none given | 51 | 66 | 77% |

## Review policy comparison

The same model answers, re-scored under the stricter policy that sends medium-confidence money values to review (`REVIEW_MEDIUM_CONFIDENCE_FINANCIAL=needs_review`). No extra AI requests.

| Policy | Records needing review | Review precision | Review recall | Unflagged financial errors |
| --- | --- | --- | --- | --- |
| Default (medium confidence shown as inferred) | 49% | 90% | 88% | 1 |
| Strict (medium confidence needs review) | 52% | 90% | 88% | 1 |

## By category

| Category | Fully correct | Run |
| --- | --- | --- |
| ambiguous_agreements | 2 | 4 |
| cancellations | 4 | 4 |
| contradictions | 3 | 4 |
| corrections | 2 | 4 |
| delivery_fees | 3 | 4 |
| discounts | 4 | 4 |
| incomplete | 2 | 4 |
| missing_prices | 2 | 4 |
| multiple_counteroffers | 3 | 4 |
| multiple_products | 3 | 4 |
| multiple_speakers | 2 | 4 |
| multiple_transactions | 3 | 4 |
| no_transaction | 3 | 3 |
| out_of_order | 2 | 4 |
| partial_payments | 3 | 4 |
| poor_ocr | 3 | 4 |
| price_negotiation | 4 | 4 |
| prompt_injection | 2 | 2 |
| simple_purchase | 4 | 4 |
| unpaid | 3 | 4 |

## Unflagged financial errors

- `unpaid-05`: payment: expected unpaid, got not_specified

## Every failed check

- `multi-05`: review: expected false, got true
- `counter-03`: item.quantity: expected 1, got empty
- `delivery-03`: agreement: expected agreement_reached, got quotation; review: expected false, got true
- `partial-02`: review: expected false, got true
- `unpaid-05`: payment: expected unpaid, got not_specified
- `ambiguous-05`: review: expected true, got false
- `ambiguous-06`: item.product: expected scarf, got scarves; item.quantity: expected empty, got item missing; item.price: expected 4000, got item missing
- `missing-05`: contains_transaction: expected true, got false; total: expected none, got no record; payment: expected unpaid, got no record; review: expected true, got no record
- `missing-06`: item.quantity: expected 1, got empty
- `speakers-02`: agreement: expected agreement_reached, got quotation
- `speakers-05`: buyer: expected Iya Bose, got empty; item.price: expected 5500, got empty; total: expected 22000, got can't be calculated; agreement: expected agreement_reached, got quotation
- `correct-05`: item.price: expected 2400, got empty; total: expected 9600, got can't be calculated; agreement: expected agreement_reached, got quotation
- `correct-06`: item.quantity: expected 1, got empty
- `conflict-02`: item.quantity: expected 1, got empty
- `twodeals-03`: deal 1, item.quantity: expected 1, got empty; deal 1, item.price: expected 200, got empty
- `ocr-03`: item.quantity: expected 1, got empty
- `order-03`: review: expected true, got false
- `order-05`: review: expected true, got false
- `incomplete-03`: contains_transaction: expected true, got false; review: expected true, got no record
- `incomplete-05`: review: expected true, got false
