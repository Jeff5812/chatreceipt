"use client";

import { useEffect } from "react";

const MAX_DEGREES = 4.5;

/**
 * Makes every `[data-tilt]` block lean a few degrees towards the pointer and lift (see globals.css).
 * One listener for the whole page. Mouse and pen only; nothing happens on touch or with reduced motion.
 */
export function TiltOnHover() {
  useEffect(() => {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    if (!window.matchMedia("(hover: hover) and (pointer: fine)").matches) return;

    let active: HTMLElement | null = null;
    let last: PointerEvent | null = null;
    let frame = 0;

    const reset = (el: HTMLElement) => {
      el.classList.remove("is-tilting");
      el.style.removeProperty("--rx");
      el.style.removeProperty("--ry");
    };
    const apply = () => {
      frame = 0;
      if (!active || !last) return;
      const box = active.getBoundingClientRect();
      const x = (last.clientX - box.left) / box.width - 0.5;
      const y = (last.clientY - box.top) / box.height - 0.5;
      active.style.setProperty("--ry", `${(x * MAX_DEGREES * 2).toFixed(2)}deg`);
      active.style.setProperty("--rx", `${(-y * MAX_DEGREES * 2).toFixed(2)}deg`);
    };
    const onMove = (event: PointerEvent) => {
      if (event.pointerType === "touch") return;
      const target = event.target instanceof Element ? event.target.closest<HTMLElement>("[data-tilt]") : null;
      if (target !== active) {
        if (active) reset(active);
        active = target;
        active?.classList.add("is-tilting");
      }
      last = event;
      if (active && !frame) frame = requestAnimationFrame(apply);
    };
    const onLeave = () => {
      if (active) reset(active);
      active = null;
    };

    document.addEventListener("pointermove", onMove, { passive: true });
    document.documentElement.addEventListener("pointerleave", onLeave);
    return () => {
      document.removeEventListener("pointermove", onMove);
      document.documentElement.removeEventListener("pointerleave", onLeave);
      cancelAnimationFrame(frame);
      if (active) reset(active);
    };
  }, []);
  return null;
}
