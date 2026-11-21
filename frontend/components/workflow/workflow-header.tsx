"use client";

import Link from "next/link";
import { Logo } from "../brand";
import { Button } from "../ui/button";
import { CheckIcon } from "../ui/icons";

const STEPS = ["Upload", "Review", "Record"] as const;

export function WorkflowHeader({
  step,
  hasWork,
  onStartOver,
}: {
  step: 1 | 2 | 3;
  hasWork: boolean;
  onStartOver: () => void;
}) {
  return (
    <header className="sticky top-0 z-40 border-b border-line/80 bg-paper">
      <div className="mx-auto flex h-16 max-w-6xl items-center justify-between gap-4 px-4 sm:px-6">
        <Link
          href="/"
          aria-label="ChatReceipt home"
          className="rounded-none"
          onClick={(event) => {
            if (hasWork && !window.confirm("Leave this page? Your screenshots and details will be lost.")) {
              event.preventDefault();
            }
          }}
        >
          <Logo />
        </Link>
        <ol aria-label="Progress" className="hidden items-center gap-1 border border-line bg-well p-1 shadow-well md:flex">
          {STEPS.map((label, index) => {
            const number = index + 1;
            const current = number === step;
            const done = number < step;
            return (
              <li key={label}>
                <span
                  aria-current={current ? "step" : undefined}
                  className={`flex items-center gap-2 px-3 py-1.5 text-sm transition-[background-color,color] duration-200 ${
                    current ? "bg-surface font-medium text-ink shadow-[inset_0_1px_0_rgb(255_255_255/0.06)]" : done ? "text-accent" : "text-muted"
                  }`}
                >
                  <span
                    className={`flex size-5 items-center justify-center font-pixel text-[16px] leading-none ${
                      current ? "bg-accent text-accent-ink" : done ? "bg-accent-soft text-accent" : "border border-line-strong"
                    }`}
                  >
                    {done ? <CheckIcon className="size-3" /> : number}
                  </span>
                  {label}
                  {done && <span className="sr-only">(done)</span>}
                </span>
              </li>
            );
          })}
        </ol>
        <div className="flex items-center gap-4">
          <span className="text-meta whitespace-nowrap text-muted md:hidden">
            Step {step} of 3<span className="hidden sm:inline"> · {STEPS[step - 1]}</span>
          </span>
          {hasWork && (
            <Button variant="quiet" className="text-sm text-muted hover:text-ink" onClick={onStartOver}>
              Start over
            </Button>
          )}
        </div>
      </div>
    </header>
  );
}
