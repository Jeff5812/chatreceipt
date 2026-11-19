"use client";

import { fieldId, type FieldSource } from "@/lib/draft";
import { formatMoney } from "@/lib/money";
import type { CurrencyInfo, FieldMeta, Issue, LineItemDraft } from "@/lib/types";
import { Button } from "../ui/button";
import { CloseIcon, PlusIcon } from "../ui/icons";
import { EvidenceList, IssueList, MoneyInput, SourceTag, hasError, inputClass } from "./field";

interface ItemsEditorProps {
  items: LineItemDraft[];
  amounts: Record<string, string | null>;
  currency: CurrencyInfo | undefined;
  issuesAt: (path: string) => Issue[];
  rowInfo: (item: LineItemDraft) => { source: FieldSource; meta?: FieldMeta };
  onUpdate: (id: string, patch: Partial<LineItemDraft>) => void;
  onAdd: () => void;
  onRemove: (id: string) => void;
}

export function ItemsEditor({ items, amounts, currency, issuesAt, rowInfo, onUpdate, onAdd, onRemove }: ItemsEditorProps) {
  const groupIssues = issuesAt("line_items");
  return (
    <div>
      <div className="hidden grid-cols-[minmax(0,1fr)_84px_168px_120px_36px] gap-3 border-b border-line pb-2 label-caps text-muted sm:grid">
        <span>Item</span>
        <span>Qty</span>
        <span>Unit price</span>
        <span className="text-right">Amount</span>
        <span />
      </div>
      <ol className="divide-y divide-line">
        {items.map((item, index) => {
          const base = `line_items.${item.id}`;
          const { source, meta } = rowInfo(item);
          const rowIssues = [...issuesAt(base), ...issuesAt(`${base}.description`), ...issuesAt(`${base}.quantity`), ...issuesAt(`${base}.unit_price`)];
          return (
            <li key={item.id} className="py-5">
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-[minmax(0,1fr)_84px_168px_120px_36px] sm:items-start">
                <div className="col-span-2 sm:col-span-1">
                  <label htmlFor={fieldId(`${base}.description`)} className="mb-1 block text-xs text-muted sm:sr-only">
                    Item {index + 1}
                  </label>
                  <input
                    id={fieldId(`${base}.description`)}
                    className={inputClass(issuesAt(`${base}.description`))}
                    aria-invalid={hasError(issuesAt(`${base}.description`)) || undefined}
                    value={item.description}
                    maxLength={200}
                    placeholder="What was sold, e.g. Brown leather bag"
                    onChange={(event) => onUpdate(item.id, { description: event.target.value })}
                  />
                </div>
                <div>
                  <label htmlFor={fieldId(`${base}.quantity`)} className="mb-1 block text-xs text-muted sm:sr-only">
                    Quantity
                  </label>
                  <input
                    id={fieldId(`${base}.quantity`)}
                    className={inputClass(issuesAt(`${base}.quantity`), "font-mono tabular-nums")}
                    aria-invalid={hasError(issuesAt(`${base}.quantity`)) || undefined}
                    inputMode="decimal"
                    autoComplete="off"
                    value={item.quantity}
                    onChange={(event) => onUpdate(item.id, { quantity: event.target.value })}
                  />
                </div>
                <div>
                  <label htmlFor={fieldId(`${base}.unit_price`)} className="mb-1 block text-xs text-muted sm:sr-only">
                    Unit price
                  </label>
                  <MoneyInput
                    id={fieldId(`${base}.unit_price`)}
                    symbol={currency?.symbol ?? "¤"}
                    value={item.unit_price}
                    issues={issuesAt(`${base}.unit_price`)}
                    onChange={(value) => onUpdate(item.id, { unit_price: value })}
                  />
                </div>
                <div className="flex items-center justify-between sm:block sm:pt-2.5 sm:text-right">
                  <span className="text-xs text-muted sm:hidden">Amount</span>
                  <span className="font-mono text-ui font-medium tabular-nums">{formatMoney(amounts[item.id] ?? null, currency)}</span>
                </div>
                <div className="flex justify-end sm:pt-1">
                  <button
                    type="button"
                    onClick={() => onRemove(item.id)}
                    aria-label={`Remove item ${index + 1}`}
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
      <IssueList issues={groupIssues} />
      <Button variant="secondary" size="sm" className="mt-3" onClick={onAdd}>
        <PlusIcon />
        Add item
      </Button>
    </div>
  );
}
