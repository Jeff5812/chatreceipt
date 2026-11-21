"use client";

import { useRef, useState, type DragEvent } from "react";
import { chatNameFrom, checkChatExport, checkFiles, isChatExport } from "@/lib/files";
import { formatBytes } from "@/lib/money";
import type { PublicConfig, Screenshot } from "@/lib/types";
import { Mascot } from "../mascot";
import { PHONE, PixelArt } from "../pixel-art";
import { ArrowChip, Button } from "../ui/button";
import { ArrowLeftIcon, ArrowRightIcon, ChatIcon, CloseIcon } from "../ui/icons";
import { WhatsAppButton } from "../whatsapp-button";
import { Notice } from "./notice";
import { StepHeading } from "./step-heading";

interface UploadStepProps {
  config: PublicConfig | null;
  configError: string | null;
  onRetryConfig: () => void;
  screenshots: Screenshot[];
  chatExport: File | null;
  onAdd: (files: File[]) => void;
  onUseChatExport: (file: File) => void;
  onRemoveChatExport: () => void;
  onRemove: (id: string) => void;
  onMove: (id: string, offset: -1 | 1) => void;
  onExtract: () => void;
  onManual: () => void;
  onExample: () => void;
  onResume: (() => void) | null;
  extractError: string | null;
  focusHeading: boolean;
}

// Small controls on screenshot thumbnails. The visible button stays compact; the ::after layer widens the
// tap area to about 44px for fingers.
const TINY_CONTROL =
  "flex size-8 items-center justify-center border border-line bg-surface text-ink shadow-e1 transition-colors after:absolute after:-inset-1.5 hover:border-ink disabled:opacity-30";

/** Chit waiting between two pixel phones. The phones lift and Chit smiles while files are dragged over. */
function TrayIllustration({ active }: { active: boolean }) {
  const phone = "absolute bottom-4 transition-transform duration-300 ease-soft";
  return (
    <div aria-hidden className="relative flex h-[108px] w-[164px] items-end justify-center">
      <span className={`${phone} left-1 ${active ? "-translate-y-4 -rotate-[16deg]" : "-rotate-[9deg] group-hover/drop:-translate-y-1.5"}`}>
        <PixelArt {...PHONE} cell={3} depth={2} />
      </span>
      <span className={`${phone} right-1 ${active ? "-translate-y-4 rotate-[16deg]" : "rotate-[9deg] group-hover/drop:-translate-y-1.5"}`}>
        <PixelArt {...PHONE} cell={3} depth={2} />
      </span>
      <span className={`relative z-10 mb-2 transition-transform duration-300 ease-soft ${active ? "-translate-y-2" : ""}`}>
        <Mascot cell={4} mood={active ? "happy" : "idle"} />
      </span>
      <span className="absolute bottom-0 h-2 w-28 rounded-[50%] bg-black/70 blur-[4px]" />
    </div>
  );
}

/** A chat export and screenshots are read separately: switching from one to the other is asked first. */
type Swap = { kind: "export"; file: File } | { kind: "screenshots"; files: File[] };

/** How to make a chat export, for Android and iPhone. */
function ExportHelp() {
  return (
    <details className="group/help border border-line bg-surface px-4 py-3 text-sm shadow-e1">
      <summary className="cursor-pointer list-none font-medium select-none">
        <span className="pixel-label mr-2 text-accent group-open/help:hidden">+</span>
        <span className="pixel-label mr-2 hidden text-accent group-open/help:inline">-</span>
        How to export a WhatsApp chat
      </summary>
      <ol className="mt-3 list-decimal space-y-1.5 pl-5 text-muted">
        <li>Open the chat in WhatsApp.</li>
        <li>
          Android: tap <span className="text-ink">⋮ › More › Export chat</span>. iPhone: tap the name at the top, then{" "}
          <span className="text-ink">Export Chat</span>.
        </li>
        <li>
          Choose <span className="text-ink">Without media</span> for a small file, or <span className="text-ink">Include media</span>{" "}
          so payment screenshots sent in the chat are read too.
        </li>
        <li>Save the file, then add it here.</li>
      </ol>
    </details>
  );
}

export function UploadStep(props: UploadStepProps) {
  const { config, screenshots, chatExport } = props;
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const [problems, setProblems] = useState<string[]>([]);
  const [swap, setSwap] = useState<Swap | null>(null);
  const ready = config !== null;
  const canExtract = ready && config.extraction_available;

  function addFiles(list: FileList | null) {
    if (!config || !list) return;
    const incoming = Array.from(list);
    const exports = incoming.filter(isChatExport);
    const images = incoming.filter((file) => !isChatExport(file));
    setSwap(null);
    if (exports.length > 0) {
      const [file, ...others] = exports;
      const found: string[] = [];
      if (others.length > 0) found.push("Add one chat export at a time.");
      if (images.length > 0) found.push("A chat export is read on its own, so the screenshots weren't added.");
      const problem = checkChatExport(file, config.limits);
      setProblems(problem ? [...found, problem] : found);
      if (problem) return;
      if (screenshots.length > 0) setSwap({ kind: "export", file });
      else props.onUseChatExport(file);
      return;
    }
    const { accepted, problems: found } = checkFiles(images, screenshots, config.limits);
    setProblems(found);
    if (accepted.length === 0) return;
    if (chatExport !== null) setSwap({ kind: "screenshots", files: accepted });
    else props.onAdd(accepted);
  }

  function confirmSwap() {
    if (swap?.kind === "export") props.onUseChatExport(swap.file);
    else if (swap?.kind === "screenshots") props.onAdd(swap.files);
    setSwap(null);
  }

  function onDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setDragging(false);
    addFiles(event.dataTransfer.files);
  }

  const limits = config?.limits;
  const hasFiles = screenshots.length > 0 || chatExport !== null;
  const otherPerson = chatExport ? chatNameFrom(chatExport.name) : "";
  const exportLimit = limits?.max_chat_export_bytes ? ` · chat export up to ${formatBytes(limits.max_chat_export_bytes)}` : "";
  const acceptedTypes = [...(limits?.accepted_types ?? ["image/*"]), ".txt", ".zip", "text/plain", "application/zip"].join(",");

  return (
    <div className="mx-auto max-w-4xl px-4 py-10 sm:px-6 sm:py-14">
      <StepHeading focus={props.focusHeading}>Add the conversation</StepHeading>
      <p className="mt-4 max-w-2xl text-lg leading-relaxed text-muted">
        Add screenshots of the conversation in the order it happened, or the chat itself exported from WhatsApp.
        Include the messages where the price, delivery and payment were discussed.
      </p>

      <div className="mt-8 space-y-4 empty:hidden">
        {props.configError && (
          <Notice
            tone="error"
            title="Can't reach ChatReceipt"
            action={
              <Button size="sm" variant="secondary" onClick={props.onRetryConfig}>
                Try again
              </Button>
            }
          >
            {props.configError}
          </Notice>
        )}
        {ready && !config.extraction_available && (
          <Notice title="Reading screenshots isn't available right now">
            You can still enter the details yourself, or look at an example to see how ChatReceipt works.
          </Notice>
        )}
        {props.extractError && (
          <Notice
            tone="error"
            title={chatExport ? "The chat couldn't be read" : "The screenshots couldn't be read"}
            action={
              <Button size="sm" variant="secondary" onClick={props.onManual}>
                Enter the details yourself
              </Button>
            }
          >
            {props.extractError}
          </Notice>
        )}
      </div>

      {chatExport && (
        <section aria-label="Chat export" className="mt-8">
          <div className="flex items-center gap-4 border border-line bg-surface px-4 py-3.5 shadow-e1 sm:px-5">
            <span className="flex size-10 shrink-0 items-center justify-center bg-accent text-accent-ink" aria-hidden>
              <ChatIcon className="size-5" />
            </span>
            <div className="min-w-0 flex-1">
              <p className="truncate text-ui font-medium">{chatExport.name}</p>
              <p className="text-meta text-muted">
                WhatsApp chat export · {formatBytes(chatExport.size)}
                {otherPerson ? ` · chat with ${otherPerson}` : ""}
              </p>
            </div>
            <button
              type="button"
              onClick={props.onRemoveChatExport}
              aria-label="Remove the chat export"
              className={`${TINY_CONTROL} relative shrink-0 rounded-none`}
            >
              <CloseIcon className="size-3.5" />
            </button>
          </div>
          <p className="mt-2 text-meta text-muted">
            The whole chat is read, and the most recent deal becomes the record. Other deals in it are listed so you can
            pick one.
          </p>
        </section>
      )}

      {screenshots.length > 0 && (
        <section aria-label="Screenshots in order" className="mt-8">
          <div className="flex items-baseline justify-between gap-3">
            <h2 className="text-sm font-semibold">
              {screenshots.length} {screenshots.length === 1 ? "screenshot" : "screenshots"}, in conversation order
            </h2>
            {screenshots.length > 1 && <p className="hidden text-meta text-muted sm:block">Use the arrows to change the order</p>}
          </div>
          <ol className="mt-3 grid grid-cols-3 gap-3 sm:grid-cols-4 lg:grid-cols-6">
            {screenshots.map((shot, index) => (
              <li
                key={shot.id}
                className="overflow-hidden rounded-xl border border-line bg-surface shadow-e1 transition-[transform,box-shadow] duration-200 ease-soft hover:-translate-y-0.5 hover:shadow-e2"
              >
                <div className="relative aspect-[9/14] bg-paper-deep">
                  {/* eslint-disable-next-line @next/next/no-img-element -- local preview of an uploaded file */}
                  <img src={shot.url} alt={`Screenshot ${index + 1}: ${shot.name}`} className="size-full object-cover object-top" />
                  <span className="absolute top-2 left-2 flex h-6 min-w-6 items-center justify-center bg-accent px-1 font-pixel text-[16px] leading-none text-accent-ink">
                    {index + 1}
                  </span>
                  <button
                    type="button"
                    onClick={() => props.onRemove(shot.id)}
                    aria-label={`Remove screenshot ${index + 1}`}
                    className={`${TINY_CONTROL} absolute top-1.5 right-1.5 rounded-none`}
                  >
                    <CloseIcon className="size-3.5" />
                  </button>
                </div>
                <div className="flex items-center justify-between gap-3 border-t border-line px-2 py-1.5">
                  <button
                    type="button"
                    onClick={() => props.onMove(shot.id, -1)}
                    disabled={index === 0}
                    aria-label={`Move screenshot ${index + 1} earlier`}
                    className={`${TINY_CONTROL} relative rounded-md`}
                  >
                    <ArrowLeftIcon className="size-3.5" />
                  </button>
                  <button
                    type="button"
                    onClick={() => props.onMove(shot.id, 1)}
                    disabled={index === screenshots.length - 1}
                    aria-label={`Move screenshot ${index + 1} later`}
                    className={`${TINY_CONTROL} relative rounded-md`}
                  >
                    <ArrowRightIcon className="size-3.5" />
                  </button>
                </div>
              </li>
            ))}
          </ol>
        </section>
      )}

      <div
        onDragOver={(event) => {
          event.preventDefault();
          if (ready) setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
        className={`group/drop relative overflow-hidden rounded-2xl border transition-[border-color,background-color,box-shadow] duration-200 ${
          hasFiles ? "mt-4 flex items-center gap-4 px-4 py-3.5 sm:px-5" : "mt-8 flex flex-col items-center px-6 py-12 text-center sm:py-14"
        } ${
          dragging
            ? "border-accent bg-accent-soft shadow-[inset_0_0_0_1px_rgb(196_242_90/0.45)]"
            : "border-dashed border-line-strong bg-well shadow-well"
        }`}
      >
        <div aria-hidden className="bg-dots-ink pointer-events-none absolute inset-0 opacity-80" />
        {hasFiles ? (
          <>
            <div className="relative hidden h-[52px] w-[76px] shrink-0 sm:block">
              <div className="absolute top-0 left-0 origin-top-left scale-[0.56]">
                <TrayIllustration active={dragging} />
              </div>
            </div>
            <div className="relative min-w-0 flex-1">
              <p className="text-ui font-medium">
                {dragging ? "Drop to add them" : chatExport ? "Use a different chat export" : "Add more screenshots"}
              </p>
              <p className="text-meta text-muted">
                {chatExport ? "A .txt or .zip file from WhatsApp" : limits ? `Up to ${limits.max_files} · JPEG, PNG or WebP` : "JPEG, PNG or WebP"}
              </p>
            </div>
            <Button className="relative shrink-0" size="sm" variant="secondary" disabled={!ready} onClick={() => inputRef.current?.click()}>
              Choose
            </Button>
          </>
        ) : (
          <>
            <div className="relative">
              <TrayIllustration active={dragging} />
            </div>
            <p className="relative mt-6 text-ui font-medium">
              {dragging ? "Drop to add them" : "Drag screenshots or a chat export here, or"}
            </p>
            <Button className="relative mt-3" variant="secondary" disabled={!ready} onClick={() => inputRef.current?.click()}>
              Choose files
            </Button>
            <p className="relative mt-4 text-meta text-muted">
              {limits
                ? `JPEG, PNG or WebP · up to ${limits.max_files} screenshots · ${formatBytes(limits.max_file_bytes)} each${exportLimit}`
                : "JPEG, PNG or WebP, or a .txt or .zip chat export"}
            </p>
          </>
        )}
      </div>
      <input
        ref={inputRef}
        type="file"
        multiple
        accept={acceptedTypes}
        className="sr-only"
        tabIndex={-1}
        aria-hidden
        onChange={(event) => {
          addFiles(event.target.files);
          event.target.value = "";
        }}
      />

      {swap && (
        <div className="mt-4">
          <Notice
            title={swap.kind === "export" ? "Read the chat export instead?" : "Use the screenshots instead?"}
            action={
              <>
                <Button size="sm" variant="secondary" onClick={confirmSwap}>
                  {swap.kind === "export" ? "Use the chat export" : "Use the screenshots"}
                </Button>
                <Button size="sm" variant="secondary" onClick={() => setSwap(null)}>
                  {swap.kind === "export" ? "Keep the screenshots" : "Keep the chat export"}
                </Button>
              </>
            }
          >
            {swap.kind === "export"
              ? `A chat export is read on its own, so the ${screenshots.length === 1 ? "screenshot" : `${screenshots.length} screenshots`} you added would be removed.`
              : "Screenshots and a chat export are read separately, so the chat export would be removed."}
          </Notice>
        </div>
      )}

      {problems.length > 0 && (
        <div className="mt-4">
          <Notice tone="warn" title={problems.length === 1 ? "One file wasn't added" : "Some files weren't added"}>
            <ul className="list-disc space-y-0.5 pl-5">
              {problems.map((problem) => (
                <li key={problem}>{problem}</li>
              ))}
            </ul>
          </Notice>
        </div>
      )}

      <div className={`${hasFiles ? "mt-6" : "mt-10"} flex flex-col gap-4 border-t border-line pt-6 sm:flex-row sm:items-center sm:justify-between`}>
        <div className="flex flex-col gap-3 sm:flex-row">
          {props.onResume && (
            <Button onClick={props.onResume}>
              Back to review
              <ArrowChip />
            </Button>
          )}
          {canExtract && (
            <Button
              variant={props.onResume ? "secondary" : "primary"}
              disabled={!hasFiles}
              onClick={props.onExtract}
            >
              {props.onResume ? (chatExport ? "Read the chat again" : "Read the screenshots again") : "Read the conversation"}
              {!props.onResume && <ArrowChip />}
            </Button>
          )}
          {ready && !canExtract && !props.onResume && (
            <Button onClick={props.onManual}>
              Enter the details yourself
              <ArrowChip />
            </Button>
          )}
        </div>
        <div className="flex flex-wrap gap-x-5 gap-y-1">
          {canExtract && (
            <Button variant="quiet" disabled={!ready} onClick={props.onManual}>
              Enter the details yourself
            </Button>
          )}
          <Button variant="quiet" disabled={!ready} onClick={props.onExample}>
            See an example
          </Button>
        </div>
      </div>

      {screenshots.length === 0 && (
        <div className="mt-6">
          <ExportHelp />
        </div>
      )}

      {!hasFiles && (
        <div className="mt-6 empty:hidden">
          <WhatsAppButton size="sm" label="Prefer WhatsApp? Send them there" />
        </div>
      )}
    </div>
  );
}
