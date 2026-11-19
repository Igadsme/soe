"use client";
import { FormEvent, useState } from "react";
import { formatDate, request } from "../../lib/api";
import { useResource } from "../../lib/useResource";
import { Badge, ConfirmButton, DataState, Message, useAction } from "../../components/ui";
import { PLATFORM_LABEL, type Source } from "../../lib/types";

type Schedule = { id: number; platform: string; query: string; channel_allowlist: string[]; interval_minutes: number; enabled: boolean; max_results: number; last_run_at?: string | null; next_run_at?: string | null };

export default function DiscoveryPage() {
  const sources = useResource<Source[]>("/api/v1/sources");
  const schedules = useResource<Schedule[]>("/api/v1/discovery/schedules");
  const search = useAction(sources.reload);
  const scheduleAction = useAction(schedules.reload);
  const [platform, setPlatform] = useState("youtube");
  async function runSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const query = String(new FormData(event.currentTarget).get("query") ?? "");
    await search.run(async () => {
      const result = await request<{ created: number; provider: string }>(`/api/v1/discovery/search?platform=${platform}&query=${encodeURIComponent(query)}`, "POST");
      search.setMessage(`${result.created} new candidates from ${result.provider}.`);
    }, "Search complete. Candidates need a rights record before processing.");
  }
  async function addSchedule(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const allowlist = String(form.get("channel_allowlist") ?? "").split(",").map((item) => item.trim()).filter(Boolean);
    await scheduleAction.run(() => request("/api/v1/discovery/schedules", "POST", {
      platform: form.get("platform"), query: form.get("query"), channel_allowlist: allowlist,
      interval_minutes: Number(form.get("interval_minutes")), max_results: Number(form.get("max_results")),
    }), "Schedule created.");
  }
  const discovered = (sources.data ?? []).filter((source) => source.platform !== "upload");
  return <>
    <header className="page-head"><p className="eyebrow">CANDIDATES</p><h1>Discovery</h1>
      <p className="muted">Discovery finds candidates only. It never grants rights; every source needs a rights record before processing.</p></header>
    <section className="panel" aria-labelledby="search-title">
      <h2 id="search-title">Search official APIs</h2>
      <form className="row-form" onSubmit={runSearch}>
        <label>Platform <select value={platform} onChange={(e) => setPlatform(e.target.value)}><option value="youtube">YouTube</option><option value="twitch">Twitch</option></select></label>
        <label>Query <input name="query" required maxLength={200} /></label>
        <button type="submit" disabled={search.busy}>{search.busy ? "Searching…" : "Search"}</button>
      </form>
      <Message text={search.message} error={search.error} />
    </section>
    <section className="panel" aria-labelledby="candidates-title">
      <h2 id="candidates-title">Discovered candidates</h2>
      <DataState resource={sources} empty="No discovered candidates yet." isEmpty={() => discovered.length === 0}>{() =>
        <div className="table-wrap"><table><thead><tr><th scope="col">Title</th><th scope="col">Platform</th><th scope="col">Status</th><th scope="col">Found</th></tr></thead>
          <tbody>{discovered.map((source) => <tr key={source.id}>
            <td><a href={source.url} target="_blank" rel="noreferrer noopener">{source.title}</a></td><td>{PLATFORM_LABEL[source.platform] ?? source.platform}</td>
            <td><Badge value={source.status} /></td><td>{formatDate(source.created_at)}</td></tr>)}</tbody></table></div>}</DataState>
    </section>
    <section className="panel" aria-labelledby="schedules-title">
      <h2 id="schedules-title">Scheduled discovery</h2>
      <form className="row-form" onSubmit={addSchedule}>
        <label>Platform <select name="platform"><option value="youtube">YouTube</option><option value="twitch">Twitch</option></select></label>
        <label>Query <input name="query" required maxLength={200} /></label>
        <label>Channel allowlist <input name="channel_allowlist" placeholder="comma separated" /></label>
        <label>Every (minutes) <input name="interval_minutes" type="number" min={1} max={43200} defaultValue={60} required /></label>
        <label>Max results <input name="max_results" type="number" min={1} max={50} defaultValue={10} required /></label>
        <button type="submit" disabled={scheduleAction.busy}>Add schedule</button>
      </form>
      <Message text={scheduleAction.message} error={scheduleAction.error} />
      <DataState resource={schedules} empty="No discovery schedules.">{(rows) => <ul className="rows">{rows.map((row) => <li key={row.id}>
        <b>{PLATFORM_LABEL[row.platform]}</b> “{row.query}” every {row.interval_minutes} min · <Badge value={row.enabled ? "enabled" : "paused"} /> · next {formatDate(row.next_run_at)}
        <button type="button" onClick={() => scheduleAction.run(() => request(`/api/v1/discovery/schedules/${row.id}`, "PATCH", { enabled: !row.enabled }), row.enabled ? "Schedule paused." : "Schedule enabled.")}>{row.enabled ? "Pause" : "Enable"}</button>
        <ConfirmButton label="Delete" confirm="Delete this schedule?" danger onConfirm={() => scheduleAction.run(() => request(`/api/v1/discovery/schedules/${row.id}`, "DELETE"), "Schedule deleted.")} />
      </li>)}</ul>}</DataState>
    </section>
  </>;
}
