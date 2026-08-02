/**
 * Shapes shared by the server and the browser.
 *
 * These live apart from `lib/store.ts` because that module is `server-only`;
 * a client component importing it is a build error. Types alone are erased at
 * compile time, but keeping them in a neutral module makes the boundary
 * obvious rather than incidental.
 *
 * Field names mirror the SQLite columns (and so the Python dataclasses) —
 * snake_case is deliberate, so a row logged in the terminal and a row in the
 * browser devtools read identically.
 */

export type WorkerStatus =
  | "provisioning"
  | "running"
  | "done"
  | "failed"
  | "torn_down";

export interface Worker {
  id: string;
  task: string;
  status: WorkerStatus;
  endpoint: string | null;
  spawned_at: string;
  finished_at: string | null;
  cost_estimate: number | null;
}

export interface FleetEvent {
  id: number;
  worker_id: string;
  ts: string;
  type: string;
  payload: Record<string, unknown> | null;
}

/** Terminal states — mirrors `provision._TERMINAL` / `cli.TERMINAL`. */
export const TERMINAL: ReadonlySet<string> = new Set([
  "done",
  "failed",
  "torn_down",
]);

export function isLive(worker: Worker): boolean {
  return !TERMINAL.has(worker.status);
}

/** Error body shared by every API route when the store cannot be read. */
export interface ApiError {
  error: string;
  message?: string;
  store?: string;
  detail?: string;
}
