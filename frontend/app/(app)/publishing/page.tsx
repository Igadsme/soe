"use client";
import { FormEvent, useEffect, useState } from "react";
import { formatDate, request } from "../../lib/api";
import { publishPayload, validatePublish } from "../../lib/publishForm";
import { useResource } from "../../lib/useResource";
import { Badge, ConfirmButton, DataState, Message, useAction } from "../../components/ui";
import { PLATFORM_LABEL, PUBLISH_PLATFORMS, type Account, type AppSettings, type Clip, type Publication } from "../../lib/types";

type PublicationEvent = { from_status: string | null; to_status: string; actor: string; error_class: string | null; created_at: string };

function Events({ id }: { id: number }) {
  const events = useResource<PublicationEvent[]>(`/api/v1/publications/${id}/events`);
  return <DataState resource={events} empty="No events.">{(rows) => <ol className="events">{rows.map((row, index) =>
    <li key={index}>{formatDate(row.created_at)} · {row.from_status ?? "new"} → {row.to_status} by {row.actor}{row.error_class ? ` (${row.error_class})` : ""}</li>)}</ol>}</DataState>;
}

export default function PublishingPage() {
  const settings = useResource<AppSettings>("/api/v1/settings");
  const accounts = useResource<Account[]>("/api/v1/accounts");
  const clips = useResource<Clip[]>("/api/v1/clips");
  const queue = useResource<Publication[]>("/api/v1/publications", 5000);
  const action = useAction(async () => { await Promise.all([accounts.reload(), queue.reload()]); });
  const [platform, setPlatform] = useState("youtube");
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [errors, setErrors] = useState<string[]>([]);
  const [openEvents, setOpenEvents] = useState<number | null>(null);
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const result = params.get("oauth");
    if (result) action.setMessage(result === "success" ? `${PLATFORM_LABEL[params.get("platform") ?? ""] ?? "Account"} connected.` : "Account connection failed. Try again.");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  async function connect(target: string) {
    await action.run(async () => {
      const { authorization_url } = await request<{ authorization_url: string }>(`/api/v1/oauth/${target}/start`);
      window.location.assign(authorization_url);
    }, "Redirecting to the platform for consent…");
  }
  async function schedule(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const input = { platform, account_id: Number(form.get("account_id")), title, description, privacy_status: String(form.get("privacy_status")),
      scheduled_at: String(form.get("scheduled_at") ?? ""), tiktok_privacy_level: String(form.get("tiktok_privacy_level") ?? "SELF_ONLY"),
      instagram_visibility_acknowledged: form.get("instagram_ack") === "on" };
    const problems = validatePublish(input);
    setErrors(problems);
    if (problems.length) return;
    await action.run(() => request(`/api/v1/clips/${form.get("clip_id")}/publish`, "POST", publishPayload(input)), "Publication queued.");
  }
  const approved = (clips.data ?? []).filter((clip) => clip.status === "approved");
  const platformAccounts = (accounts.data ?? []).filter((account) => account.platform === platform && account.status === "connected");
  return <>
    <header className="page-head"><p className="eyebrow">DISTRIBUTION</p><h1>Publishing</h1></header>
    <Message text={action.message} error={action.error} />
    <DataState resource={settings} empty="">{(data) => <>
      {!data.publishing_enabled && <p className="alert">Publishing is disabled by configuration. Nothing will be uploaded.</p>}
      {data.publishing_dry_run && <p className="alert">Dry-run mode: publications are simulated and never sent to a platform.</p>}
    </>}</DataState>
    <section className="panel" aria-labelledby="accounts-title">
      <h2 id="accounts-title">Connected accounts</h2>
      <div className="grid">{PUBLISH_PLATFORMS.map((name) => {
        const flags = settings.data?.publishing_platforms?.[name];
        return <div className="card" key={name}><h3>{PLATFORM_LABEL[name]}</h3>
          <p className="muted">{flags ? (flags.oauth_configured ? "OAuth configured" : "OAuth not configured") : "—"}{flags && !flags.enabled ? " · publishing disabled" : ""}</p>
          <button type="button" disabled={!flags?.oauth_configured} onClick={() => connect(name)}>Connect {PLATFORM_LABEL[name]}</button></div>;
      })}</div>
      <DataState resource={accounts} empty="No accounts connected.">{(rows) => <ul className="rows">{rows.map((account) => <li key={account.id}>
        <b>{PLATFORM_LABEL[account.platform]}</b> · {account.display_name} · <Badge value={account.status} /> · limit {account.daily_upload_limit}/day
        <ConfirmButton label="Disconnect" danger confirm={`Disconnect ${account.display_name}?`} onConfirm={() => action.run(() => request(`/api/v1/accounts/${account.id}`, "DELETE"), "Account disconnected.")} />
      </li>)}</ul>}</DataState>
    </section>
    <section className="panel" aria-labelledby="schedule-title">
      <h2 id="schedule-title">Schedule a clip</h2>
      <DataState resource={clips} empty="No approved clips. Approve clips in AI studio first." isEmpty={() => approved.length === 0}>{() =>
        <form className="stack-form" onSubmit={schedule} noValidate>
          <label>Clip <select name="clip_id" required>{approved.map((clip) => <option key={clip.id} value={clip.id}>#{clip.id} · {clip.hook}</option>)}</select></label>
          <label>Platform <select value={platform} onChange={(e) => setPlatform(e.target.value)}>{PUBLISH_PLATFORMS.map((name) => <option key={name} value={name}>{PLATFORM_LABEL[name]}</option>)}</select></label>
          <label>Account <select name="account_id">{platformAccounts.length ? platformAccounts.map((account) => <option key={account.id} value={account.id}>{account.display_name}</option>) : <option value="">No connected {PLATFORM_LABEL[platform]} account</option>}</select></label>
          <label>Title <input value={title} onChange={(e) => setTitle(e.target.value)} aria-describedby="title-count" /></label>
          <small id="title-count" className={title.length > 100 ? "over" : "muted"}>{title.length}/100</small>
          <label>Description <textarea value={description} onChange={(e) => setDescription(e.target.value)} rows={4} aria-describedby="description-count" /></label>
          <small id="description-count" className={description.length > 5000 ? "over" : "muted"}>{description.length}/5000</small>
          <label>Visibility <select name="privacy_status"><option value="private">Private</option>{platform === "youtube" && <option value="unlisted">Unlisted</option>}</select></label>
          {platform === "tiktok" && <label>TikTok privacy <select name="tiktok_privacy_level"><option value="SELF_ONLY">Only me</option></select></label>}
          {platform === "instagram" && <label className="check"><input type="checkbox" name="instagram_ack" /> I understand Instagram Reels use the account’s own visibility.</label>}
          <label>Publish at (optional) <input type="datetime-local" name="scheduled_at" /></label>
          {errors.length > 0 && <div className="alert error" role="alert"><ul>{errors.map((item) => <li key={item}>{item}</li>)}</ul></div>}
          <button type="submit" disabled={action.busy}>Queue publication</button>
        </form>}</DataState>
    </section>
    <section className="panel" aria-labelledby="queue-title">
      <h2 id="queue-title">Publication queue</h2>
      <DataState resource={queue} empty="Nothing queued.">{(rows) => <div className="table-wrap" tabIndex={0} role="region" aria-label="Scrollable table"><table>
        <thead><tr><th scope="col">ID</th><th scope="col">Clip</th><th scope="col">Platform</th><th scope="col">Status</th><th scope="col">When</th><th scope="col">Progress</th><th scope="col">Actions</th></tr></thead>
        <tbody>{rows.map((row) => <tr key={row.id}>
          <td>{row.id}{row.dry_run ? " (dry run)" : ""}</td><td>#{row.clip_id} {row.title ?? ""}</td><td>{PLATFORM_LABEL[row.platform]}</td>
          <td><Badge value={row.status} />{row.error_class && <small> {row.error_class}</small>}{row.external_url && <> · <a href={row.external_url} target="_blank" rel="noreferrer noopener">view</a></>}</td>
          <td>{formatDate(row.scheduled_at)}</td>
          <td>{row.upload_progress?.bytes_total ? <progress max={row.upload_progress.bytes_total} value={row.upload_progress.bytes_sent} aria-label={`Upload progress for publication ${row.id}`} /> : "—"}</td>
          <td className="actions">
            {row.status === "failed" && <button type="button" onClick={() => action.run(() => request(`/api/v1/publications/${row.id}/retry`, "POST"), "Retry queued.")}>Retry</button>}
            {["queued", "scheduled"].includes(row.status) && <button type="button" onClick={() => action.run(() => request(`/api/v1/publications/${row.id}/cancel`, "POST"), "Publication cancelled.")}>Cancel</button>}
            <button type="button" aria-expanded={openEvents === row.id} onClick={() => setOpenEvents(openEvents === row.id ? null : row.id)}>Events</button>
            {openEvents === row.id && <Events id={row.id} />}
          </td></tr>)}</tbody></table></div>}</DataState>
    </section>
  </>;
}
