import { expect, it, vi } from "vitest";
import { apiFetch } from "../app/lib/api";

it.each(["POST", "PUT", "PATCH", "DELETE", "post", "GET", "HEAD", "OPTIONS", undefined])("handles CSRF and credentials for %s", async (method) => {
  document.cookie = "soe_csrf=csrf%3Dtoken; path=/";
  const fetch = vi.fn().mockResolvedValue({ ok: true });
  vi.stubGlobal("fetch", fetch);
  await apiFetch("/test", { method, headers: { "X-Custom": "kept" }, credentials: "omit" });
  const options = fetch.mock.calls[0][1];
  expect(options.credentials).toBe("include");
  expect(options.headers.get("X-Custom")).toBe("kept");
  expect(options.headers.get("X-CSRF-Token")).toBe(["POST", "PUT", "PATCH", "DELETE"].includes(method?.toUpperCase()) ? "csrf=token" : null);
});
it("does not invent a CSRF token without a cookie", async () => {
  const fetch = vi.fn().mockResolvedValue({ ok: true }); vi.stubGlobal("fetch", fetch);
  await apiFetch("/test", { method: "POST" });
  expect(fetch.mock.calls[0][1].headers.has("X-CSRF-Token")).toBe(false);
});
