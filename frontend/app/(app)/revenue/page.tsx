"use client";
import { FormEvent } from "react";
import { api, cents, formatDate, request } from "../../lib/api";
import { useResource } from "../../lib/useResource";
import { Badge, DataState, Message, useAction } from "../../components/ui";
import { PLATFORM_LABEL } from "../../lib/types";

type Entry = { id: number; kind: string; category: string; cents: number; currency: string; provenance: string; platform: string | null;
  reference: string | null; note: string | null; created_at: string };
type PlatformEarnings = { status: "available" | "unavailable"; cents: number | null; reason?: string; provenance?: string[] };
export type Summary = { currency: string; revenue_cents: { verified_platform: number; imported: number; manual: number; total: number };
  cost_cents: { actual: number; estimated: number; total: number }; net_cents: number; net_label: string; excluded_revenue_cents: number;
  platform_earnings: Record<string, PlatformEarnings>; other_currencies_not_converted: string[] };
type Revenue = { entries: Entry[]; summary: Summary };

const REVENUE_CATEGORIES = ["sponsorship", "affiliate", "platform_earnings", "other"];
const COST_CATEGORIES = ["processing_cost", "cloud_cost", "other"];

export default function RevenuePage() {
  const revenue = useResource<Revenue>("/api/v1/revenue");
  const action = useAction(revenue.reload);
  async function addEntry(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const kind = String(form.get("kind"));
    const amount = Number(form.get("amount"));
    await action.run(() => request("/api/v1/revenue/entries", "POST", {
      kind, category: form.get("category"), cents: Math.round(amount * 100), currency: "USD",
      provenance: kind === "expense" && form.get("estimated") === "on" ? "estimated" : "manual",
      platform: form.get("platform") || undefined, reference: form.get("reference"),
    }), "Ledger entry recorded.");
  }
  async function importCsv(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    await action.run(async () => {
      const result = await request<{ rows: number }>("/api/v1/revenue/import", "POST", form);
      action.setMessage(`Imported ${result.rows} rows.`);
    }, "Statement imported.");
  }
  return <>
    <header className="page-head"><p className="eyebrow">MONETIZATION</p><h1>Revenue</h1>
      <p className="muted">Every amount carries its provenance. Estimates are costs only and never count as revenue. Earnings a platform does not expose are shown as unavailable, never as $0.</p></header>
    <Message text={action.message} error={action.error} />
    <DataState resource={revenue} empty="">{({ summary, entries }) => <>
      <div className="grid">
        <div className="card"><p className="muted">Revenue (verified + imported + manual)</p><strong>{cents(summary.revenue_cents.total)}</strong>
          <small>verified {cents(summary.revenue_cents.verified_platform)} · imported {cents(summary.revenue_cents.imported)} · manual {cents(summary.revenue_cents.manual)}</small></div>
        <div className="card"><p className="muted">Actual costs</p><strong>{cents(summary.cost_cents.actual)}</strong></div>
        <div className="card"><p className="muted">Estimated costs</p><strong>{cents(summary.cost_cents.estimated)}</strong><small>estimates, not invoices</small></div>
        <div className="card"><p className="muted">Net</p><strong>{cents(summary.net_cents)}</strong><small>{summary.net_label}</small></div>
      </div>
      {summary.other_currencies_not_converted.length > 0 && <p className="alert">Entries in {summary.other_currencies_not_converted.join(", ")} are listed below but not converted or totalled.</p>}
      <section className="panel" aria-labelledby="platform-title"><h2 id="platform-title">Platform earnings</h2>
        <ul className="rows">{Object.entries(summary.platform_earnings).map(([platform, item]) => <li key={platform}>
          <b>{PLATFORM_LABEL[platform] ?? platform}</b>
          {item.status === "available" ? <>{cents(item.cents)} <Badge value={(item.provenance ?? []).join(", ")} /></> : <><Badge value="unavailable" /> <span className="muted">{item.reason}</span></>}
        </li>)}</ul>
      </section>
      <section className="panel" aria-labelledby="ledger-title"><h2 id="ledger-title">Ledger</h2>
        <p><a href={`${api}/api/v1/revenue/export.csv`}>Export CSV</a></p>
        {entries.length === 0 ? <p className="empty-state">No ledger entries yet.</p> :
          <div className="table-wrap" tabIndex={0} role="region" aria-label="Scrollable table"><table><thead><tr><th scope="col">Date</th><th scope="col">Kind</th><th scope="col">Category</th><th scope="col">Amount</th><th scope="col">Provenance</th><th scope="col">Reference</th></tr></thead>
            <tbody>{entries.map((entry) => <tr key={entry.id}><td>{formatDate(entry.created_at)}</td><td>{entry.kind}</td><td>{entry.category.replaceAll("_", " ")}{entry.platform ? ` · ${PLATFORM_LABEL[entry.platform]}` : ""}</td>
              <td>{entry.currency === "USD" ? cents(entry.cents) : `${(entry.cents / 100).toFixed(2)} ${entry.currency}`}</td>
              <td><Badge value={entry.provenance} /></td><td>{entry.reference ?? entry.note ?? "—"}</td></tr>)}</tbody></table></div>}
      </section>
    </>}</DataState>
    <section className="panel" aria-labelledby="manual-title"><h2 id="manual-title">Record an entry</h2>
      <form className="row-form" onSubmit={addEntry}>
        <label>Kind <select name="kind" defaultValue="revenue"><option value="revenue">Revenue</option><option value="expense">Cost</option></select></label>
        <label>Category <select name="category">{[...new Set([...REVENUE_CATEGORIES, ...COST_CATEGORIES])].map((value) => <option key={value} value={value}>{value.replaceAll("_", " ")}</option>)}</select></label>
        <label>Amount (USD) <input name="amount" type="number" min="0.01" step="0.01" required /></label>
        <label>Platform <select name="platform" defaultValue=""><option value="">none</option><option value="youtube">YouTube</option><option value="tiktok">TikTok</option><option value="instagram">Instagram</option></select></label>
        <label>Reference <input name="reference" required minLength={3} maxLength={500} placeholder="invoice / statement id" /></label>
        <label className="check"><input type="checkbox" name="estimated" /> Cost is an estimate</label>
        <button type="submit" disabled={action.busy}>Record</button>
      </form>
    </section>
    <section className="panel" aria-labelledby="import-title"><h2 id="import-title">Import a statement (CSV)</h2>
      <p className="muted">Header: date,kind,category,amount,currency,platform,reference. Max 1 MB / 5000 rows. Re-importing the same file is rejected.</p>
      <form className="row-form" onSubmit={importCsv}>
        <label>CSV file <input name="file" type="file" accept=".csv,text/csv" required /></label>
        <button type="submit" disabled={action.busy}>Import</button>
      </form>
    </section>
  </>;
}
