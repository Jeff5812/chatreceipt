"use client";

import { useState, type ReactNode } from "react";
import { PixelCheck } from "./pixel-check";

/**
 * A checkbox drawn as a pixel box. Ticking it draws a pixel checkmark that pops. The real input stays in the
 * page (visually hidden), so keyboards, screen readers and form behaviour are unchanged.
 */
export function PixelCheckbox({
  checked,
  onChange,
  children,
}: {
  checked: boolean;
  onChange: (checked: boolean) => void;
  children: ReactNode;
}) {
  const [plays, setPlays] = useState(0);
  return (
    <label className="group flex cursor-pointer gap-3 text-ui leading-snug">
      <input
        type="checkbox"
        className="peer sr-only"
        checked={checked}
        onChange={(event) => {
          if (event.target.checked) setPlays((n) => n + 1);
          onChange(event.target.checked);
        }}
      />
      <span
        aria-hidden
        className="relative mt-[-1px] flex size-6 shrink-0 items-center justify-center border-2 border-line-strong bg-well shadow-well transition-colors group-hover:border-mist peer-checked:border-accent peer-checked:bg-accent-soft peer-focus-visible:outline-2 peer-focus-visible:outline-offset-2 peer-focus-visible:outline-accent peer-focus-visible:outline-solid"
      >
        {checked && <PixelCheck cell={2} play={plays} className="text-accent" />}
      </span>
      <span>{children}</span>
    </label>
  );
}
