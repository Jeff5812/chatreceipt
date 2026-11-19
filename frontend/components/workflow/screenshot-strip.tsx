"use client";

import { useRef, useState } from "react";
import type { Screenshot } from "@/lib/types";
import { CloseIcon } from "../ui/icons";

/** Thumbnails of the screenshots, numbered to match the evidence quotes; tap to enlarge. */
export function ScreenshotStrip({ screenshots, title }: { screenshots: Screenshot[]; title: string }) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [open, setOpen] = useState<number | null>(null);

  function show(index: number) {
    setOpen(index);
    dialogRef.current?.showModal();
  }

  const current = open === null ? null : screenshots[open];

  return (
    <div>
      <div className="flex items-baseline justify-between gap-3">
        <p className="text-sm font-medium">{title}</p>
        <p className="text-caption text-muted">Select to enlarge</p>
      </div>
      <ol className="-mx-1 mt-2 flex gap-3 overflow-x-auto px-1 pt-1 pb-2">
        {screenshots.map((shot, index) => (
          <li key={shot.id} className="shrink-0">
            <button
              type="button"
              onClick={() => show(index)}
              className="group block w-[76px] overflow-hidden rounded-lg border border-line bg-surface shadow-e1 transition-[transform,box-shadow,border-color] duration-200 ease-soft hover:-translate-y-0.5 hover:border-ink/30 hover:shadow-e2"
              aria-label={`Open screenshot ${index + 1}`}
            >
              {/* eslint-disable-next-line @next/next/no-img-element -- local preview of an uploaded file */}
              <img src={shot.url} alt="" className="aspect-[9/14] w-full object-cover object-top" />
              <span className="block border-t border-line py-1 text-center text-label font-medium text-muted tabular-nums group-hover:text-ink">
                {index + 1}
              </span>
            </button>
          </li>
        ))}
      </ol>
      <dialog
        ref={dialogRef}
        onClose={() => setOpen(null)}
        onClick={(event) => {
          if (event.target === dialogRef.current) dialogRef.current?.close();
        }}
        className="m-auto w-[min(92vw,520px)] max-w-none overflow-hidden rounded-2xl border border-line bg-surface p-0 text-ink shadow-e3 backdrop:bg-noir/65"
      >
        <div className="flex items-center justify-between border-b border-line px-4 py-3">
          <p className="text-sm font-medium">{open === null ? "" : `Screenshot ${open + 1}`}</p>
          <button
            type="button"
            onClick={() => dialogRef.current?.close()}
            aria-label="Close"
            className="flex size-8 items-center justify-center rounded-md hover:bg-paper"
          >
            <CloseIcon />
          </button>
        </div>
        <div className="max-h-[78dvh] overflow-y-auto bg-paper-deep">
          {current && (
            // eslint-disable-next-line @next/next/no-img-element -- local preview of an uploaded file
            <img src={current.url} alt={`Screenshot ${(open ?? 0) + 1}`} className="w-full" />
          )}
        </div>
      </dialog>
    </div>
  );
}
