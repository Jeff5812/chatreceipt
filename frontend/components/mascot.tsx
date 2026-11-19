"use client";

import { useEffect, useRef, useState } from "react";
import { CHIT_EVENT, type ChitSignal } from "@/lib/chit";
import { CHIT_H, CHIT_W, ChitBody, ChitEyes, type Mood } from "./chit";

function prefersReducedMotion(): boolean {
  return typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

const SCAN: [number, number][] = [
  [-1, 0],
  [0, 0],
  [1, 0],
  [0, 0],
];

/**
 * Chit, alive: the eyes follow the pointer, blink every few seconds and scan while it is reading.
 * With `listen`, it also reacts for a moment to signals from anywhere in the app (see lib/chit.ts).
 * Decorative (aria-hidden); whatever wraps it carries the label.
 */
export function Mascot({
  cell = 2,
  mood: givenMood = "idle",
  track = true,
  listen = false,
  className = "",
}: {
  cell?: number;
  mood?: Mood;
  track?: boolean;
  listen?: boolean;
  className?: string;
}) {
  const ref = useRef<SVGSVGElement>(null);
  const [look, setLook] = useState<[number, number]>([0, 0]);
  const [blink, setBlink] = useState(false);
  const [scan, setScan] = useState(0);
  const [signalled, setSignalled] = useState<Mood | null>(null);
  const mood = signalled ?? givenMood;

  // React to app-wide signals: smile when a record is ready, worry when something fails.
  useEffect(() => {
    if (!listen) return;
    let timer = 0;
    const onSignal = (event: Event) => {
      const { mood: next, ms } = (event as CustomEvent<ChitSignal>).detail;
      setSignalled(next);
      window.clearTimeout(timer);
      timer = window.setTimeout(() => setSignalled(null), ms);
    };
    window.addEventListener(CHIT_EVENT, onSignal);
    return () => {
      window.removeEventListener(CHIT_EVENT, onSignal);
      window.clearTimeout(timer);
    };
  }, [listen]);

  // Look towards the pointer. One update per frame, and only when the direction actually changes.
  useEffect(() => {
    if (!track) return;
    let frame = 0;
    let px = 0;
    let py = 0;
    const update = () => {
      frame = 0;
      const box = ref.current?.getBoundingClientRect();
      if (!box) return;
      const dx = px - (box.left + box.width / 2);
      const dy = py - (box.top + box.height * 0.35);
      const qx = Math.abs(dx) < box.width * 0.75 ? 0 : Math.sign(dx);
      const qy = Math.abs(dy) < box.height * 0.75 ? 0 : Math.sign(dy);
      setLook((prev) => (prev[0] === qx && prev[1] === qy ? prev : [qx, qy]));
    };
    const onMove = (event: PointerEvent) => {
      px = event.clientX;
      py = event.clientY;
      if (!frame) frame = requestAnimationFrame(update);
    };
    window.addEventListener("pointermove", onMove, { passive: true });
    return () => {
      window.removeEventListener("pointermove", onMove);
      cancelAnimationFrame(frame);
    };
  }, [track]);

  // Blink at uneven intervals, like something alive.
  useEffect(() => {
    if (prefersReducedMotion()) return;
    let wait = 0;
    let open = 0;
    const schedule = () => {
      wait = window.setTimeout(
        () => {
          setBlink(true);
          open = window.setTimeout(() => setBlink(false), 140);
          schedule();
        },
        2200 + Math.random() * 3800,
      );
    };
    schedule();
    return () => {
      window.clearTimeout(wait);
      window.clearTimeout(open);
    };
  }, []);

  // While reading, the eyes run along the lines.
  useEffect(() => {
    if (mood !== "reading" || prefersReducedMotion()) return;
    const timer = window.setInterval(() => setScan((step) => (step + 1) % SCAN.length), 280);
    return () => window.clearInterval(timer);
  }, [mood]);

  const eyesLook = mood === "reading" ? SCAN[scan] : look;
  return (
    <svg
      ref={ref}
      viewBox={`0 0 ${CHIT_W} ${CHIT_H}`}
      width={CHIT_W * cell}
      height={CHIT_H * cell}
      shapeRendering="crispEdges"
      aria-hidden
      className={`shrink-0 ${className}`}
    >
      <ChitBody />
      <ChitEyes look={eyesLook} blink={blink && mood !== "happy"} mood={mood} />
    </svg>
  );
}

const SIZES = {
  sm: { cell: 2, word: "text-[16px]", gap: "gap-2" },
  md: { cell: 2, word: "text-[24px]", gap: "gap-2.5" },
  lg: { cell: 3, word: "text-[24px]", gap: "gap-3" },
};

/** Chit and the pixel wordmark. Chit smiles while the logo is hovered. */
export function Logo({ size = "md" }: { size?: keyof typeof SIZES }) {
  const [hover, setHover] = useState(false);
  const s = SIZES[size];
  return (
    <span className={`inline-flex items-center ${s.gap}`} onPointerEnter={() => setHover(true)} onPointerLeave={() => setHover(false)}>
      <Mascot cell={s.cell} mood={hover ? "happy" : "idle"} listen />
      <span className={`font-pixel ${s.word} leading-none tracking-[0.01em] text-ink`}>ChatReceipt</span>
    </span>
  );
}

// Things Chit says when pressed. Short, and true to how the product behaves.
const LINES = ["Send me the screenshots.", "I read the chat in order.", "₦23,000 was agreed, not ₦25,000.", "Anything unclear, I flag.", "You get a PDF or PNG."];

/** The big Chit in the hero: press it and it hops and says something. */
export function HeroChit() {
  const body = useRef<HTMLSpanElement>(null);
  const [said, setSaid] = useState(-1);
  const [happy, setHappy] = useState(false);
  const settle = useRef(0);

  useEffect(() => () => window.clearTimeout(settle.current), []);

  const press = async () => {
    setSaid((index) => (index + 1) % LINES.length);
    setHappy(true);
    window.clearTimeout(settle.current);
    settle.current = window.setTimeout(() => setHappy(false), 1400);
    if (prefersReducedMotion() || !body.current) return;
    const { animate } = await import("animejs");
    animate(body.current, {
      translateY: [
        { to: -18, duration: 200, ease: "outQuad" },
        { to: 0, duration: 520, ease: "outBounce" },
      ],
      scaleY: [
        { to: 1.06, duration: 200, ease: "outQuad" },
        { to: 0.92, duration: 90, ease: "inQuad" },
        { to: 1, duration: 380, ease: "outElastic(1, .5)" },
      ],
    });
  };

  return (
    <div className="relative inline-flex">
      <button
        type="button"
        onClick={press}
        aria-label="Chit, the ChatReceipt mascot. Press to hear what it does."
        className="group relative rounded-none p-2 outline-offset-4"
      >
        <span ref={body} className="block origin-bottom">
          <Mascot cell={9} mood={happy ? "happy" : "idle"} className="transition-transform duration-200 group-hover:-translate-y-0.5" />
        </span>
        <span aria-hidden className="mx-auto mt-2 block h-1.5 w-20 rounded-[50%] bg-black/60 blur-[3px]" />
      </button>
      {/* Speech bubble: typing dots until Chit is pressed. */}
      <p
        aria-live="polite"
        className="absolute top-0 left-[calc(100%-6px)] w-max max-w-[120px] -translate-y-1/3 sm:max-w-[240px]"
      >
        <span className="notch-4 relative block bg-accent px-3 py-2 font-mono text-[13px] leading-snug font-medium text-accent-ink">
          {said < 0 ? (
            <span className="flex gap-1 py-1.5">
              <span className="sr-only">Chit is waiting.</span>
              {[0, 1, 2].map((i) => (
                <span key={i} aria-hidden className="anim-dot block size-1.5 bg-accent-ink" style={{ animationDelay: `${i * 0.2}s` }} />
              ))}
            </span>
          ) : (
            LINES[said]
          )}
        </span>
        <span aria-hidden className="absolute -bottom-1.5 left-2 block size-1.5 bg-accent" />
        <span aria-hidden className="absolute -bottom-3 left-0.5 block size-1.5 bg-accent" />
      </p>
    </div>
  );
}
