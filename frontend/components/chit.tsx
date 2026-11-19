/**
 * Chit, the ChatReceipt mascot: a little receipt slip on a 12 x 13 pixel grid, with a torn edge at the
 * bottom, two printed lines and the lime total. Only the eyes change; everything else is fixed art.
 * This file has no hooks, so the static marks render on the server; mascot.tsx adds the interaction.
 */

export const CHIT_W = 12;
export const CHIT_H = 13;

export const CHIT_COLORS = {
  paper: "#eef3ea",
  shade: "#c3ccc5",
  line: "#9aa59d",
  lime: "#c4f25a",
  eye: "#0a0c0b",
} as const;

// W paper, S shade, G printed line, L lime total. Eyes are drawn separately.
const ART = [
  ".WWWWWWWWWS.",
  "WWWWWWWWWWWS",
  "WWWWWWWWWWWS",
  "WWWWWWWWWWWS",
  "WWWWWWWWWWWS",
  "WWWWWWWWWWWS",
  "WWWWWWWWWWWS",
  "WWGGGGGGGWWS",
  "WWWWWWWWWWWS",
  "WWGGGWWLLWWS",
  "WWWWWWWWWWWS",
  "SSSSSSSSSSSS",
  ".S..S..S..S.",
];

const FILL: Record<string, string> = {
  W: CHIT_COLORS.paper,
  S: CHIT_COLORS.shade,
  G: CHIT_COLORS.line,
  L: CHIT_COLORS.lime,
};

interface Run {
  x: number;
  y: number;
  w: number;
  fill: string;
}

// Merge each row into runs of one colour, so the art is a few dozen rects rather than 150.
const RUNS: Run[] = ART.flatMap((row, y) => {
  const runs: Run[] = [];
  let x = 0;
  while (x < row.length) {
    const ch = row[x];
    if (ch === ".") {
      x += 1;
      continue;
    }
    let w = 1;
    while (x + w < row.length && row[x + w] === ch) w += 1;
    runs.push({ x, y, w, fill: FILL[ch] });
    x += w;
  }
  return runs;
});

export type Mood = "idle" | "happy" | "reading" | "worried";

export interface EyeState {
  /** Where the eyes look: -1, 0 or 1 on each axis. */
  look: [number, number];
  blink: boolean;
  mood: Mood;
}

const EYE_X = [2, 7];
const EYE_Y = 3;

/** The eye rects for a state, in grid units. */
export function eyeRects({ look, blink, mood }: EyeState): { x: number; y: number; w: number; h: number }[] {
  const [dx, dy] = look;
  if (mood === "happy") {
    // Closed, smiling eyes: a small arch over each eye.
    return EYE_X.flatMap((x) => [
      { x, y: EYE_Y + 1, w: 2, h: 1 },
      { x: x - 1, y: EYE_Y + 2, w: 1, h: 1 },
      { x: x + 2, y: EYE_Y + 2, w: 1, h: 1 },
    ]);
  }
  if (blink) return EYE_X.map((x) => ({ x: x + dx, y: EYE_Y + 2 + dy, w: 2, h: 1 }));
  if (mood === "reading") return EYE_X.map((x) => ({ x: x + dx, y: EYE_Y + 1, w: 2, h: 2 }));
  if (mood === "worried") return EYE_X.map((x) => ({ x: x + dx, y: EYE_Y + 2, w: 2, h: 2 }));
  return EYE_X.map((x) => ({ x: x + dx, y: EYE_Y + dy, w: 2, h: 3 }));
}

/** The fixed body of the slip. */
export function ChitBody() {
  return (
    <>
      {RUNS.map((r) => (
        <rect key={`${r.x}-${r.y}`} x={r.x} y={r.y} width={r.w} height={1} fill={r.fill} />
      ))}
    </>
  );
}

export function ChitEyes(state: EyeState) {
  return (
    <>
      {eyeRects(state).map((e, i) => (
        <rect key={i} x={e.x} y={e.y} width={e.w} height={e.h} fill={CHIT_COLORS.eye} />
      ))}
    </>
  );
}

const IDLE: EyeState = { look: [0, 0], blink: false, mood: "idle" };

/** A still Chit, for places that don't move: the record, the PDF, icons. */
export function ChitStatic({ cell = 2, mood = "idle", className = "" }: { cell?: number; mood?: Mood; className?: string }) {
  return (
    <svg
      viewBox={`0 0 ${CHIT_W} ${CHIT_H}`}
      width={CHIT_W * cell}
      height={CHIT_H * cell}
      shapeRendering="crispEdges"
      aria-hidden
      className={className}
    >
      <ChitBody />
      <ChitEyes {...IDLE} mood={mood} />
    </svg>
  );
}

/** Chit on a dark tile: the app icon, and the mark on white paper where a light slip would vanish. */
export function ChitTile({ className = "size-8" }: { className?: string }) {
  return (
    <svg viewBox="0 0 18 18" shapeRendering="crispEdges" aria-hidden className={className}>
      <rect width="18" height="18" rx="4" fill="#0a0c0b" shapeRendering="geometricPrecision" />
      <g transform="translate(3 2.5)">
        <ChitBody />
        <ChitEyes {...IDLE} />
      </g>
    </svg>
  );
}
