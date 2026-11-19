import type { ReactNode } from "react";
import { Eyebrow } from "../ui/eyebrow";
import { CheckIcon, ClockIcon, MinusIcon } from "../ui/icons";
import { StatusShape } from "../ui/status";
import { Bubble } from "./chat";
import { PaperRecord } from "./example-record";

type Outcome = "ignored" | "recorded" | "promise";

interface Event {
  side: "in" | "out";
  text: string;
  kind: string;
  outcome: string;
  result: Outcome;
}

// The example from CLAUDE.md: the first price isn't the agreement, and a promise to pay isn't a payment.
const EVENTS: Event[] = [
  { side: "in", text: "The bag is ₦25,000.", kind: "Opening price", outcome: "Not the agreement", result: "ignored" },
  { side: "out", text: "Can you do ₦22,000?", kind: "Counter-offer", outcome: "Not accepted", result: "ignored" },
  { side: "in", text: "₦23,000 is my final price.", kind: "Final price", outcome: "Price on the record", result: "recorded" },
  { side: "out", text: "Okay, I'll take it.", kind: "Acceptance", outcome: "Confirms ₦23,000", result: "recorded" },
  { side: "in", text: "Delivery to Yaba is ₦2,000.", kind: "Delivery fee", outcome: "Added as delivery", result: "recorded" },
  { side: "out", text: "I'll transfer tomorrow morning.", kind: "Promise to pay", outcome: "Recorded as unpaid", result: "promise" },
];

const RESULT_LOOK: Record<Outcome, { icon: ReactNode; tile: string; text: string }> = {
  ignored: { icon: <MinusIcon className="size-3.5" />, tile: "bg-paper-deep text-ink-soft/70", text: "text-ink-soft/75" },
  recorded: { icon: <CheckIcon className="size-3.5" />, tile: "bg-accent-soft text-accent", text: "text-ink" },
  promise: { icon: <ClockIcon className="size-3.5" />, tile: "bg-unpaid-soft text-unpaid", text: "text-unpaid" },
};

export function Demo() {
  return (
    <section aria-labelledby="demo-title" className="relative border-y border-line bg-paper-deep/60">
      <div className="mx-auto grid max-w-6xl gap-14 px-4 py-16 sm:px-6 sm:py-24 lg:grid-cols-[minmax(0,1.08fr)_minmax(0,0.92fr)] lg:gap-16">
        <div className="min-w-0">
          <Eyebrow>Reads the whole chat</Eyebrow>
          <h2 id="demo-title" data-reveal className="mt-4 font-serif text-[38px] leading-[1.04] tracking-[-0.025em] text-balance sm:text-[48px]">
            It follows the negotiation to the actual deal
          </h2>
          <p className="mt-4 max-w-xl text-lg leading-relaxed text-muted">
            The first price in a chat is rarely the agreement. ChatReceipt reads every message in order and records
            what both sides accepted.
          </p>

          <ol data-reveal className="mt-10 overflow-hidden rounded-2xl border border-line bg-chat-wall shadow-e2">
            {EVENTS.map((event, index) => {
              const look = RESULT_LOOK[event.result];
              return (
                <li
                  key={event.text}
                  className={`grid gap-2 px-4 py-3 sm:grid-cols-[minmax(0,1fr)_210px] sm:items-center sm:gap-6 sm:px-5 ${
                    index > 0 ? "border-t border-black/5" : ""
                  }`}
                >
                  <div className="flex flex-col">
                    <Bubble side={event.side} faded={event.result === "ignored"}>
                      {event.result === "recorded" ? <span className="mark">{event.text}</span> : event.text}
                    </Bubble>
                  </div>
                  <div className="flex items-center gap-2.5 sm:border-l sm:border-black/5 sm:pl-5">
                    <span className={`flex size-6 shrink-0 items-center justify-center rounded-md ${look.tile}`}>{look.icon}</span>
                    <div className="min-w-0">
                      <p className="label-caps text-2xs text-ink-soft/80">{event.kind}</p>
                      <p className={`text-sm font-medium ${look.text}`}>{event.outcome}</p>
                    </div>
                  </div>
                </li>
              );
            })}
          </ol>

          {/* What the six messages come down to on the record. */}
          <dl data-reveal data-reveal-delay={120} className="mt-5 grid gap-3 sm:grid-cols-3">
            <div className="rounded-xl border border-line bg-surface px-4 py-3.5 shadow-e1">
              <dt className="label-caps text-2xs text-muted">Agreed price</dt>
              <dd className="mt-1.5 font-serif text-[26px] leading-none tracking-[-0.02em]">₦23,000</dd>
              <dd className="mt-1.5 text-meta text-muted">Not the ₦25,000 first asked</dd>
            </div>
            <div className="rounded-xl border border-line bg-surface px-4 py-3.5 shadow-e1">
              <dt className="label-caps text-2xs text-muted">Delivery</dt>
              <dd className="mt-1.5 font-serif text-[26px] leading-none tracking-[-0.02em]">₦2,000</dd>
              <dd className="mt-1.5 text-meta text-muted">Agreed separately, added on top</dd>
            </div>
            <div className="rounded-xl border border-unpaid/20 bg-unpaid-soft/50 px-4 py-3.5 shadow-e1">
              <dt className="label-caps text-2xs text-unpaid">Payment</dt>
              <dd className="mt-1.5 flex items-center gap-2 font-serif text-[26px] leading-none tracking-[-0.02em] text-unpaid">
                <StatusShape status="unpaid" className="size-4" />
                Unpaid
              </dd>
              <dd className="mt-1.5 text-meta text-unpaid/80">A promise to pay isn&apos;t a payment</dd>
            </div>
          </dl>
        </div>

        <div className="min-w-0 lg:sticky lg:top-24 lg:self-start">
          <div data-reveal data-reveal-delay={140} className="rounded-[24px] bg-well p-4 shadow-well sm:p-6">
            <PaperRecord variant="full" idPrefix="demo-rec" />
          </div>
          <p className="mt-4 text-center text-meta text-muted">The record this conversation produces.</p>
        </div>
      </div>
    </section>
  );
}
