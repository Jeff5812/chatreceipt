import type { ReactNode } from "react";
import { ChitTile } from "../chit";

/* The example record, drawn as the same receipt the app exports (backend/app/documents/templates/record.html). */

const CUT = "my-4 h-[2px] bg-[linear-gradient(90deg,#cfd5cc_50%,transparent_0)] bg-[length:8px_2px] bg-repeat-x";
const DOUBLE = "h-[6px] bg-[linear-gradient(90deg,#161816_50%,transparent_0),linear-gradient(90deg,#161816_50%,transparent_0)] bg-[length:8px_2px,8px_2px] bg-[position:0_0,0_4px] bg-repeat-x";
const LEADER = "min-w-3 flex-1 -translate-y-1 border-b-2 border-dotted border-[#d7dcd4]";
const KEY = "shrink-0 font-mono text-[10.5px] tracking-[0.08em] uppercase text-muted";

function Row({ k, children, strong = false }: { k: string; children: ReactNode; strong?: boolean }) {
  return (
    <div className="flex items-baseline gap-2 py-[3px]">
      <span className={`${KEY} ${strong ? "font-semibold text-ink" : ""}`}>{k}</span>
      <span aria-hidden className={LEADER} />
      <span className={`shrink-0 text-right ${strong ? "font-semibold" : "font-medium"}`}>{children}</span>
    </div>
  );
}

function Money({ children }: { children: ReactNode }) {
  return <span className="font-mono font-normal tabular-nums">{children}</span>;
}

/**
 * The example receipt: one leather bag at the agreed ₦23,000, ₦2,000 delivery, ₦25,000 in total and
 * unpaid, because the buyer only promised to transfer.
 */
export function PaperRecord({ className = "" }: { className?: string }) {
  return (
    <article
      aria-label="Example transaction record"
      className={`theme-paper receipt-edges relative bg-sheet px-6 pt-7 pb-6 text-[13px] leading-[1.45] ${className}`}
    >
      <header className="text-center">
        <ChitTile className="mx-auto size-11" />
        <p className="mt-2.5 font-pixel text-[16px] leading-none">ChatReceipt</p>
        <h3 className="mt-2.5 font-pixel text-[16px] leading-none tracking-[0.08em] text-muted uppercase">Transaction record</h3>
        <p className="mt-2 font-mono text-[11px] text-muted">Prepared 8 November 2026</p>
      </header>

      <div aria-hidden className={CUT} />
      <Row k="Seller">Tola&apos;s Bags</Row>
      <Row k="Buyer">
        <span className="font-normal text-muted">Not specified</span>
      </Row>
      <Row k="Date">8 November 2026</Row>
      <Row k="Currency">Naira (NGN)</Row>

      <div aria-hidden className={CUT} />
      <div className="py-1">
        <p className="font-semibold">Leather bag</p>
        <div className="mt-0.5 flex items-baseline gap-2">
          <span className="shrink-0 font-mono text-[12px] text-muted">1 × ₦23,000.00</span>
          <span aria-hidden className={LEADER} />
          <Money>₦23,000.00</Money>
        </div>
      </div>

      <div aria-hidden className={CUT} />
      <Row k="Subtotal">
        <Money>₦23,000.00</Money>
      </Row>
      <Row k="Delivery">
        <Money>₦2,000.00</Money>
      </Row>
      <div aria-hidden className={`${DOUBLE} mt-3`} />
      <div className="flex items-baseline justify-between gap-3 py-2">
        <span className="font-pixel text-[16px] tracking-[0.08em] uppercase">Total</span>
        <span className="font-display text-[28px] leading-none font-bold tracking-[-0.035em] tabular-nums">₦25,000.00</span>
      </div>
      <div aria-hidden className={`${DOUBLE} mb-2`} />
      <Row k="Amount paid">
        <Money>₦0.00</Money>
      </Row>
      <Row k="Outstanding" strong>
        <Money>₦25,000.00</Money>
      </Row>

      <div className="mt-6 mb-1 text-center">
        <span className="inline-flex -rotate-[4deg] items-center gap-2 border-[3px] border-unpaid px-3.5 pt-[7px] pb-1.5 font-pixel text-[22px] leading-none tracking-[0.06em] text-unpaid uppercase outline-1 outline-offset-[3px] outline-unpaid outline-solid">
          <svg viewBox="0 0 6 6" aria-hidden className="size-3" fill="currentColor" shapeRendering="crispEdges">
            <path d="M1 0h4v1h-4zM0 1h1v4h-1zM5 1h1v4h-1zM1 5h4v1h-4z" />
          </svg>
          Unpaid
        </span>
        <p className="mx-auto mt-3 max-w-[260px] text-[11.5px] text-muted">Recorded from the conversation. ChatReceipt doesn&apos;t verify payments.</p>
      </div>

      <div aria-hidden className={CUT} />
      <dl className="space-y-2.5">
        <div>
          <dt className={KEY}>Delivery terms</dt>
          <dd className="mt-0.5">Delivery to Yaba.</dd>
        </div>
        <div>
          <dt className={KEY}>Payment terms</dt>
          <dd className="mt-0.5">Buyer says they will transfer tomorrow morning.</dd>
        </div>
      </dl>

      <div aria-hidden className={CUT} />
      <p className="text-center text-[10.5px] leading-normal text-muted">
        Prepared with ChatReceipt from 2 conversation screenshots. The details were checked by the person who prepared
        this record.
      </p>
    </article>
  );
}
