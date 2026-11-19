# Intelligence evaluation: development split, 42 cases

- **Model:** `gemini-3.5-flash-lite` (no fallback model), run 2026-11-19
- **Engine:** 2026.11.19-1; prompts transcribe-v1, interpret-v1; schema interpretation-schema-v1
- **Cases:** 42 of 42 selected were run
- **AI requests behind these results:** 43 (median 4294 ms, 90th percentile 9491 ms; 103,947 input and 59,513 output tokens)

The conversations are synthetic: written and labelled by Claude for this evaluation, not taken from real customers, so these figures show how the engine handles the situations in the dataset, not how often each situation happens in real chats. Only the labels a case gives are scored. See `evals/intelligence/README.md` for the method.

## Results

- **Cases fully correct:** 36 of 42 (86%)
- **Checks passed:** 343 of 359 (96%)
- **Unflagged financial errors:** 0 (a wrong money value on a record that could be approved without anyone being asked)
- **Wrong money values marked as confirmed:** 0
- **Provider errors:** 0

| Measure | Correct | Checked | Accuracy | What it checks |
| --- | --- | --- | --- | --- |
| Transaction detection | 42 | 42 | 100% | Says whether the chat holds a transaction at all |
| Number of records | 39 | 40 | 98% | One record per transaction, no more and no fewer |
| Product | 35 | 36 | 97% | Each agreed item is on the record |
| Item count | 33 | 34 | 97% | No extra or missing rows |
| Quantity | 32 | 36 | 89% | Quantity of each agreed item |
| Unit price | 33 | 36 | 92% | The agreed price, not an earlier offer |
| Currency | 26 | 26 | 100% |  |
| Speaker attribution | 13 | 14 | 93% | Who sold and who bought |
| Delivery fee | 4 | 4 | 100% |  |
| Discount | 2 | 2 | 100% | Total discount, as the backend calculates it |
| Total | 23 | 25 | 92% | The backend's own calculation from the record |
| Payment classification | 10 | 10 | 100% | not specified / unpaid / partially paid / paid / refunded |
| Amount paid | 4 | 4 | 100% |  |
| Agreement detection | 20 | 22 | 91% | enquiry, quotation, negotiation, agreed, unclear, cancelled |
| Transaction validation | 27 | 28 | 96% | Whether the record must be reviewed before approval |

Negotiated prices (the final accepted price in negotiation and counter-offer chats): 5 of 5 (100%).

## Review flags

Positive means the record must be reviewed before approval.

|  | Labelled needs review | Labelled clean |
| --- | --- | --- |
| Engine asked for review | 15 | 1 |
| Engine allowed approval | 0 | 12 |

- **Precision:** 94%; **recall:** 100%
- **Review effort:** 23 of 41 proposed records needed review (56%)

### Agreement status

Rows are the labels, columns what the engine recorded.

| labelled \ recorded | agreement_reached | agreement_unclear | cancelled | negotiation | quotation |
| --- | --- | --- | --- | --- | --- |
| **agreement_reached** | 17 | 1 |  |  | 1 |
| **agreement_unclear** |  | 1 |  |  |  |
| **cancelled** |  |  | 1 |  |  |
| **negotiation** |  |  |  | 1 |  |

### Payment status

Rows are the labels, columns what the engine recorded.

| labelled \ recorded | not_specified | paid | partially_paid | unpaid |
| --- | --- | --- | --- | --- |
| **not_specified** | 2 |  |  |  |
| **paid** |  | 1 |  |  |
| **partially_paid** |  |  | 2 |  |
| **unpaid** |  |  |  | 5 |

## Confidence calibration

How often a value was right, by the state the engine gave it. A useful state is right much more often when it says confirmed than when it says needs review.

| Field state | Right | Values | Accuracy |
| --- | --- | --- | --- |
| confirmed | 109 | 109 | 100% |
| inferred | 20 | 20 | 100% |
| needs_review | 8 | 10 | 80% |
| contradictory | 2 | 5 | 40% |
| missing | 5 | 8 | 62% |

| Model confidence | Right | Values | Accuracy |
| --- | --- | --- | --- |
| high | 114 | 114 | 100% |
| medium | 3 | 3 | 100% |
| none given | 27 | 35 | 77% |

## Review policy comparison

The same model answers, re-scored under the stricter policy that sends medium-confidence money values to review (`REVIEW_MEDIUM_CONFIDENCE_FINANCIAL=needs_review`). No extra AI requests.

| Policy | Records needing review | Review precision | Review recall | Unflagged financial errors |
| --- | --- | --- | --- | --- |
| Default (medium confidence shown as inferred) | 56% | 94% | 100% | 0 |
| Strict (medium confidence needs review) | 61% | 88% | 100% | 0 |

## By category

| Category | Fully correct | Run |
| --- | --- | --- |
| ambiguous_agreements | 2 | 2 |
| cancellations | 1 | 2 |
| contradictions | 1 | 2 |
| corrections | 2 | 2 |
| delivery_fees | 2 | 2 |
| discounts | 2 | 2 |
| incomplete | 2 | 2 |
| missing_prices | 1 | 2 |
| multiple_counteroffers | 2 | 2 |
| multiple_products | 2 | 2 |
| multiple_speakers | 1 | 2 |
| multiple_transactions | 1 | 2 |
| no_transaction | 2 | 2 |
| out_of_order | 2 | 2 |
| partial_payments | 2 | 2 |
| poor_ocr | 2 | 2 |
| price_negotiation | 3 | 3 |
| prompt_injection | 2 | 2 |
| simple_purchase | 2 | 3 |
| unpaid | 2 | 2 |

## Every failed check

- `simple-04`: agreement: expected agreement_reached, got quotation; review: expected false, got true
- `cancel-04`: item.quantity: expected 1, got empty; item.price: expected 60000, got empty; total: expected 60000, got can't be calculated; agreement: expected agreement_reached, got agreement_unclear
- `missing-01`: item.quantity: expected 1, got empty
- `speakers-04`: seller: expected Chi, got empty; item.price: expected 13000, got empty; total: expected 13000, got can't be calculated
- `conflict-01`: item.quantity: expected 1, got empty
- `twodeals-01`: records: expected 2, got 1; deal 1, item.count: expected 1, got no record; deal 1, item.product: expected cake, got no record; deal 1, item.quantity: expected 2, got no record; deal 1, item.price: expected 15000, got no record
