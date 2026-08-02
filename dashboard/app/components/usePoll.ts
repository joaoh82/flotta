"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import type { ApiError } from "@/lib/types";

export interface PollState<T> {
  data: T | null;
  error: ApiError | null;
  /** True until the first response lands — distinguishes "empty" from "loading". */
  loading: boolean;
  /** Force an immediate refetch, e.g. right after a kill. */
  refresh: () => void;
}

/**
 * Poll a JSON endpoint on an interval.
 *
 * Deliberately hand-rolled rather than pulling in a data-fetching library: one
 * interval and one in-flight guard is the whole requirement, and the repo's
 * convention for the dashboard is to keep dependencies boring.
 *
 * Two details that matter:
 *  - `cache: "no-store"` defeats the *browser's* HTTP cache. The server-side
 *    `force-dynamic` is a separate layer; both are needed.
 *  - The `cancelled` flag plus `clearInterval` keeps React 19 Strict Mode's
 *    double-invoked effect from leaving a second interval running in dev.
 */
export function usePoll<T>(url: string, intervalMs = 3000): PollState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(true);
  const inFlight = useRef(false);
  const [nonce, setNonce] = useState(0);

  const refresh = useCallback(() => setNonce((n) => n + 1), []);

  useEffect(() => {
    let cancelled = false;

    const tick = async () => {
      // A slow response must not stack up requests behind it.
      if (inFlight.current) return;
      inFlight.current = true;
      try {
        const res = await fetch(url, { cache: "no-store" });
        const body = await res.json();
        if (cancelled) return;
        if (!res.ok) {
          setError(body as ApiError);
          setData(null);
        } else {
          setData(body as T);
          setError(null);
        }
      } catch (err) {
        // The dev server restarting mid-poll shows up here; report it rather
        // than silently freezing the last-known state.
        if (!cancelled) {
          setError({
            error: "unreachable",
            message: err instanceof Error ? err.message : String(err),
          });
        }
      } finally {
        inFlight.current = false;
        if (!cancelled) setLoading(false);
      }
    };

    void tick();
    const id = setInterval(() => void tick(), intervalMs);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [url, intervalMs, nonce]);

  return { data, error, loading, refresh };
}
