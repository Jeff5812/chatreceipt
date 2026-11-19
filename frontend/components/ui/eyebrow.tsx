import type { ReactNode } from "react";

/** The small label above a section heading: a short rule and crisp uppercase sans. */
export function Eyebrow({ children, tone = "accent" }: { children: ReactNode; tone?: "accent" | "mist" }) {
  const look = tone === "accent" ? "text-accent" : "text-mist";
  const rule = tone === "accent" ? "bg-accent/45" : "bg-mist/45";
  return (
    <p className={`label-caps flex items-center gap-2.5 ${look}`}>
      <span aria-hidden className={`h-px w-7 ${rule}`} />
      {children}
    </p>
  );
}
