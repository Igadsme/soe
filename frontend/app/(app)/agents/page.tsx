"use client";
import { useState } from "react";
import { formatDate, request } from "../../lib/api";
import { useResource } from "../../lib/useResource";
import { Badge, DataState, Message, useAction } from "../../components/ui";
import type { AuditEvent, Job } from "../../lib/types";

type Agent = { name: string; status: string; detail: string; queued_workflows: number };
const STATUSES = ["", "queued", "running", "cancel_requested", "succeeded", "failed", "dead_letter", "cancelled"];
const KINDS = ["", "process_source", "render_clip", "quality_check", "discovery", "publish", "analytics", "optimize"];

export default function AgentsPage() {
  const agents = useResource<Agent[]>("/api/v1/agents");
  const [status, setStatus] = useState("");
  const [kind, setKind] = useState("");
  const params = new URLSearchParams(Object.entries({ status, kind }).filter(([, value]) => value)).toString();
  const jobs = useResource<Job[]>(`/api/v1/jobs${params ? `?${params}` : ""}`, 5000);
  const audit = useResource<AuditEvent[]>("/api/v1/audit?limit=50");
  const action = useAction(async () => { await Promise.all([jobs.reload(), audit.reload()]); });
  return <>
    <header className="page-head"><p className="eyebrow">ORCHESTRATION</p><h1>AI agents</h1></header>
    <Message text={action.message} error={action.error} />
    <section className="panel" aria-labelledby="agents-title">
      <h2 id="agents-title">Agent readiness</h2>
      <DataState resource={agents} empty="No agents reported.">{(rows) => <div className="grid">{rows.map((agent) =>
        <div className="card" key={agent.name}><h3>{agent.name}</h3><Badge value={agent.status} /><p className="muted">{agent.detail}</p></div>)}</div>}</DataState>
    </section>
    <section className="panel" aria-labelledby="jobs-title">
      <h2 id="jobs-title">Jobs</h2>
      <div className="row-form">
        <label>Status <select value={status} onChange={(e) => setStatus(e.target.value)}>{STATUSES.map((value) => <option key={value} value={value}>{value ? value.replaceAll("_", " ") : "any"}</option>)}</select></label>
        <label>Kind <select value={kind} onChange={(e) => setKind(e.target.value)}>{KINDS.map((value) => <option key={value} value={value}>{value ? value.replaceAll("_", " ") : "any"}</option>)}</select></label>
      </div>
      <DataState resource={jobs} empty="No jobs match these filters.">{(rows) => <div className="table-wrap" tabIndex={0} role="region" aria-label="Scrollable table"><table>
        <thead><tr><th scope="col">Kind</th><th scope="col">Status</th><th scope="col">Attempts</th><th scope="col">Created</th><th scope="col">Detail</th><th scope="col">Actions</th></tr></thead>
        <tbody>{rows.map((job) => <tr key={job.id}>
          <td>{job.kind}{job.source_id ? ` · source ${job.source_id}` : ""}</td><td><Badge value={job.status} /></td><td>{job.attempts}/{job.max_attempts}</td>
          <td>{formatDate(job.created_at)}</td><td>{job.last_error ? `${job.error_class ?? "error"}: ${job.last_error}` : job.wait_reason ?? "—"}</td>
          <td className="actions">
            {["queued", "running"].includes(job.status) && <button type="button" onClick={() => action.run(() => request(`/api/v1/jobs/${job.id}/cancel`, "POST"), "Cancellation requested.")}>Cancel</button>}
            {["dead_letter", "failed", "cancelled"].includes(job.status) && <button type="button" onClick={() => action.run(() => request(`/api/v1/jobs/${job.id}/replay`, "POST"), "Job replayed.")}>Replay</button>}
          </td></tr>)}</tbody></table></div>}</DataState>
    </section>
    <section className="panel" aria-labelledby="audit-title">
      <h2 id="audit-title">Audit trail</h2>
      <DataState resource={audit} empty="No audit events yet.">{(rows) => <ol className="events">{rows.map((row) =>
        <li key={row.id}>{formatDate(row.created_at)} · <b>{row.actor}</b> {row.action} {row.entity} {row.entity_id}</li>)}</ol>}</DataState>
    </section>
  </>;
}
