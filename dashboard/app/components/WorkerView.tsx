"use client";

import Link from "next/link";
import { useState } from "react";

import { fmtClock, fmtDuration, fmtCost, workerDuration } from "@/lib/format";
import { isLive, type ApiError, type FleetEvent, type Worker } from "@/lib/types";

import { ErrorPanel } from "./ErrorPanel";
import { StatusBadge } from "./StatusBadge";
import { usePoll } from "./usePoll";

interface Payload {
  worker: Worker;
  events: FleetEvent[];
}

/** Shape of the CLI's `kill --json` output, as relayed by the API route. */
interface TeardownResult {
  cancelled?: boolean;
  cancel_error?: string | null;
  already_torn_down?: boolean;
}

/** The agent's answer, pulled out of whichever event carries it. */
function finalResponse(events: FleetEvent[]): string | null {
  for (let i = events.length - 1; i >= 0; i -= 1) {
    const value = events[i].payload?.final_response;
    if (typeof value === "string" && value.trim()) return value;
  }
  return null;
}

function KillButton({
  worker,
  onDone,
}: {
  worker: Worker;
  onDone: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<ApiError | null>(null);

  const kill = async () => {
    setBusy(true);
    setFailure(null);
    try {
      const res = await fetch(`/api/workers/${worker.id}`, {
        method: "DELETE",
      });
      const body = await res.json();
      // A failed teardown is reported, never swallowed: the container may still
      // be running and billing, and the user needs to know that.
      if (!res.ok) {
        setFailure(body as ApiError);
        return;
      }
      // `teardown` records a cancel failure rather than raising, so the request
      // can succeed while the container survives. Treating HTTP 200 as "killed"
      // is precisely how a kill button starts lying — check the cancel outcome.
      const result = (body as { result?: TeardownResult }).result;
      if (result && result.cancelled === false && !result.already_torn_down) {
        setFailure({
          error: "cancel_rejected",
          message:
            "The worker row was closed, but Modal did not confirm the container was cancelled. It may still be running.",
          detail: result.cancel_error ?? undefined,
        });
        return;
      }
      onDone();
    } catch (err) {
      setFailure({
        error: "unreachable",
        message: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-2">
      <button
        type="button"
        onClick={kill}
        disabled={busy}
        className="rounded-md bg-red-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-red-700 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {busy ? "Tearing down…" : "Kill worker"}
      </button>
      {failure && (
        <div className="rounded-md border border-red-300 bg-red-50 p-3 text-sm dark:border-red-900 dark:bg-red-950/40">
          <p className="font-medium text-red-900 dark:text-red-200">
            Teardown failed — the worker may still be running.
          </p>
          <p className="mt-1 text-red-800 dark:text-red-300">
            {failure.message}
          </p>
          {failure.detail && (
            <pre className="mt-2 overflow-x-auto whitespace-pre-wrap font-mono text-xs text-red-700 dark:text-red-400">
              {failure.detail}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}

function Field({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <dt className="text-xs uppercase tracking-wide text-neutral-500">
        {label}
      </dt>
      <dd className="mt-0.5 font-mono text-sm text-neutral-800 dark:text-neutral-200">
        {children}
      </dd>
    </div>
  );
}

export function WorkerView({ id }: { id: string }) {
  const { data, error, loading, refresh } = usePoll<Payload>(
    `/api/workers/${id}`,
  );

  if (error) {
    if (error.error === "not_found") {
      return (
        <ErrorPanel
          error={{ error: "not_found", message: `No worker with id ${id}.` }}
        />
      );
    }
    return <ErrorPanel error={error} />;
  }
  if (loading || !data) {
    return <p className="text-sm text-neutral-500">Loading worker…</p>;
  }

  const { worker, events } = data;
  const response = finalResponse(events);

  return (
    <div className="space-y-6">
      <div>
        <Link
          href="/"
          className="text-sm text-blue-600 hover:underline dark:text-blue-400"
        >
          ← Fleet
        </Link>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <h1 className="font-mono text-xl font-semibold">{worker.id}</h1>
        <StatusBadge status={worker.status} />
      </div>

      <p className="whitespace-pre-wrap rounded-lg border border-neutral-200 bg-neutral-50 p-4 text-sm dark:border-neutral-800 dark:bg-neutral-900">
        {worker.task}
      </p>

      <dl className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        <Field label="Duration">{fmtDuration(workerDuration(worker))}</Field>
        <Field label="Spawned">{fmtClock(worker.spawned_at)}</Field>
        <Field label="Finished">
          {worker.finished_at ? fmtClock(worker.finished_at) : "—"}
        </Field>
        <Field label="Est. cost">{fmtCost(worker.cost_estimate)}</Field>
      </dl>

      {worker.endpoint && (
        <Field label="Endpoint">
          <span className="break-all">{worker.endpoint}</span>
        </Field>
      )}

      {response && (
        <section className="space-y-2">
          <h2 className="text-sm font-semibold">Output</h2>
          <pre className="overflow-x-auto whitespace-pre-wrap rounded-lg border border-neutral-200 bg-neutral-50 p-4 font-mono text-sm dark:border-neutral-800 dark:bg-neutral-900">
            {response}
          </pre>
        </section>
      )}

      <section className="space-y-2">
        <h2 className="text-sm font-semibold">Timeline</h2>
        <ol className="space-y-2">
          {events.map((event) => (
            <li
              key={event.id}
              className="rounded-lg border border-neutral-200 p-3 dark:border-neutral-800"
            >
              <div className="flex items-baseline gap-3">
                <span className="font-mono text-xs text-neutral-500">
                  {fmtClock(event.ts)}
                </span>
                <span className="font-mono text-sm font-medium">
                  {event.type}
                </span>
              </div>
              {event.payload && (
                <pre className="mt-2 overflow-x-auto whitespace-pre-wrap font-mono text-xs text-neutral-600 dark:text-neutral-400">
                  {JSON.stringify(event.payload, null, 2)}
                </pre>
              )}
            </li>
          ))}
        </ol>
      </section>

      {isLive(worker) && <KillButton worker={worker} onDone={refresh} />}
    </div>
  );
}
