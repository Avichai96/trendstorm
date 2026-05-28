import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";
import { formatDistanceToNow, format, parseISO } from "date-fns";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function formatRelative(iso: string): string {
  return formatDistanceToNow(parseISO(iso), { addSuffix: true });
}

export function formatDate(iso: string): string {
  return format(parseISO(iso), "MMM d, yyyy HH:mm");
}

export function formatCurrency(usd: number): string {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 4,
    maximumFractionDigits: 4,
  }).format(usd);
}

export function formatSeconds(seconds: number): string {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

export function slaUrgency(deadlineIso: string): "low" | "medium" | "high" | "expired" {
  const remaining = parseISO(deadlineIso).getTime() - Date.now();
  if (remaining < 0) return "expired";
  const hours = remaining / 3_600_000;
  if (hours < 4) return "high";
  if (hours < 12) return "medium";
  return "low";
}
