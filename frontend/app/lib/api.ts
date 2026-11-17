export const api = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export async function apiFetch(url: string, options: RequestInit = {}) {
  const headers = new Headers(options.headers);
  const method = (options.method ?? "GET").toUpperCase();
  const csrf = document.cookie.split(";").map((row) => row.trim()).find((row) => row.startsWith("soe_csrf="))?.slice("soe_csrf=".length);
  if (csrf && ["POST", "PUT", "DELETE", "PATCH"].includes(method)) headers.set("X-CSRF-Token", decodeURIComponent(csrf));
  const response = await fetch(url, { ...options, method, headers, credentials: "include" });
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
