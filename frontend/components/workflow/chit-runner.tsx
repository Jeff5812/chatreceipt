"use client";

import { useEffect, useRef, useState } from "react";
import { Mascot } from "../mascot";
import { COIN, PixelArt } from "../pixel-art";

// Tuned so a jump peaks about 60px up and lasts about 0.7s.
const SPEED = 140; // how fast the world scrolls, px/s
const JUMP_SPEED = 330; // px/s upwards at take-off
const GRAVITY = 900; // px/s²
const CHIT = { w: 36, h: 39 };
const COIN_SIZE = 32;
const LOW = 6; // a coin on the ground: walked into
const HIGH = 56; // a coin in the air: needs a jump

interface Coin {
  x: number;
  y: number;
  alive: boolean;
}

/**
 * A retro wait screen: Chit runs along a pixel floor collecting coins while the screenshots are read.
 * It jumps on its own; press the screen (click, tap, Enter or Space) to jump yourself. Purely for fun,
 * it never blocks anything. With reduced motion the scene stands still.
 */
export function ChitRunner({ label }: { label: string }) {
  const stage = useRef<HTMLButtonElement>(null);
  const chit = useRef<HTMLSpanElement>(null);
  const coinEls = useRef<(HTMLSpanElement | null)[]>([]);
  const jumpRef = useRef<(manual: boolean) => void>(() => {});
  const [score, setScore] = useState(0);

  useEffect(() => {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const box = stage.current;
    if (!box) return;
    let width = box.clientWidth;
    const coins: Coin[] = [0, 1, 2].map((i) => ({ x: width + 60 + i * 190, y: i % 2 ? HIGH : LOW, alive: true }));
    const body = { y: 0, vy: 0, airborne: false, lastManual: -Infinity };
    let last = performance.now();
    let frame = 0;

    const jump = () => {
      if (body.airborne) return;
      body.vy = JUMP_SPEED;
      body.airborne = true;
    };
    jumpRef.current = (manual) => {
      if (manual) body.lastManual = performance.now();
      jump();
    };

    const tick = (now: number) => {
      const dt = Math.min(0.05, (now - last) / 1000);
      last = now;
      width = box.clientWidth;
      const chitX = width * 0.16;

      if (body.airborne) {
        body.vy -= GRAVITY * dt;
        body.y += body.vy * dt;
        if (body.y <= 0) {
          body.y = 0;
          body.vy = 0;
          body.airborne = false;
        }
      }

      coins.forEach((coin, i) => {
        coin.x -= SPEED * dt;
        if (coin.x < -COIN_SIZE - 10) {
          coin.x = width + 40 + Math.random() * 160;
          coin.y = Math.random() < 0.5 ? HIGH : LOW;
          coin.alive = true;
        }
        // Autopilot: hop for a coin in the air unless the visitor has been jumping themselves.
        const gap = coin.x - chitX;
        if (coin.alive && coin.y === HIGH && !body.airborne && now - body.lastManual > 3000 && gap > 42 && gap < 66) jump();
        const overlapX = coin.x < chitX + CHIT.w - 4 && coin.x + COIN_SIZE - 4 > chitX + 4;
        const overlapY = coin.y < body.y + CHIT.h - 5 && coin.y + COIN_SIZE - 4 > body.y;
        if (coin.alive && overlapX && overlapY) {
          coin.alive = false;
          setScore((n) => n + 1);
        }
        const el = coinEls.current[i];
        if (el) {
          el.style.transform = `translate(${coin.x.toFixed(1)}px, ${-coin.y}px)`;
          el.style.opacity = coin.alive ? "1" : "0";
        }
      });

      if (chit.current) chit.current.style.transform = `translateY(${(-body.y).toFixed(1)}px)`;
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, []);

  return (
    <button
      ref={stage}
      type="button"
      onClick={() => jumpRef.current(true)}
      aria-label="Make Chit jump while you wait"
      className="scanlines relative block h-[156px] w-full cursor-pointer overflow-hidden bg-noir-raised text-left outline-offset-[-3px]"
    >
      <span aria-hidden className="pointer-events-none absolute inset-x-0 bottom-0 h-[60px] bg-[linear-gradient(to_top,rgb(196_242_90/0.07),transparent)]" />
      <span aria-hidden className="pixel-label absolute top-3.5 left-4 text-accent">
        {label}
        <span className="anim-blink">_</span>
      </span>
      <span aria-hidden className="pixel-label absolute top-3.5 right-4 text-ink tabular-nums">
        Coins {String(score).padStart(2, "0")}
      </span>
      <span aria-hidden className="ground absolute inset-x-0 bottom-[22px] block h-[2px]" />
      <span ref={chit} aria-hidden className="absolute bottom-[24px] left-[16%] block">
        <Mascot cell={3} mood="reading" track={false} />
      </span>
      {[0, 1, 2].map((i) => (
        <span
          key={i}
          ref={(el) => {
            coinEls.current[i] = el;
          }}
          aria-hidden
          className="absolute bottom-[24px] left-0 block"
          style={{ transform: `translate(${300 + i * 190}px, ${i % 2 ? -HIGH : -LOW}px)` }}
        >
          <PixelArt {...COIN} cell={2} depth={1} />
        </span>
      ))}
      <span aria-hidden className="absolute right-4 bottom-1.5 font-mono text-[11px] text-muted">
        tap to jump
      </span>
    </button>
  );
}
