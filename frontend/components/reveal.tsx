"use client";

import { useEffect } from "react";

/**
 * Fades elements marked `data-reveal` up into place the first time they scroll into view.
 * Content is visible by default: the effect only arms once this script runs, and never with reduced motion.
 * An optional `data-reveal-delay` (ms) staggers siblings.
 */
export function RevealOnScroll() {
  useEffect(() => {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const root = document.documentElement;
    const targets = Array.from(document.querySelectorAll<HTMLElement>("[data-reveal]"));
    const fold = window.innerHeight;
    for (const el of targets) {
      // Anything already on screen stays put, so nothing above the fold blinks.
      if (el.getBoundingClientRect().top < fold * 0.92) el.classList.add("is-revealed");
      const wait = el.dataset.revealDelay;
      if (wait) el.style.setProperty("--reveal-delay", `${wait}ms`);
    }
    root.classList.add("reveal-armed");
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            entry.target.classList.add("is-revealed");
            observer.unobserve(entry.target);
          }
        }
      },
      { rootMargin: "0px 0px -8% 0px", threshold: 0.12 },
    );
    for (const el of targets) if (!el.classList.contains("is-revealed")) observer.observe(el);
    return () => {
      observer.disconnect();
      root.classList.remove("reveal-armed");
    };
  }, []);
  return null;
}
