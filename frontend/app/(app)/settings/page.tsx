"use client";
import { FormEvent } from "react";
import { request } from "../../lib/api";
import { dollarsToCents, formatWindows, parseWindows, type Policy } from "../../lib/policy";
import { useResource } from "../../lib/useResource";
import { Badge, ConfirmButton, DataState, Message, useAction } from "../../components/ui";
import { PLATFORM_LABEL, PUBLISH_PLATFORMS, type Account, type AppSettings, type SystemStatus } from "../../lib/types";

export default function SettingsPage() {
  const policy = useResource<Policy>("/api/v1/automation/policy");
  const status = useResource<SystemStatus>("/api/v1/system/status", 10000);
  const settings = useResource<AppSettings>("/api/v1/settings");
  const accounts = useResource<Account[]>("/api/v1/accounts");
  const action = useAction(async () => { await Promise.all([policy.reload(), status.reload()]); });
  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    await action.run(async () => {
      const next: Policy = {
        auto_process_authorized: form.get("auto_process_authorized") === "on", auto_publish: form.get("auto_publish") === "on",
        approval_mode: form.get("approval_mode") as Policy["approval_mode"], min_clip_score: Number(form.get("min_clip_score")),
        max_clips_per_source: Number(form.get("max_clips_per_source")),
        daily_publish_limits: Object.fromEntries(PUBLISH_PLATFORMS.map((name) => [name, Number(form.get(`limit_${name}`) || 0)])),
        target_accounts: Object.fromEntries(PUBLISH_PLATFORMS.map((name) => [name, form.getAll(`target_${name}`).map(Number)]).filter(([, ids]) => (ids as number[]).length)),
        publishing_windows: parseWindows(String(form.get("publishing_windows") ?? "")), timezone: String(form.get("timezone")),
        default_privacy: form.get("default_privacy") as Policy["default_privacy"], instagram_visibility_acknowledged: form.get("instagram_ack") === "on",
        daily_budget_cents: dollarsToCents(String(form.get("daily_budget") ?? "")), monthly_budget_cents: dollarsToCents(String(form.get("monthly_budget") ?? "")),
        max_concurrent_processing_jobs: Number(form.get("max_processing")), max_concurrent_publish_jobs: Number(form.get("max_publish")),
      };
      await request("/api/v1/automation/policy", "PUT", next);
    }, "Automation policy saved.");
  }
  return <>
    <header className="page-head"><p className="eyebrow">CONTROL</p><h1>Settings</h1></header>
    <Message text={action.message} error={action.error} />
    <section className="panel" aria-labelledby="stop-title">
      <h2 id="stop-title">Emergency stop</h2>
      <DataState resource={status} empty="">{(data) => <div className="actions">
        <Badge value={data.emergency_stopped ? "emergency stopped" : "automation running"} />
        {data.emergency_stopped
          ? <ConfirmButton label="Resume automation" confirm="Resume all automation?" onConfirm={() => action.run(() => request("/api/v1/system/resume", "POST"), "Automation resumed.")} />
          : <ConfirmButton label="Emergency stop" danger confirm="Stop all automation and cancel running jobs?" onConfirm={() => action.run(() => request("/api/v1/system/emergency-stop", "POST"), "Emergency stop engaged.")} />}
      </div>}</DataState>
    </section>
    <section className="panel" aria-labelledby="policy-title">
      <h2 id="policy-title">Automation policy</h2>
      <DataState resource={policy} empty="">{(data) => <form className="stack-form" onSubmit={save} key={JSON.stringify(data)}>
        <label className="check"><input type="checkbox" name="auto_process_authorized" defaultChecked={data.auto_process_authorized} /> Automatically process sources with valid rights</label>
        <label className="check"><input type="checkbox" name="auto_publish" defaultChecked={data.auto_publish} /> Automatically publish approved clips</label>
        <label>Approval mode <select name="approval_mode" defaultValue={data.approval_mode}><option value="manual">Manual review</option><option value="auto_after_qc">Auto-approve after quality checks</option></select></label>
        <label>Minimum clip score <input name="min_clip_score" type="number" min={0} max={100} step="0.1" defaultValue={data.min_clip_score} /></label>
        <label>Max clips per source <input name="max_clips_per_source" type="number" min={1} max={25} defaultValue={data.max_clips_per_source} /></label>
        <fieldset><legend>Daily publish limits</legend>{PUBLISH_PLATFORMS.map((name) =>
          <label key={name}>{PLATFORM_LABEL[name]} <input name={`limit_${name}`} type="number" min={0} max={1000} defaultValue={data.daily_publish_limits[name] ?? 0} /></label>)}</fieldset>
        <fieldset><legend>Target accounts</legend>{(accounts.data ?? []).length === 0 ? <p className="muted">No connected accounts.</p> : (accounts.data ?? []).map((account) =>
          <label className="check" key={account.id}><input type="checkbox" name={`target_${account.platform}`} value={account.id} defaultChecked={(data.target_accounts[account.platform] ?? []).includes(account.id)} /> {PLATFORM_LABEL[account.platform]} · {account.display_name}</label>)}</fieldset>
        <label>Publishing windows (hours) <input name="publishing_windows" defaultValue={formatWindows(data.publishing_windows)} placeholder="e.g. 9-12, 18-22" /></label>
        <label>Timezone <input name="timezone" defaultValue={data.timezone} required /></label>
        <label>Default privacy <select name="default_privacy" defaultValue={data.default_privacy}><option value="private">Private</option><option value="unlisted">Unlisted (YouTube)</option></select></label>
        <label className="check"><input type="checkbox" name="instagram_ack" defaultChecked={data.instagram_visibility_acknowledged} /> Instagram Reels use the account’s visibility</label>
        <label>Daily budget (USD) <input name="daily_budget" type="number" min={0} step="0.01" defaultValue={data.daily_budget_cents === null ? "" : data.daily_budget_cents / 100} /></label>
        <label>Monthly budget (USD) <input name="monthly_budget" type="number" min={0} step="0.01" defaultValue={data.monthly_budget_cents === null ? "" : data.monthly_budget_cents / 100} /></label>
        <label>Concurrent processing jobs <input name="max_processing" type="number" min={1} max={32} defaultValue={data.max_concurrent_processing_jobs} /></label>
        <label>Concurrent publish jobs <input name="max_publish" type="number" min={1} max={32} defaultValue={data.max_concurrent_publish_jobs} /></label>
        <button type="submit" disabled={action.busy}>Save policy</button>
      </form>}</DataState>
    </section>
    <section className="panel" aria-labelledby="env-title">
      <h2 id="env-title">Environment</h2>
      <DataState resource={settings} empty="">{(data) => <dl className="facts">
        <div><dt>Environment</dt><dd>{data.environment}</dd></div>
        <div><dt>Transcription model</dt><dd>{data.whisper_model_size}</dd></div>
        <div><dt>Max upload</dt><dd>{Math.round(data.max_upload_bytes / 1048576)} MB</dd></div>
        <div><dt>Discovery APIs</dt><dd>{Object.entries(data.configured_integrations).map(([key, value]) => `${PLATFORM_LABEL[key] ?? key}: ${value ? "configured" : "not configured"}`).join(" · ")}</dd></div>
      </dl>}</DataState>
    </section>
  </>;
}
