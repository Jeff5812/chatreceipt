import type { CSSProperties } from "react";

// A 9 x 7 pixel tick. Cells are numbered left to right, so the tick draws itself along the stroke.
const GRID = ["........#", ".......##", "......##.", "#....##..", "##..##...", ".####....", "..##....."];

const CELLS = GRID.flatMap((row, y) => [...row].flatMap((c, x) => (c === "#" ? [{ x, y }] : []))).sort(
  (a, b) => a.x - b.x || b.y - a.y,
);

// Four sparks fly out from the corners once the tick is drawn.
const SPARKS = [
  { left: "-12%", top: "10%", sx: "-10px", sy: "-8px" },
  { left: "104%", top: "-12%", sx: "10px", sy: "-10px" },
  { left: "-6%", top: "88%", sx: "-10px", sy: "9px" },
  { left: "100%", top: "70%", sx: "11px", sy: "8px" },
];

/**
 * A pixel checkmark that draws itself cell by cell, pops and throws four sparks. Change `play` to replay it.
 * With reduced motion it simply appears. Decorative: pair it with text that says what was checked.
 */
export function PixelCheck({ cell = 4, play = 0, burst = true, className = "" }: { cell?: number; play?: number; burst?: boolean; className?: string }) {
  const done = CELLS.length * 45 + 40;
  return (
    <span
      key={play}
      aria-hidden
      className={`pxc relative inline-block shrink-0 ${className}`}
      style={{ width: 9 * cell, height: 7 * cell, "--done": `${done}ms` } as CSSProperties}
    >
      <svg viewBox="0 0 9 7" width={9 * cell} height={7 * cell} shapeRendering="crispEdges" className="pxc-pop block" fill="currentColor">
        {CELLS.map((c, i) => (
          <rect key={`${c.x}-${c.y}`} x={c.x} y={c.y} width={1} height={1} className="pxc-cell" style={{ "--i": i } as CSSProperties} />
        ))}
      </svg>
      {burst &&
        SPARKS.map((s) => (
          <i
            key={s.left + s.top}
            className="pxc-spark absolute block bg-current"
            style={{ left: s.left, top: s.top, width: Math.max(2, cell - 1), height: Math.max(2, cell - 1), "--sx": s.sx, "--sy": s.sy } as CSSProperties}
          />
        ))}
    </span>
  );
}
