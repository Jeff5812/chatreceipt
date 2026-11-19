// Mirrors backend/app/models.py. Amounts are decimal strings; null means unknown.

export type PaymentStatus = "not_specified" | "unpaid" | "partially_paid" | "paid" | "refunded";
export type Confidence = "high" | "medium" | "low";
export type AdjustmentKind = "amount" | "percent";

export interface LineItemDraft {
  id: string;
  description: string;
  quantity: string;
  unit_price: string;
}

export interface AdjustmentDraft {
  id: string;
  label: string;
  kind: AdjustmentKind;
  value: string;
}

export interface TransactionDraft {
  currency: string;
  seller: string;
  buyer: string;
  transaction_date: string;
  due_date: string;
  line_items: LineItemDraft[];
  discounts: AdjustmentDraft[];
  additional_fees: AdjustmentDraft[];
  delivery_fee: string;
  amount_paid: string;
  payment_status: PaymentStatus;
  payment_terms: string;
  delivery_terms: string;
  notes: string;
  stated_total: string;
  stated_outstanding: string;
}

export type RowGroup = "line_items" | "discounts" | "additional_fees";
export type ScalarField = Exclude<keyof TransactionDraft, RowGroup>;

export interface Issue {
  code: string;
  severity: "error" | "warning";
  field: string | null;
  message: string;
}

export interface AmountLine {
  id: string;
  amount: string | null;
}

export interface Calculation {
  currency: string | null;
  minor_units: number | null;
  line_items: AmountLine[];
  items_subtotal: string | null;
  discounts: AmountLine[];
  discount_total: string | null;
  additional_fees: AmountLine[];
  fee_total: string | null;
  delivery_fee: string | null;
  total: string | null;
  amount_paid: string | null;
  outstanding: string | null;
  issues: Issue[];
  can_export: boolean;
}

export interface Evidence {
  screenshot: number;
  quote: string;
}

export interface FieldMeta {
  found: boolean;
  confidence: Confidence | null;
  evidence: Evidence[];
  note: string | null;
}

export interface ReviewReason {
  field: string | null;
  message: string;
}

export interface ExtractionResponse {
  contains_transaction: boolean;
  summary: string;
  draft: TransactionDraft;
  field_meta: Record<string, FieldMeta>;
  review_required: boolean;
  review_reasons: ReviewReason[];
  calculation: Calculation | null;
  screenshots: number;
  images_sent: number;
}

export interface CurrencyInfo {
  code: string;
  name: string;
  symbol: string;
  minor_units: number;
}

export interface PublicConfig {
  currencies: CurrencyInfo[];
  default_currency: string;
  extraction_available: boolean;
  limits: {
    max_files: number;
    max_file_bytes: number;
    max_total_bytes: number;
    accepted_types: string[];
  };
}

export interface ClientStats {
  from_extraction: boolean;
  edited_fields: number;
  added_fields: number;
}

export interface Screenshot {
  id: string;
  name: string;
  size: number;
  url: string;
  file: File | null;
}
