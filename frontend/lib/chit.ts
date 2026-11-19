/**
 * A tiny signal for Chit's reactions: any part of the app can make the Chits that listen (the one in the
 * header) smile, worry or read for a moment, without passing props through the tree.
 */
export type ChitMood = "idle" | "happy" | "reading" | "worried";

export const CHIT_EVENT = "chatreceipt:chit";

export interface ChitSignal {
  mood: ChitMood;
  /** How long the mood lasts, in milliseconds. */
  ms: number;
}

export function cheer(mood: ChitMood = "happy", ms = 1800): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new CustomEvent<ChitSignal>(CHIT_EVENT, { detail: { mood, ms } }));
}
