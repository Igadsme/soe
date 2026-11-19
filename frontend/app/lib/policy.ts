export type Policy = {
  auto_process_authorized: boolean; auto_publish: boolean; approval_mode: "manual" | "auto_after_qc"; min_clip_score: number; max_clips_per_source: number;
  daily_publish_limits: Record<string, number>; target_accounts: Record<string, number[]>; publishing_windows: { start_hour: number; end_hour: number }[];
  timezone: string; default_privacy: "private" | "unlisted"; instagram_visibility_acknowledged: boolean; daily_budget_cents: number | null;
  monthly_budget_cents: number | null; max_concurrent_processing_jobs: number; max_concurrent_publish_jobs: number;
};

/** "9-12, 18-22" → [{start_hour: 9, end_hour: 12}, ...]; throws on malformed input. */
export function parseWindows(text: string): Policy["publishing_windows"] {
  return text.split(",").map((part) => part.trim()).filter(Boolean).map((part) => {
    const match = /^(\d{1,2})\s*-\s*(\d{1,2})$/.exec(part);
    if (!match) throw new Error(`Invalid window “${part}”. Use start-end hours, e.g. 9-17.`);
    const [start, end] = [Number(match[1]), Number(match[2])];
    if (start > 23 || end > 24 || start === end) throw new Error(`Invalid window “${part}”.`);
    return { start_hour: start, end_hour: end };
  });
}

export function formatWindows(windows: Policy["publishing_windows"]): string {
  return windows.map((window) => `${window.start_hour}-${window.end_hour}`).join(", ");
}

/** Dollars text → cents, blank means "no ceiling". */
export function dollarsToCents(value: string): number | null {
  if (!value.trim()) return null;
  const amount = Number(value);
  if (!Number.isFinite(amount) || amount < 0) throw new Error("Budget must be a non-negative amount.");
  return Math.round(amount * 100);
}
