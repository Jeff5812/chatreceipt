import type { CurrencyInfo } from "./types";

/**
 * Format a decimal string from the backend for display, e.g. "19000.00" → "₦19,000.00".
 * Works on the string directly (no floating point) and matches the backend's format_money.
 */
export function formatMoney(amount: string | null, currency: CurrencyInfo | undefined): string {
  if (amount === null || currency === undefined) return "—";
  const negative = amount.startsWith("-");
  const [whole, fraction = ""] = amount.replace(/^-/, "").split(".");
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const decimals =
    currency.minor_units > 0 ? `.${fraction.padEnd(currency.minor_units, "0").slice(0, currency.minor_units)}` : "";
  const separator = /[A-Za-z]$/.test(currency.symbol) ? " " : "";
  return `${negative ? "-" : ""}${currency.symbol}${separator}${grouped}${decimals}`;
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
