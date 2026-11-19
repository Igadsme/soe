"use client";
import { useState } from "react";
import { formatDate, request } from "../../lib/api";
import { useResource } from "../../lib/useResource";
import { Badge, ConfirmButton, DataState, Message, useAction } from "../../components/ui";
import { ClipCard } from "../../components/ClipCard";
import { PLATFORM_LABEL, type Clip, type Source } from "../../lib/types";

export default function LibraryPage() {
  const sources = useResource<Source[]>("/api/v1/sources");
  const clips = useResource<Clip[]>("/api/v1/clips");
  const reload = async () => { await Promise.all([sources.reload(), clips.reload()]); };
  const action = useAction(reload);
  const [clipStatus, setClipStatus] = useState("all");
  const [query, setQuery] = useState("");
  const visibleSources = (sources.data ?? []).filter((source) => source.status !== "archived" && source.title.toLowerCase().includes(query.toLowerCase()));
  const visibleClips = (clips.data ?? []).filter((clip) => clipStatus === "all" || clip.status === clipStatus);
  return <>
    <header className="page-head"><p className="eyebrow">ASSETS</p><h1>Content library</h1></header>
    <Message text={action.message} error={action.error} />
    <section className="panel" aria-labelledby="sources-title">
      <h2 id="sources-title">Sources</h2>
      <label>Filter by title <input type="search" value={query} onChange={(e) => setQuery(e.target.value)} /></label>
      <DataState resource={sources} empty="No sources yet. Upload one in AI studio." isEmpty={() => visibleSources.length === 0}>{() =>
        <div className="table-wrap" tabIndex={0} role="region" aria-label="Scrollable table"><table><thead><tr><th scope="col">Title</th><th scope="col">Origin</th><th scope="col">Status</th><th scope="col">Added</th><th scope="col">Actions</th></tr></thead>
          <tbody>{visibleSources.map((source) => <tr key={source.id}>
            <td>{source.title}</td><td>{PLATFORM_LABEL[source.platform] ?? source.platform}</td><td><Badge value={source.status} /></td><td>{formatDate(source.created_at)}</td>
            <td><ConfirmButton label="Archive" confirm={`Archive “${source.title}”?`} onConfirm={() => action.run(() => request(`/api/v1/sources/${source.id}/archive`, "POST"), "Source archived.")} /></td>
          </tr>)}</tbody></table></div>}</DataState>
    </section>
    <section className="panel" aria-labelledby="clips-title">
      <h2 id="clips-title">Clips</h2>
      <label>Status <select value={clipStatus} onChange={(e) => setClipStatus(e.target.value)}>
        {["all", "pending_review", "approved", "rejected", "qc_failed", "qc_pending", "rendering"].map((value) => <option key={value} value={value}>{value.replaceAll("_", " ")}</option>)}
      </select></label>
      <DataState resource={clips} empty="No clips match." isEmpty={() => visibleClips.length === 0}>{() =>
        <div className="clip-grid">{visibleClips.map((clip) => <ClipCard key={clip.id} clip={clip} run={action.run} />)}</div>}</DataState>
    </section>
  </>;
}
