"use client";
import { ReactNode, useState } from "react";
import type { Resource } from "../lib/useResource";

export function DataState<T>({ resource, empty, isEmpty, children }: {
  resource: Resource<T>; empty: string; isEmpty?: (data: T) => boolean; children: (data: T) => ReactNode;
}) {
  if (resource.loading && resource.data === null) return <p className="muted" role="status">Loading…</p>;
  if (resource.error && resource.data === null) return <p className="alert error" role="alert">{resource.error}</p>;
  if (resource.data === null) return null;
  const blank = isEmpty ? isEmpty(resource.data) : Array.isArray(resource.data) && resource.data.length === 0;
  return <>{resource.error && <p className="alert error" role="alert">{resource.error}</p>}{blank ? <p className="empty-state">{empty}</p> : children(resource.data)}</>;
}

export function Badge({ value }: { value: string | null | undefined }) {
  const text = value ?? "unknown";
  const tone = /fail|dead|revoked|error|blocked|rejected/.test(text) ? "bad" : /succeeded|approved|connected|authorized|ready|ok/.test(text) ? "good" : "neutral";
  return <span className={`badge ${tone}`}>{text.replaceAll("_", " ")}</span>;
}

export function Message({ text, error }: { text: string; error?: boolean }) {
  return <div aria-live="polite">{text && <p className={`alert${error ? " error" : ""}`} role={error ? "alert" : "status"}>{text}</p>}</div>;
}

export function ConfirmButton({ label, confirm, onConfirm, danger }: { label: string; confirm: string; onConfirm: () => void | Promise<void>; danger?: boolean }) {
  const [asking, setAsking] = useState(false);
  if (!asking) return <button type="button" className={danger ? "danger" : ""} onClick={() => setAsking(true)}>{label}</button>;
  return <span role="group" aria-label={confirm} className="confirm">
    <span>{confirm}</span>
    <button type="button" className="danger" onClick={async () => { setAsking(false); await onConfirm(); }}>Confirm</button>
    <button type="button" onClick={() => setAsking(false)}>Cancel</button>
  </span>;
}

export function useAction(onDone?: () => void | Promise<void>) {
  const [message, setMessage] = useState("");
  const [error, setError] = useState(false);
  const [busy, setBusy] = useState(false);
  async function run(action: () => Promise<unknown>, success: string) {
    setBusy(true); setError(false); setMessage("");
    try { await action(); setError(false); setMessage(success); await onDone?.(); }
    catch (err) { setError(true); setMessage(err instanceof Error ? err.message : "Action failed"); }
    finally { setBusy(false); }
  }
  return { message, error, busy, run, setMessage };
}
