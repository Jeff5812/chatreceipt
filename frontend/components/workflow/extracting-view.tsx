"use client";

import { useEffect, useState, type CSSProperties } from "react";
import type { Screenshot } from "@/lib/types";
import { Button } from "../ui/button";
import { ChatIcon } from "../ui/icons";
import { ChitRunner } from "./chit-runner";
import { StepHeading } from "./step-heading";

const CELLS = 24;

export function ExtractingView({
  phase,
  fraction,
  screenshots,
  chatExport = null,
  onCancel,
}: {
  phase: "uploading" | "reading";
  fraction: number;
  screenshots: Screenshot[];
  chatExport?: File | null;
  onCancel: () => void;
}) {
  const [elapsed, setElapsed] = useState(0);
  useEffect(() => {
    const started = performance.now();
    const timer = window.setInterval(() => setElapsed(Math.floor((performance.now() - started) / 1000)), 1000);
    return () => window.clearInterval(timer);
  }, []);

  const count = screenshots.length;
  const percent = Math.round(fraction * 100);
  const noun = count === 1 ? "screenshot" : "screenshots";
  const lit = Math.round(fraction * CELLS);
  const shown = screenshots.slice(0, 5);

  return (
    <div className="mx-auto max-w-2xl px-4 py-14 sm:px-6 sm:py-20">
      <StepHeading focus>Reading the conversation</StepHeading>
      <div className="mt-8 overflow-hidden border border-line-strong bg-surface shadow-e3" aria-live="polite">
        <ChitRunner label={phase === "uploading" ? "Uploading" : "Reading the chat"} />

        <div className="border-t border-line p-6">
          <p className="text-ui font-medium">
            {phase === "uploading"
              ? chatExport
                ? `Uploading the chat export… ${percent}%`
                : `Uploading ${count} ${noun}… ${percent}%`
              : chatExport
                ? "Reading the chat: who is who, the deal, the agreed price and the payments…"
                : "Finding the agreed items, prices and payment details…"}
          </p>

          {/* A segmented bar, like an old game's loading screen. */}
          <div
            role="progressbar"
            aria-label={phase === "uploading" ? "Upload progress" : "Reading the conversation"}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={phase === "uploading" ? percent : undefined}
            className={`mt-4 flex gap-[3px] border border-line bg-well p-[3px] ${phase === "reading" ? "seg-sweep" : ""}`}
          >
            {Array.from({ length: CELLS }, (_, i) => (
              <span
                key={i}
                className={`h-3 flex-1 ${phase === "uploading" && i < lit ? "bg-accent" : "bg-line"}`}
                style={{ "--i": i } as CSSProperties}
              />
            ))}
          </div>

          <p className="mt-4 text-sm text-muted">
            This may take a minute.{elapsed >= 5 ? ` ${elapsed} seconds so far.` : ""}
          </p>

          {chatExport && (
            <p className="mt-5 flex items-center gap-2 font-mono text-caption text-muted">
              <ChatIcon className="size-4 shrink-0 text-accent" />
              <span className="truncate">{chatExport.name}</span>
            </p>
          )}

          {shown.length > 0 && (
            <ul aria-label="Screenshots being read" className="mt-5 flex items-end gap-2">
              {shown.map((shot, index) => (
                <li key={shot.id} className="relative h-[68px] w-[46px] overflow-hidden border border-line-strong bg-sheet">
                  {/* eslint-disable-next-line @next/next/no-img-element -- local preview of an uploaded file */}
                  <img src={shot.url} alt={`Screenshot ${index + 1}`} className="size-full object-cover object-top" />
                  {phase === "reading" && index === 0 && (
                    <span aria-hidden className="anim-scan-loop absolute inset-x-0 top-0 h-[24%] bg-linear-to-b from-transparent via-[rgb(196_242_90/0.25)] to-transparent" />
                  )}
                </li>
              ))}
              {count > shown.length && <li className="pb-1 font-mono text-caption text-muted">+{count - shown.length}</li>}
            </ul>
          )}
        </div>
      </div>
      <Button variant="secondary" className="mt-6" onClick={onCancel}>
        Cancel
      </Button>
    </div>
  );
}
