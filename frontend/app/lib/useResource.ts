"use client";
import { useCallback, useEffect, useState } from "react";
import { request } from "./api";

export type Resource<T> = { data: T | null; error: string; loading: boolean; reload: () => Promise<void> };

/** Loads a GET endpoint with explicit loading / error states and optional polling. */
export function useResource<T>(path: string | null, intervalMs = 0): Resource<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(Boolean(path));
  const reload = useCallback(async () => {
    if (!path) return;
    try { setData(await request<T>(path)); setError(""); }
    catch (err) { setError(err instanceof Error ? err.message : "Request failed"); }
    finally { setLoading(false); }
  }, [path]);
  useEffect(() => {
    reload();
    if (!intervalMs) return;
    const timer = setInterval(reload, intervalMs);
    return () => clearInterval(timer);
  }, [reload, intervalMs]);
  return { data, error, loading, reload };
}
