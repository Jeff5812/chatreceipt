"use client";

import { useEffect, useState } from "react";
import { cheer } from "@/lib/chit";
import { calculate, isAbortError, messageOf } from "@/lib/api";
import { blankAdjustment, blankLineItem, fieldId, focusField, rowSource, scalarSource } from "@/lib/draft";
import type {
  AdjustmentDraft,
  Calculation,
  ExtractionResponse,
  LineItemDraft,
  PublicConfig,
  ScalarField,
  Screenshot,
  TransactionDraft,
} from "@/lib/types";
import { ArrowChip, Button } from "../ui/button";
import { PixelCheckbox } from "../ui/pixel-checkbox";
import { ArrowLeftIcon } from "../ui/icons";
import { AdjustmentsEditor } from "./adjustments-editor";
import { ChatExportCard, OtherDeals, WhoIsWho } from "./conversation-panels";
import { EvidenceSourceContext, Field, MoneyInput, Section, chatEvidenceLabel, hasError, inputClass } from "./field";
import { ItemsEditor } from "./items-editor";
import { Notice } from "./notice";
import { AMOUNT_PAID_HINTS, PaymentStatusField } from "./payment-section";
import { ScreenshotStrip } from "./screenshot-strip";
import { StepHeading } from "./step-heading";
import { TotalsPanel } from "./totals-panel";

type Group = "discounts" | "additional_fees";

interface ReviewStepProps {
  config: PublicConfig;
  draft: TransactionDraft;
  onDraftChange: (update: (draft: TransactionDraft) => TransactionDraft) => void;
  extraction: ExtractionResponse | null;
  isExample: boolean;
  screenshots: Screenshot[];
  // The chat export the draft was read from, and a way to read it again for another deal it holds.
  chatExport: File | null;
  onReadDeal: ((deal: string) => void) | null;
  acknowledged: boolean;
  onAcknowledge: (value: boolean) => void;
  onBack: () => void;
  onContinue: () => void;
  focusHeading: boolean;
}

function amountMap(lines: { id: string; amount: string | null }[] | undefined): Record<string, string | null> {
  return Object.fromEntries((lines ?? []).map((line) => [line.id, line.amount]));
}

export function ReviewStep(props: ReviewStepProps) {
  const { config, draft, onDraftChange, extraction } = props;
  const original = extraction?.draft ?? null;
  const meta = extraction?.field_meta ?? {};
  const currency = config.currencies.find((c) => c.code === draft.currency);
  const symbol = currency?.symbol ?? "¤";

  // Totals always come from the backend: recalculated shortly after every change.
  const [calc, setCalc] = useState<{ draft: TransactionDraft; result: Calculation } | null>(() =>
    extraction?.calculation && extraction.draft === draft ? { draft, result: extraction.calculation } : null,
  );
  const [calcError, setCalcError] = useState<string | null>(null);
  const [blocked, setBlocked] = useState<string | null>(null);
  // Which side the person is on, as the chat showed it; swapping the names swaps it too.
  const [ownerSide, setOwnerSide] = useState<"seller" | "buyer" | "">(extraction?.parties?.owner_role ?? "");

  useEffect(() => {
    if (calc?.draft === draft) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      calculate(draft, controller.signal).then(
        (result) => {
          setCalc({ draft, result });
          setCalcError(null);
        },
        (error: unknown) => {
          if (!isAbortError(error)) setCalcError(messageOf(error));
        },
      );
    }, 300);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [draft, calc]);

  const pending = calc?.draft !== draft;
  const result = calc?.result ?? null;
  const issues = result?.issues ?? [];
  const issuesAt = (path: string) => issues.filter((issue) => issue.field === path);
  const info = (field: ScalarField) => ({
    source: scalarSource(field, draft, original, meta[field]),
    meta: meta[field],
    issues: issuesAt(field),
  });

  function change(update: (current: TransactionDraft) => TransactionDraft) {
    setBlocked(null);
    onDraftChange(update);
  }
  function setField<K extends ScalarField>(field: K, value: TransactionDraft[K]) {
    change((current) => ({ ...current, [field]: value }));
  }
  function updateItem(id: string, patch: Partial<LineItemDraft>) {
    change((current) => ({
      ...current,
      line_items: current.line_items.map((item) => (item.id === id ? { ...item, ...patch } : item)),
    }));
  }
  function addItem() {
    const item = blankLineItem();
    change((current) => ({ ...current, line_items: [...current.line_items, item] }));
    window.requestAnimationFrame(() => focusField(`line_items.${item.id}.description`));
  }
  function updateAdjustment(group: Group, id: string, patch: Partial<AdjustmentDraft>) {
    change((current) => ({ ...current, [group]: current[group].map((row) => (row.id === id ? { ...row, ...patch } : row)) }));
  }
  function addAdjustment(group: Group) {
    const row = blankAdjustment(group);
    change((current) => ({ ...current, [group]: [...current[group], row] }));
    window.requestAnimationFrame(() => focusField(`${group}.${row.id}.label`));
  }
  function removeRow(group: Group | "line_items", id: string) {
    change((current) => ({ ...current, [group]: (current[group] as Array<{ id: string }>).filter((row) => row.id !== id) }));
  }
  function swapSides() {
    change((current) => ({ ...current, seller: current.buyer, buyer: current.seller }));
    setOwnerSide((side) => (side === "seller" ? "buyer" : side === "buyer" ? "seller" : side));
  }

  const reasons = extraction?.review_reasons ?? [];
  const fromExport = extraction?.source === "chat_export";
  const stripTitle = props.isExample ? "Example conversation" : "Your screenshots";
  const needsAcknowledgement = extraction?.review_required ?? false;
  const youTag = (side: "seller" | "buyer") => (ownerSide === side ? "You" : undefined);

  function handleContinue() {
    if (!result || pending) return;
    const errors = result.issues.filter((issue) => issue.severity === "error");
    if (errors.length > 0) {
      setBlocked(
        errors.length === 1 ? "Fix the highlighted detail to continue." : `Fix the ${errors.length} highlighted details to continue.`,
      );
      focusField(errors[0].field);
      return;
    }
    if (needsAcknowledgement && !props.acknowledged) {
      setBlocked("Confirm that you've checked the details marked for review.");
      return;
    }
    setBlocked(null);
    props.onContinue();
  }

  return (
    <EvidenceSourceContext.Provider value={fromExport ? chatEvidenceLabel : (source) => `Screenshot ${source}`}>
    <div className="mx-auto max-w-6xl px-4 py-8 sm:px-6 sm:py-12">
      <Button variant="quiet" className="-ml-1 text-muted hover:text-ink" onClick={props.onBack}>
        <ArrowLeftIcon />
        {extraction && !props.isExample ? (fromExport ? "Back to the chat export" : "Back to screenshots") : "Back"}
      </Button>

      <div className="mt-4 max-w-3xl">
        <StepHeading focus={props.focusHeading}>Review the details</StepHeading>
        <p className="mt-4 text-lg leading-relaxed text-muted">
          {extraction
            ? `Check each detail against your ${fromExport ? "chat" : "screenshots"} and correct anything that's wrong.`
            : "Enter what was agreed. ChatReceipt works out the totals."}
        </p>
      </div>

      <div className="mt-8 grid gap-10 lg:grid-cols-[minmax(0,1fr)_340px]">
        <div className="min-w-0 space-y-6">
          <div className="space-y-4 empty:hidden">
            {props.isExample && (
              <Notice
                title="This is an example"
                action={
                  <Button size="sm" variant="secondary" onClick={props.onBack}>
                    Use your own screenshots
                  </Button>
                }
              >
                These details come from a sample conversation, not your screenshots. Change anything to see the totals
                update.
              </Notice>
            )}
            {extraction?.summary && (
              <div className="rounded-xl border border-line bg-surface px-4 py-3.5 shadow-e1">
                <p className="label-caps text-muted">Summary of the conversation</p>
                <p className="mt-1.5 text-ui leading-relaxed">{extraction.summary}</p>
              </div>
            )}
            {reasons.length > 0 && (
              <Notice tone="warn" title={reasons.length === 1 ? "Check this before you continue" : `Check these ${reasons.length} things before you continue`}>
                <ul className="mt-1 space-y-1.5">
                  {reasons.map((reason) => (
                    <li key={`${reason.field}-${reason.message}`}>
                      {reason.field ? (
                        <button
                          type="button"
                          onClick={() => focusField(reason.field)}
                          className="text-left underline decoration-warn/40 underline-offset-4 hover:decoration-warn"
                        >
                          {reason.message}
                        </button>
                      ) : (
                        reason.message
                      )}
                    </li>
                  ))}
                </ul>
              </Notice>
            )}
            {extraction && <OtherDeals deals={extraction.other_deals ?? []} onReadDeal={props.onReadDeal} />}
          </div>
          {props.screenshots.length > 0 && (
            <div className="lg:hidden">
              <ScreenshotStrip screenshots={props.screenshots} title={stripTitle} />
            </div>
          )}
          {fromExport && extraction?.chat && (
            <div className="border border-line bg-surface px-4 py-3.5 shadow-e1 lg:hidden">
              <ChatExportCard file={props.chatExport} chat={extraction.chat} />
            </div>
          )}

          <Section title="People and dates">
            {extraction && !props.isExample && (
              <WhoIsWho
                extraction={extraction}
                draft={draft}
                ownerSide={ownerSide}
                onFill={(side, name) => setField(side, name)}
                onSwap={swapSides}
              />
            )}
            <div className="grid gap-x-6 gap-y-5 sm:grid-cols-2">
              <Field label="Seller" path="seller" tag={youTag("seller")} {...info("seller")}>
                <input
                  id={fieldId("seller")}
                  className={inputClass(issuesAt("seller"))}
                  value={draft.seller}
                  maxLength={120}
                  autoComplete="off"
                  placeholder="Business or person selling"
                  onChange={(event) => setField("seller", event.target.value)}
                />
              </Field>
              <Field label="Buyer" path="buyer" tag={youTag("buyer")} {...info("buyer")}>
                <input
                  id={fieldId("buyer")}
                  className={inputClass(issuesAt("buyer"))}
                  value={draft.buyer}
                  maxLength={120}
                  autoComplete="off"
                  placeholder="Person or business buying"
                  onChange={(event) => setField("buyer", event.target.value)}
                />
              </Field>
              <Field label="Transaction date" path="transaction_date" {...info("transaction_date")}>
                <input
                  id={fieldId("transaction_date")}
                  type="date"
                  className={inputClass(issuesAt("transaction_date").filter((i) => i.severity === "error"))}
                  value={draft.transaction_date}
                  onChange={(event) => setField("transaction_date", event.target.value)}
                />
              </Field>
              <Field label="Due date" path="due_date" optional {...info("due_date")}>
                <input
                  id={fieldId("due_date")}
                  type="date"
                  className={inputClass(issuesAt("due_date"))}
                  value={draft.due_date}
                  onChange={(event) => setField("due_date", event.target.value)}
                />
              </Field>
              <Field label="Currency" path="currency" {...info("currency")}>
                <select
                  id={fieldId("currency")}
                  className={inputClass(issuesAt("currency"), "appearance-auto")}
                  aria-invalid={hasError(issuesAt("currency")) || undefined}
                  value={draft.currency}
                  onChange={(event) => setField("currency", event.target.value)}
                >
                  {draft.currency === "" && <option value="">Choose a currency</option>}
                  {config.currencies.map((c) => (
                    <option key={c.code} value={c.code}>
                      {c.code} · {c.name}
                    </option>
                  ))}
                </select>
              </Field>
            </div>
          </Section>

          <Section title="Items" description="What was agreed, at the price both sides accepted.">
            <ItemsEditor
              items={draft.line_items}
              amounts={amountMap(result?.line_items)}
              currency={currency}
              issuesAt={issuesAt}
              rowInfo={(item) => ({ source: rowSource("line_items", item, original), meta: meta[`line_items.${item.id}`] })}
              onUpdate={updateItem}
              onAdd={addItem}
              onRemove={(id) => removeRow("line_items", id)}
            />
          </Section>

          <Section title="Discounts, fees and delivery">
            <div className="space-y-8">
              <AdjustmentsEditor
                group="discounts"
                title="Discounts"
                description="Reductions agreed separately from the price. A lower negotiated price belongs in the item."
                addLabel="Add discount"
                labelPlaceholder="e.g. Returning customer"
                rows={draft.discounts}
                amounts={amountMap(result?.discounts)}
                currency={currency}
                issuesAt={issuesAt}
                rowInfo={(row) => ({ source: rowSource("discounts", row, original), meta: meta[`discounts.${row.id}`] })}
                onUpdate={(id, patch) => updateAdjustment("discounts", id, patch)}
                onAdd={() => addAdjustment("discounts")}
                onRemove={(id) => removeRow("discounts", id)}
              />
              <AdjustmentsEditor
                group="additional_fees"
                title="Additional fees"
                description="Other charges agreed separately, such as packaging or a service charge."
                addLabel="Add fee"
                labelPlaceholder="e.g. Packaging"
                rows={draft.additional_fees}
                amounts={amountMap(result?.additional_fees)}
                currency={currency}
                issuesAt={issuesAt}
                rowInfo={(row) => ({ source: rowSource("additional_fees", row, original), meta: meta[`additional_fees.${row.id}`] })}
                onUpdate={(id, patch) => updateAdjustment("additional_fees", id, patch)}
                onAdd={() => addAdjustment("additional_fees")}
                onRemove={(id) => removeRow("additional_fees", id)}
              />
              <div className="max-w-xs">
                <Field label="Delivery fee" path="delivery_fee" optional {...info("delivery_fee")}>
                  <MoneyInput
                    id={fieldId("delivery_fee")}
                    symbol={symbol}
                    value={draft.delivery_fee}
                    issues={issuesAt("delivery_fee")}
                    onChange={(value) => setField("delivery_fee", value)}
                  />
                </Field>
              </div>
            </div>
          </Section>

          <Section title="Payment">
            <div className="space-y-6">
              <PaymentStatusField
                value={draft.payment_status}
                {...info("payment_status")}
                currency={currency}
                onChange={(value) => setField("payment_status", value)}
              />
              <div className="max-w-xs">
                <Field label="Amount paid" path="amount_paid" hint={AMOUNT_PAID_HINTS[draft.payment_status]} {...info("amount_paid")}>
                  <MoneyInput
                    id={fieldId("amount_paid")}
                    symbol={symbol}
                    value={draft.amount_paid}
                    issues={issuesAt("amount_paid")}
                    onChange={(value) => setField("amount_paid", value)}
                  />
                </Field>
              </div>
              <Field label="Payment terms" path="payment_terms" optional {...info("payment_terms")}>
                <textarea
                  id={fieldId("payment_terms")}
                  className={inputClass(issuesAt("payment_terms"), "min-h-20")}
                  value={draft.payment_terms}
                  maxLength={1000}
                  placeholder="e.g. Buyer will transfer the balance on delivery."
                  onChange={(event) => setField("payment_terms", event.target.value)}
                />
              </Field>
            </div>
          </Section>

          <Section title="Delivery and notes">
            <div className="space-y-6">
              <Field label="Delivery or collection terms" path="delivery_terms" optional {...info("delivery_terms")}>
                <textarea
                  id={fieldId("delivery_terms")}
                  className={inputClass(issuesAt("delivery_terms"), "min-h-20")}
                  value={draft.delivery_terms}
                  maxLength={1000}
                  placeholder="e.g. Dispatch to Yaba on Friday."
                  onChange={(event) => setField("delivery_terms", event.target.value)}
                />
              </Field>
              <Field label="Notes" path="notes" optional {...info("notes")}>
                <textarea
                  id={fieldId("notes")}
                  className={inputClass(issuesAt("notes"), "min-h-20")}
                  value={draft.notes}
                  maxLength={2000}
                  placeholder="Anything else that matters to the agreement."
                  onChange={(event) => setField("notes", event.target.value)}
                />
              </Field>
            </div>
          </Section>
        </div>

        <aside className="space-y-4">
          {props.screenshots.length > 0 && (
            <div className="hidden rounded-2xl border border-line bg-surface px-5 pt-4 pb-3 shadow-e1 lg:block">
              <ScreenshotStrip screenshots={props.screenshots} title={stripTitle} />
            </div>
          )}
          {fromExport && extraction?.chat && (
            <div className="hidden border border-line bg-surface px-5 py-4 shadow-e1 lg:block">
              <ChatExportCard file={props.chatExport} chat={extraction.chat} />
            </div>
          )}
          {/* Only the totals and the button stay in view while scrolling, so the button is never pushed off a short screen. */}
          <div className="space-y-4 lg:sticky lg:top-24">
            <TotalsPanel
              draft={draft}
              result={result}
              pending={pending}
              error={calcError}
              currency={currency}
              onIgnoreStatedTotal={() => setField("stated_total", "")}
              onIgnoreStatedOutstanding={() => setField("stated_outstanding", "")}
            />
            <div className="space-y-4 rounded-2xl border border-line bg-surface p-5 shadow-e1">
              {needsAcknowledgement && (
                <PixelCheckbox
                  checked={props.acknowledged}
                  onChange={(checked) => {
                    setBlocked(null);
                    props.onAcknowledge(checked);
                    if (checked) cheer("happy", 1400);
                  }}
                >
                  I&apos;ve checked the details marked for review.
                </PixelCheckbox>
              )}
              <Button className="w-full" onClick={handleContinue} disabled={pending || result === null}>
                {pending ? (
                  "Checking the details…"
                ) : (
                  <>
                    Preview the record
                    <ArrowChip />
                  </>
                )}
              </Button>
              {blocked && (
                <p role="alert" className="text-meta text-danger">
                  {blocked}
                </p>
              )}
            </div>
          </div>
        </aside>
      </div>
    </div>
    </EvidenceSourceContext.Provider>
  );
}
