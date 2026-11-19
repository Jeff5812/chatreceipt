"use client";

import { useEffect, useRef, type CSSProperties, type ReactNode } from "react";
import { BUBBLE, COIN, MATERIALS, PHONE, PixelArt, TICK } from "../pixel-art";

/**
 * Floating pixel objects with depth: extruded sprites that bob slowly, cast a soft shadow on the "floor"
 * and drift a little with the pointer (deeper objects drift further). Purely decorative.
 */
function Float({
  children,
  className,
  depth,
  rotate,
  duration,
  delay,
}: {
  children: ReactNode;
  className: string;
  depth: number;
  rotate: number;
  duration: number;
  delay: number;
}) {
  const vars = { "--k": depth, "--rot": `${rotate}deg`, "--dur": `${duration}s`, "--delay": `${delay}s` } as CSSProperties;
  return (
    <div className={`float-drift absolute ${className}`} style={vars}>
      <div className="float-bob">{children}</div>
      <span aria-hidden className="float-shadow mx-auto mt-3 block h-2 w-3/4 rounded-[50%] bg-black blur-[6px]" />
    </div>
  );
}

const WHITE_BUBBLE = { ...BUBBLE, palette: { L: MATERIALS.paper, K: MATERIALS.ink } };

/** Pointer parallax: one pair of CSS variables on the wrapper, written at most once a frame. */
function useParallax() {
  const root = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    let frame = 0;
    let mx = 0;
    let my = 0;
    const write = () => {
      frame = 0;
      root.current?.style.setProperty("--mx", mx.toFixed(3));
      root.current?.style.setProperty("--my", my.toFixed(3));
    };
    const onMove = (event: PointerEvent) => {
      mx = (event.clientX / window.innerWidth) * 2 - 1;
      my = (event.clientY / window.innerHeight) * 2 - 1;
      if (!frame) frame = requestAnimationFrame(write);
    };
    window.addEventListener("pointermove", onMove, { passive: true });
    return () => {
      window.removeEventListener("pointermove", onMove);
      cancelAnimationFrame(frame);
    };
  }, []);
  return root;
}

export function HeroFloats() {
  const root = useParallax();
  return (
    <div ref={root} aria-hidden className="pointer-events-none absolute inset-x-0 top-0 h-[620px] select-none">
      {/* Wide screens: four objects in the margins, clear of the centred text. */}
      <Float className="top-[84px] left-[calc(50%-600px)] hidden xl:block" depth={14} rotate={-8} duration={7} delay={0}>
        <PixelArt {...PHONE} cell={5} depth={2} />
      </Float>
      <Float className="top-[372px] left-[calc(50%-520px)] hidden xl:block" depth={22} rotate={12} duration={6} delay={-2.2}>
        <PixelArt {...COIN} cell={5} depth={2} />
      </Float>
      <Float className="top-[96px] right-[calc(50%-610px)] hidden xl:block" depth={18} rotate={7} duration={6.5} delay={-1.1}>
        <PixelArt {...WHITE_BUBBLE} cell={5} depth={2} />
      </Float>
      <Float className="top-[388px] right-[calc(50%-540px)] hidden xl:block" depth={10} rotate={-10} duration={5.5} delay={-3.4}>
        <PixelArt {...TICK} cell={6} depth={2} />
      </Float>

      {/* Phones and tablets: two small objects in the top corners, above the headline. */}
      <Float className="top-[28px] left-[5%] xl:hidden" depth={8} rotate={-10} duration={6} delay={-1}>
        <PixelArt {...COIN} cell={3} depth={1} />
      </Float>
      <Float className="top-[176px] right-[5%] xl:hidden" depth={8} rotate={9} duration={5.5} delay={-2.6}>
        <PixelArt {...TICK} cell={3} depth={1} />
      </Float>
    </div>
  );
}

/** The closing call to action: a coin, a phone and a bubble drifting around Chit. */
export function CtaFloats() {
  const root = useParallax();
  return (
    <div ref={root} aria-hidden className="pointer-events-none absolute inset-0 select-none">
      <Float className="top-[18%] left-[calc(50%-330px)] hidden md:block" depth={16} rotate={-12} duration={6.5} delay={-1.5}>
        <PixelArt {...COIN} cell={4} depth={2} />
      </Float>
      <Float className="top-[52%] left-[calc(50%-420px)] hidden lg:block" depth={10} rotate={8} duration={7} delay={-3}>
        <PixelArt {...PHONE} cell={4} depth={2} />
      </Float>
      <Float className="top-[22%] right-[calc(50%-340px)] hidden md:block" depth={20} rotate={9} duration={6} delay={-0.6}>
        <PixelArt {...WHITE_BUBBLE} cell={4} depth={2} />
      </Float>
      <Float className="top-[56%] right-[calc(50%-430px)] hidden lg:block" depth={12} rotate={-8} duration={5.5} delay={-2.2}>
        <PixelArt {...TICK} cell={5} depth={2} />
      </Float>
    </div>
  );
}
