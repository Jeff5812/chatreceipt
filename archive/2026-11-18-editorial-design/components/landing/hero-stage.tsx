import Image from "next/image";
import type { CSSProperties, ReactNode } from "react";
import heroPhoto from "@/assets/imagery/hero-hands.jpg";
import { BrandMark } from "../brand";
import { Bubble, Marked } from "./chat";

const delay = (seconds: number): CSSProperties => ({ animationDelay: `${seconds}s` });

// The slip's torn bottom edge: a row of small teeth cut with clip-path.
const TEETH = 14;
const TORN_EDGE = `polygon(0 0, 100% 0, ${Array.from({ length: TEETH * 2 + 1 }, (_, i) => {
  const x = 100 - (i * 100) / (TEETH * 2);
  return `${x.toFixed(3)}% ${i % 2 === 0 ? "calc(100% - 1.3cqw)" : "100%"}`;
}).join(", ")})`;

/** One message in the hero conversation: it arrives at `at` seconds. Times show only on wider stages. */
function Message({ side, time, at, children, inner }: { side: "in" | "out"; time: string; at: number; children: ReactNode; inner?: { className: string; at: number } }) {
  return (
    <div className={`anim-rise flex ${side === "in" ? "justify-start" : "justify-end"}`} style={delay(at)}>
      <Bubble side={side} size="lg" className={inner?.className ?? ""} style={inner ? delay(inner.at) : undefined}>
        {children}
        <span className="mt-[0.15em] block text-right text-[0.7em] font-normal tabular-nums @max-md:hidden">{time}</span>
      </Bubble>
    </div>
  );
}

/**
 * The hero: a photograph of the moment, with the example negotiation as large live chat bubbles.
 * It plays once as three beats: the messages arrive, the offers that weren't accepted step back while the
 * agreed price and the acceptance are marked, then the record slip appears with only the agreed price.
 * Everything is sized from the stage's own width (container units), so it reads the same on a phone.
 */
export function HeroStage() {
  return (
    <div className="anim-rise mx-auto w-full max-w-[560px]" style={delay(0.1)}>
      <div className="@container relative aspect-[6/7] w-full overflow-hidden rounded-[30px] bg-paper-deep shadow-[0_2px_4px_rgb(20_16_8/0.12),0_40px_80px_-32px_rgb(20_16_8/0.45)] ring-1 ring-black/5">
        <Image
          src={heroPhoto}
          alt=""
          fill
          priority
          placeholder="blur"
          sizes="(min-width: 640px) 560px, 92vw"
          className="object-cover object-left"
        />

        <span className="label-caps absolute top-[4.5%] left-[4.5%] flex items-center gap-2 rounded-full bg-noir px-3 py-1.5 text-[clamp(9.5px,1.95cqw,11.5px)] text-sheet shadow-[inset_0_1px_0_rgb(255_255_255/0.14),0_8px_18px_-8px_rgb(0_0_0/0.6)]">
          <span className="relative flex size-2">
            <span className="anim-ping absolute inset-0 rounded-full bg-[#7fd3a0]" />
            <span className="relative size-2 rounded-full bg-[#7fd3a0]" />
          </span>
          Example conversation
        </span>

        {/* Beat 3: the record, holding only what was agreed. */}
        <div className="absolute top-[12.5%] left-[4.5%] w-[37%] -rotate-[4deg]">
          <div className="anim-rise" style={delay(3.1)}>
            <div className="anim-float" style={delay(4.6)}>
              <div className="[filter:drop-shadow(0_2px_2px_rgb(0_0_0/0.25))_drop-shadow(0_20px_26px_rgb(20_16_8/0.38))]">
                <div
                  className="bg-noir bg-linear-to-b from-noir-raised to-noir px-[3.2cqw] pt-[2.8cqw] pb-[4.4cqw] text-sheet shadow-[inset_0_1px_0_rgb(255_255_255/0.12)]"
                  style={{ clipPath: TORN_EDGE, borderRadius: "1.8cqw 1.8cqw 0 0" }}
                >
                  <p className="label-caps flex items-center gap-[1.2cqw] text-[clamp(8.5px,1.75cqw,10.5px)] text-mist">
                    <BrandMark className="size-[clamp(13px,2.7cqw,16px)]" idPrefix="hero-slip" />
                    On the record
                  </p>
                  <p className="mt-[2cqw] text-[clamp(11px,2.45cqw,14px)] text-mist">Leather bag × 1</p>
                  <p className="mt-[0.5cqw] font-serif text-[clamp(20px,5.5cqw,31px)] leading-none tracking-[-0.02em] tabular-nums">₦23,000.00</p>
                  <p className="mt-[2.4cqw] border-t border-dashed border-white/15 pt-[1.8cqw] text-[clamp(10px,2.05cqw,12px)] leading-snug text-mist">
                    Agreed price, not the ₦25,000 first asked
                  </p>
                </div>
              </div>
            </div>
          </div>
        </div>

        {/* Beats 1 and 2: the conversation, then the reading. */}
        <div className="absolute right-[4.5%] bottom-[5%] flex w-[58%] flex-col @max-md:w-[68%] gap-[1.5cqw]">
          <Message side="in" time="10:36" at={0.5} inner={{ className: "anim-dim-in", at: 2.2 }}>
            The bag is ₦25,000.
          </Message>
          <Message side="out" time="10:39" at={0.8} inner={{ className: "anim-dim-out", at: 2.3 }}>
            Can you do ₦22,000?
          </Message>
          <Message side="in" time="10:41" at={1.1}>
            <Marked at={2.5}>₦23,000 is my final price.</Marked>
          </Message>
          <Message side="out" time="10:42" at={1.4}>
            <Marked at={2.75}>Okay, I&apos;ll take it.</Marked>
          </Message>
        </div>

        <p className="sr-only">
          An example chat: the seller asks ₦25,000 for a bag, the buyer offers ₦22,000, the seller&apos;s final price is
          ₦23,000 and the buyer accepts. ChatReceipt records ₦23,000 as the agreed price.
        </p>
      </div>
    </div>
  );
}
