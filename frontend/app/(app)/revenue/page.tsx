"use client";
import { cents } from "../../lib/api";
import { useResource } from "../../lib/useResource";
import { Badge, DataState } from "../../components/ui";

type Entry = { id: number; kind: string; category: string; cents: number; verified: boolean; note: string | null; provenance?: string };
type Revenue = { entries: Entry[] };

export default function RevenuePage() {
  const revenue = useResource<Revenue>("/api/v1/revenue");
  return <>
    <header className="page-head"><p className="eyebrow">MONETIZATION</p><h1>Revenue</h1>
      <p className="muted">Revenue totals count verified entries only. Platform earnings that are not available from an API are shown as unavailable, never as $0.</p></header>
    <DataState resource={revenue} empty="No ledger entries. Platform earnings are unavailable until a verified source or import is recorded." isEmpty={(data) => data.entries.length === 0}>{(data) => {
      const verified = data.entries.filter((entry) => entry.verified);
      const sum = (kind: string) => verified.filter((entry) => entry.kind === kind).reduce((total, entry) => total + entry.cents, 0);
      return <>
        <div className="grid">
          <div className="card"><p className="muted">Verified revenue</p><strong>{cents(sum("revenue"))}</strong></div>
          <div className="card"><p className="muted">Verified expenses</p><strong>{cents(sum("expense"))}</strong></div>
        </div>
        <section className="panel" aria-labelledby="ledger-title"><h2 id="ledger-title">Ledger</h2>
          <div className="table-wrap" tabIndex={0} role="region" aria-label="Scrollable table"><table><thead><tr><th scope="col">Kind</th><th scope="col">Category</th><th scope="col">Amount</th><th scope="col">Provenance</th><th scope="col">Note</th></tr></thead>
            <tbody>{data.entries.map((entry) => <tr key={entry.id}><td>{entry.kind}</td><td>{entry.category}</td><td>{cents(entry.cents)}</td>
              <td><Badge value={entry.provenance ?? (entry.verified ? "verified" : "unverified")} /></td><td>{entry.note ?? "—"}</td></tr>)}</tbody></table></div>
        </section>
      </>;
    }}</DataState>
  </>;
}
