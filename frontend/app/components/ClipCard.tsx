"use client";
import { FormEvent, useState } from "react";
import { api, request } from "../lib/api";
import type { Clip } from "../lib/types";
import { Badge, ConfirmButton } from "./ui";

export function clipPreviewUrl(clip: Clip) {
  return `${api}/api/v1/clips/${clip.id}/download?inline=true`;
}

export function ClipCard({ clip, onChange, run, editable }: {
  clip: Clip; editable?: boolean; onChange?: () => void;
  run: (action: () => Promise<unknown>, success: string) => Promise<void>;
}) {
  const [editing, setEditing] = useState(false);
  const evidence = clip.evidence ?? {};
  const qcReasons = Array.isArray(evidence.qc_reasons) ? (evidence.qc_reasons as string[]) : [];
  const transcript = typeof evidence.transcript === "string" ? evidence.transcript : "";
  async function rerender(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    await run(() => request(`/api/v1/clips/${clip.id}/rerender`, "POST", { start_seconds: Number(form.get("start")), end_seconds: Number(form.get("end")) }), `Re-render queued for clip ${clip.id}.`);
    setEditing(false); onChange?.();
  }
  return <article className="clip-card" aria-labelledby={`clip-${clip.id}`}>
    {clip.output_path
      ? <video controls preload="metadata" src={clipPreviewUrl(clip)} aria-label={`Preview of clip ${clip.id}`} crossOrigin="use-credentials" />
      : <div className="video-placeholder">Not rendered yet</div>}
    <div className="clip-body">
      <h3 id={`clip-${clip.id}`}>#{clip.id} · {clip.hook}</h3>
      <p className="muted">{clip.start_seconds.toFixed(2)}s–{clip.end_seconds.toFixed(2)}s · score {clip.score.toFixed(1)} · <Badge value={clip.status} /></p>
      <p>{clip.description}</p>
      {transcript && <details><summary>Transcript evidence</summary><p>{transcript}</p></details>}
      {qcReasons.length ? <p className="alert error">Quality check failed: {qcReasons.join("; ")}</p> : null}
      <div className="actions">
        {clip.output_path && <a href={`${api}/api/v1/clips/${clip.id}/download`}>Download MP4</a>}
        {editable && clip.status === "pending_review" && <button type="button" onClick={() => run(() => request(`/api/v1/clips/${clip.id}/approve`, "POST"), `Clip ${clip.id} approved.`)}>Approve</button>}
        {editable && ["pending_review", "approved", "qc_failed"].includes(clip.status) && <button type="button" onClick={() => run(() => request(`/api/v1/clips/${clip.id}/reject`, "POST"), `Clip ${clip.id} rejected.`)}>Reject</button>}
        {editable && <button type="button" aria-expanded={editing} onClick={() => setEditing(!editing)}>Adjust timing</button>}
        <ConfirmButton label="Delete" danger confirm={`Delete clip ${clip.id}?`} onConfirm={() => run(() => request(`/api/v1/clips/${clip.id}`, "DELETE"), `Clip ${clip.id} deleted.`)} />
      </div>
      {editing && <form className="row-form" onSubmit={rerender}>
        <label>Start (s) <input name="start" type="number" step="0.1" min={0} defaultValue={clip.start_seconds} required /></label>
        <label>End (s) <input name="end" type="number" step="0.1" min={0} defaultValue={clip.end_seconds} required /></label>
        <button type="submit">Re-render</button>
      </form>}
    </div>
  </article>;
}
