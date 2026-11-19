"use client";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { useResource } from "../../lib/useResource";
import { DataState } from "../../components/ui";
import { totalsByMetric, type Metric } from "../../lib/metrics";

type Analytics = { verified: Metric[]; unverified: Metric[] };

export default function AnalyticsPage() {
  const analytics = useResource<Analytics>("/api/v1/analytics");
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
            <div className="table-wrap"><table><thead><tr><th scope="col">Metric</th><th scope="col">Total</th></tr></thead>
              <tbody>{totals.map((row) => <tr key={row.metric}><td>{row.metric}</td><td>{row.value.toLocaleString()}</td></tr>)}</tbody></table></div>
          </> : <p className="empty-state">No verified metrics yet.</p>}
        </section>
        {data.unverified.length > 0 && <section className="panel" aria-labelledby="unverified-title">
          <h2 id="unverified-title">Unverified metrics</h2><p className="muted">Shown separately; not counted above.</p>
          <ul className="rows">{totalsByMetric(data.unverified).map((row) => <li key={row.metric}>{row.metric}: {row.value.toLocaleString()}</li>)}</ul>
        </section>}
      </>;
    }}</DataState>
  </>;
}
