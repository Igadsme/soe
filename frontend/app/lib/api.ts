export const api = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export class ApiError extends Error {
  constructor(message: string, public status: number) { super(message); }
}

function csrfToken(): string | undefined {
  const row = document.cookie.split(";").map((item) => item.trim()).find((item) => item.startsWith("soe_csrf="));
  return row ? decodeURIComponent(row.slice("soe_csrf=".length)) : undefined;
}

export function csrfHeaders(method: string): Record<string, string> {
  const token = csrfToken();
  return token && ["POST", "PUT", "DELETE", "PATCH"].includes(method.toUpperCase()) ? { "X-CSRF-Token": token } : {};
}

/** Redirects to the login page when the server reports an expired or missing session. */
export function handleUnauthorized(status: number) {
  if (status !== 401 || typeof window === "undefined") return;
  if (!window.location.pathname.startsWith("/login")) window.location.assign("/login?expired=1");
}

export async function apiFetch(url: string, options: RequestInit = {}) {
  const headers = new Headers(options.headers);
  const method = (options.method ?? "GET").toUpperCase();
  for (const [name, value] of Object.entries(csrfHeaders(method))) headers.set(name, value);
  const response = await fetch(url, { ...options, method, headers, credentials: "include" });
  handleUnauthorized(response.status);
  if (!response.ok && method === "GET") throw new Error("Unable to load API data.");
  return response;
}

export function errorDetail(detail: unknown, fallback: string): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map((entry) => {
    const location = Array.isArray(entry?.loc) ? entry.loc.join(".") : "";
    return [location, entry?.msg ?? fallback].filter(Boolean).join(": ");
  }).join("; ") || fallback;
  return fallback;
}

/** JSON request helper: returns the parsed body or throws ApiError with the server's detail. */
export async function request<T = unknown>(path: string, method = "GET", body?: unknown): Promise<T> {
  const headers = new Headers(csrfHeaders(method));
  let payload: BodyInit | undefined;
  if (body instanceof FormData || body instanceof URLSearchParams) payload = body;
  else if (body !== undefined) { headers.set("Content-Type", "application/json"); payload = JSON.stringify(body); }
  const response = await fetch(`${api}${path}`, { method, headers, body: payload, credentials: "include" });
  handleUnauthorized(response.status);
  if (response.status === 204) return undefined as T;
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new ApiError(errorDetail(data?.detail, `Request failed (${response.status})`), response.status);
  return data as T;
}

export function formatDate(value?: string | null): string {
  return value ? new Date(value).toLocaleString() : "—";
}

export function cents(value: number | null | undefined): string {
  return value === null || value === undefined ? "unavailable" : `$${(value / 100).toFixed(2)}`;
}
