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

export type PersonRole = "seller" | "buyer" | "payer" | "payee" | "agent" | "other";

/** Someone with a part in the deal, as the conversation shows them. */
export interface Person {
  name: string;
  role: PersonRole;
  is_owner: boolean;
  phone: string;
  quote: string;
}

/** Who is who, seen from the person whose phone the conversation came from. */
export interface Parties {
  owner_role: "seller" | "buyer" | "";
  other_party: string;
  other_phone: string;
  people: Person[];
}

/** A separate deal in the same conversation, left out of this record. */
export interface OtherDeal {
  summary: string;
  last_message: string;
}

export interface ChatParticipant {
  key: string;
  name: string;
  side: "owner" | "contact" | "unknown";
  phone: string;
  messages: number;
}

/** What was read from a WhatsApp chat export. */
export interface ChatSummary {
  participants: ChatParticipant[];
  group: boolean;
  messages: number;
  first_day: string;
  last_day: string;
  owner_source: "file_name" | "your_name" | "";
  photos_read: number;
  media_in_archive: number;
  truncated: boolean;
}

export interface ExtractionResponse {
  contains_transaction: boolean;
  summary: string;
  draft: TransactionDraft;
  // Optional so the bundled example, written before these existed, still fits.
  source?: "screenshots" | "chat_export";
  parties?: Parties;
  other_deals?: OtherDeal[];
  chat?: ChatSummary | null;
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
    // A WhatsApp chat export (.txt, or .zip with media). 0 when the API predates exports.
    max_chat_export_bytes?: number;
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
