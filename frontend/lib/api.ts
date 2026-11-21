import type { Calculation, ClientStats, ExtractionResponse, PublicConfig, TransactionDraft } from "./types";

export const API_URL = (process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000").replace(/\/$/, "");

const NETWORK_MESSAGE = "Couldn't reach ChatReceipt. Check your connection and try again.";
const GENERIC_MESSAGE = "Something went wrong. Please try again.";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly details: unknown = null,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

interface ErrorBody {
  error?: { code?: string; message?: string; details?: unknown };
}

function errorFrom(status: number, body: unknown): ApiError {
  const error = (body as ErrorBody | null)?.error;
  return new ApiError(status, error?.code ?? "http_error", error?.message ?? GENERIC_MESSAGE, error?.details ?? null);
}

export function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

export function messageOf(error: unknown): string {
  return error instanceof ApiError ? error.message : GENERIC_MESSAGE;
}

export async function send(path: string, init: RequestInit & { json?: unknown } = {}): Promise<Response> {
  const { json, headers, ...rest } = init;
  let response: Response;
  try {
    response = await fetch(`${API_URL}${path}`, {
      ...rest,
      headers: json === undefined ? headers : { "Content-Type": "application/json", ...headers },
      body: json === undefined ? rest.body : JSON.stringify(json),
    });
  } catch (error) {
    if (isAbortError(error)) throw error;
    throw new ApiError(0, "network_error", NETWORK_MESSAGE);
  }
  if (!response.ok) {
    let body: unknown = null;
    try {
      body = await response.json();
    } catch {
      body = null;
    }
    throw errorFrom(response.status, body);
  }
  return response;
}

export async function getConfig(signal?: AbortSignal): Promise<PublicConfig> {
  return (await send("/api/config", { signal })).json();
}

export async function calculate(draft: TransactionDraft, signal?: AbortSignal): Promise<Calculation> {
  return (await send("/api/transactions/calculate", { method: "POST", json: draft, signal })).json();
}

export async function previewDocument(draft: TransactionDraft, signal?: AbortSignal): Promise<string> {
  return (await send("/api/documents/preview", { method: "POST", json: { draft }, signal })).text();
}

/** Render a record as a PDF or PNG file. */
export async function fetchDocument(draft: TransactionDraft, format: "pdf" | "png", stats: ClientStats): Promise<File> {
  const response = await send(`/api/documents/${format}`, { method: "POST", json: { draft, stats } });
  const blob = await response.blob();
  const disposition = response.headers.get("Content-Disposition") ?? "";
  const filename = /filename="([^"]+)"/.exec(disposition)?.[1] ?? `chatreceipt.${format}`;
  return new File([blob], filename, { type: blob.type || (format === "pdf" ? "application/pdf" : "image/png") });
}

export async function downloadDocument(
  draft: TransactionDraft,
  format: "pdf" | "png",
  stats: ClientStats,
): Promise<void> {
  const file = await fetchDocument(draft, format, stats);
  const url = URL.createObjectURL(file);
  const link = document.createElement("a");
  link.href = url;
  link.download = file.name;
  document.body.append(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 30_000);
}

/** Whether this browser can hand a file to the phone's share sheet (Safari on iPhone, Chrome on Android). */
export function canShareFiles(): boolean {
  if (typeof navigator === "undefined" || typeof navigator.canShare !== "function") return false;
  try {
    return navigator.canShare({ files: [new File([""], "receipt.png", { type: "image/png" })] });
  } catch {
    return false;
  }
}

/**
 * Open the share sheet with a file. Resolves false when the person closed the sheet. Rejects with a
 * NotAllowedError when the browser wants a fresh tap (Safari does, after a slow download).
 */
export async function shareFile(file: File, title: string): Promise<boolean> {
  try {
    await navigator.share({ files: [file], title });
    return true;
  } catch (reason) {
    if (reason instanceof DOMException && reason.name === "AbortError") return false;
    throw reason;
  }
}

interface ExtractOptions {
  signal: AbortSignal;
  onUploadProgress: (fraction: number) => void;
  onUploaded: () => void;
}

/** Upload screenshots (in order) and read the transaction. Uses XHR for upload progress. */
export function extractTransaction(files: File[], options: ExtractOptions): Promise<ExtractionResponse> {
  const form = new FormData();
  for (const file of files) form.append("files", file, file.name);
  return upload("/api/extractions", form, options);
}

/**
 * Upload a WhatsApp chat export (.txt or .zip) and read the transaction. ``focus`` names another deal
 * the chat holds, to make the record for that one instead of the latest.
 */
export function extractChat(file: File, focus: string, options: ExtractOptions): Promise<ExtractionResponse> {
  const form = new FormData();
  form.append("file", file, file.name);
  if (focus) form.append("focus", focus);
  return upload("/api/extractions/chat", form, options);
}

function upload(path: string, form: FormData, options: ExtractOptions): Promise<ExtractionResponse> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${API_URL}${path}`);
    xhr.responseType = "json";
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) options.onUploadProgress(event.loaded / event.total);
    };
    xhr.upload.onload = () => options.onUploaded();
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300 && xhr.response) resolve(xhr.response as ExtractionResponse);
      else reject(errorFrom(xhr.status, xhr.response));
    };
    xhr.onerror = () => reject(new ApiError(0, "network_error", NETWORK_MESSAGE));
    xhr.onabort = () => reject(new DOMException("Cancelled", "AbortError"));
    options.signal.addEventListener("abort", () => xhr.abort(), { once: true });
    xhr.send(form);
  });
}
