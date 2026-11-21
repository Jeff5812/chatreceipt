"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { canShareFiles, downloadDocument, fetchDocument, isAbortError, messageOf, previewDocument, shareFile } from "@/lib/api";
import { cheer } from "@/lib/chit";
import type { ClientStats, TransactionDraft } from "@/lib/types";
import { Celebration } from "../critters";
import { Button } from "../ui/button";
import { ArrowLeftIcon, ArrowRightIcon, DownloadIcon } from "../ui/icons";
import { PixelCheck } from "../ui/pixel-check";
import { PixelSpinner } from "../ui/pixel-spinner";
import { Notice } from "./notice";
import { StepHeading } from "./step-heading";

type Format = "pdf" | "png";

/** While the record is being prepared: a little printer feeding paper out, line by line. */
function PrintingLoader() {
  return (
    <div role="status" className="flex flex-col items-center gap-5 px-6 py-16">
      <div aria-hidden className="w-[220px]">
        <div className="relative z-10 h-7 border border-line-strong bg-noir shadow-e1" />
        <div className="-mt-px h-[150px] overflow-hidden px-4">
          <div className="print-feed space-y-2.5 bg-sheet px-4 py-4">
            <span className="block h-2.5 w-1/2 bg-[#d9ddd5]" />
            <span className="block h-1.5 w-full bg-[#e6e9e3]" />
            <span className="block h-1.5 w-5/6 bg-[#e6e9e3]" />
            <span className="block h-1.5 w-full bg-[#e6e9e3]" />
            <span className="block h-3 w-2/3 bg-[#c9cfc6]" />
            <span className="block h-1.5 w-3/4 bg-[#e6e9e3]" />
            <span className="block h-1.5 w-1/2 bg-[#e6e9e3]" />
          </div>
        </div>
      </div>
      <p className="pixel-label text-accent">
        Printing your record<span className="anim-blink">_</span>
      </p>
    </div>
  );
}

export function PreviewStep({
  draft,
  stats,
  onEdit,
  onStartOver,
  focusHeading,
}: {
  draft: TransactionDraft;
  stats: ClientStats;
  onEdit: () => void;
  onStartOver: () => void;
  focusHeading: boolean;
}) {
  const [html, setHtml] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [exporting, setExporting] = useState<Format | null>(null);
  const [downloaded, setDownloaded] = useState<Format | null>(null);
  const [exportError, setExportError] = useState<string | null>(null);
  const [frameHeight, setFrameHeight] = useState(960);
  // Sharing straight into WhatsApp from the phone's share sheet, where the browser supports it. This step
  // only ever renders in the browser (it follows the upload and review steps), so it can ask at once.
  const [canShare] = useState(canShareFiles);
  const [sharing, setSharing] = useState(false);
  // The image, kept when the browser wanted a fresh tap before opening the share sheet.
  const [readyToShare, setReadyToShare] = useState<File | null>(null);
  const frameRef = useRef<HTMLIFrameElement>(null);
  const settle = useRef(0);

  useEffect(() => {
    const controller = new AbortController();
    previewDocument(draft, controller.signal).then(
      (document) => {
        setHtml(document);
        cheer("happy", 2600);
      },
      (reason: unknown) => {
        if (isAbortError(reason)) return;
        setError(messageOf(reason));
        cheer("worried", 2400);
      },
    );
    return () => controller.abort();
  }, [draft]);

  useEffect(() => () => window.clearTimeout(settle.current), []);

  // The preview is our own HTML in a sandboxed frame (no scripts); size it to its content.
  const measure = useCallback(() => {
    const height = frameRef.current?.contentDocument?.documentElement.scrollHeight;
    if (height) setFrameHeight(height);
  }, []);
  useEffect(() => {
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, [measure]);

  async function download(format: Format) {
    if (exporting !== null) return;
    setExporting(format);
    setDownloaded(null);
    setExportError(null);
    try {
      await downloadDocument(draft, format, stats);
      setDownloaded(format);
      cheer("happy", 1800);
      window.clearTimeout(settle.current);
      settle.current = window.setTimeout(() => setDownloaded(null), 2800);
    } catch (reason) {
      setExportError(messageOf(reason));
      cheer("worried", 2400);
    } finally {
      setExporting(null);
    }
  }

  async function share() {
    if (sharing || exporting !== null) return;
    setSharing(true);
    setExportError(null);
    try {
      const file = readyToShare ?? (await fetchDocument(draft, "png", stats));
      try {
        await shareFile(file, "Transaction record");
        setReadyToShare(null);
      } catch (reason) {
        if (reason instanceof DOMException && reason.name === "NotAllowedError") {
          setReadyToShare(file); // Safari wants a fresh tap after the wait: the next tap opens the sheet
          return;
        }
        throw reason;
      }
    } catch (reason) {
      setExportError(messageOf(reason));
      cheer("worried", 2400);
    } finally {
      setSharing(false);
    }
  }

  function label(format: Format, idle: string, busy: string) {
    if (exporting === format) {
      return (
        <>
          <PixelSpinner className="size-4" />
          {busy}
        </>
      );
    }
    if (downloaded === format) {
      return (
        <>
          <PixelCheck cell={2} />
          Downloaded
        </>
      );
    }
    return (
      <>
        <DownloadIcon />
        {idle}
      </>
    );
  }

  return (
    <div className="mx-auto max-w-6xl px-4 py-8 sm:px-6 sm:py-12">
      <Button variant="quiet" className="-ml-1 text-muted hover:text-ink" onClick={onEdit}>
        <ArrowLeftIcon />
        Edit the details
      </Button>
      <div className="mt-4 max-w-3xl">
        <StepHeading focus={focusHeading}>Your transaction record</StepHeading>
        <p className="mt-4 text-lg leading-relaxed text-muted">Check it over, then download it as a PDF or an image.</p>
      </div>

      <div className="mt-8 grid gap-8 lg:grid-cols-[minmax(0,1fr)_300px]">
        {/* The receipt rests on a sunken desk. */}
        <div className="min-w-0 overflow-hidden border border-line bg-well shadow-well">
          {html !== null ? (
            <iframe
              ref={frameRef}
              title="Transaction record preview"
              srcDoc={html}
              sandbox="allow-same-origin"
              onLoad={measure}
              style={{ height: frameHeight }}
              className="block w-full"
            />
          ) : error ? (
            <div className="p-5">
              <Notice
                tone="error"
                title="The preview couldn't be created"
                action={
                  <Button size="sm" variant="secondary" onClick={onEdit}>
                    Edit the details
                  </Button>
                }
              >
                {error}
              </Notice>
            </div>
          ) : (
            <PrintingLoader />
          )}
        </div>

        <aside className="space-y-5 lg:sticky lg:top-24 lg:self-start">
          {html !== null && (
            <div className="border border-line bg-surface px-5 pt-5 pb-5 shadow-e2">
              <Celebration />
              <p className="mt-6 flex items-center justify-center gap-2.5 text-ui font-medium">
                <PixelCheck cell={2} className="text-accent" />
                Your record is ready
              </p>
            </div>
          )}
          <div className="space-y-4 border border-line bg-surface p-5 shadow-e2">
            <p className="label-caps text-muted">Download</p>
            <Button
              className="w-full"
              disabled={html === null || exporting === "png"}
              aria-busy={exporting === "pdf"}
              onClick={() => download("pdf")}
            >
              {label("pdf", "Download PDF", "Printing the PDF…")}
            </Button>
            <Button
              variant="secondary"
              className="w-full"
              disabled={html === null || exporting === "pdf"}
              aria-busy={exporting === "png"}
              onClick={() => download("png")}
            >
              {label("png", "Download image (PNG)", "Printing the image…")}
            </Button>
            {canShare && (
              <Button
                variant="secondary"
                className="w-full"
                disabled={html === null || exporting !== null}
                aria-busy={sharing}
                onClick={share}
              >
                {sharing ? <PixelSpinner className="size-4" /> : <ArrowRightIcon />}
                {sharing ? "Printing the image…" : readyToShare ? "Tap again to share" : "Share the image"}
              </Button>
            )}
            <p aria-live="polite" className="sr-only">
              {downloaded ? `The ${downloaded.toUpperCase()} has downloaded.` : ""}
            </p>
            {exportError && (
              <p role="alert" className="text-meta text-danger">
                {exportError}
              </p>
            )}
          </div>
          <p className="px-1 text-meta leading-relaxed text-muted">Records aren&apos;t saved. Download the PDF or image to keep a copy.</p>
          <Button variant="quiet" onClick={onStartOver}>
            Start a new record
          </Button>
        </aside>
      </div>
    </div>
  );
}
