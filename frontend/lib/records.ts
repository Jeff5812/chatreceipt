import { send } from "./api";
import type { Calculation, PaymentStatus, TransactionDraft } from "./types";

// Mirrors backend/app/api/records.py.

export interface RecordSummary {
  id: string;
  reference: string;
  source: string;
  currency: string;
  seller: string;
  buyer: string;
  transaction_date: string | null;
  total: string;
  amount_paid: string | null;
  outstanding: string | null;
  payment_status: PaymentStatus;
  created_at: string;
}

export interface RecordList {
  records: RecordSummary[];
  next_before: string | null;
}

export interface RecordDetail extends RecordSummary {
  draft: TransactionDraft;
  calculation: Calculation;
}

const SESSION_KEY = "chatreceipt.records.session";

// The session lasts for this tab only. Storage can be unavailable (private windows, blocked site data), in
// which case the session simply isn't remembered across reloads.
export function readSession(): string | null {
  try {
    return window.sessionStorage.getItem(SESSION_KEY);
  } catch {
    return null;
  }
}

function writeSession(token: string | null): void {
  try {
    if (token) window.sessionStorage.setItem(SESSION_KEY, token);
    else window.sessionStorage.removeItem(SESSION_KEY);
  } catch {
    // Nothing to do: the token stays in memory for this page.
  }
}

export function clearSession(): void {
  writeSession(null);
}

function bearer(token: string): HeadersInit {
  return { Authorization: `Bearer ${token}` };
}

/** Swap the one-time code from the WhatsApp link for a session. */
export async function startSession(code: string): Promise<string> {
  const response = await send("/api/records/session", { method: "POST", json: { code } });
  const { token } = (await response.json()) as { token: string };
  writeSession(token);
  return token;
}

export async function endSession(token: string): Promise<void> {
  clearSession();
  await send("/api/records/session/end", { method: "POST", headers: bearer(token) });
}

export async function listRecords(token: string, before?: string): Promise<RecordList> {
  const query = before ? `?before=${encodeURIComponent(before)}` : "";
  return (await send(`/api/records${query}`, { headers: bearer(token) })).json();
}

export async function getRecord(token: string, id: string): Promise<RecordDetail> {
  return (await send(`/api/records/${encodeURIComponent(id)}`, { headers: bearer(token) })).json();
}
