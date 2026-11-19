import type { ButtonHTMLAttributes, CSSProperties } from "react";

export type ButtonVariant = "primary" | "secondary" | "quiet" | "light";
export type ButtonSize = "sm" | "md" | "lg";

const BASE =
  "group/btn relative inline-flex items-center justify-center gap-2 font-medium whitespace-nowrap select-none transition-[transform,background-color,border-color,color] duration-150 ease-soft disabled:pointer-events-none";

// Every button is a pixel key (.btn-px in globals.css): a notched, bevelled face on a 5px base that lifts on
// hover and sinks when pressed. Lime is the one key that moves you forward; graphite is everything else.
const VARIANTS: Record<ButtonVariant, string> = {
  primary: "btn-px key-lime font-semibold",
  secondary: "btn-px key-dark",
  quiet: "text-ink underline-offset-4 hover:underline disabled:opacity-40",
  light: "btn-px key-dark",
};

// On touch screens (pointer: coarse) the small and quiet buttons grow to 40px so they are easier to tap.
const SIZES: Record<ButtonSize, string> = {
  sm: "h-9 px-3.5 text-sm pointer-coarse:h-10",
  md: "h-11 px-5 text-ui",
  lg: "h-[52px] px-6 text-base",
};

export function buttonClass(variant: ButtonVariant = "primary", size: ButtonSize = "md", extra = ""): string {
  const sizing = variant === "quiet" ? "h-9 px-1 text-ui pointer-coarse:h-10" : SIZES[size];
  return [BASE, VARIANTS[variant], sizing, extra].filter(Boolean).join(" ");
}

/** The arrow inside a forward button: a pixel arrow that steps right when the button is hovered. */
export function ArrowChip({ style }: { style?: CSSProperties }) {
  return (
    <svg
      aria-hidden
      viewBox="0 0 8 8"
      shapeRendering="crispEdges"
      className="-mr-1 ml-0.5 size-3.5 shrink-0 transition-transform duration-150 ease-[steps(2)] group-hover/btn:translate-x-0.5"
      style={style}
      fill="currentColor"
    >
      <path d="M0 3h5V1h1v1h1v1h1v2H7v1H6v1H5V5H0z" />
    </svg>
  );
}

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
}

export function Button({ variant = "primary", size = "md", className = "", type = "button", ...props }: ButtonProps) {
  return <button type={type} className={buttonClass(variant, size, className)} {...props} />;
}
