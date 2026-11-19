import type { CSSProperties, ReactNode } from "react";

const delay = (seconds: number): CSSProperties => ({ animationDelay: `${seconds}s` });

// Incoming messages are warm white; outgoing ones are emerald with light text. Both are soft, raised shapes.
// A faded bubble is a message that didn't become part of the deal.
const LOOK = {
  in: {
    base: "self-start rounded-tl-[5px] bg-sheet text-ink ring-1 ring-black/[0.04] shadow-[0_1px_1px_rgb(20_16_8/0.08),0_10px_20px_-12px_rgb(20_16_8/0.4)]",
    faded: "self-start rounded-tl-[5px] bg-sheet/75 text-ink/65 ring-1 ring-black/[0.03]",
    time: "text-muted",
  },
  out: {
    base: "self-end rounded-tr-[5px] bg-bubble-out bg-linear-to-br from-bubble-out to-accent-strong text-white shadow-[inset_0_1px_0_rgb(255_255_255/0.22),0_1px_1px_rgb(10_30_20/0.2),0_12px_22px_-12px_rgb(10_40_28/0.6)]",
    faded: "self-end rounded-tr-[5px] bg-accent-soft text-ink/65 ring-1 ring-black/[0.03]",
    time: "text-white/85",
  },
} as const;

const SIZE = {
  // In lists and cards.
  sm: "max-w-[86%] rounded-[14px] px-3 py-1.5 text-meta leading-snug",
  // On the hero stage: sized from the stage's own width, so it stays readable on a phone.
  lg: "max-w-[92%] rounded-[1.25em] px-[1em] py-[0.62em] text-[clamp(13.5px,3.05cqw,17px)] leading-[1.32] font-medium",
} as const;

/** A chat message bubble. */
export function Bubble({
  side,
  time,
  faded = false,
  size = "sm",
  className = "",
  style,
  children,
}: {
  side: "in" | "out";
  time?: string;
  faded?: boolean;
  size?: keyof typeof SIZE;
  className?: string;
  style?: CSSProperties;
  children: ReactNode;
}) {
  const look = LOOK[side];
  return (
    <p className={`${SIZE[size]} ${faded ? look.faded : look.base} ${className}`} style={style}>
      {children}
      {time && <span className={`ml-2 inline-block translate-y-px text-[0.72em] font-normal tabular-nums ${faded ? "text-muted" : look.time}`}>{time}</span>}
    </p>
  );
}

/** A marker highlight over the words a value came from. Animates in when `at` (seconds) is set. */
export function Marked({ at, children }: { at?: number; children: ReactNode }) {
  return (
    <span className={`mark ${at !== undefined ? "anim-mark" : ""}`} style={at !== undefined ? delay(at) : undefined}>
      {children}
    </span>
  );
}
