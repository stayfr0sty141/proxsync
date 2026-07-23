import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/**
 * Merge class names with Tailwind conflict resolution. `clsx` handles
 * conditional/array/object inputs; `twMerge` ensures a later utility wins over
 * an earlier conflicting one (e.g. `px-2` then `px-4` keeps `px-4`).
 */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
