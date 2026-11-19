"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { ReactNode, useEffect, useState } from "react";
import { ApiError, request } from "../lib/api";

export const NAV = [
  ["/", "Overview"], ["/discovery", "Discovery"], ["/library", "Content library"], ["/studio", "AI studio"],
  ["/publishing", "Publishing"], ["/analytics", "Analytics"], ["/revenue", "Revenue"], ["/agents", "AI agents"], ["/settings", "Settings"],
] as const;

/** Route protection: every dashboard page renders only after /auth/me confirms the session. */
export function Shell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const [user, setUser] = useState<string | null>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const [failure, setFailure] = useState("");
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    request<{ username: string }>("/api/v1/auth/me")
      .then((me) => { setUser(me.username); setFailure(""); })
      .catch((err) => {
        // Only a 401 means "not signed in"; rate limits or outages must not log the operator out.
        if (err instanceof ApiError && err.status === 401) window.location.assign(`/login?next=${encodeURIComponent(window.location.pathname)}`);
        else setFailure(err instanceof Error ? err.message : "API unavailable");
      });
  }, [attempt]);
  useEffect(() => setMenuOpen(false), [pathname]);
  async function logout() {
    try { await request("/api/v1/auth/logout", "POST"); } finally { window.location.assign("/login"); }
  }
  if (!user && failure) return <main className="center"><div className="panel"><p className="alert error" role="alert">Cannot reach the API: {failure}</p>
    <button type="button" onClick={() => { setFailure(""); setAttempt(attempt + 1); }}>Retry</button></div></main>;
  if (!user) return <main className="center"><p role="status">Checking session…</p></main>;
  return <div className="layout">
    <a className="skip" href="#content">Skip to content</a>
    <header className="topbar">
      <span className="brand"><span className="orb" aria-hidden="true" />SOE</span>
      <button type="button" className="menu-toggle" aria-expanded={menuOpen} aria-controls="primary-nav" onClick={() => setMenuOpen(!menuOpen)}>Menu</button>
    </header>
    <nav id="primary-nav" aria-label="Primary" className={menuOpen ? "open" : ""}>
      <span className="brand desktop-only"><span className="orb" aria-hidden="true" />SOE</span>
      <ul>{NAV.map(([href, label]) => <li key={href}>
        <Link href={href} className={`nav${pathname === href ? " active" : ""}`} aria-current={pathname === href ? "page" : undefined}>{label}</Link>
      </li>)}</ul>
      <p className="muted">Signed in as {user}</p>
      <button type="button" onClick={logout}>Sign out</button>
    </nav>
    <main id="content" tabIndex={-1}>{children}</main>
  </div>;
}
