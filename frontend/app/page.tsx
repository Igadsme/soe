"use client";
import { FormEvent, useEffect, useState } from "react";

type Overview = { environment: string; demo_mode: boolean; counts: Record<string, number>; revenue_cents: number; expense_cents: number };
type Source = { id: number; platform: string; title: string; url: string; status: string };
const api = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const sections = ["Overview", "Discovery", "Content library", "AI studio", "Publishing", "Analytics", "Revenue", "AI agents", "Settings"];

export default function Home() {
  const [section, setSection] = useState("Overview");
  const [data, setData] = useState<Overview | null>(null);
  const [sources, setSources] = useState<Source[]>([]);
  const [query, setQuery] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const refresh = () => Promise.all([fetch(`${api}/api/v1/overview`).then((r) => r.json()), fetch(`${api}/api/v1/sources`).then((r) => r.json())]).then(([overview, sourceRows]) => { setData(overview); setSources(sourceRows); }).catch(() => setError("API unavailable. Start the backend on port 8000."));
  useEffect(() => { refresh(); }, []);
  const discover = async (event: FormEvent) => { event.preventDefault(); setError(""); const result = await fetch(`${api}/api/v1/discovery/search?platform=youtube&query=${encodeURIComponent(query)}`, { method: "POST" }); const body = await result.json(); if (!result.ok) setError(body.detail ?? "Discovery failed"); else { setMessage(`${body.created} new sources recorded by ${body.provider}.`); refresh(); } };
  const cards = data ? [["Sources discovered", data.counts.sources], ["Clips generated", data.counts.clips], ["Workflow runs", data.counts.workflows], ["Verified revenue", `$${(data.revenue_cents / 100).toFixed(2)}`]] : [];
  return <main><aside><div className="brand"><span className="orb" />SOE</div><p className="muted">AUTONOMOUS CONTENT OPS</p>{sections.map((item) => <button className={`nav ${section === item ? "active" : ""}`} onClick={() => setSection(item)} key={item}>{item}</button>)}</aside><section><header><div><p className="eyebrow">OPERATIONS CENTER / {section.toUpperCase()}</p><h1>{section === "Overview" ? "Good morning, operator." : section}</h1><p className="muted">{data?.demo_mode ? "Development mode — fixture data is labeled." : "Connected production data."}</p></div><div className="status"><span className="dot" /> {data?.demo_mode ? "DEVELOPMENT MODE" : "LIVE"}</div></header>{error && <div className="alert error">{error}</div>}{message && <div className="alert">{message}</div>}{section === "Overview" && <><div className="grid">{cards.map(([label, value]) => <div className="card" key={String(label)}><p className="muted">{label}</p><strong>{value}</strong><small>{data?.demo_mode ? "No production data connected" : "Verified source"}</small></div>)}</div><Pipeline /></>}{section === "Discovery" && <div className="panel"><p className="eyebrow">SOURCE DISCOVERY</p><h2>Search authorized platforms</h2><form onSubmit={discover} className="search"><input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Topic or niche" required /><button type="submit">Discover</button></form><div className="source-list">{sources.length ? sources.map((source) => <div className="source" key={source.id}><div><b>{source.title}</b><small>{source.platform} · {source.status}</small></div><a href={source.url} target="_blank">Inspect source ↗</a></div>) : <p className="muted">No sources recorded yet.</p>}</div></div>}{!(["Overview", "Discovery"].includes(section)) && <div className="panel empty"><h2>{section} is connected to the service boundary.</h2><p className="muted">This surface is intentionally empty until its provider, rights, or account integration is configured. No fake metrics are shown.</p></div>}</section></main>;
}

function Pipeline() { return <div className="panel"><div className="panel-title"><div><p className="eyebrow">PIPELINE</p><h2>Autonomous workflow</h2></div><span className="pill">SAFE MODE</span></div><div className="pipeline">{["DISCOVER", "RIGHTS CHECK", "ANALYZE", "HIGHLIGHTS", "EDIT", "APPROVE", "PUBLISH"].map((step, i) => <div className="step" key={step}><span>{String(i + 1).padStart(2, "0")}</span><b>{step}</b></div>)}</div><p className="note">Publishing stays disabled until a rights record and authorized platform account are present.</p></div>; }
