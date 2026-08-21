/**
 * Display helpers, deliberately matching the CLI's formatting.
 *
 * `flotta ps` and this dashboard read the same rows, so a worker that reads
 * `3m04s` in the terminal must not read `184 seconds` in the browser. These are
 * ports of `fmt_duration` / `fmt_age` in `src/flotta/cli.py`; the shapes are
 * pinned by tests on both sides.
 *
 * No `server-only` here — this module is imported by client components too.
 */

/** Parse a store timestamp, tolerating anything unexpected. */
export function parseTs(value: string | null | undefined): Date | null {
  if (!value) return null;
  // Python writes `datetime.isoformat()`, which JS parses natively.
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

/** 0.8s · 12s · 3m04s · 1h02m — identical to the CLI's `fmt_duration`. */
export function fmtDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || seconds < 0) return "—";
  if (seconds < 10) return `${seconds.toFixed(1)}s`;
  const whole = Math.floor(seconds);
  if (whole < 60) return `${whole}s`;
  if (whole < 3600) {
    const s = whole % 60;
    return `${Math.floor(whole / 60)}m${String(s).padStart(2, "0")}s`;
  }
  const m = Math.floor((whole % 3600) / 60);
  return `${Math.floor(whole / 3600)}h${String(m).padStart(2, "0")}m`;
}

/** How long ago `ts` was, e.g. `12s ago`. */
export function fmtAge(
  ts: string | null | undefined,
  now: Date = new Date(),
): string {
  const parsed = parseTs(ts);
  if (!parsed) return "—";
  return `${fmtDuration((now.getTime() - parsed.getTime()) / 1000)} ago`;
}

/** Wall-clock time, for the event timeline. */
export function fmtClock(ts: string | null | undefined): string {
  const parsed = parseTs(ts);
  if (!parsed) return "—";
  return parsed.toLocaleTimeString(undefined, { hour12: false });
}

/**
 * Elapsed time for a worker: to `finished_at`, or to now while it is live.
 * Mirrors `cli.worker_duration`.
 */
export function workerDuration(
  worker: { spawned_at: string; finished_at: string | null },
  now: Date = new Date(),
): number | null {
  const start = parseTs(worker.spawned_at);
  if (!start) return null;
  const end = parseTs(worker.finished_at) ?? now;
  return Math.max(0, (end.getTime() - start.getTime()) / 1000);
}

/**
 * Cost, when the store has one.
 *
 * Populated only when the operator sets `FLOTTA_COST_PER_SECOND`; otherwise
 * this renders an em dash. Modal's billing API cannot attribute cost to one
 * worker — it is keyed by App id at daily/hourly resolution and calls cannot be
 * tagged — so the figure is duration x a rate the operator chose. It is never
 * derived from a rate nobody picked: a fabricated dollar figure that looks
 * authoritative is worse than an honest blank.
 */
export function fmtCost(cost: number | null | undefined): string {
  if (cost === null || cost === undefined) return "—";
  return cost < 0.01 ? `<$0.01` : `$${cost.toFixed(2)}`;
}
