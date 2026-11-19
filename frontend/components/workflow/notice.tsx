import type { ReactNode } from "react";
import { AlertIcon, ErrorIcon, InfoIcon } from "../ui/icons";

type Tone = "info" | "warn" | "error";

const TONES: Record<Tone, { box: string; title: string; icon: ReactNode }> = {
  info: { box: "border-line bg-surface shadow-e1", title: "text-ink", icon: <InfoIcon className="size-[18px] text-accent" /> },
  warn: { box: "border-warn/30 bg-warn-soft", title: "text-warn", icon: <AlertIcon className="size-[18px] text-warn" /> },
  error: { box: "border-danger/30 bg-danger-soft", title: "text-danger", icon: <ErrorIcon className="size-[18px] text-danger" /> },
};

export function Notice({
  tone = "info",
  title,
  children,
  action,
}: {
  tone?: Tone;
  title?: string;
  children?: ReactNode;
  action?: ReactNode;
}) {
  const look = TONES[tone];
  return (
    <div role={tone === "error" ? "alert" : "status"} className={`flex gap-3 rounded-xl border px-4 py-3.5 ${look.box}`}>
      <span className="mt-0.5 shrink-0" aria-hidden>
        {look.icon}
      </span>
      <div className="min-w-0 flex-1">
        {title && <p className={`text-ui font-semibold ${look.title}`}>{title}</p>}
        {children && <div className={`text-ui leading-relaxed ${title ? "mt-1" : ""}`}>{children}</div>}
        {action && <div className="mt-3 flex flex-wrap gap-3">{action}</div>}
      </div>
    </div>
  );
}
