import { formatBytes } from "./money";
import type { PublicConfig } from "./types";

export interface FileCheck {
  accepted: File[];
  problems: string[];
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
          : `${file.name} isn't a JPEG, PNG or WebP image.`,
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
