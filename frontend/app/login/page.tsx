"use client";
import { FormEvent, useEffect, useState } from "react";
import { request } from "../lib/api";

export default function Login() {
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [pending, setPending] = useState(false);
  useEffect(() => {
    if (new URLSearchParams(window.location.search).get("expired")) setNotice("Your session expired. Sign in again.");
  }, []);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPending(true); setError("");
    try {
      await request("/api/v1/auth/login", "POST", new URLSearchParams(new FormData(event.currentTarget) as unknown as Record<string, string>));
      const next = new URLSearchParams(window.location.search).get("next");
      window.location.assign(next && next.startsWith("/") && !next.startsWith("//") ? next : "/");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Sign-in failed");
    } finally { setPending(false); }
  }
  return <main className="center">
    <form className="panel login" onSubmit={submit} aria-labelledby="login-title">
      <h1 id="login-title">Sign in to Soe</h1>
      {notice && <p className="alert" role="status">{notice}</p>}
      <label>Username <input name="username" autoComplete="username" required /></label>
      <label>Password <input name="password" type="password" autoComplete="current-password" required /></label>
      {error && <p className="alert error" role="alert">{error}</p>}
      <button type="submit" disabled={pending}>{pending ? "Signing in…" : "Sign in"}</button>
    </form>
  </main>;
}
