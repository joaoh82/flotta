/**
 * Read-only access to the Flotta fleet-state store.
 *
 * The dashboard is a *reader*. Per decision D10 the store is written only by
 * local provisioning code that can actually reach Modal — so every connection
 * here is opened `readOnly`, and the kill button goes through the CLI rather
 * than writing a `torn_down` row itself (see `lib/teardown.ts`).
 *
 * Uses Node's built-in `node:sqlite`, so the dashboard needs no native module
 * and no npm database dependency. It is still flagged experimental in Node 24
 * and prints one warning on first use; that is cosmetic.
 *
 * `server-only` makes a client-component import a build error rather than a
 * confusing "Module not found: fs" at runtime.
 */
import "server-only";

import { existsSync } from "node:fs";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";

import type { FleetEvent, Worker } from "./types";

export type { FleetEvent, Worker, WorkerStatus } from "./types";
export { TERMINAL } from "./types";

/** Raised when the store file is absent, so the UI can say so explicitly. */
export class StoreMissingError extends Error {
  constructor(public readonly storePath: string) {
    super(`no fleet-state store at ${storePath}`);
    this.name = "StoreMissingError";
  }
}

/**
 * `$FLOTTA_STORE`, else `fleet.db` beside the repo root.
 *
 * The CLI's default is `./fleet.db` in the *working directory*; the dev server
 * runs one level down in `dashboard/`, so the bare default resolves upward to
 * find the same file a CLI run from the repo root would create.
 */
export function resolveStorePath(): string {
  const configured = process.env.FLOTTA_STORE?.trim();
  if (configured) return path.resolve(configured);
  return path.resolve(process.cwd(), "..", "fleet.db");
}

/**
 * Open the store, run `fn`, and always close.
 *
 * Opened per request rather than kept alive: under WAL a long-lived reader can
 * pin an old snapshot and quietly serve stale rows to a polling UI, which is
 * the one thing this dashboard must not do. Opening SQLite is cheap.
 *
 * `readOnly` is load-bearing twice over — it keeps the reader honest, and it
 * stops a mistyped path from *creating* an empty database that would render as
 * a healthy, empty fleet instead of an error.
 */
function withStore<T>(fn: (db: DatabaseSync) => T): T {
  const storePath = resolveStorePath();
  if (!existsSync(storePath)) throw new StoreMissingError(storePath);

  const db = new DatabaseSync(storePath, { readOnly: true });
  try {
    return fn(db);
  } finally {
    db.close();
  }
}

function parsePayload(raw: unknown): Record<string, unknown> | null {
  if (typeof raw !== "string" || raw === "") return null;
  try {
    return JSON.parse(raw) as Record<string, unknown>;
  } catch {
    // A malformed payload is a curiosity, not a reason to fail the request.
    return { raw };
  }
}

export function listWorkers(): Worker[] {
  return withStore((db) =>
    db
      .prepare(
        `SELECT id, task, status, endpoint, spawned_at, finished_at, cost_estimate
           FROM workers
          ORDER BY spawned_at DESC, id DESC`,
      )
      .all()
      .map((row) => row as unknown as Worker),
  );
}

export function getWorker(id: string): Worker | null {
  return withStore((db) => {
    const row = db
      .prepare(
        `SELECT id, task, status, endpoint, spawned_at, finished_at, cost_estimate
           FROM workers WHERE id = ?`,
      )
      .get(id);
    return row ? (row as unknown as Worker) : null;
  });
}

export function getEvents(workerId: string): FleetEvent[] {
  return withStore((db) =>
    db
      .prepare(
        `SELECT id, worker_id, ts, type, payload_json
           FROM events WHERE worker_id = ? ORDER BY id`,
      )
      .all(workerId)
      .map((row) => {
        const r = row as unknown as Omit<FleetEvent, "payload"> & {
          payload_json: string | null;
        };
        return {
          id: r.id,
          worker_id: r.worker_id,
          ts: r.ts,
          type: r.type,
          payload: parsePayload(r.payload_json),
        };
      }),
  );
}
