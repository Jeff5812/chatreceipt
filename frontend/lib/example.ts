import type { ExtractionResponse, Screenshot } from "./types";

// A sample conversation (public/examples/example-chat.png) and what a correct extraction of it looks
// like, so people can see the review step before uploading anything. Totals are still calculated by
// the backend: `calculation` is null so the review step requests them.

export const EXAMPLE_SCREENSHOT: Screenshot = {
  id: "example-1",
  name: "example-chat.png",
  size: 0,
  url: "/examples/example-chat.png",
  file: null,
};

const none = { found: false, confidence: null, evidence: [], note: null };

export const EXAMPLE_EXTRACTION: ExtractionResponse = {
  contains_transaction: true,
  summary:
    "The buyer agreed to buy a brown leather tote for ₦23,000 plus ₦2,000 delivery to Yaba, and says they will transfer tomorrow morning.",
  draft: {
    currency: "NGN",
    seller: "Tola's Bags",
    buyer: "",
    transaction_date: "2026-11-06",
    due_date: "",
    line_items: [{ id: "item-1", description: "Brown leather tote", quantity: "1", unit_price: "23000" }],
    discounts: [],
    additional_fees: [],
    delivery_fee: "2000",
    amount_paid: "",
    payment_status: "unpaid",
    payment_terms: "Buyer says they will transfer tomorrow morning.",
    delivery_terms: "Seller says they can send it to Yaba on Friday.",
    notes: "",
    stated_total: "",
    stated_outstanding: "",
  },
  field_meta: {
    currency: {
      found: true,
      confidence: "high",
      evidence: [{ screenshot: 1, quote: "The brown leather tote is ₦25,000." }],
      note: null,
    },
    seller: { found: true, confidence: "high", evidence: [{ screenshot: 1, quote: "Tola's Bags" }], note: null },
    buyer: { ...none, note: "The buyer only appears as “You”, so no name is shown." },
    transaction_date: {
      found: true,
      confidence: "high",
      evidence: [{ screenshot: 1, quote: "6 November 2026" }],
      note: null,
    },
    due_date: none,
    "line_items.item-1": {
      found: true,
      confidence: "high",
      evidence: [
        { screenshot: 1, quote: "₦23,000 is my final price." },
        { screenshot: 1, quote: "Okay, I'll take it." },
      ],
      note: "The first price was ₦25,000 and the buyer offered ₦22,000. Both sides accepted ₦23,000.",
    },
    delivery_fee: {
      found: true,
      confidence: "high",
      evidence: [{ screenshot: 1, quote: "Delivery to Yaba is ₦2,000." }],
      note: null,
    },
    stated_total: none,
    stated_outstanding: none,
    amount_paid: { ...none, note: "No payment is reported yet." },
    payment_status: {
      found: true,
      confidence: "high",
      evidence: [{ screenshot: 1, quote: "Alright. I'll transfer tomorrow morning." }],
      note: "A promise to pay, not a payment.",
    },
    payment_terms: {
      found: true,
      confidence: "high",
      evidence: [{ screenshot: 1, quote: "Alright. I'll transfer tomorrow morning." }],
      note: null,
    },
    delivery_terms: {
      found: true,
      confidence: "low",
      evidence: [{ screenshot: 1, quote: "I can send it on Friday." }],
      note: "“Friday” isn't tied to a date.",
    },
    notes: none,
  },
  review_required: true,
  review_reasons: [{ field: "delivery_terms", message: "Check the delivery terms: “Friday” isn't tied to a date." }],
  calculation: null,
  screenshots: 1,
  images_sent: 1,
};
