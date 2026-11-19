import type { CSSProperties } from "react";

// Eight cells round a 4 x 4 ring, lit one after another like an old loading icon.
const RING = [
  [1, 0],
  [2, 0],
  [3, 1],
  [3, 2],
  [2, 3],
  [1, 3],
  [0, 2],
  [0, 1],
];

export function PixelSpinner({ className = "size-4" }: { className?: string }) {
  return (
    <span aria-hidden className={`px-spinner relative inline-block shrink-0 ${className}`}>
      {RING.map(([x, y], n) => (
        <i key={n} style={{ left: `${x * 25}%`, top: `${y * 25}%`, "--n": n } as CSSProperties} />
      ))}
    </span>
  );
}
