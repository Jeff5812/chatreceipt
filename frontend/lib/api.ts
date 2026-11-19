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

export async function downloadDocument(
  draft: TransactionDraft,
  format: "pdf" | "png",
  stats: ClientStats,
): Promise<void> {
  const response = await send(`/api/documents/${format}`, { method: "POST", json: { draft, stats } });
  const blob = await response.blob();
  const disposition = response.headers.get("Content-Disposition") ?? "";
  const filename = /filename="([^"]+)"/.exec(disposition)?.[1] ?? `chatreceipt.${format}`;
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 30_000);
}

interface ExtractOptions {
  signal: AbortSignal;
  onUploadProgress: (fraction: number) => void;
  onUploaded: () => void;
}

/** Upload screenshots (in order) and read the transaction. Uses XHR for upload progress. */
export function extractTransaction(files: File[], options: ExtractOptions): Promise<ExtractionResponse> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const form = new FormData();
    for (const file of files) form.append("files", file, file.name);

    xhr.open("POST", `${API_URL}/api/extractions`);
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
