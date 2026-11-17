"use client";
import { FormEvent, useEffect, useState } from "react";

type Overview = { environment: string; demo_mode: boolean; counts: Record<string, number>; revenue_cents: number; expense_cents: number };
type Source = { has_media: boolean; id: number; platform: string; title: string; url: string; status: string };
type Clip = { id: number; source_id: number; start_seconds: number; end_seconds: number; description: string; hook: string; score: number; status: string };
const api = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
async function apiFetch(url: string, options: RequestInit = {}) {
  const headers = new Headers(options.headers);
  const csrf = document.cookie.split("; ").find((row) => row.startsWith("soe_csrf="))?.split("=").slice(1).join("=");
  if (csrf && ["POST", "PUT", "DELETE", "PATCH"].includes(options.method ?? "GET")) headers.set("X-CSRF-Token", decodeURIComponent(csrf));
  const response = await fetch(url, { ...options, headers, credentials: "include" });
  if (!response.ok && (options.method ?? "GET") === "GET") throw new Error("Unable to load API data.");
  return response;
}
const sections = ["Overview", "Discovery", "Content library", "AI studio", "Publishing", "Analytics", "Revenue", "AI agents", "Settings"];

export default function Home() {
  const [section, setSection] = useState("Overview");
  const [data, setData] = useState<Overview | null>(null);
  const [sources, setSources] = useState<Source[]>([]);
  const [clips, setClips] = useState<Clip[]>([]);
  const [query, setQuery] = useState("");
  const [title, setTitle] = useState("Authorized upload");
  const [file, setFile] = useState<File | null>(null);
  const [selectedSource, setSelectedSource] = useState<number | null>(null);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [panelData, setPanelData] = useState<unknown>(null);
  const refresh = async () => { try { const [overview, sourceRows, clipRows] = await Promise.all([apiFetch(`${api}/api/v1/overview`).then((r) => r.json()), apiFetch(`${api}/api/v1/sources`).then((r) => r.json()), apiFetch(`${api}/api/v1/clips`).then((r) => r.json())]); setData(overview); setSources(sourceRows); setClips(clipRows); } catch { setError("Unable to load API data. Sign in or check the API connection."); } };
  useEffect(() => { refresh(); }, []);
  useEffect(() => { const routes: Record<string, string> = { Publishing: "/api/v1/publications", Analytics: "/api/v1/analytics", Revenue: "/api/v1/revenue", "AI agents": "/api/v1/agents", Settings: "/api/v1/settings" }; if (routes[section]) apiFetch(`${api}${routes[section]}`).then((r) => r.json()).then(setPanelData).catch(() => setError(`Unable to load ${section}.`)); }, [section]);
  const discover = async (event: FormEvent) => { event.preventDefault(); setError(""); const result = await apiFetch(`${api}/api/v1/discovery/search?platform=youtube&query=${encodeURIComponent(query)}`, { method: "POST" }); const body = await result.json(); if (!result.ok) setError(body.detail ?? "Discovery failed"); else { setMessage(`${body.created} new sources recorded by ${body.provider}.`); refresh(); } };
  const upload = async (event: FormEvent) => { event.preventDefault(); if (!file) return; const form = new FormData(); form.append("file", file); form.append("title", title); const result = await apiFetch(`${api}/api/v1/sources/upload`, { method: "POST", body: form }); const body = await result.json(); if (!result.ok) setError(body.detail ?? "Upload failed"); else { setSelectedSource(body.source_id); setMessage(`Upload accepted as source ${body.source_id}. Add rights approval before processing.`); refresh(); } };
  const approveRights = async () => { if (!selectedSource) return; const result = await apiFetch(`${api}/api/v1/sources/${selectedSource}/rights`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ authorization_type: "owned", commercial_use: true, publishing_allowed: true, allowed_platforms: ["youtube"], evidence: "Original locally generated test media; operator-approved." }) }); const body = await result.json(); if (!result.ok) setError(body.detail ?? "Rights approval failed"); else { setMessage(`Rights approved for source ${body.source_id}.`); refresh(); } };
  const process = async () => {
    if (!selectedSource) return;
    setError("");
    setMessage("Queuing processing…");
    try {
      const response = await apiFetch(`${api}/api/v1/sources/${selectedSource}/process`, { method: "POST" });
      const queued = await response.json();
      if (!response.ok) throw new Error(queued.detail ?? "Processing failed");
      for (;;) {
        const statusResponse = await apiFetch(`${api}/api/v1/jobs/${queued.job_id}`);
        const job = await statusResponse.json();
        if (!statusResponse.ok) throw new Error(job.detail ?? "Could not check job status");
        setMessage(`Processing: ${job.status}`);
        if (job.status === "failed") { await refresh(); throw new Error(job.last_error ?? "Processing failed"); }
        if (job.status === "succeeded") {
          setMessage(`${job.result.clips_generated} clips rendered from ${job.result.transcript_segments} transcript segments.`);
          await refresh();
          break;
        }
        await new Promise((resolve) => setTimeout(resolve, 2000));
      }
    } catch (error) { setError(error instanceof Error ? error.message : "Processing failed"); }
  };
  const signIn = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    try {
      const response = await apiFetch(`${api}/api/v1/auth/login`, { method: "POST", body: form });
      if (!response.ok) { setError("Sign in failed. Check credentials or try again later."); return; }
      setError(""); refresh();
    } catch { setError("Sign in failed. Check credentials or try again later."); }
  };
  const signOut = async () => {
    try {
      const response = await apiFetch(`${api}/api/v1/auth/logout`, { method: "POST" });
      if (!response.ok) { setError("Sign out failed."); return; }
      setData(null); setSources([]); setClips([]); setPanelData(null); setMessage("Signed out.");
    } catch { setError("Sign out failed."); }
  };
  const cards = data ? [["Sources discovered", data.counts.sources], ["Clips generated", data.counts.clips], ["Workflow runs", data.counts.workflows], ["Verified revenue", `$${(data.revenue_cents / 100).toFixed(2)}`]] : [];
  return <main><aside><div className="brand"><span className="orb" />SOE</div><p className="muted">AUTONOMOUS CONTENT OPS</p>{sections.map((item) => <button className={`nav ${section === item ? "active" : ""}`} onClick={() => setSection(item)} key={item}>{item}</button>)}</aside><section><header><div><p className="eyebrow">OPERATIONS CENTER / {section.toUpperCase()}</p><h1>{section === "Overview" ? "Good morning, operator." : section}</h1><p className="muted">{data?.demo_mode ? "Development mode — fixture data is labeled." : "Connected production data."}</p></div><div className="status"><span className="dot" /> {data?.demo_mode ? "DEVELOPMENT MODE" : "LIVE"}</div></header><form onSubmit={signIn} className="search"><input name="username" autoComplete="username" placeholder="Username" required maxLength={255} /><input name="password" type="password" autoComplete="current-password" placeholder="Password" required maxLength={1024} /><button type="submit">Sign in</button><button type="button" onClick={signOut}>Sign out</button></form>{error && <div className="alert error">{error}</div>}{message && <div className="alert">{message}</div>}{section === "Overview" && <><div className="grid">{cards.map(([label, value]) => <div className="card" key={String(label)}><p className="muted">{label}</p><strong>{value}</strong><small>{data?.demo_mode ? "No production data connected" : "Verified source"}</small></div>)}</div><Pipeline /></>}{section === "Discovery" && <Discovery query={query} setQuery={setQuery} discover={discover} sources={sources} />}{section === "AI studio" && <Studio title={title} setTitle={setTitle} file={file} setFile={setFile} upload={upload} sources={sources} selectedSource={selectedSource} setSelectedSource={setSelectedSource} approveRights={approveRights} process={process} clips={clips} />}{!["Overview", "Discovery", "AI studio"].includes(section) && <ServicePanel section={section} data={panelData} />}</section></main>;
}

function ServicePanel({ section, data }: { section: string; data: unknown }) { return <div className="panel"><p className="eyebrow">LIVE SERVICE DATA</p><h2>{section}</h2>{data ? <pre className="data">{JSON.stringify(data, null, 2)}</pre> : <p className="muted">Loading persisted data…</p>}</div>; }

function Discovery({ query, setQuery, discover, sources }: { query: string; setQuery: (x: string) => void; discover: (e: FormEvent) => void; sources: Source[] }) { return <div className="panel"><p className="eyebrow">SOURCE DISCOVERY</p><h2>Search authorized platforms</h2><form onSubmit={discover} className="search"><input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Topic or niche" required /><button type="submit">Discover</button></form><div className="source-list">{sources.length ? sources.map((source) => <div className="source" key={source.id}><div><b>{source.title}</b><small>{source.platform} · {source.status}</small></div><a href={source.url} target="_blank">Inspect source ↗</a></div>) : <p className="muted">No sources recorded yet.</p>}</div></div>; }

function Studio({ title, setTitle, file, setFile, upload, sources, selectedSource, setSelectedSource, approveRights, process, clips }: { title: string; setTitle: (x: string) => void; file: File | null; setFile: (x: File | null) => void; upload: (e: FormEvent) => void; sources: Source[]; selectedSource: number | null; setSelectedSource: (x: number) => void; approveRights: () => void; process: () => void; clips: Clip[] }) { return <><div className="panel"><p className="eyebrow">AUTHORIZED MEDIA → AI PIPELINE</p><h2>Process a source video</h2><form onSubmit={upload} className="upload"><input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Source title" required /><input type="file" accept="video/mp4,video/quicktime,video/webm" onChange={(e) => setFile(e.target.files?.[0] ?? null)} required /><button type="submit">Upload source</button></form><div className="source-list">{sources.filter((s) => s.platform === "upload").map((source) => <button className={`source selectable ${selectedSource === source.id ? "selected" : ""}`} onClick={() => setSelectedSource(source.id)} key={source.id}><span><b>{source.title}</b><small>Source {source.id} · {source.status}</small></span><span>{selectedSource === source.id ? "Selected" : "Select"}</span></button>)}</div><div className="actions"><button disabled={!selectedSource} onClick={approveRights}>Approve owned rights</button><button disabled={!selectedSource} onClick={process}>Start AI processing</button></div></div><div className="panel"><div className="panel-title"><div><p className="eyebrow">RENDERED OUTPUTS</p><h2>Highlight candidates</h2></div><span className="pill">EVIDENCE-BACKED</span></div>{clips.length ? clips.filter((clip) => !selectedSource || clip.source_id === selectedSource).map((clip) => <div className="clip" key={clip.id}><div><b>#{clip.id} · {clip.hook}</b><small>{clip.start_seconds.toFixed(2)}s–{clip.end_seconds.toFixed(2)}s · score {clip.score.toFixed(1)} · {clip.status}</small><p>{clip.description}</p></div><a href={`${api}/api/v1/clips/${clip.id}/download`}>Download MP4</a></div>) : <p className="muted">No rendered clips yet.</p>}</div></>; }

function Pipeline() { return <div className="panel"><div className="panel-title"><div><p className="eyebrow">PIPELINE</p><h2>Autonomous workflow</h2></div><span className="pill">SAFE MODE</span></div><div className="pipeline">{["DISCOVER", "RIGHTS CHECK", "ANALYZE", "HIGHLIGHTS", "EDIT", "APPROVE", "PUBLISH"].map((step, i) => <div className="step" key={step}><span>{String(i + 1).padStart(2, "0")}</span><b>{step}</b></div>)}</div><p className="note">Publishing stays disabled until a rights record and authorized platform account are present.</p></div>; }
