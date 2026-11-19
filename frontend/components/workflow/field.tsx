import type { ReactNode } from "react";
import { fieldId, type FieldSource } from "@/lib/draft";
import type { FieldMeta, Issue } from "@/lib/types";
import { AlertIcon, ErrorIcon } from "../ui/icons";

export function hasError(issues: Issue[]): boolean {
  return issues.some((issue) => issue.severity === "error");
}

const FOCUS_RING = "focus:border-accent focus:shadow-[0_0_0_3px_rgb(196_242_90/0.2)]";

// Inputs sit slightly into the surface (a soft inner shadow) and gain an emerald ring on focus.
export function inputClass(issues: Issue[] = [], extra = ""): string {
  const state = hasError(issues)
    ? "border-danger focus:shadow-[0_0_0_3px_rgb(242_139_123/0.22)]"
    : issues.length > 0
      ? "border-warn focus:shadow-[0_0_0_3px_rgb(242_184_76/0.22)]"
      : `border-line-strong hover:border-ink/35 ${FOCUS_RING}`;
  return `block w-full rounded-lg border bg-surface px-3 py-2.5 text-ui text-ink shadow-field transition-[border-color,box-shadow] duration-150 placeholder:text-muted focus:outline-none ${state} ${extra}`;
}

const SOURCE_LABELS: Record<Exclude<FieldSource, "manual">, { label: string; look: string; dot: string }> = {
  extracted: { label: "From the chat", look: "bg-accent-soft text-accent", dot: "bg-accent" },
  edited: { label: "Edited", look: "bg-paper-deep text-ink-soft", dot: "bg-ink" },
  added: { label: "Added by you", look: "bg-paper-deep text-muted", dot: "bg-muted" },
  missing: { label: "Not in the chat", look: "border border-dashed border-line-strong text-muted", dot: "border border-muted bg-transparent" },
};

const NEEDS_CHECK = { label: "Check this", look: "bg-warn-soft font-medium text-warn", dot: "bg-marigold" };

export function SourceTag({ source, meta }: { source: FieldSource; meta?: FieldMeta }) {
  if (source === "manual") return null;
  const needsCheck = source === "extracted" && meta?.confidence !== "high";
  const { label, look, dot } = needsCheck ? NEEDS_CHECK : SOURCE_LABELS[source];
  return (
    <span className={`inline-flex shrink-0 items-center gap-1.5 rounded-md px-1.5 py-0.5 text-xs ${look}`}>
      <span className={`size-1.5 rounded-none ${dot}`} aria-hidden />
      {label}
    </span>
  );
}

/** Quotes from the screenshots that support a value, and the extraction's note. */
export function EvidenceList({ meta, source }: { meta?: FieldMeta; source: FieldSource }) {
  if (!meta || source === "manual") return null;
  const showQuotes = source === "extracted" || source === "edited";
  if (!(showQuotes && meta.evidence.length > 0) && !meta.note) return null;
  return (
    <div className="space-y-1 text-meta leading-snug text-muted">
      {showQuotes &&
        meta.evidence.map((evidence) => (
          <p key={`${evidence.screenshot}-${evidence.quote}`}>
            <span className="text-caption font-medium text-ink-soft">Screenshot {evidence.screenshot}</span> · “
            <span className="mark">{evidence.quote}</span>”
          </p>
        ))}
      {meta.note && <p className="italic">{meta.note}</p>}
    </div>
  );
}

export function IssueList({ issues }: { issues: Issue[] }) {
  if (issues.length === 0) return null;
  return (
    <ul className="space-y-1 text-meta leading-snug">
      {issues.map((issue) => (
        <li
          key={`${issue.code}-${issue.field}`}
          className={`flex gap-1.5 ${issue.severity === "error" ? "text-danger" : "text-warn"}`}
        >
          {issue.severity === "error" ? <ErrorIcon className="mt-px size-3.5 shrink-0" /> : <AlertIcon className="mt-px size-3.5 shrink-0" />}
          <span>{issue.message}</span>
        </li>
      ))}
    </ul>
  );
}

export function Field({
  label,
  path,
  source,
  meta,
  issues,
  hint,
  optional = false,
  children,
}: {
  label: string;
  path: string;
  source: FieldSource;
  meta?: FieldMeta;
  issues: Issue[];
  hint?: string;
  optional?: boolean;
  children: ReactNode;
}) {
  return (
    <div className="min-w-0 space-y-1.5">
      <div className="flex items-baseline justify-between gap-3">
        <label htmlFor={fieldId(path)} className="text-sm font-medium">
          {label}
          {optional && <span className="font-normal text-muted"> (optional)</span>}
        </label>
        <SourceTag source={source} meta={meta} />
      </div>
      {children}
      {hint && <p className="text-meta text-muted">{hint}</p>}
      <EvidenceList meta={meta} source={source} />
      <IssueList issues={issues} />
    </div>
  );
}

export function MoneyInput({
  id,
  value,
  symbol,
  issues = [],
  onChange,
  placeholder = "0",
  label,
}: {
  id: string;
  value: string;
  symbol: string;
  issues?: Issue[];
  onChange: (value: string) => void;
  placeholder?: string;
  label?: string;
}) {
  const state = hasError(issues)
    ? "border-danger focus-within:shadow-[0_0_0_3px_rgb(242_139_123/0.22)]"
    : issues.length > 0
      ? "border-warn focus-within:shadow-[0_0_0_3px_rgb(242_184_76/0.22)]"
      : "border-line-strong hover:border-ink/35 focus-within:border-accent focus-within:shadow-[0_0_0_3px_rgb(196_242_90/0.2)]";
  return (
    <div
      className={`flex items-stretch overflow-hidden rounded-lg border bg-surface shadow-field transition-[border-color,box-shadow] duration-150 ${state}`}
    >
      <span className="flex items-center border-r border-line bg-well px-3 text-sm text-muted" aria-hidden>
        {symbol}
      </span>
      <input
        id={id}
        aria-label={label}
        aria-invalid={hasError(issues) || undefined}
        inputMode="decimal"
        autoComplete="off"
        className="w-full min-w-0 bg-transparent px-3 py-2.5 font-mono text-ui tabular-nums focus:outline-none"
        value={value}
        placeholder={placeholder}
        onChange={(event) => onChange(event.target.value)}
      />
    </div>
  );
}

export function Section({ title, description, children }: { title: string; description?: string; children: ReactNode }) {
  return (
    <section className="rounded-2xl border border-line bg-surface p-5 shadow-e1 sm:p-7">
      <div className="mb-6 border-b border-line pb-4">
        <h2 className="font-display text-[24px] leading-tight font-bold tracking-[-0.025em]">{title}</h2>
        {description && <p className="mt-1 text-sm text-muted">{description}</p>}
      </div>
      {children}
    </section>
  );
}
