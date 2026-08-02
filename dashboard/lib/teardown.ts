/**
 * The kill button's bridge to a real teardown.
 *
 * Tearing a worker down means cancelling a Modal function call, which needs
 * Python, Modal credentials and the workspace-profile resolution that already
 * lives in `flotta.cli`. So the dashboard shells out to the CLI rather than
 * reimplementing any of it.
 *
 * The rejected alternative was writing `torn_down` straight into the store from
 * here. It would have been a handful of lines and it would have been a lie: the
 * row would close while the container kept running and billing. A kill button
 * that does not kill is worse than no kill button. This also keeps decision D10
 * intact — only code that can reach Modal writes to the store.
 */
import "server-only";

import { execFile } from "node:child_process";
import path from "node:path";
import { promisify } from "node:util";

import { resolveStorePath } from "./store";

const execFileAsync = promisify(execFile);

/**
 * Worker ids are minted as `w-<12 hex>` but `create_worker` accepts a caller
 * supplied id, so this stays permissive about shape while strict about the
 * character set. It is the only untrusted value that reaches the command line.
 */
const WORKER_ID_RE = /^[A-Za-z0-9_-]{1,64}$/;

export class InvalidWorkerIdError extends Error {}

export interface TeardownResult {
  worker_id: string;
  already_torn_down?: boolean;
  [key: string]: unknown;
}

/** Repo root — one level above `dashboard/`, where pyproject and `.env` live. */
function repoRoot(): string {
  return path.resolve(process.cwd(), "..");
}

/**
 * Run `flotta kill <id> --json`, returning the CLI's parsed result.
 *
 * Arguments are passed as an argv array, never a shell string, so even a
 * hostile id could not inject a command — and `WORKER_ID_RE` rejects it before
 * that matters anyway.
 */
export async function killWorker(workerId: string): Promise<TeardownResult> {
  if (!WORKER_ID_RE.test(workerId)) {
    throw new InvalidWorkerIdError(`refusing to kill malformed id ${workerId}`);
  }

  const { stdout } = await execFileAsync(
    "uv",
    ["run", "flotta", "kill", workerId, "--json"],
    {
      cwd: repoRoot(),
      // Pin the store so the CLI cannot resolve a different file than the one
      // this dashboard is displaying.
      env: { ...process.env, FLOTTA_STORE: resolveStorePath() },
      timeout: 120_000,
      maxBuffer: 1024 * 1024,
    },
  );

  return JSON.parse(stdout) as TeardownResult;
}
