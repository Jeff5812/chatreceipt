import { formatBytes } from "./money";
import type { PublicConfig } from "./types";

export interface FileCheck {
  accepted: File[];
  problems: string[];
}

const CHAT_EXPORT_TYPES = ["text/plain", "application/zip", "application/x-zip-compressed"];

/** A WhatsApp chat export: the .txt file, or the .zip WhatsApp makes when media is included. */
export function isChatExport(file: File): boolean {
  return /\.(txt|zip)$/i.test(file.name) || CHAT_EXPORT_TYPES.includes(file.type);
}

/** The other person's name, from an export's file name ("WhatsApp Chat with Tola's Bags.txt"). */
export function chatNameFrom(filename: string): string {
  const stem = filename.replace(/\.(txt|zip)$/i, "").replace(/\s*\(\d+\)$/, "").trim();
  const found = /^WhatsApp Chat (?:with|-) (.+)$/i.exec(stem);
  return found ? found[1].trim() : "";
}

/** Quick checks on a chat export before upload. The backend reads and checks it again in full. */
export function checkChatExport(file: File, limits: PublicConfig["limits"]): string | null {
  if (file.size === 0) return `${file.name} is empty.`;
  const limit = limits.max_chat_export_bytes ?? 0;
  if (limit > 0 && file.size > limit) {
    return `${file.name} is larger than ${formatBytes(limit)}. Export the chat again without media, or use just the .txt file.`;
  }
  return null;
}

/**
 * Quick checks before upload so people get immediate feedback. The backend checks every file
 * again by its content; these checks are only for convenience.
 */
export function checkFiles(
  incoming: File[],
  existing: { name: string; size: number }[],
  limits: PublicConfig["limits"],
): FileCheck {
  const accepted: File[] = [];
  const problems: string[] = [];
  let count = existing.length;
  let total = existing.reduce((sum, file) => sum + file.size, 0);

  for (const file of incoming) {
    if (!limits.accepted_types.includes(file.type)) {
      const isPhoto = /\.(heic|heif)$/i.test(file.name);
      problems.push(
        isPhoto
          ? `${file.name} is a HEIC photo. Take a screenshot instead, or save it as JPEG or PNG.`
          : `${file.name} isn't a screenshot (JPEG, PNG or WebP) or a WhatsApp chat export (.txt or .zip).`,
      );
      continue;
    }
    if (file.size > limits.max_file_bytes) {
      problems.push(`${file.name} is larger than ${formatBytes(limits.max_file_bytes)}.`);
      continue;
    }
    if (existing.some((f) => f.name === file.name && f.size === file.size)) {
      problems.push(`${file.name} is already added.`);
      continue;
    }
    if (count >= limits.max_files) {
      problems.push(`You can add up to ${limits.max_files} screenshots.`);
      break;
    }
    if (total + file.size > limits.max_total_bytes) {
      problems.push(`Together, these screenshots are larger than ${formatBytes(limits.max_total_bytes)}.`);
      break;
    }
    accepted.push(file);
    count += 1;
    total += file.size;
  }
  return { accepted, problems };
}
