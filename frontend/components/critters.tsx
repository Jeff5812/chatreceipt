"use client";

import { useEffect, useState, type CSSProperties } from "react";
import { Mascot } from "./mascot";
import { MATERIALS, PixelBody, type Palette } from "./pixel-art";

/*
  Chit's friends, who turn up when a record is ready: Kobo, a coin (named after the naira's smaller unit),
  and a lime chat bubble. Each is an extruded pixel body with blinking eyes that can smile.
*/

interface Critter {
  grid: readonly string[];
  palette: Palette;
  eyes: [number, number];
  eyeY: number;
}

const KOBO: Critter = {
  grid: [
    "...RRRRR...",
    ".RRSSSSSRR.",
    ".RSSSSSSSR.",
    "RSSSSSSSSSR",
    "RSSSSSSSSSR",
    "RSSSSSSSSSR",
    "RSSSSSSSSSR",
    "RSSSSSSSSSR",
    ".RSSSSSSSR.",
    ".RRSSSSSRR.",
    "...RRRRR...",
  ],
  palette: { R: ["#aab3ac", "#565f58"], S: MATERIALS.silver },
  eyes: [3, 6],
  eyeY: 4,
};

const BUBBLE: Critter = {
  grid: [
    ".LLLLLLLLLLL.",
    "LLLLLLLLLLLLL",
    "LLLLLLLLLLLLL",
    "LLLLLLLLLLLLL",
    "LLLLLLLLLLLLL",
    "LLLLLLLLLLLLL",
    ".LLLLLLLLLLL.",
    "..LL.........",
    ".LL..........",
  ],
  palette: { L: MATERIALS.lime },
  eyes: [4, 7],
  eyeY: 2,
};

function CritterSprite({ critter, cell, happy }: { critter: Critter; cell: number; happy: boolean }) {
  const [blink, setBlink] = useState(false);
  useEffect(() => {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    let wait = 0;
    let open = 0;
    const schedule = () => {
      wait = window.setTimeout(
        () => {
          setBlink(true);
          open = window.setTimeout(() => setBlink(false), 130);
          schedule();
        },
        1800 + Math.random() * 3200,
      );
    };
    schedule();
    return () => {
      window.clearTimeout(wait);
      window.clearTimeout(open);
    };
  }, []);

  const depth = 1;
  const width = Math.max(...critter.grid.map((row) => row.length)) + depth;
  const height = critter.grid.length + depth;
  const y = critter.eyeY;
  const eyes = critter.eyes.flatMap((x) =>
    happy
      ? [
          { x, y, w: 2, h: 1 },
          { x: x - 1, y: y + 1, w: 1, h: 1 },
          { x: x + 2, y: y + 1, w: 1, h: 1 },
        ]
      : blink
        ? [{ x, y: y + 1, w: 2, h: 1 }]
        : [{ x, y, w: 2, h: 2 }],
  );
  return (
    <svg viewBox={`0 0 ${width} ${height}`} width={width * cell} height={height * cell} shapeRendering="crispEdges" aria-hidden>
      <PixelBody grid={critter.grid} palette={critter.palette} depth={depth} />
      {eyes.map((e, i) => (
        <rect key={i} x={e.x} y={e.y} width={e.w} height={e.h} fill="#0a0c0b" />
      ))}
    </svg>
  );
}

// A short burst of pixel confetti: where each square flies, its colour and when it leaves.
const CONFETTI = [
  ["-110px", "-70px", "#c4f25a", 0],
  ["-70px", "-110px", "#eef3ea", 40],
  ["-20px", "-120px", "#f6e3a9", 80],
  ["30px", "-115px", "#c4f25a", 20],
  ["80px", "-95px", "#eef3ea", 60],
  ["120px", "-60px", "#c4f25a", 100],
  ["-130px", "-25px", "#f6e3a9", 120],
  ["135px", "-20px", "#eef3ea", 30],
  ["-55px", "-80px", "#d6ddd7", 140],
  ["60px", "-70px", "#f6e3a9", 90],
  ["-95px", "-50px", "#c4f25a", 160],
  ["100px", "-40px", "#d6ddd7", 50],
] as const;

/**
 * Chit and friends celebrate a finished record: each hops in turn, smiling, under a burst of pixel
 * confetti, then settles to blinking and watching the pointer. Plays once on mount; remount to replay.
 * With reduced motion they simply stand there smiling.
 */
export function Celebration() {
  const [happy, setHappy] = useState(true);
  useEffect(() => {
    const timer = window.setTimeout(() => setHappy(false), 2600);
    return () => window.clearTimeout(timer);
  }, []);

  return (
    <div aria-hidden className="relative flex h-[118px] items-end justify-center">
      <div className="confetti pointer-events-none absolute inset-0">
        {CONFETTI.map(([x, y, color, delay], i) => (
          <i key={i} style={{ background: color, "--x": x, "--y": y, "--d": `${delay + 380}ms` } as CSSProperties} />
        ))}
      </div>
      <div className="relative flex items-end gap-4">
        <span className="anim-hop block" style={{ "--d": "160ms" } as CSSProperties}>
          <CritterSprite critter={KOBO} cell={3} happy={happy} />
        </span>
        <span className="anim-hop block" style={{ "--d": "0ms" } as CSSProperties}>
          <Mascot cell={5} mood={happy ? "happy" : "idle"} />
        </span>
        <span className="anim-hop mb-1 block" style={{ "--d": "300ms" } as CSSProperties}>
          <CritterSprite critter={BUBBLE} cell={3} happy={happy} />
        </span>
      </div>
      <span className="absolute bottom-[-10px] h-2 w-48 rounded-[50%] bg-black/80 blur-[5px]" />
    </div>
  );
}
