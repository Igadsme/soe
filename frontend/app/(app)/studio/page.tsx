"use client";
import { FormEvent, useState } from "react";
import { request } from "../../lib/api";
import { pollJob } from "../../lib/pollJob";
import { uploadWithProgress } from "../../lib/upload";
import { useResource } from "../../lib/useResource";
import { Badge, DataState, Message, useAction } from "../../components/ui";
import { ClipCard } from "../../components/ClipCard";
import { RightsForm } from "../../components/RightsForm";
import type { Clip, Source } from "../../lib/types";

export default function StudioPage() {
  const sources = useResource<Source[]>("/api/v1/sources");
  const clips = useResource<Clip[]>("/api/v1/clips", 5000);
  const reload = async () => { await Promise.all([sources.reload(), clips.reload()]); };
  const action = useAction(reload);
  const [selected, setSelected] = useState<number | null>(null);
  const [progress, setProgress] = useState<number | null>(null);
  const [processing, setProcessing] = useState(false);
  async function upload(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const file = form.get("file");
    if (!(file instanceof File) || !file.size) { action.setMessage("Choose a video file."); return; }
    setProgress(0);
    await action.run(async () => {
      const result = await uploadWithProgress(file, String(form.get("title")), setProgress);
      setSelected(result.source_id);
    }, "Upload complete. Record rights authorization before processing.");
    setProgress(null);
  }
  async function process() {
    if (!selected) return;
    setProcessing(true);
    await action.run(async () => {
      const queued = await request<{ job_id: string }>(`/api/v1/sources/${selected}/process`, "POST");
      const job = await pollJob(queued.job_id, { onStatus: (state) => action.setMessage(`Processing: ${state.status}`) });
      if (job.status !== "succeeded") throw new Error(`Processing ${job.status}: ${job.last_error ?? "no details"}`);
    }, "Processing finished. Review the clips below.");
    setProcessing(false);
  }
  const uploads = (sources.data ?? []).filter((source) => source.status !== "archived");
  const sourceClips = (clips.data ?? []).filter((clip) => clip.source_id === selected);
  const current = uploads.find((source) => source.id === selected);
  return <>
    <header className="page-head"><p className="eyebrow">AUTHORIZED MEDIA → CLIPS</p><h1>AI studio</h1></header>
    <Message text={action.message} error={action.error} />
    <section className="panel" aria-labelledby="upload-title">
      <h2 id="upload-title">1. Upload a source video</h2>
      <form className="row-form" onSubmit={upload}>
        <label>Title <input name="title" required maxLength={500} defaultValue="" /></label>
        <label>Video file <input name="file" type="file" accept="video/mp4,video/quicktime,video/webm" required /></label>
        <button type="submit" disabled={progress !== null}>Upload</button>
      </form>
      {progress !== null && <progress max={1} value={progress} aria-label="Upload progress">{Math.round(progress * 100)}%</progress>}
    </section>
    <section className="panel" aria-labelledby="pick-title">
      <h2 id="pick-title">2. Choose a source</h2>
      <DataState resource={sources} empty="No sources yet." isEmpty={() => uploads.length === 0}>{() =>
        <div className="source-list" role="radiogroup" aria-label="Sources">{uploads.map((source) =>
          <button type="button" role="radio" aria-checked={selected === source.id} key={source.id} className={`source selectable${selected === source.id ? " selected" : ""}`} onClick={() => setSelected(source.id)}>
            <span><b>{source.title}</b> <small>#{source.id} · {source.platform}</small></span><Badge value={source.status} />
          </button>)}</div>}</DataState>
    </section>
    {current && <section className="panel" aria-labelledby="rights-title">
      <h2 id="rights-title">3. Rights authorization</h2>
      <p className="muted">Processing requires an unexpired, unrevoked record that allows commercial use and publishing.</p>
      <RightsForm key={current.id} sourceId={current.id} onApproved={() => { action.setMessage("Rights recorded."); reload(); }} />
      <div className="actions"><button type="button" onClick={process} disabled={processing || !current.has_media}>{processing ? "Processing…" : "Start AI processing"}</button>
        {!current.has_media && <span className="muted">This source has no media attached yet.</span>}</div>
    </section>}
    {current && <section className="panel" aria-labelledby="review-title">
      <h2 id="review-title">4. Review clips</h2>
      <DataState resource={clips} empty="No clips for this source yet." isEmpty={() => sourceClips.length === 0}>{() =>
        <div className="clip-grid">{sourceClips.map((clip) => <ClipCard key={clip.id} clip={clip} run={action.run} editable />)}</div>}</DataState>
    </section>}
  </>;
}
