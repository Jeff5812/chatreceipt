"use client";

import { formatMoney } from "@/lib/money";
import type { Calculation, CurrencyInfo, TransactionDraft } from "@/lib/types";

function Row({ label, value, strong = false }: { label: string; value: string; strong?: boolean }) {
  // Figures use the mono face; words such as "Not specified" stay in the interface face.
  const isFigure = /\d/.test(value);
  return (
    <div className={`flex justify-between gap-4 py-1.5 ${strong ? "text-ui font-semibold" : "text-sm"}`}>
      <dt className="min-w-0 truncate">{label}</dt>
      <dd className={`shrink-0 ${isFigure ? "font-mono tabular-nums" : "font-normal text-muted"}`}>{value}</dd>
    </div>
  );
}

export function TotalsPanel({
  draft,
  result,
  pending,
  error,
  currency,
  onIgnoreStatedTotal,
  onIgnoreStatedOutstanding,
}: {
  draft: TransactionDraft;
  result: Calculation | null;
  pending: boolean;
  error: string | null;
  currency: CurrencyInfo | undefined;
  onIgnoreStatedTotal: () => void;
  onIgnoreStatedOutstanding: () => void;
}) {
  const money = (value: string | null) => formatMoney(value, currency);
  const amountFor = (lines: { id: string; amount: string | null }[] | undefined, id: string) =>
    lines?.find((line) => line.id === id)?.amount ?? null;
  const statedMismatch = result?.issues.some((issue) => issue.code === "stated_total_mismatch") ?? false;
  const balanceMismatch = result?.issues.some((issue) => issue.code === "stated_outstanding_mismatch") ?? false;
  const statedBalance = (draft.stated_outstanding ?? "").trim();

  return (
    <section aria-labelledby="totals-title" className="overflow-hidden rounded-2xl border border-line bg-surface shadow-e2" aria-busy={pending}>
      <div className="flex items-baseline justify-between border-b border-line bg-paper/70 px-5 py-3.5">
        <h2 id="totals-title" className="font-display text-[21px] leading-none font-bold tracking-[-0.02em]">
          Totals
        </h2>
        <span className="text-caption text-muted" aria-live="polite">
          {pending ? "Updating…" : ""}
        </span>
      </div>
      <div className="px-5 pt-3 pb-5">
        <dl className={`transition-opacity duration-200 ${pending ? "opacity-55" : ""}`}>
          <Row label="Subtotal" value={money(result?.items_subtotal ?? null)} />
          {draft.discounts.map((row) => (
            <Row
              key={row.id}
              label={`${row.label.trim() || "Discount"}${row.kind === "percent" && row.value ? ` (${row.value}%)` : ""}`}
              value={amountFor(result?.discounts, row.id) === null ? "—" : `−${money(amountFor(result?.discounts, row.id))}`}
            />
          ))}
          {draft.additional_fees.map((row) => (
            <Row
              key={row.id}
              label={`${row.label.trim() || "Additional fee"}${row.kind === "percent" && row.value ? ` (${row.value}%)` : ""}`}
              value={money(amountFor(result?.additional_fees, row.id))}
            />
          ))}
          {draft.delivery_fee.trim() !== "" && <Row label="Delivery" value={money(result?.delivery_fee ?? null)} />}
          <div className="mt-2 flex items-baseline justify-between gap-4 border-t border-ink pt-3 pb-1">
            <dt className="text-ui font-semibold">Total</dt>
            <dd className="shrink-0 font-display text-[26px] leading-none font-bold tracking-[-0.03em] tabular-nums">{money(result?.total ?? null)}</dd>
          </div>
          <Row label="Amount paid" value={result?.amount_paid ? money(result.amount_paid) : "Not specified"} />
          <Row label="Outstanding" value={result?.outstanding ? money(result.outstanding) : "Not specified"} strong />
        </dl>
        {result && result.total === null && (
          <p className="mt-3 text-meta text-muted">Complete the highlighted details to see the total.</p>
        )}
        {error && (
          <p role="alert" className="mt-3 text-meta text-danger">
            Totals couldn&apos;t be updated: {error}
          </p>
        )}
        {draft.stated_total.trim() !== "" && (
          <div className={`mt-4 rounded-lg border px-3 py-2.5 text-meta ${statedMismatch ? "border-warn/30 bg-warn-soft text-warn" : "border-line bg-paper/60 text-muted"}`}>
            <p>
              The chat mentions a total of {money(draft.stated_total)}.{" "}
              {statedMismatch ? "That doesn't match the calculated total." : result?.total ? "It matches." : ""}
            </p>
            <button type="button" onClick={onIgnoreStatedTotal} className="mt-1 text-ink underline-offset-4 hover:underline">
              Ignore the total mentioned
            </button>
          </div>
        )}
        {statedBalance !== "" && (
          <div className={`mt-4 rounded-lg border px-3 py-2.5 text-meta ${balanceMismatch ? "border-warn/30 bg-warn-soft text-warn" : "border-line bg-paper/60 text-muted"}`}>
            <p>
              The chat mentions a balance of {money(statedBalance)} still owed.{" "}
              {balanceMismatch ? "That doesn't match the calculated outstanding amount." : result?.outstanding ? "It matches." : ""}
            </p>
            <button type="button" onClick={onIgnoreStatedOutstanding} className="mt-1 text-ink underline-offset-4 hover:underline">
              Ignore the balance mentioned
            </button>
          </div>
        )}
        <p className="mt-4 border-t border-dashed border-line-strong pt-3 text-meta leading-snug text-muted">
          Calculated from the details on this page.
        </p>
      </div>
    </section>
  );
}
