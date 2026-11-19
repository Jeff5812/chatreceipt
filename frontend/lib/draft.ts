import type {
  AdjustmentDraft,
  FieldMeta,
  LineItemDraft,
  RowGroup,
  ScalarField,
  TransactionDraft,
} from "./types";

// Row ids only need to be unique within a draft; a counter avoids randomness during render.
let rowCounter = 0;
export function newRowId(prefix: "item" | "discount" | "fee"): string {
  rowCounter += 1;
  return `${prefix}-new-${rowCounter}`;
}

export function blankLineItem(): LineItemDraft {
  return { id: newRowId("item"), description: "", quantity: "1", unit_price: "" };
}

export function blankAdjustment(group: "discounts" | "additional_fees"): AdjustmentDraft {
  return { id: newRowId(group === "discounts" ? "discount" : "fee"), label: "", kind: "amount", value: "" };
}

export function emptyDraft(currency: string): TransactionDraft {
  return {
    currency,
    seller: "",
    buyer: "",
    transaction_date: "",
    due_date: "",
    line_items: [blankLineItem()],
    discounts: [],
    additional_fees: [],
    delivery_fee: "",
    amount_paid: "",
    payment_status: "not_specified",
    payment_terms: "",
    delivery_terms: "",
    notes: "",
    stated_total: "",
    stated_outstanding: "",
  };
}

/**
 * Where a value came from, shown next to each field:
 * extracted (unchanged from the chat), edited, added (by the person), missing (not in the chat),
 * or manual (no extraction at all, so no label is needed).
 */
export type FieldSource = "extracted" | "edited" | "added" | "missing" | "manual";

export function scalarSource(
  field: ScalarField,
  draft: TransactionDraft,
  original: TransactionDraft | null,
  meta: FieldMeta | undefined,
): FieldSource {
  if (original === null) return "manual";
  const changed = draft[field] !== original[field];
  if (meta?.found) return changed ? "edited" : "extracted";
  return changed && draft[field] !== "" ? "added" : "missing";
}

export function rowSource(
  group: RowGroup,
  row: LineItemDraft | AdjustmentDraft,
  original: TransactionDraft | null,
): FieldSource {
  if (original === null) return "manual";
  const before = (original[group] as Array<LineItemDraft | AdjustmentDraft>).find((r) => r.id === row.id);
  if (!before) return "added";
  return JSON.stringify(before) === JSON.stringify(row) ? "extracted" : "edited";
}

const COUNTED_FIELDS: ScalarField[] = [
  "currency",
  "seller",
  "buyer",
  "transaction_date",
  "due_date",
  "delivery_fee",
  "amount_paid",
  "payment_status",
  "payment_terms",
  "delivery_terms",
  "notes",
];
const GROUPS: RowGroup[] = ["line_items", "discounts", "additional_fees"];

/** How many values the person changed or added after extraction, for export metrics. */
export function countChanges(
  draft: TransactionDraft,
  original: TransactionDraft | null,
  meta: Record<string, FieldMeta>,
): { edited: number; added: number } {
  let edited = 0;
  let added = 0;
  if (original === null) return { edited, added };
  for (const field of COUNTED_FIELDS) {
    const source = scalarSource(field, draft, original, meta[field]);
    if (source === "edited") edited += 1;
    if (source === "added") added += 1;
  }
  for (const group of GROUPS) {
    for (const row of draft[group]) {
      const source = rowSource(group, row, original);
      if (source === "edited") edited += 1;
      if (source === "added") added += 1;
    }
    const removed = original[group].filter((row) => !draft[group].some((r) => r.id === row.id)).length;
    edited += removed;
  }
  return { edited, added };
}

/** A DOM id for a field path such as "line_items.item-1.unit_price". */
export function fieldId(path: string): string {
  return `field-${path.replace(/[^A-Za-z0-9_-]/g, "-")}`;
}

/** Move focus to the input for a path, falling back to the first input in that row. */
export function focusField(path: string | null): void {
  if (!path) return;
  const candidates = [path, `${path}.description`, `${path}.value`, path.split(".").slice(0, 2).join(".")];
  for (const candidate of candidates) {
    const element = document.getElementById(fieldId(candidate));
    if (element) {
      element.scrollIntoView({ block: "center", behavior: "smooth" });
      element.focus({ preventScroll: true });
      return;
    }
  }
}
