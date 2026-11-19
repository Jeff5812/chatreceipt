/**
 * Pixel art with depth. Each grid character maps to a material with a face colour and a darker side
 * colour; `depth` extrudes the whole shape down and to the right, one cell per layer, so the sprite reads
 * as a small block rather than a flat sticker. No hooks: renders on the server.
 */

export type Material = readonly [face: string, side: string];
export type Palette = Record<string, Material>;

interface Run {
  x: number;
  y: number;
  w: number;
  ch: string;
}

function runsOf(grid: readonly string[]): Run[] {
  const runs: Run[] = [];
  grid.forEach((row, y) => {
    let x = 0;
    while (x < row.length) {
      const ch = row[x];
      if (ch === "." || ch === " ") {
        x += 1;
        continue;
      }
      let w = 1;
      while (x + w < row.length && row[x + w] === ch) w += 1;
      runs.push({ x, y, w, ch });
      x += w;
    }
  });
  return runs;
}

/** The extruded layers and the face of a sprite, as SVG content (no wrapper), so others can draw on top. */
export function PixelBody({ grid, palette, depth = 0 }: { grid: readonly string[]; palette: Palette; depth?: number }) {
  const runs = runsOf(grid);
  const layers = Array.from({ length: depth }, (_, i) => depth - i);
  return (
    <>
      {layers.map((k) => (
        <g key={k} transform={`translate(${k} ${k})`}>
          {runs.map((r) => (
            <rect key={`${r.x}-${r.y}`} x={r.x} y={r.y} width={r.w} height={1} fill={palette[r.ch][1]} />
          ))}
        </g>
      ))}
      {runs.map((r) => (
        <rect key={`${r.x}-${r.y}`} x={r.x} y={r.y} width={r.w} height={1} fill={palette[r.ch][0]} />
      ))}
    </>
  );
}

export function PixelArt({
  grid,
  palette,
  cell = 4,
  depth = 0,
  className = "",
}: {
  grid: readonly string[];
  palette: Palette;
  cell?: number;
  depth?: number;
  className?: string;
}) {
  const runs = runsOf(grid);
  const width = Math.max(...grid.map((row) => row.length)) + depth;
  const height = grid.length + depth;
  const layers = Array.from({ length: depth }, (_, i) => depth - i);
  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      width={width * cell}
      height={height * cell}
      shapeRendering="crispEdges"
      aria-hidden
      className={className}
    >
      {layers.map((k) => (
        <g key={k} transform={`translate(${k} ${k})`}>
          {runs.map((r) => (
            <rect key={`${r.x}-${r.y}`} x={r.x} y={r.y} width={r.w} height={1} fill={palette[r.ch][1]} />
          ))}
        </g>
      ))}
      {runs.map((r) => (
        <rect key={`${r.x}-${r.y}`} x={r.x} y={r.y} width={r.w} height={1} fill={palette[r.ch][0]} />
      ))}
    </svg>
  );
}

/* ---------- Shared materials and sprites ---------- */

export const MATERIALS = {
  paper: ["#eef3ea", "#7f8a82"],
  line: ["#9aa59d", "#5d675f"],
  lime: ["#c4f25a", "#5f7d17"],
  ink: ["#0a0c0b", "#050606"],
  silver: ["#d6ddd7", "#6c766e"],
  frame: ["#3a4640", "#171d1a"],
  screen: ["#0f1311", "#0a0d0b"],
} as const satisfies Record<string, Material>;

/** A lime chat bubble with typing dots. */
export const BUBBLE = {
  grid: [
    ".LLLLLLLLLLLL.",
    "LLLLLLLLLLLLLL",
    "LLLLLLLLLLLLLL",
    "LLLKKLKKLKKLLL",
    "LLLKKLKKLKKLLL",
    "LLLLLLLLLLLLLL",
    ".LLLLLLLLLLLL.",
    "..LL..........",
    ".LL...........",
    "LL............",
  ],
  palette: { L: MATERIALS.lime, K: MATERIALS.ink },
};

/** A naira coin. */
export const COIN = {
  // A tall N with its diagonal showing between two short bars, so it reads as ₦ and not as #.
  grid: [
    ".....SSSSS.....",
    "...SSSSSSSSS...",
    "..SSSSSSSSSSS..",
    ".SSSKSSSSSKSSS.",
    ".SSSKKSSSSKSSS.",
    "SSSSKKSSSSKSSSS",
    "SSSKKKKKKKKKSSS",
    "SSSSKSSKSSKSSSS",
    "SSSKKKKKKKKKSSS",
    "SSSSKSSSKKKSSSS",
    ".SSSKSSSSKKSSS.",
    ".SSSKSSSSSKSSS.",
    "..SSSSSSSSSSS..",
    "...SSSSSSSSS...",
    ".....SSSSS.....",
  ],
  palette: { S: MATERIALS.silver, K: MATERIALS.ink },
};

/** A phone showing a short chat: one message in, one out. */
export const PHONE = {
  grid: [
    ".FFFFFFFFFF.",
    "FFFFFFFFFFFF",
    "FDDDDDDDDDDF",
    "FDWWWWWDDDDF",
    "FDWWWWWWDDDF",
    "FDDDDDDDDDDF",
    "FDDDDLLLLLDF",
    "FDDDDLLLLLDF",
    "FDDDDDDDDDDF",
    "FDWWWWDDDDDF",
    "FDDDDDDDDDDF",
    "FDDDDDDDDDDF",
    "FFFFFFFFFFFF",
    "FFFFFKKFFFFF",
    ".FFFFFFFFFF.",
  ],
  palette: { F: MATERIALS.frame, D: MATERIALS.screen, W: MATERIALS.paper, L: MATERIALS.lime, K: MATERIALS.ink },
};

/** A magnifier over two printed lines, one of them the lime figure it found. */
export const MAGNIFIER = {
  grid: [
    "...SSSSS......",
    "..SDDDDDS.....",
    ".SDDDDDDDS....",
    "SDDDDDDDDDS...",
    "SDLLLLLDDDS...",
    "SDDDDDDDDDS...",
    "SDWWWWDDDDS...",
    "SDDDDDDDDDS...",
    ".SDDDDDDDS....",
    "..SDDDDDS.....",
    "...SSSSSLL....",
    ".........LL...",
    "..........LL..",
    "...........LL.",
  ],
  palette: { S: MATERIALS.silver, D: MATERIALS.screen, L: MATERIALS.lime, W: MATERIALS.paper },
};

/** A lime tile with a tick: something agreed. */
export const TICK = {
  grid: [
    "LLLLLLLLL",
    "LLLLLLLLL",
    "LLLLLLLKL",
    "LLLLLLKKL",
    "LKLLLKKLL",
    "LKKLKKLLL",
    "LLKKKLLLL",
    "LLLKLLLLL",
    "LLLLLLLLL",
  ],
  palette: { L: MATERIALS.lime, K: MATERIALS.ink },
};
