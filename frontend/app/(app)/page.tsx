"use client";
import Link from "next/link";
import { cents } from "../lib/api";
import { useResource } from "../lib/useResource";
import { Badge, DataState } from "../components/ui";
import type { Job, SystemStatus } from "../lib/types";
import { totalActive } from "../lib/metrics";

type Overview = { environment: string; demo_mode: boolean; counts: Record<string, number>; revenue_cents: number; expense_cents: number };

export default function OverviewPage() {
  const overview = useResource<Overview>("/api/v1/overview");
  const status = useResource<SystemStatus>("/api/v1/system/status", 10000);
  const failures = useResource<Job[]>("/api/v1/jobs?status=dead_letter");
  return <>
    <header className="page-head"><p className="eyebrow">OPERATIONS CENTER</p><h1>Overview</h1></header>
    <DataState resource={overview} empty="">{(data) => <>
      {data.demo_mode && <p className="alert">Development mode: discovery uses labeled fixtures.</p>}
      <div className="grid">
        <div className="card"><p className="muted">Sources</p><strong>{data.counts.sources}</strong></div>
        <div className="card"><p className="muted">Clips</p><strong>{data.counts.clips}</strong></div>
        <div className="card"><p className="muted">Verified revenue</p><strong>{cents(data.revenue_cents)}</strong><small>verified ledger entries only</small></div>
        <div className="card"><p className="muted">Verified expenses</p><strong>{cents(data.expense_cents)}</strong></div>
      </div>
    </>}</DataState>
    <section className="panel" aria-labelledby="system-title">
      <h2 id="system-title">System health</h2>
      <DataState resource={status} empty="">{(data) => <dl className="facts">
        <div><dt>Automation</dt><dd><Badge value={data.emergency_stopped ? "emergency stopped" : "running"} /></dd></div>
        <div><dt>Active jobs</dt><dd>{totalActive(data)}</dd></div>
        <div><dt>Worker heartbeat</dt><dd>{data.worker_heartbeat_age_seconds === null ? "no heartbeat recorded" : `${Math.round(data.worker_heartbeat_age_seconds)}s ago`}</dd></div>
        <div><dt>Budget today</dt><dd>{cents(Number(data.budget?.daily_used_cents ?? 0))} of {cents(Number(data.budget?.daily_ceiling_cents ?? 0))}</dd></div>
      </dl>}</DataState>
    </section>
    <section className="panel" aria-labelledby="failures-title">
      <h2 id="failures-title">Dead-lettered jobs</h2>
      <DataState resource={failures} empty="No dead-lettered jobs.">{(jobs) => <ul className="rows">{jobs.slice(0, 5).map((job) =>
        <li key={job.id}><Badge value={job.status} /> {job.kind} · {job.error_class ?? "error"}: {job.last_error ?? "—"}</li>)}
        <li><Link href="/agents">Open job console</Link></li></ul>}</DataState>
    </section>
  </>;
}
