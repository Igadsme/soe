import type { SystemStatus } from "./types";

export type Metric = { metric: string; value: number; publication_id: number | null };

export function totalsByMetric(rows: Metric[]) {
  const totals = new Map<string, number>();
  for (const row of rows) totals.set(row.metric, (totals.get(row.metric) ?? 0) + row.value);
  return [...totals].map(([metric, value]) => ({ metric, value }));
}

export function totalActive(status: SystemStatus): number {
  return ["queued", "running", "cancel_requested"].reduce((sum, key) => sum + Object.values(status.queue_depth?.[key] ?? {}).reduce((a, b) => a + b, 0), 0);
}
