"use client";

import Link from "next/link";
import { useState } from "react";

import { fmtAge, fmtCost, fmtDuration, workerDuration } from "@/lib/format";
import { isLive, type Worker } from "@/lib/types";

import { ErrorPanel } from "./ErrorPanel";
import { StatusBadge } from "./StatusBadge";
import { usePoll } from "./usePoll";

export function FleetTable() {
  const { data, error, loading } = usePoll<{ workers: Worker[] }>(
    "/api/workers",
  );
  const [showFinished, setShowFinished] = useState(false);

  if (error) return <ErrorPanel error={error} />;
  if (loading) {
    return <p className="text-sm text-neutral-500">Reading fleet state…</p>;
  }

  const all = data?.workers ?? [];
  const live = all.filter(isLive);
  // Same default as `flotta ps`: show what is live, because finished workers
  // pile up fast and are rarely what you opened the page to see.
  const shown = showFinished ? all : live;
  const finishedCount = all.length - live.length;

  return (
    <div className="space-y-3">
      <div className="flex items-baseline justify-between gap-4">
        <p className="text-sm text-neutral-500">
          {live.length} live
          {finishedCount > 0 && `, ${finishedCount} finished`}
        </p>
        {finishedCount > 0 && (
          <button
            type="button"
            onClick={() => setShowFinished((v) => !v)}
            className="text-sm text-blue-600 hover:underline dark:text-blue-400"
          >
            {showFinished ? "Hide finished" : "Show finished"}
          </button>
        )}
      </div>

      {shown.length === 0 ? (
        <p className="rounded-lg border border-dashed border-neutral-300 p-8 text-center text-sm text-neutral-500 dark:border-neutral-700">
          {all.length === 0
            ? "No agents yet. Create one with `uv run flotta create <name>`."
            : "No live workers."}
        </p>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-neutral-200 dark:border-neutral-800">
          <table className="w-full text-left text-sm">
            <thead className="border-b border-neutral-200 bg-neutral-50 text-xs uppercase tracking-wide text-neutral-500 dark:border-neutral-800 dark:bg-neutral-900">
              <tr>
                <th className="px-4 py-2 font-medium">Worker</th>
                <th className="px-4 py-2 font-medium">Status</th>
                <th className="px-4 py-2 font-medium">Task</th>
                <th className="px-4 py-2 font-medium">Duration</th>
                <th className="px-4 py-2 font-medium">Spawned</th>
                <th className="px-4 py-2 font-medium">Est. cost</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-neutral-100 dark:divide-neutral-800">
              {shown.map((worker) => (
                <tr
                  key={worker.id}
                  className="hover:bg-neutral-50 dark:hover:bg-neutral-900/50"
                >
                  <td className="whitespace-nowrap px-4 py-2">
                    <Link
                      href={`/workers/${worker.id}`}
                      className="font-mono text-blue-600 hover:underline dark:text-blue-400"
                    >
                      {worker.id}
                    </Link>
                  </td>
                  <td className="px-4 py-2">
                    <StatusBadge status={worker.status} />
                  </td>
                  <td
                    className="max-w-md truncate px-4 py-2 text-neutral-700 dark:text-neutral-300"
                    title={worker.task}
                  >
                    {worker.task}
                  </td>
                  <td className="whitespace-nowrap px-4 py-2 font-mono text-xs text-neutral-600 dark:text-neutral-400">
                    {fmtDuration(workerDuration(worker))}
                  </td>
                  <td className="whitespace-nowrap px-4 py-2 font-mono text-xs text-neutral-600 dark:text-neutral-400">
                    {fmtAge(worker.spawned_at)}
                  </td>
                  <td className="whitespace-nowrap px-4 py-2 font-mono text-xs text-neutral-600 dark:text-neutral-400">
                    {fmtCost(worker.cost_estimate)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
