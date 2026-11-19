"use client";

import { fieldId, type FieldSource } from "@/lib/draft";
import { formatMoney } from "@/lib/money";
import type { AdjustmentDraft, CurrencyInfo, FieldMeta, Issue } from "@/lib/types";
import { Button } from "../ui/button";
import { CloseIcon, PlusIcon } from "../ui/icons";
import { EvidenceList, IssueList, MoneyInput, SourceTag, hasError, inputClass } from "./field";

interface AdjustmentsEditorProps {
  group: "discounts" | "additional_fees";
  title: string;
  description: string;
  addLabel: string;
  labelPlaceholder: string;
  rows: AdjustmentDraft[];
  amounts: Record<string, string | null>;
  currency: CurrencyInfo | undefined;
  issuesAt: (path: string) => Issue[];
  rowInfo: (row: AdjustmentDraft) => { source: FieldSource; meta?: FieldMeta };
  onUpdate: (id: string, patch: Partial<AdjustmentDraft>) => void;
  onAdd: () => void;
  onRemove: (id: string) => void;
}

export function AdjustmentsEditor(props: AdjustmentsEditorProps) {
  const { group, rows, currency, issuesAt } = props;
  const sign = group === "discounts" ? "−" : "";
  return (
    <div>
      <h3 className="text-ui font-medium">{props.title}</h3>
      <p className="mt-0.5 text-meta text-muted">{props.description}</p>
      {rows.length > 0 && (
        <ol className="mt-3 divide-y divide-line border-y border-line">
          {rows.map((row, index) => {
            const base = `${group}.${row.id}`;
            const { source, meta } = props.rowInfo(row);
            const rowIssues = [...issuesAt(base), ...issuesAt(`${base}.value`)];
            const amount = props.amounts[row.id] ?? null;
            return (
              <li key={row.id} className="py-4">
                <div className="grid grid-cols-[minmax(0,1fr)_auto] gap-3 sm:grid-cols-[minmax(0,1fr)_112px_168px_120px_36px] sm:items-start">
                  <div className="col-span-2 sm:col-span-1">
                    <label htmlFor={fieldId(`${base}.label`)} className="sr-only">
                      {props.title} {index + 1} label
                    </label>
                    <input
                      id={fieldId(`${base}.label`)}
                      className={inputClass()}
                      value={row.label}
                      maxLength={120}
                      placeholder={props.labelPlaceholder}
                      onChange={(event) => props.onUpdate(row.id, { label: event.target.value })}
                    />
                  </div>
                  <div role="radiogroup" aria-label="Amount or percentage" className="inline-flex h-[44px] rounded-lg bg-well p-1 shadow-well">
                    {(["amount", "percent"] as const).map((kind) => (
                      <button
                        key={kind}
                        type="button"
                        role="radio"
                        aria-checked={row.kind === kind}
                        onClick={() => props.onUpdate(row.id, { kind })}
                        className={`min-w-11 flex-1 rounded-md px-2 text-sm transition-[background-color,box-shadow,color] duration-150 ${
                          row.kind === kind ? "bg-surface font-medium text-ink shadow-e1" : "text-muted hover:text-ink"
                        }`}
                      >
                        {kind === "amount" ? (currency?.symbol ?? "Amount") : "%"}
                      </button>
                    ))}
                  </div>
                  <div>
                    {row.kind === "amount" ? (
                      <MoneyInput
                        id={fieldId(`${base}.value`)}
                        label={`${props.title} ${index + 1} amount`}
                        symbol={currency?.symbol ?? "¤"}
                        value={row.value}
                        issues={issuesAt(`${base}.value`)}
                        onChange={(value) => props.onUpdate(row.id, { value })}
                      />
                    ) : (
                      <div className="flex items-stretch">
                        <input
                          id={fieldId(`${base}.value`)}
                          aria-label={`${props.title} ${index + 1} percentage`}
                          aria-invalid={hasError(issuesAt(`${base}.value`)) || undefined}
                          className={inputClass(issuesAt(`${base}.value`), "rounded-r-none font-mono tabular-nums")}
                          inputMode="decimal"
                          autoComplete="off"
                          placeholder="10"
                          value={row.value}
                          onChange={(event) => props.onUpdate(row.id, { value: event.target.value })}
                        />
                        <span className="flex items-center rounded-r-lg border border-l-0 border-line-strong bg-well px-3 text-sm text-muted">%</span>
                      </div>
                    )}
                  </div>
                  <div className="flex items-center justify-between sm:block sm:pt-2.5 sm:text-right">
                    <span className="text-xs text-muted sm:hidden">Amount</span>
                    <span className="font-mono text-ui font-medium tabular-nums">
                      {amount === null ? "—" : `${sign}${formatMoney(amount, currency)}`}
                    </span>
                  </div>
                  <div className="flex justify-end sm:pt-1">
                    <button
                      type="button"
                      onClick={() => props.onRemove(row.id)}
                      aria-label={`Remove ${props.title.toLowerCase()} ${index + 1}`}
                      className="relative flex size-9 items-center justify-center rounded-md border border-transparent text-muted transition-colors after:absolute after:-inset-1 hover:border-line hover:bg-paper hover:text-ink"
                    >
                      <CloseIcon />
                    </button>
                  </div>
                </div>
                {(source !== "manual" || rowIssues.length > 0) && (
                  <div className="mt-2 space-y-1">
                    <SourceTag source={source} meta={meta} />
                    <EvidenceList meta={meta} source={source} />
                    <IssueList issues={rowIssues} />
                  </div>
                )}
              </li>
            );
          })}
        </ol>
      )}
      <Button variant="secondary" size="sm" className="mt-3" onClick={props.onAdd}>
        <PlusIcon />
        {props.addLabel}
      </Button>
    </div>
  );
}
