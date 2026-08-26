/**
 * The dashboard's link to the control plane (M4.5).
 *
 * Replaces `lib/store.ts`, which opened the fleet's SQLite file directly. §8.3
 * is explicit that this had to go — "reads the API, not the DB directly;
 * v0.1's direct-SQLite read must go" — and M4 made the cost concrete: once
 * fleet state could live on Postgres, a dashboard that could only read a local
 * file had to answer 501 rather than show a fleet it could not reach.
 *
 * Two things fall out of the change, both simplifications:
 *
 * - **The dashboard no longer needs to know how fleet state is stored.**
 *   SQLite, Postgres, or whatever M9 brings — it asks the control plane.
 * - **The kill button stops shelling out to the CLI.** `lib/teardown.ts` ran
 *   `flotta kill <id>` as a subprocess because tearing a box down needs Modal
 *   or Fly credentials the dashboard does not have. The control plane has
 *   them, so a DELETE is enough, and decision D10 — only code that can reach
 *   the substrate writes to the store — is preserved by the API rather than by
 *   spawning python.
 *
 * The UI's "worker" vocabulary survives in route and type names on purpose:
 * renaming it is cosmetic churn, and this file is already the change §8.3
 * asked for.
 */
import "server-only";

import type { FleetEvent, Worker } from "./types";

export type { FleetEvent, Worker, WorkerStatus } from "./types";
export { TERMINAL } from "./types";

const DEFAULT_CONTROL_URL = "http://127.0.0.1:8080";

/** Raised when the control plane cannot be reached, so the UI can say so. */
export class ControlPlaneUnreachableError extends Error {
  constructor(
    public readonly url: string,
    cause?: unknown,
  ) {
    super(
      `cannot reach the Flotta control plane at ${url}. Start it with ` +
        `\`flotta serve\`, or point $FLOTTA_CONTROL_URL at a running one.` +
        (cause instanceof Error ? ` (${cause.message})` : ""),
    );
    this.name = "ControlPlaneUnreachableError";
  }
}

/** Raised when the control plane answers, but with an error. */
export class ControlPlaneError extends Error {
  constructor(
    public readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ControlPlaneError";
  }
}

export function controlPlaneUrl(): string {
  return (process.env.FLOTTA_CONTROL_URL?.trim() || DEFAULT_CONTROL_URL).replace(/\/$/, "");
}

/**
 * The dashboard's own token (M5).
 *
 * Read server-side only — this module is `server-only`, so the token never
 * reaches the browser. That is the whole reason the dashboard proxies the
 * control plane through its own API routes instead of letting the page call it
 * directly: a token in client JavaScript is a token in everyone's devtools.
 *
 * Mint it with `flotta token mint dashboard --scope fleet:read`. **Read-only
 * is the right scope**: the fleet view needs to list boxes, and the kill
 * button is the one thing worth deciding on deliberately — give it
 * `box:destroy` only if you want anyone with dashboard access to be able to
 * delete an agent's entire memory.
 */
export function controlPlaneToken(): string | null {
  return process.env.FLOTTA_CONTROL_TOKEN?.trim() || null;
}

async function call(path: string, init?: RequestInit): Promise<unknown> {
  const base = controlPlaneUrl();
  let response: Response;
  try {
    const token = controlPlaneToken();
    response = await fetch(`${base}${path}`, {
      ...init,
      headers: {
        ...(init?.headers ?? {}),
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      // The fleet changes underneath us; a cached view is actively misleading
      // rather than merely stale. Same reasoning the routes already carry.
      cache: "no-store",
    });
  } catch (cause) {
    // A refused connection is by far the most likely failure here, and it has
    // a specific fix. Reporting it as a generic 500 would send someone
    // debugging the dashboard when the control plane simply is not running.
    throw new ControlPlaneUnreachableError(base, cause);
  }

  if (!response.ok) {
    // 401/403 have one likely cause and a specific fix, and the raw detail
    // ("missing bearer token") does not say where the token goes.
    if (response.status === 401 || response.status === 403) {
      const how = controlPlaneToken()
        ? "Its token was rejected — expired, or minted with a different signing key, or missing a scope."
        : "No $FLOTTA_CONTROL_TOKEN is set.";
      throw new ControlPlaneError(
        response.status,
        `${how} Mint one with \`flotta token mint dashboard --scope fleet:read\` ` +
          `and set FLOTTA_CONTROL_TOKEN before starting the dashboard.`,
      );
    }
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = (await response.json()) as { detail?: string; message?: string };
      detail = body.detail ?? body.message ?? detail;
    } catch {
      // Not JSON — keep the status line.
    }
    throw new ControlPlaneError(response.status, detail);
  }
  return response.json();
}

/**
 * A control-plane box, shaped for the UI.
 *
 * The row is a **box**: the fleet is the boxes, and the kill button destroys a
 * machine. `task` carries its most recent task's prompt, and `cost_estimate`
 * is summed across its tasks — cost lives on tasks, because the formula
 * measures start-to-verdict and a box spans months.
 */
interface ControlBox {
  id: string;
  name: string;
  status: string;
  endpoint: string | null;
  created_at: string;
  destroyed_at: string | null;
  latest_task?: string | null;
  /** Summed across the box's tasks by the control plane. */
  cost_estimate?: number | null;
  task_count?: number;
}

interface ControlTask {
  prompt: string;
  cost_estimate: number | null;
}

function toWorker(box: ControlBox, tasks?: ControlTask[]): Worker {
  // The list endpoint sums cost itself (one query per box beats N round
  // trips); the detail endpoint returns the tasks, so sum them here. Trusting
  // the box's own field first means the list view is not silently costless.
  const costs = (tasks ?? [])
    .map((t) => t.cost_estimate)
    .filter((c): c is number => typeof c === "number");
  const cost =
    tasks === undefined
      ? (box.cost_estimate ?? null)
      : costs.length
        ? costs.reduce((a, b) => a + b, 0)
        : null;
  // The list endpoint carries `latest_task`; the detail endpoint carries the
  // tasks themselves (newest first). Taking whichever is present keeps the
  // detail page from rendering a box with no task at all — which it did, since
  // only the list view was checked.
  const task = box.latest_task ?? tasks?.[0]?.prompt ?? "";

  return {
    id: box.id,
    task,
    status: box.status as Worker["status"],
    endpoint: box.endpoint,
    // `spawned_at` / `finished_at` are the UI's field names; on a box they are
    // created/destroyed. Age, not runtime — a box asleep for a month is a
    // month old and has run for none of it.
    spawned_at: box.created_at,
    finished_at: box.destroyed_at,
    cost_estimate: cost,
  };
}

export async function listWorkers(): Promise<Worker[]> {
  const body = (await call("/api/boxes?all_=true")) as { boxes: ControlBox[] };
  return body.boxes.map((box) => toWorker(box));
}

export async function getWorker(id: string): Promise<Worker | null> {
  try {
    const body = (await call(`/api/boxes/${encodeURIComponent(id)}`)) as {
      box: ControlBox;
      tasks: ControlTask[];
    };
    return toWorker(body.box, body.tasks);
  } catch (error) {
    if (error instanceof ControlPlaneError && error.status === 404) return null;
    throw error;
  }
}

export async function getEvents(id: string): Promise<FleetEvent[]> {
  const body = (await call(`/api/boxes/${encodeURIComponent(id)}/events`)) as {
    events: {
      id: number;
      entity_kind: string;
      entity_id: string;
      ts: string;
      type: string;
      payload: Record<string, unknown> | null;
    }[];
  };
  return body.events.map((e) => ({
    id: e.id,
    worker_id: id,
    ts: e.ts,
    type: e.type,
    payload: e.payload,
  }));
}

export interface TeardownResult {
  box_id: string;
  [key: string]: unknown;
}

export async function killWorker(id: string): Promise<TeardownResult> {
  const body = (await call(`/api/boxes/${encodeURIComponent(id)}`, {
    method: "DELETE",
  })) as { result: TeardownResult };
  return body.result;
}
