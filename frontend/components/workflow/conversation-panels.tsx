"use client";

import type { ChatSummary, ExtractionResponse, OtherDeal, Person, TransactionDraft } from "@/lib/types";
import { Button } from "../ui/button";
import { ChatIcon } from "../ui/icons";

type Side = "seller" | "buyer";

const ROLE_WORDS: Record<Person["role"], string> = {
  seller: "seller",
  buyer: "buyer",
  payer: "paid",
  payee: "received the money",
  agent: "arranged or delivered it",
  other: "took part",
};

function sameName(first: string, second: string): boolean {
  const key = (name: string) => name.toLocaleLowerCase().replace(/[^\p{L}\p{N}]/gu, "");
  return key(first) !== "" && key(first) === key(second);
}

function shortDate(iso: string): string {
  const [year, month, day] = iso.split("-").map(Number);
  if (!year || !month || !day) return iso;
  return new Date(Date.UTC(year, month - 1, day)).toLocaleDateString("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  });
}

/** The export's own name for the person who exported it, when the file showed who that is. */
export function ownerLabel(chat: ChatSummary | null | undefined): string {
  const owner = chat?.participants.find((person) => person.side === "owner");
  return owner && !owner.phone ? owner.name : "";
}

/**
 * Who is who, above the seller and buyer fields: which side the person is on, a one-tap fill for
 * their own name from a chat export, anyone else who paid or helped, and a swap for names read the
 * wrong way round.
 */
export function WhoIsWho({
  extraction,
  draft,
  ownerSide,
  onFill,
  onSwap,
}: {
  extraction: ExtractionResponse;
  draft: TransactionDraft;
  ownerSide: Side | "";
  onFill: (side: Side, name: string) => void;
  onSwap: () => void;
}) {
  const label = ownerLabel(extraction.chat);
  const fromExport = extraction.source === "chat_export";
  const others = (extraction.parties?.people ?? []).filter(
    (person) =>
      person.name &&
      !person.is_owner &&
      !["seller", "buyer"].includes(person.role) &&
      !sameName(person.name, draft.seller) &&
      !sameName(person.name, draft.buyer),
  );
  const canSwap = draft.seller.trim() !== "" || draft.buyer.trim() !== "";

  let line: string;
  if (ownerSide && fromExport && label) line = `The chat was exported from your phone, where you're “${label}”. You're the ${ownerSide}.`;
  else if (ownerSide) line = `You're the ${ownerSide} in this chat.`;
  else line = "It isn't clear from the chat which side you're on, so check the seller and buyer.";

  return (
    <div className="mb-6 space-y-3 border border-line bg-well px-4 py-3.5">
      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
        <p className="min-w-0 text-ui leading-relaxed">{line}</p>
        {canSwap && (
          <Button size="sm" variant="secondary" onClick={onSwap}>
            Swap seller and buyer
          </Button>
        )}
      </div>
      {ownerSide && label && draft[ownerSide].trim() === "" && (
        <Button size="sm" variant="secondary" onClick={() => onFill(ownerSide, label)}>
          Put “{label}” as the {ownerSide}
        </Button>
      )}
      {others.length > 0 && (
        <ul className="space-y-1 text-meta text-muted">
          {others.map((person) => (
            <li key={`${person.role}-${person.name}`}>
              <span className="font-medium text-ink">{person.name}</span> {ROLE_WORDS[person.role]}
              {person.role === "payer" ? " for the buyer. The buyer stays as shown." : "."}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** Other deals in the same conversation, left out of this record. A chat export can be read again for one. */
export function OtherDeals({ deals, onReadDeal }: { deals: OtherDeal[]; onReadDeal: ((deal: string) => void) | null }) {
  if (deals.length === 0) return null;
  return (
    <div className="border border-line bg-surface px-4 py-3.5 shadow-e1">
      <p className="label-caps text-muted">Other deals in this chat</p>
      <p className="mt-1.5 text-meta text-muted">
        {onReadDeal
          ? "This record is for the most recent deal. Pick another to make its record instead."
          : "This record is for the most recent deal. To record another, add only the screenshots of that one."}
      </p>
      <ul className="mt-3 divide-y divide-line border-t border-line">
        {deals.map((deal) => (
          <li key={deal.summary} className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 py-2.5">
            <span className="min-w-0 text-ui">{deal.summary}</span>
            {onReadDeal && (
              <Button size="sm" variant="secondary" onClick={() => onReadDeal(deal.summary)}>
                Make this record
              </Button>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

const SIDE_WORDS: Record<ChatSummary["participants"][number]["side"], string> = {
  owner: "you",
  contact: "the other person",
  unknown: "",
};

/** What was read from the chat export, in place of the screenshot thumbnails. */
export function ChatExportCard({ file, chat }: { file: File | null; chat: ChatSummary }) {
  const days = chat.first_day && chat.last_day ? (chat.first_day === chat.last_day ? shortDate(chat.first_day) : `${shortDate(chat.first_day)} – ${shortDate(chat.last_day)}`) : "";
  return (
    <div>
      <div className="flex items-center gap-2">
        <ChatIcon className="size-4 shrink-0 text-accent" />
        <p className="text-sm font-medium">{chat.group ? "Your group chat export" : "Your chat export"}</p>
      </div>
      {file && <p className="mt-1 truncate font-mono text-caption text-muted">{file.name}</p>}
      <p className="mt-2 text-meta text-muted">
        {chat.messages} {chat.messages === 1 ? "message" : "messages"}
        {days ? ` · ${days}` : ""}
        {chat.photos_read > 0 ? ` · ${chat.photos_read} ${chat.photos_read === 1 ? "photo" : "photos"} read` : ""}
      </p>
      <ul className="mt-3 space-y-1.5 border-t border-line pt-3">
        {chat.participants.slice(0, 8).map((person) => (
          <li key={person.key} className="flex items-baseline justify-between gap-3 text-meta">
            <span className="min-w-0 truncate">
              <span className="font-medium">{person.name}</span>
              {SIDE_WORDS[person.side] && <span className="text-muted"> · {SIDE_WORDS[person.side]}</span>}
            </span>
            <span className="shrink-0 font-mono text-caption text-muted tabular-nums">{person.messages} sent</span>
          </li>
        ))}
      </ul>
      {chat.owner_source === "" && !chat.group && (
        <p className="mt-3 text-meta text-muted">The file name didn&apos;t say whose phone it came from, so check who is who.</p>
      )}
      {chat.truncated && <p className="mt-3 text-meta text-warn">This chat is long, so only its latest 600 messages were read.</p>}
    </div>
  );
}
