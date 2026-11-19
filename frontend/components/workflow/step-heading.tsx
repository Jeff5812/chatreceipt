"use client";

import { useEffect, useRef, type ReactNode } from "react";

/** The page heading for a workflow step. Takes focus when the step changes, for screen readers. */
export function StepHeading({ children, focus }: { children: ReactNode; focus: boolean }) {
  const ref = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    if (focus) ref.current?.focus();
  }, [focus]);
  return (
    <h1
      ref={ref}
      tabIndex={-1}
      className="font-display text-[36px] leading-[1.04] font-bold tracking-[-0.035em] text-balance sm:text-[48px]"
    >
      {children}
    </h1>
  );
}
