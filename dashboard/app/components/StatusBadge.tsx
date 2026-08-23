import type { WorkerStatus } from "@/lib/types";

/**
 * Colour carries meaning here, so it is paired with the status word itself —
 * the badge is never colour-only.
 */
const STYLES: Record<WorkerStatus, string> = {
  provisioning:
    "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300",
  running: "bg-blue-100 text-blue-800 dark:bg-blue-950 dark:text-blue-300",
  // A stopped box is idle, not broken and not finished — cool and quiet, but
  // clearly still part of the fleet, unlike the grey of torn_down.
  stopped: "bg-slate-100 text-slate-700 dark:bg-slate-900 dark:text-slate-300",
  done: "bg-green-100 text-green-800 dark:bg-green-950 dark:text-green-300",
  failed: "bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-300",
  torn_down:
    "bg-neutral-200 text-neutral-700 dark:bg-neutral-800 dark:text-neutral-400",
};

export function StatusBadge({ status }: { status: WorkerStatus }) {
  const style = STYLES[status] ?? STYLES.torn_down;
  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 font-mono text-xs font-medium ${style}`}
    >
      {status}
    </span>
  );
}
