import type { PaymentStatus } from "@/lib/types";

// Each payment status has a shape as well as a colour, so it never relies on colour alone:
// paid = filled, partially paid = half filled, unpaid = empty ring, refunded = return arrow,
// not specified = dashed ring.
const STATUS: Record<PaymentStatus, { label: string; look: string }> = {
  paid: { label: "Paid", look: "border-accent/25 bg-accent-soft text-accent" },
  partially_paid: { label: "Partially paid", look: "border-partial/25 bg-partial-soft text-partial" },
  unpaid: { label: "Unpaid", look: "border-unpaid/25 bg-unpaid-soft text-unpaid" },
  refunded: { label: "Refunded", look: "border-refunded/25 bg-refunded-soft text-refunded" },
  not_specified: { label: "Not specified", look: "border-dashed border-line-strong bg-paper-deep text-muted" },
};

// The same shapes drawn on a 6 x 6 pixel grid ('#' is a filled cell).
const SHAPES: Record<PaymentStatus, readonly string[]> = {
  paid: [".####.", "######", "######", "######", "######", ".####."],
  partially_paid: [".####.", "###..#", "###..#", "###..#", "###..#", ".####."],
  unpaid: [".####.", "#....#", "#....#", "#....#", "#....#", ".####."],
  refunded: [".#....", "######", ".#...#", ".....#", ".....#", ".####."],
  not_specified: [".#..#.", "#....#", "......", "......", "#....#", ".#..#."],
};

function shapePath(grid: readonly string[]): string {
  return grid
    .flatMap((row, y) => [...row].map((cell, x) => (cell === "#" ? `M${x} ${y}h1v1h-1z` : "")))
    .join("");
}

export function StatusShape({ status, className = "size-3" }: { status: PaymentStatus; className?: string }) {
  return (
    <svg viewBox="0 0 6 6" className={className} aria-hidden="true" fill="currentColor" shapeRendering="crispEdges">
      <path d={shapePath(SHAPES[status])} />
    </svg>
  );
}

export function statusLabel(status: PaymentStatus): string {
  return STATUS[status].label;
}

export function StatusPill({ status, className = "" }: { status: PaymentStatus; className?: string }) {
  const { label, look } = STATUS[status];
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-md border px-2 py-0.5 text-meta font-semibold whitespace-nowrap ${look} ${className}`}>
      <StatusShape status={status} />
      {label}
    </span>
  );
}
