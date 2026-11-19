"use client";

import { useEffect, useRef } from "react";

type Tone = "muted" | "ink" | "lime" | "amber";

interface Event {
  time: string;
  who: "seller" | "buyer";
  text: string;
  tag: string;
  tone: Tone;
}

// The example from CLAUDE.md: the first price isn't the agreement, and a promise to pay isn't a payment.
const EVENTS: Event[] = [
  { time: "10:36", who: "seller", text: "The bag is ₦25,000.", tag: "asking price", tone: "muted" },
  { time: "10:39", who: "buyer", text: "Can you do ₦22,000?", tag: "offer · not accepted", tone: "muted" },
  { time: "10:41", who: "seller", text: "₦23,000 is my final price.", tag: "final price", tone: "ink" },
  { time: "10:42", who: "buyer", text: "Okay, I'll take it.", tag: "✓ deal agreed", tone: "lime" },
  { time: "10:44", who: "seller", text: "Delivery to Yaba is ₦2,000.", tag: "✓ delivery fee", tone: "lime" },
  { time: "10:45", who: "buyer", text: "I'll transfer tomorrow morning.", tag: "! promise to pay", tone: "amber" },
];

interface Total {
  mark: string;
  label: string;
  value: string;
  note?: string;
  tone: Tone;
}

const TOTALS: Total[] = [
  { mark: "✓", label: "agreed price", value: "₦23,000.00", tone: "lime" },
  { mark: "✓", label: "delivery", value: "₦2,000.00", tone: "lime" },
  { mark: "=", label: "total", value: "₦25,000.00", note: "price + delivery", tone: "ink" },
  { mark: "!", label: "payment", value: "unpaid", note: "promised for tomorrow", tone: "amber" },
];

const TONE: Record<Tone, string> = {
  muted: "text-muted",
  ink: "text-ink",
  lime: "text-accent",
  amber: "text-warn",
};

const CELLS = 12;

export function TerminalDemo() {
  const root = useRef<HTMLDivElement>(null);

  // Type the session out once, the first time the terminal scrolls into view. Lines are only hidden once
  // this script has armed them, so the full output shows without JavaScript and with reduced motion.
  useEffect(() => {
    const el = root.current;
    if (!el || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    el.classList.add("term-armed");
    let cancelled = false;
    let fallback = 0;
    const observer = new IntersectionObserver(
      async ([entry]) => {
        if (!entry?.isIntersecting) return;
        observer.disconnect();
        fallback = window.setTimeout(() => el.classList.remove("term-armed"), 4000);
        const { createTimeline, stagger } = await import("animejs");
        if (cancelled) return;
        window.clearTimeout(fallback);
        const lines = el.querySelectorAll<HTMLElement>("[data-line]");
        const cells = el.querySelectorAll<HTMLElement>("[data-cell]");
        const show = { opacity: [0, 1], translateX: [-6, 0], duration: 260, ease: "out(3)" };
        createTimeline({ onComplete: () => el.classList.remove("term-armed") })
          .add(lines[0], show)
          .add(lines[1], show, "+=120")
          .add(cells, { backgroundColor: ["#34403a", "#c4f25a"], duration: 60, delay: stagger(70), ease: "steps(1)" })
          .add(Array.from(lines).slice(2), { ...show, delay: stagger(230) }, "+=140");
      },
      { threshold: 0.35 },
    );
    observer.observe(el);
    return () => {
      cancelled = true;
      observer.disconnect();
      window.clearTimeout(fallback);
    };
  }, []);

  return (
    <figure className="mx-auto max-w-4xl">
      <div ref={root} className="overflow-hidden rounded-none border border-line bg-noir-raised shadow-e3">
        <div className="flex items-center gap-2 border-b border-line bg-noir px-4 py-3">
          <span className="size-2.5 rounded-none bg-line-strong" />
          <span className="size-2.5 rounded-none bg-line-strong" />
          <span className="size-2.5 rounded-none bg-line-strong" />
          <span className="ml-2 truncate font-mono text-caption text-muted">chatreceipt · reading 2 screenshots</span>
        </div>

        <div className="px-4 py-5 font-mono text-[12.5px] leading-[1.7] sm:px-6 sm:py-6 sm:text-[13px]">
          <p data-line className="text-ink">
            <span className="text-accent">›</span> read <span className="text-muted">screenshot-1.png screenshot-2.png</span>
          </p>
          <p data-line className="flex flex-wrap items-center gap-x-3 text-muted">
            <span>reading</span>
            <span className="flex gap-[3px]" aria-hidden>
              {Array.from({ length: CELLS }, (_, i) => (
                <span key={i} data-cell className="block h-[0.85em] w-[0.55em] bg-accent" />
              ))}
            </span>
            <span className="text-ink">2 of 2</span>
          </p>

          <ol className="mt-4 space-y-2 sm:space-y-0.5">
            {EVENTS.map((event) => (
              <li
                key={event.time}
                data-line
                className="grid grid-cols-[3.4rem_minmax(0,1fr)] gap-x-2 sm:grid-cols-[3.4rem_3.6rem_minmax(0,1fr)_13.5rem] sm:gap-x-3"
              >
                <span className="text-muted">{event.time}</span>
                <span className="text-muted sm:order-none">{event.who}</span>
                <span className="col-start-2 text-ink sm:col-start-auto">&ldquo;{event.text}&rdquo;</span>
                <span className={`col-start-2 sm:col-start-auto sm:text-right ${TONE[event.tone]}`}>{event.tag}</span>
              </li>
            ))}
          </ol>

          <dl className="mt-5 space-y-2 border-t border-dashed border-line-strong pt-4 sm:space-y-0.5">
            {TOTALS.map((total) => (
              <div
                key={total.label}
                data-line
                className="grid grid-cols-[1.2rem_7rem_minmax(0,1fr)] gap-x-2 sm:grid-cols-[1.2rem_8rem_8rem_minmax(0,1fr)] sm:gap-x-3"
              >
                <span className={TONE[total.tone]}>{total.mark}</span>
                <dt className="text-muted">{total.label}</dt>
                <dd className={`tabular-nums ${total.tone === "amber" ? TONE.amber : "text-ink"}`}>{total.value}</dd>
                {total.note && <dd className="col-start-2 col-end-4 text-muted sm:col-start-auto sm:col-end-auto">{total.note}</dd>}
              </div>
            ))}
          </dl>

          <p data-line className="mt-4 text-ink">
            <span className="text-accent">›</span> 1 detail to check, then download{" "}
            <span aria-hidden className="anim-blink inline-block h-[1.05em] w-[0.55em] translate-y-[0.18em] bg-accent" />
          </p>
        </div>
      </div>
      <figcaption className="mt-3 text-center text-meta text-muted">
        A sample conversation and what ChatReceipt records from it.
      </figcaption>
    </figure>
  );
}
