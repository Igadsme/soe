"use client";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { useResource } from "../../lib/useResource";
import { Badge, DataState } from "../../components/ui";
import { totalsByMetric, type Metric } from "../../lib/metrics";

type Analytics = { verified: Metric[]; unverified: Metric[] };
type Recommendations = { state: "insufficient_data" | "learned"; sample_size: number; required_samples: number; target: string; disclaimer: string;
  weights: Record<string, number> | null; blend_weight: number; suggestions?: { best_posting_hour_utc: number; duration: string; heuristic_agrees_with_outcomes: boolean } };
type Collection = { id: number; publication_id: number; window: string; provider: string; status: string; reason: string | null; attempted_at: string };

export default function AnalyticsPage() {
  const analytics = useResource<Analytics>("/api/v1/analytics");
  const recommendations = useResource<Recommendations>("/api/v1/analytics/recommendations");
  const collections = useResource<Collection[]>("/api/v1/analytics/collections");
  return <>
    <header className="page-head"><p className="eyebrow">PERFORMANCE</p><h1>Analytics</h1>
      <p className="muted">Only metrics collected from platform APIs are verified. Nothing here is estimated.</p></header>
    <DataState resource={analytics} empty="No metrics collected yet. Metrics appear after a published clip has been polled by the analytics job." isEmpty={(data) => !data.verified.length && !data.unverified.length}>{(data) => {
      const totals = totalsByMetric(data.verified);
      return <>
        <section className="panel" aria-labelledby="verified-title">
          <h2 id="verified-title">Verified platform metrics</h2>
          {totals.length ? <>
            <div className="chart" role="img" aria-label={`Bar chart of verified totals: ${totals.map((t) => `${t.metric} ${t.value}`).join(", ")}`}>
              <ResponsiveContainer width="100%" height={260}><BarChart data={totals}><CartesianGrid strokeDasharray="3 3" /><XAxis dataKey="metric" /><YAxis /><Tooltip /><Bar dataKey="value" fill="#7c5cff" /></BarChart></ResponsiveContainer>
            </div>
            <div className="table-wrap" tabIndex={0} role="region" aria-label="Scrollable table"><table><thead><tr><th scope="col">Metric</th><th scope="col">Total</th></tr></thead>
              <tbody>{totals.map((row) => <tr key={row.metric}><td>{row.metric}</td><td>{row.value.toLocaleString()}</td></tr>)}</tbody></table></div>
          </> : <p className="empty-state">No verified metrics yet.</p>}
        </section>
        {data.unverified.length > 0 && <section className="panel" aria-labelledby="unverified-title">
          <h2 id="unverified-title">Unverified metrics</h2><p className="muted">Shown separately; not counted above.</p>
          <ul className="rows">{totalsByMetric(data.unverified).map((row) => <li key={row.metric}>{row.metric}: {row.value.toLocaleString()}</li>)}</ul>
        </section>}
      </>;
    }}</DataState>
    <section className="panel" aria-labelledby="collections-title">
      <h2 id="collections-title">Collection attempts</h2>
      <DataState resource={collections} empty="No collection attempts yet.">{(rows) => <ul className="rows">{rows.slice(0, 30).map((row) => <li key={row.id}>
        Publication {row.publication_id} · {row.window} · {row.provider} · <Badge value={row.status} />{row.reason && <span className="muted">{row.reason}</span>}
      </li>)}</ul>}</DataState>
    </section>
    <section className="panel" aria-labelledby="learn-title">
      <h2 id="learn-title">Recommendations</h2>
      <DataState resource={recommendations} empty="">{(data) => data.state === "insufficient_data"
        ? <p className="empty-state">Insufficient data: {data.sample_size} of {data.required_samples} publications have verified 7-day metrics. Ranking uses the transparent heuristic only.</p>
        : <>
          <dl className="facts">
            <div><dt>Sample size</dt><dd>{data.sample_size}</dd></div>
            <div><dt>Learned share of score</dt><dd>{Math.round(data.blend_weight * 100)}%</dd></div>
            <div><dt>Best posting hour (UTC)</dt><dd>{data.suggestions?.best_posting_hour_utc}:00</dd></div>
            <div><dt>Duration trend</dt><dd>{data.suggestions?.duration} clips</dd></div>
          </dl>
          <p className="muted">Weights: {Object.entries(data.weights ?? {}).map(([key, value]) => `${key} ${value.toFixed(3)}`).join(" · ")}</p>
        </>}</DataState>
      <p className="muted">{recommendations.data?.disclaimer}</p>
    </section>
  </>;
}
