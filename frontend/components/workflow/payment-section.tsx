"use client";

import { fieldId, type FieldSource } from "@/lib/draft";
import type { CurrencyInfo, FieldMeta, Issue, PaymentStatus } from "@/lib/types";
import { StatusShape } from "../ui/status";
import { EvidenceList, IssueList, SourceTag } from "./field";

const SHAPE_COLOUR: Record<PaymentStatus, string> = {
  not_specified: "text-muted",
  unpaid: "text-unpaid",
  partially_paid: "text-partial",
  paid: "text-accent",
  refunded: "text-refunded",
};

const OPTIONS: { value: PaymentStatus; label: string; description: string }[] = [
  { value: "not_specified", label: "Not specified", description: "Payment wasn't discussed." },
  { value: "unpaid", label: "Unpaid", description: "Payment is expected or promised." },
  { value: "partially_paid", label: "Partially paid", description: "Part of the amount was paid." },
  { value: "paid", label: "Paid", description: "The conversation says the full amount was paid." },
  { value: "refunded", label: "Refunded", description: "The conversation says it was refunded." },
];

export const AMOUNT_PAID_HINTS: Record<PaymentStatus, string> = {
  not_specified: "Only if the conversation says an amount was paid.",
  unpaid: "Leave this empty.",
  partially_paid: "How much has been paid so far.",
  paid: "Leave empty if the full total was paid.",
  refunded: "Only if the conversation says an amount was paid.",
};

export function PaymentStatusField({
  value,
  source,
  meta,
  issues,
  onChange,
}: {
  value: PaymentStatus;
  source: FieldSource;
  meta?: FieldMeta;
  issues: Issue[];
  currency: CurrencyInfo | undefined;
  onChange: (value: PaymentStatus) => void;
}) {
  return (
    <fieldset className="space-y-2">
      <div className="flex items-baseline justify-between gap-3">
        <legend className="text-sm font-medium">Payment status</legend>
        <SourceTag source={source} meta={meta} />
      </div>
      <div className="grid gap-2 sm:grid-cols-2">
        {OPTIONS.map((option) => {
          const checked = option.value === value;
          return (
            <label
              key={option.value}
              className={`flex cursor-pointer gap-3 rounded-xl border px-3.5 py-3 transition-[border-color,box-shadow,background-color] duration-150 ${
                checked
                  ? "border-accent bg-accent-soft/60 shadow-[0_0_0_1px_rgb(196_242_90/0.45)]"
                  : "border-line-strong bg-surface hover:border-ink/35 hover:shadow-e1"
              }`}
            >
              <input
                id={checked ? fieldId("payment_status") : undefined}
                type="radio"
                name="payment_status"
                value={option.value}
                checked={checked}
                onChange={() => onChange(option.value)}
                className="mt-1 size-4 shrink-0 accent-accent"
              />
              <span>
                <span className="flex items-center gap-2 text-ui font-medium">
                  <StatusShape status={option.value} className={`size-3.5 ${SHAPE_COLOUR[option.value]}`} />
                  {option.label}
                </span>
                <span className="block text-meta leading-snug text-muted">{option.description}</span>
              </span>
            </label>
          );
        })}
      </div>
      <EvidenceList meta={meta} source={source} />
      <IssueList issues={issues} />
      <p className="text-meta text-muted">
        This records what the conversation says. ChatReceipt doesn&apos;t verify payments.
      </p>
    </fieldset>
  );
}
