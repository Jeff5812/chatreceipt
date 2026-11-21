"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, downloadDocument, getConfig, messageOf } from "@/lib/api";
import { formatMoney } from "@/lib/money";
import {
  clearSession,
  endSession,
  getRecord,
  listRecords,
  readSession,
  startSession,
  type RecordSummary,
} from "@/lib/records";
import type { CurrencyInfo } from "@/lib/types";
import { Logo } from "../brand";
import { Button } from "../ui/button";
import { AlertIcon, CheckIcon, DownloadIcon, InfoIcon } from "../ui/icons";
import { PixelSpinner } from "../ui/pixel-spinner";
import { StatusPill } from "../ui/status";
import { WhatsAppButton } from "../whatsapp-button";
import { Notice } from "../workflow/notice";

type View =
  | { name: "loading" }
  | { name: "signed-out"; problem: string | null }
  | { name: "failed"; problem: string }
  | { name: "ready" };

function readableDate(record: RecordSummary): string {
  const value = record.transaction_date ?? record.created_at.slice(0, 10);
  const [year, month, day] = value.split("-").map(Number);
  return new Date(Date.UTC(year, month - 1, day)).toLocaleDateString("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  });
}

// The other person's answer to the record's confirmation link, and whether its payment arrived, both in
// words with an icon, so neither relies on colour. "Arrived" is the customer's own answer, not a check.
const ANSWERS = {
  agreed: { icon: CheckIcon, text: "Confirmed by them", look: "text-ink-soft" },
  disputed: { icon: AlertIcon, text: "They say something's wrong", look: "text-warn" },
} as const;
const PAYMENT_CHECKS = {
  claimed: { icon: InfoIcon, text: "Payment not checked yet", look: "text-muted" },
  not_received: { icon: AlertIcon, text: "Not in your account yet", look: "text-warn" },
  received: { icon: CheckIcon, text: "You said the payment arrived", look: "text-ink-soft" },
} as const;

function RecordChecks({ record }: { record: RecordSummary }) {
  const marks = [record.response ? ANSWERS[record.response] : null, record.payment_check ? PAYMENT_CHECKS[record.payment_check] : null].filter(
    (mark) => mark !== null,
  );
  if (marks.length === 0) return null;
  return (
    <ul className="mt-3 flex flex-wrap gap-x-4 gap-y-1.5 text-meta">
      {marks.map(({ icon: Icon, text, look }) => (
        <li key={text} className={`inline-flex items-center gap-1.5 ${look}`}>
          <Icon className="size-4 shrink-0" />
          {text}
        </li>
      ))}
    </ul>
  );
}

function RecordCard({
  record,
  currency,
  busy,
  onDownload,
}: {
  record: RecordSummary;
  currency: CurrencyInfo | undefined;
  busy: boolean;
  onDownload: () => void;
}) {
  const money = (amount: string | null) => (currency ? formatMoney(amount, currency) : `${record.currency} ${amount ?? "—"}`);
  const people = [record.seller && `Seller: ${record.seller}`, record.buyer && `Buyer: ${record.buyer}`].filter(Boolean);
  return (
    <li className="border border-line bg-surface p-5 shadow-e1">
      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
        <div className="min-w-0">
          <p className="font-mono text-meta text-muted">
            {record.reference} · {readableDate(record)}
          </p>
          <p className="mt-2 font-mono text-[26px] leading-none font-bold tracking-[-0.02em] text-accent tabular-nums">{money(record.total)}</p>
        </div>
        <StatusPill status={record.payment_status} />
      </div>
      {people.length > 0 && <p className="mt-3 text-ui break-words text-ink-soft">{people.join(" · ")}</p>}
      <dl className="mt-3 grid grid-cols-2 gap-3 border-t border-line pt-3 font-mono text-meta">
        <div>
          <dt className="text-muted">Paid</dt>
          <dd className="mt-0.5 text-ink tabular-nums">{record.amount_paid === null ? "Not stated" : money(record.amount_paid)}</dd>
        </div>
        <div>
          <dt className="text-muted">Outstanding</dt>
          <dd className="mt-0.5 text-ink tabular-nums">{record.outstanding === null ? "Not stated" : money(record.outstanding)}</dd>
        </div>
      </dl>
      <RecordChecks record={record} />
      <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
        <p className="max-w-xs text-meta leading-relaxed text-muted">Recorded from the chat. ChatReceipt doesn&apos;t verify payments.</p>
        <Button size="sm" variant="secondary" disabled={busy} onClick={onDownload}>
          {busy ? <PixelSpinner className="size-4" /> : <DownloadIcon className="size-4" />}
          Download PDF
        </Button>
      </div>
    </li>
  );
}

export function RecordsView() {
  const [view, setView] = useState<View>({ name: "loading" });
  const [records, setRecords] = useState<RecordSummary[]>([]);
  const [nextBefore, setNextBefore] = useState<string | null>(null);
  const [currencies, setCurrencies] = useState<CurrencyInfo[]>([]);
  const [downloading, setDownloading] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const token = useRef<string | null>(null);

  const signedOut = useCallback((problem: string | null) => {
    clearSession();
    token.current = null;
    setRecords([]);
    setNextBefore(null);
    setView({ name: "signed-out", problem });
  }, []);

  const handleFailure = useCallback(
    (error: unknown) => {
      if (error instanceof ApiError && error.status === 401) signedOut(error.message);
      else setView({ name: "failed", problem: messageOf(error) });
    },
    [signedOut],
  );

  const open = useCallback(async () => {
    setView({ name: "loading" });
    try {
      // The code is in the page address's fragment, so it never reaches a server log. It is removed from the
      // address at once, and can only be used one time.
      const code = /[#&]code=([A-Za-z0-9_-]+)/.exec(window.location.hash)?.[1];
      if (code) {
        window.history.replaceState(null, "", window.location.pathname);
        token.current = await startSession(code);
      } else {
        token.current = token.current ?? readSession();
      }
      if (!token.current) {
        signedOut(null);
        return;
      }
      const [page, config] = await Promise.all([listRecords(token.current), getConfig().catch(() => null)]);
      setCurrencies(config?.currencies ?? []);
      setRecords(page.records);
      setNextBefore(page.next_before);
      setView({ name: "ready" });
    } catch (error) {
      handleFailure(error);
    }
  }, [handleFailure, signedOut]);

  useEffect(() => {
    // Deferred, so a cancelled mount (React's strict mode rehearses one) never spends the one-time code.
    const timer = window.setTimeout(() => void open(), 0);
    return () => window.clearTimeout(timer);
  }, [open]);

  async function loadMore() {
    if (!token.current || !nextBefore) return;
    setLoadingMore(true);
    try {
      const page = await listRecords(token.current, nextBefore);
      setRecords((current) => [...current, ...page.records]);
      setNextBefore(page.next_before);
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) handleFailure(error);
      else setNotice(messageOf(error));
    } finally {
      setLoadingMore(false);
    }
  }

  async function download(record: RecordSummary) {
    if (!token.current) return;
    setDownloading(record.id);
    setNotice(null);
    try {
      const detail = await getRecord(token.current, record.id);
      await downloadDocument(detail.draft, "pdf", { from_extraction: true, edited_fields: 0, added_fields: 0 });
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) handleFailure(error);
      else setNotice(messageOf(error));
    } finally {
      setDownloading(null);
    }
  }

  async function signOut() {
    const current = token.current;
    signedOut(null);
    if (current) await endSession(current).catch(() => undefined);
  }

  return (
    <div className="min-h-dvh">
      <header className="sticky top-0 z-40 border-b border-line bg-paper">
        <div className="mx-auto flex h-16 max-w-3xl items-center justify-between gap-4 px-4 sm:px-6">
          <Link href="/" aria-label="ChatReceipt home" className="rounded-none">
            <Logo />
          </Link>
          {view.name === "ready" && (
            <Button variant="quiet" className="text-sm text-muted hover:text-ink" onClick={() => void signOut()}>
              Sign out
            </Button>
          )}
        </div>
      </header>

      <main className="mx-auto max-w-3xl px-4 py-10 sm:px-6 sm:py-14">
        <p className="pixel-label text-accent">My records</p>
        <h1 className="mt-3 font-display text-[34px] leading-[1.05] font-bold tracking-[-0.032em] sm:text-[44px]">Your saved records</h1>

        {view.name === "loading" && (
          <p role="status" className="mt-8 flex items-center gap-3 text-ui text-muted">
            <PixelSpinner className="size-4" />
            Opening your records…
          </p>
        )}

        {view.name === "signed-out" && (
          <div className="mt-6 max-w-xl space-y-5">
            {view.problem && <Notice tone="warn" title="You'll need a new link">{view.problem}</Notice>}
            <p className="text-lg leading-relaxed text-muted">
              Records you confirm on WhatsApp are kept here. To open them, send <span className="font-mono text-ink">web</span> to
              ChatReceipt on WhatsApp and tap the private link it sends you.
            </p>
            <div className="flex flex-wrap gap-3 empty:hidden">
              <WhatsAppButton label="Open WhatsApp" message="web" />
            </div>
          </div>
        )}

        {view.name === "failed" && (
          <div className="mt-6 max-w-xl">
            <Notice
              tone="error"
              title="Your records couldn't be opened"
              action={
                <Button size="sm" variant="secondary" onClick={() => void open()}>
                  Try again
                </Button>
              }
            >
              {view.problem}
            </Notice>
          </div>
        )}

        {view.name === "ready" && (
          <>
            {notice && (
              <div className="mt-6">
                <Notice tone="error" title="That didn't work">
                  {notice}
                </Notice>
              </div>
            )}
            {records.length === 0 ? (
              <p className="mt-6 max-w-xl text-lg leading-relaxed text-muted">
                You don&apos;t have any saved records yet. Send a screenshot to ChatReceipt on WhatsApp and confirm the details to save your first one.
              </p>
            ) : (
              <ul className="mt-8 space-y-4">
                {records.map((record) => (
                  <RecordCard
                    key={record.id}
                    record={record}
                    currency={currencies.find((entry) => entry.code === record.currency)}
                    busy={downloading === record.id}
                    onDownload={() => void download(record)}
                  />
                ))}
              </ul>
            )}
            {nextBefore && (
              <div className="mt-6 text-center">
                <Button variant="secondary" disabled={loadingMore} onClick={() => void loadMore()}>
                  {loadingMore && <PixelSpinner className="size-4" />}
                  Show more
                </Button>
              </div>
            )}
          </>
        )}
      </main>
    </div>
  );
}
