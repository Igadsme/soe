"use client";
import { FormEvent, useState } from "react";
import { api, apiFetch, errorDetail } from "../lib/api";

export function RightsForm({ sourceId, onApproved }: { sourceId: number; onApproved: (sourceId: number) => void }) {
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const evidence = String(form.get("evidence") ?? "").trim();
    const platforms = form.getAll("allowed_platforms");
    if (evidence.replace(/\s/g, "").length < 20) { setError("Evidence must contain at least 20 non-whitespace characters."); return; }
    if (!platforms.length) { setError("Select at least one allowed platform."); return; }
    setError(""); setPending(true);
    try {
      const expires = String(form.get("expires_at") ?? "");
      const response = await apiFetch(`${api}/api/v1/sources/${sourceId}/rights`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ authorization_type: form.get("authorization_type"), evidence,
          allowed_platforms: platforms, commercial_use: form.has("commercial_use"), publishing_allowed: form.has("publishing_allowed"),
          expires_at: expires ? new Date(expires).toISOString() : null }),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(errorDetail(body.detail, "Rights approval failed."));
      onApproved(body.source_id);
    } catch (err) { setError(err instanceof Error ? err.message : "Rights approval failed."); }
    finally { setPending(false); }
  }
  return <form className="rights-form" onSubmit={submit} noValidate aria-label="Rights approval">
    <h3>Rights approval for source {sourceId}</h3>
    <label>Authorization type <select name="authorization_type" defaultValue="owned">
      <option value="owned">Owned</option><option value="licensed">Licensed</option>
      <option value="explicit_creator_authorization">Creator permission</option>
    </select></label>
    <label>Evidence <textarea name="evidence" required aria-describedby="evidence-help" /></label>
    <p id="evidence-help">Required: at least 20 non-whitespace characters describing your authorization.</p>
    <fieldset><legend>Allowed platforms (select at least one)</legend>{["youtube", "twitch", "tiktok", "instagram"].map((platform) =>
      <label key={platform}><input type="checkbox" name="allowed_platforms" value={platform} />{platform}</label>)}</fieldset>
    <label><input type="checkbox" name="commercial_use" />Commercial use</label>
    <label><input type="checkbox" name="publishing_allowed" />Publishing allowed</label>
    <label>Expires at (local time, optional) <input type="datetime-local" name="expires_at" /></label>
    {error && <p role="alert">{error}</p>}
    <button type="submit" disabled={pending}>{pending ? "Submitting…" : "Approve rights"}</button>
  </form>;
}
