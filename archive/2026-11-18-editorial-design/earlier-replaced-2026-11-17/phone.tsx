import type { CSSProperties, ReactNode } from "react";
import { Marked } from "./chat";

const delay = (seconds: number): CSSProperties => ({ animationDelay: `${seconds}s` });

function StatusBar() {
  return (
    <div className="flex h-[46px] items-center justify-between px-[28px] pt-[8px] text-[12.5px] font-semibold text-ink">
      <span className="tracking-[-0.01em] tabular-nums">10:44</span>
      <span aria-hidden className="flex items-center gap-[5px]">
        <svg viewBox="0 0 17 11" className="h-[10px] w-[16px]" fill="currentColor">
          <rect x="0" y="7" width="3" height="4" rx="0.7" />
          <rect x="4.5" y="5" width="3" height="6" rx="0.7" />
          <rect x="9" y="2.5" width="3" height="8.5" rx="0.7" />
          <rect x="13.5" y="0" width="3" height="11" rx="0.7" />
        </svg>
        <svg viewBox="0 0 16 12" className="h-[10px] w-[14px]" fill="currentColor">
          <path d="M8 2.4c2.3 0 4.4.9 6 2.4l1.1-1.2A10.2 10.2 0 0 0 8 .7 10.2 10.2 0 0 0 .9 3.6L2 4.8a8.5 8.5 0 0 1 6-2.4Zm0 3.4c1.4 0 2.6.5 3.6 1.4l1.1-1.2A6.9 6.9 0 0 0 8 4.1a6.9 6.9 0 0 0-4.7 1.9l1.1 1.2c1-.9 2.2-1.4 3.6-1.4Zm0 3.4c.5 0 1 .2 1.3.5L8 11.3 6.7 9.7c.3-.3.8-.5 1.3-.5Z" />
        </svg>
        <span className="relative flex h-[11px] w-[23px] items-center rounded-[3.5px] border border-ink/45 p-[1.5px]">
          <span className="h-full w-[72%] rounded-[1.5px] bg-ink" />
          <span className="absolute -right-[3px] h-[4px] w-[1.5px] rounded-r-sm bg-ink/45" />
        </span>
      </span>
    </div>
  );
}

/**
 * One message. The time sits at the bottom-right corner, with space reserved at the end of the last line,
 * the way messaging apps lay it out.
 */
function Message({
  side,
  time,
  at,
  faded = false,
  children,
}: {
  side: "in" | "out";
  time: string;
  at?: number;
  faded?: boolean;
  children: ReactNode;
}) {
  const look =
    side === "in"
      ? "self-start rounded-[15px] rounded-tl-[4px] bg-sheet"
      : "self-end rounded-[15px] rounded-tr-[4px] bg-bubble-out";
  return (
    <p
      className={`relative max-w-[90%] px-[10px] pt-[6px] pb-[6px] text-[12.5px] leading-[1.38] text-ink shadow-[0_1px_0.5px_rgb(0_0_0/0.13)] ${look} ${faded ? "text-ink/60" : ""} ${at !== undefined ? "anim-rise" : ""}`}
      style={at !== undefined ? delay(at) : undefined}
    >
      {children}
      <span aria-hidden className="inline-block w-[31px]" />
      <span className="absolute right-[8px] bottom-[4px] text-[9.5px] text-ink-soft/80 tabular-nums">{time}</span>
    </p>
  );
}

/**
 * The example negotiation as a live chat. With `animate`, messages arrive, then the deal is highlighted.
 * It is laid out for a screen 275px wide; `height` lets it fit screens of other proportions.
 */
export function ChatScreen({ animate = false, height = 592 }: { animate?: boolean; height?: number }) {
  const at = (seconds: number) => (animate ? seconds : undefined);
  return (
    <div className="relative flex flex-col" style={{ height }}>
      <div className="relative z-10 bg-sheet/95 shadow-[0_1px_0_rgb(0_0_0/0.05)]">
        <StatusBar />
        <div className="flex items-center gap-[10px] px-[14px] pt-[3px] pb-[11px]">
          <svg aria-hidden viewBox="0 0 10 16" className="h-[14px] w-[9px] text-accent" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M8 2 2 8l6 6" />
          </svg>
          <span
            aria-hidden
            className="flex size-[36px] items-center justify-center rounded-full bg-linear-to-b from-[#e7d6b4] to-[#c9b084] font-serif text-[16px] font-semibold text-[#5a4320] shadow-[inset_0_1px_0_rgb(255_255_255/0.55),0_1px_2px_rgb(0_0_0/0.12)]"
          >
            T
          </span>
          <div className="min-w-0 flex-1">
            <p className="text-[14px] leading-tight font-semibold tracking-[-0.01em] text-ink">Tola&apos;s Bags</p>
            <p className="flex items-center gap-[5px] text-[11px] text-accent">
              <span className="size-[6px] rounded-full bg-[#3fae6a]" />
              online
            </p>
          </div>
          <svg aria-hidden viewBox="0 0 20 20" className="size-[18px] text-accent" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
            <path d="M5.2 2.8 7.6 2l1.6 3.7-1.7 1.3a9 9 0 0 0 5.5 5.5l1.3-1.7 3.7 1.6-.8 2.4a2.4 2.4 0 0 1-2.6 1.6A14.7 14.7 0 0 1 3.6 5.4a2.4 2.4 0 0 1 1.6-2.6Z" />
          </svg>
        </div>
      </div>

      <div className="bg-dots-ink flex flex-1 flex-col gap-[7px] px-[12px] pt-[12px]">
        <span className="mx-auto mb-[3px] rounded-md bg-sheet/85 px-[9px] py-[2px] text-[10px] font-medium text-ink-soft/80 shadow-[0_1px_0.5px_rgb(0_0_0/0.08)]">
          Today
        </span>
        <Message side="out" time="10:36" at={at(0.35)} faded>
          Hi, is the brown leather bag still available?
        </Message>
        <Message side="in" time="10:38" at={at(0.5)} faded>
          Yes it is. The bag is ₦25,000.
        </Message>
        <Message side="out" time="10:39" at={at(0.65)} faded>
          Can you do ₦22,000?
        </Message>
        <Message side="in" time="10:41" at={at(0.8)}>
          <Marked at={at(1.7)}>₦23,000 is my final price.</Marked>
        </Message>
        <Message side="out" time="10:42" at={at(0.95)}>
          <Marked at={at(2)}>Okay, I&apos;ll take it.</Marked>
        </Message>
        <Message side="in" time="10:42" at={at(1.1)}>
          <Marked at={at(2.3)}>Delivery to Yaba is ₦2,000.</Marked>
        </Message>
        <Message side="out" time="10:44" at={at(1.25)}>
          <span className="underline decoration-marigold decoration-dashed decoration-2 underline-offset-[3px]">
            I&apos;ll transfer tomorrow morning.
          </span>
        </Message>
        <p
          aria-hidden
          className={`flex items-center gap-[4px] self-start rounded-[15px] rounded-tl-[4px] bg-sheet px-[12px] py-[10px] shadow-[0_1px_0.5px_rgb(0_0_0/0.13)] ${animate ? "anim-rise" : ""}`}
          style={animate ? delay(3) : undefined}
        >
          {[0, 0.16, 0.32].map((d) => (
            <span key={d} className="anim-typing size-[6px] rounded-full bg-muted" style={{ animationDelay: `${d}s` }} />
          ))}
        </p>
      </div>

      <div className="flex items-center gap-[8px] bg-sheet/95 px-[12px] pt-[9px] pb-[24px] shadow-[0_-1px_0_rgb(0_0_0/0.05)]">
        <span className="flex h-[34px] flex-1 items-center rounded-full border border-black/5 bg-white px-[13px] text-[12.5px] text-muted shadow-[inset_0_1px_1px_rgb(0_0_0/0.04)]">
          Message
        </span>
        <span aria-hidden className="flex size-[34px] items-center justify-center rounded-full bg-linear-to-b from-accent-light to-accent text-white shadow-[inset_0_1px_0_rgb(255_255_255/0.22),0_2px_4px_rgb(31_51_40/0.3)]">
          <svg viewBox="0 0 16 16" className="size-[14px]" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round">
            <rect x="5.5" y="1.5" width="5" height="8.5" rx="2.5" />
            <path d="M3 8a5 5 0 0 0 10 0M8 13v2" />
          </svg>
        </span>
      </div>
      <span aria-hidden className="absolute bottom-[8px] left-1/2 h-[4px] w-[116px] -translate-x-1/2 rounded-full bg-ink/85" />

      {animate && (
        <div
          aria-hidden
          className="anim-scan pointer-events-none absolute inset-x-0 top-0 z-20 h-[18%] bg-linear-to-b from-transparent via-[rgb(226_178_60/0.2)] to-transparent"
          style={delay(1.35)}
        >
          <div className="absolute inset-x-0 top-1/2 h-px bg-[rgb(226_178_60/0.6)]" />
        </div>
      )}
      <p className="sr-only">
        An example chat: the buyer asks about a leather bag, the seller asks ₦25,000, the buyer offers ₦22,000, the
        seller&apos;s final price of ₦23,000 is accepted, delivery to Yaba is ₦2,000 and the buyer promises to transfer
        tomorrow morning.
      </p>
    </div>
  );
}
