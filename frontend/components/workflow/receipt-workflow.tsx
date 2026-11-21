"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { extractChat, extractTransaction, getConfig, isAbortError, messageOf } from "@/lib/api";
import { cheer } from "@/lib/chit";
import { countChanges, emptyDraft } from "@/lib/draft";
import { EXAMPLE_EXTRACTION, EXAMPLE_SCREENSHOT } from "@/lib/example";
import type { ExtractionResponse, PublicConfig, Screenshot, TransactionDraft } from "@/lib/types";
import { ExtractingView } from "./extracting-view";
import { PreviewStep } from "./preview-step";
import { ReviewStep } from "./review-step";
import { UploadStep } from "./upload-step";
import { WorkflowHeader } from "./workflow-header";

type Stage = "upload" | "extracting" | "review" | "preview";
type Origin = "screenshots" | "chat_export" | "manual" | "example";

let screenshotCounter = 0;

/** Upload → Extract → Review → Preview → Export, kept in this component's state only. */
export function ReceiptWorkflow() {
  const [config, setConfig] = useState<PublicConfig | null>(null);
  const [configError, setConfigError] = useState<string | null>(null);
  const [configAttempt, setConfigAttempt] = useState(0);

  const [stage, setStage] = useState<Stage>("upload");
  const [navigated, setNavigated] = useState(false);
  const [screenshots, setScreenshots] = useState<Screenshot[]>([]);
  // A WhatsApp chat export is read on its own, never together with screenshots.
  const [chatExport, setChatExport] = useState<File | null>(null);
  const [origin, setOrigin] = useState<Origin>("screenshots");
  const [extraction, setExtraction] = useState<ExtractionResponse | null>(null);
  const [draft, setDraft] = useState<TransactionDraft | null>(null);
  const [acknowledged, setAcknowledged] = useState(false);
  const [progress, setProgress] = useState<{ phase: "uploading" | "reading"; fraction: number }>({
    phase: "uploading",
    fraction: 0,
  });
  const [extractError, setExtractError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const objectUrls = useRef<string[]>([]);

  useEffect(() => {
    const controller = new AbortController();
    getConfig(controller.signal).then(setConfig, (error: unknown) => {
      if (!isAbortError(error)) setConfigError(messageOf(error));
    });
    return () => controller.abort();
  }, [configAttempt]);

  useEffect(() => {
    const urls = objectUrls.current;
    return () => {
      abortRef.current?.abort();
      for (const url of urls) URL.revokeObjectURL(url);
    };
  }, []);

  const hasWork = draft !== null || screenshots.length > 0 || chatExport !== null;
  useEffect(() => {
    if (!hasWork) return;
    const warn = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [hasWork]);

  function go(next: Stage) {
    setStage(next);
    setNavigated(true);
    window.scrollTo({ top: 0 });
  }

  function clearScreenshots() {
    for (const shot of screenshots) if (shot.file) URL.revokeObjectURL(shot.url);
    setScreenshots([]);
  }

  function addScreenshots(files: File[]) {
    const added = files.map((file) => {
      const url = URL.createObjectURL(file);
      objectUrls.current.push(url);
      screenshotCounter += 1;
      return { id: `shot-${screenshotCounter}`, name: file.name, size: file.size, url, file };
    });
    setExtractError(null);
    setChatExport(null);
    setScreenshots((current) => [...current, ...added]);
  }

  function chooseChatExport(file: File) {
    clearScreenshots();
    setExtractError(null);
    setChatExport(file);
  }

  function removeScreenshot(id: string) {
    const target = screenshots.find((shot) => shot.id === id);
    if (target?.file) URL.revokeObjectURL(target.url);
    setScreenshots((current) => current.filter((shot) => shot.id !== id));
  }

  function moveScreenshot(id: string, offset: -1 | 1) {
    setScreenshots((current) => {
      const from = current.findIndex((shot) => shot.id === id);
      const to = from + offset;
      if (from < 0 || to < 0 || to >= current.length) return current;
      const next = [...current];
      [next[from], next[to]] = [next[to], next[from]];
      return next;
    });
  }

  function startDraft(nextOrigin: Origin, nextExtraction: ExtractionResponse | null, nextDraft: TransactionDraft) {
    setOrigin(nextOrigin);
    setExtraction(nextExtraction);
    setDraft(nextDraft);
    setAcknowledged(false);
    go("review");
  }

  async function extract(focus = "") {
    const files = screenshots.flatMap((shot) => (shot.file ? [shot.file] : []));
    if (chatExport === null && files.length === 0) return;
    const what = chatExport !== null ? "the chat" : "the screenshots";
    if (draft !== null && !window.confirm(`Reading ${what} again replaces the details you've reviewed. Continue?`)) {
      return;
    }
    const controller = new AbortController();
    abortRef.current = controller;
    setExtractError(null);
    setProgress({ phase: "uploading", fraction: 0 });
    go("extracting");
    const options = {
      signal: controller.signal,
      onUploadProgress: (fraction: number) => setProgress({ phase: "uploading", fraction }),
      onUploaded: () => setProgress({ phase: "reading", fraction: 1 }),
    };
    try {
      if (chatExport !== null) {
        const result = await extractChat(chatExport, focus, options);
        startDraft("chat_export", result, result.draft);
      } else {
        const result = await extractTransaction(files, options);
        startDraft("screenshots", result, result.draft);
      }
    } catch (error) {
      if (!isAbortError(error)) {
        setExtractError(messageOf(error));
        cheer("worried", 2600);
      }
      go("upload");
    } finally {
      abortRef.current = null;
    }
  }

  function enterManually() {
    if (!config) return;
    if (draft !== null && !window.confirm("Start a blank record? The details you've reviewed will be replaced.")) return;
    startDraft("manual", null, emptyDraft(config.default_currency));
  }

  function openExample() {
    if (draft !== null && !window.confirm("Open the example? The details you've reviewed will be replaced.")) return;
    startDraft("example", EXAMPLE_EXTRACTION, EXAMPLE_EXTRACTION.draft);
  }

  function startOver() {
    if (hasWork && !window.confirm("Start over? Your screenshots or chat and the details will be cleared.")) return;
    abortRef.current?.abort();
    clearScreenshots();
    setChatExport(null);
    setExtraction(null);
    setDraft(null);
    setOrigin("screenshots");
    setAcknowledged(false);
    setExtractError(null);
    go("upload");
  }

  const updateDraft = useCallback((update: (current: TransactionDraft) => TransactionDraft) => {
    setDraft((current) => (current === null ? current : update(current)));
  }, []);

  const step = stage === "review" ? 2 : stage === "preview" ? 3 : 1;
  const reviewScreenshots = origin === "example" ? [EXAMPLE_SCREENSHOT] : origin === "screenshots" ? screenshots : [];
  const changes = draft ? countChanges(draft, extraction?.draft ?? null, extraction?.field_meta ?? {}) : null;
  const stats = {
    from_extraction: origin === "screenshots" || origin === "chat_export",
    edited_fields: changes?.edited ?? 0,
    added_fields: changes?.added ?? 0,
  };

  return (
    <>
      <WorkflowHeader step={step} hasWork={hasWork} onStartOver={startOver} />
      <main>
        {stage === "upload" && (
          <UploadStep
            config={config}
            configError={configError}
            onRetryConfig={() => {
              setConfigError(null);
              setConfigAttempt((n) => n + 1);
            }}
            screenshots={screenshots}
            chatExport={chatExport}
            onAdd={addScreenshots}
            onUseChatExport={chooseChatExport}
            onRemoveChatExport={() => setChatExport(null)}
            onRemove={removeScreenshot}
            onMove={moveScreenshot}
            onExtract={() => extract()}
            onManual={enterManually}
            onExample={openExample}
            onResume={draft !== null ? () => go("review") : null}
            extractError={extractError}
            focusHeading={navigated}
          />
        )}
        {stage === "extracting" && (
          <ExtractingView
            phase={progress.phase}
            fraction={progress.fraction}
            screenshots={chatExport !== null ? [] : screenshots}
            chatExport={chatExport}
            onCancel={() => abortRef.current?.abort()}
          />
        )}
        {stage === "review" && draft !== null && config !== null && (
          <ReviewStep
            config={config}
            draft={draft}
            onDraftChange={updateDraft}
            extraction={extraction}
            isExample={origin === "example"}
            screenshots={reviewScreenshots}
            chatExport={origin === "chat_export" ? chatExport : null}
            onReadDeal={origin === "chat_export" && chatExport !== null ? (deal) => extract(deal) : null}
            acknowledged={acknowledged}
            onAcknowledge={setAcknowledged}
            onBack={() => go("upload")}
            onContinue={() => go("preview")}
            focusHeading={navigated}
          />
        )}
        {stage === "preview" && draft !== null && (
          <PreviewStep
            draft={draft}
            stats={stats}
            onEdit={() => go("review")}
            onStartOver={startOver}
            focusHeading={navigated}
          />
        )}
      </main>
    </>
  );
}
