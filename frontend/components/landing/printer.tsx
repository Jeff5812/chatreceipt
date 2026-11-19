"use client";

import { useEffect, useRef } from "react";
import { PaperRecord } from "./example-record";

/**
 * The record printing out: a black slot with a status light, and the paper feeding out of it in steps
 * the first time it scrolls into view. Without JavaScript or with reduced motion, the paper is simply there.
 */
export function RecordPrinter() {
  const root = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = root.current;
    if (!el || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    el.classList.add("print-armed");
    let cancelled = false;
    let fallback = 0;
    const observer = new IntersectionObserver(
      async ([entry]) => {
        if (!entry?.isIntersecting) return;
        observer.disconnect();
        fallback = window.setTimeout(() => el.classList.remove("print-armed"), 3000);
        const { animate } = await import("animejs");
        if (cancelled) return;
        window.clearTimeout(fallback);
        const paper = el.querySelector<HTMLElement>("[data-paper]");
        const light = el.querySelector<HTMLElement>("[data-light]");
        if (light) animate(light, { opacity: [1, 0.25, 1, 0.25, 1], duration: 1500, ease: "steps(1)" });
        if (paper) {
          animate(paper, {
            translateY: ["-62%", "0%"],
            duration: 1500,
            ease: "steps(16)",
            onComplete: () => el.classList.remove("print-armed"),
          });
        }
      },
      { threshold: 0.3 },
    );
    observer.observe(el);
    return () => {
      cancelled = true;
      observer.disconnect();
      window.clearTimeout(fallback);
    };
  }, []);

  return (
    <div ref={root} className="relative mx-auto w-full max-w-[460px]">
      {/* The slot. */}
      <div className="relative z-10 flex h-12 items-center justify-between rounded-none border border-line-strong bg-noir px-4 shadow-e2">
        <span className="font-mono text-caption text-muted">record.pdf</span>
        <span className="flex items-center gap-2 font-mono text-caption text-muted">
          <span data-light className="block size-2 bg-accent" />
          ready
        </span>
        <span aria-hidden className="absolute inset-x-6 bottom-0 h-1 translate-y-1/2 rounded-none bg-black" />
      </div>
      {/* The paper feeds out from under the slot. */}
      <div className="-mt-1 overflow-hidden px-4 pb-4 sm:px-6">
        <div data-paper>
          <PaperRecord />
        </div>
      </div>
    </div>
  );
}
