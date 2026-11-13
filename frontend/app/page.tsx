"use client";
import { useEffect, useState } from "react";

type Overview = { environment: string; demo_mode: boolean; counts: Record<string, number>; revenue_cents: number; expense_cents: number };
const api = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export default function Home() {
  const [data, setData] = useState<Overview | null>(null);
  useEffect(() => { fetch(`${api}/api/v1/overview`).then((r) => r.json()).then(setData).catch(() => setData(null)); }, []);
  const cards = data ? [["Sources discovered", data.counts.sources], ["Clips generated", data.counts.clips], ["Workflow runs", data.counts.workflows], ["Verified revenue", `$${(data.revenue_cents / 100).toFixed(2)}`]] : [];
  return <main><aside><div className="brand"><span className="orb" />SOE</div><p className="muted">AUTONOMOUS CONTENT OPS</p>{["Overview","Discovery","Content library","AI studio","Publishing","Analytics","Revenue","AI agents","Settings"].map((item, i) => <div className={`nav ${i === 0 ? "active" : ""}`} key={item}>{item}</div>)}</aside><section><header><div><p className="eyebrow">OPERATIONS CENTER / OVERVIEW</p><h1>Good morning, operator.</h1><p className="muted">Your content intelligence system at a glance.</p></div><div className="status"><span className="dot" /> {data?.demo_mode ? "DEVELOPMENT MODE" : "LIVE"}</div></header><div className="grid">{cards.map(([label, value]) => <div className="card" key={String(label)}><p className="muted">{label}</p><strong>{value}</strong><small>Awaiting connected data</small></div>)}</div><div className="panel"><div className="panel-title"><div><p className="eyebrow">PIPELINE</p><h2>Autonomous workflow</h2></div><span className="pill">SAFE MODE</span></div><div className="pipeline">{["DISCOVER","RIGHTS CHECK","ANALYZE","HIGHLIGHTS","EDIT","APPROVE","PUBLISH"].map((step, i) => <div className="step" key={step}><span>{String(i + 1).padStart(2, "0")}</span><b>{step}</b></div>)}</div><p className="note">No publishing or monetization is active until an authorized account and rights record are configured.</p></div></section></main>;
}
