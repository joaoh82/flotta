import type { ApiError } from "@/lib/types";

/**
 * Explains *why* there is nothing to show.
 *
 * A missing store is the common case — the dashboard was started somewhere the
 * fleet file is not — and it must never be rendered as a healthy, empty fleet.
 * So the panel names the exact path it looked at and how to point it elsewhere.
 */
export function ErrorPanel({ error }: { error: ApiError }) {
  const missing = error.error === "store_missing";
  return (
    <div className="rounded-lg border border-amber-300 bg-amber-50 p-4 text-sm dark:border-amber-900 dark:bg-amber-950/40">
      <p className="font-medium text-amber-900 dark:text-amber-200">
        {missing ? "No fleet-state store found" : "Cannot read the fleet state"}
      </p>
      <p className="mt-1 text-amber-800 dark:text-amber-300">{error.message}</p>
      {error.store && (
        <p className="mt-2 font-mono text-xs text-amber-700 dark:text-amber-400">
          looked at: {error.store}
        </p>
      )}
      {missing && (
        <p className="mt-3 text-amber-800 dark:text-amber-300">
          Create an agent with{" "}
          <code className="font-mono">uv run flotta create &lt;name&gt;</code>, or
          point the control plane at an existing store by setting{" "}
          <code className="font-mono">FLOTTA_STORE</code> before{" "}
          <code className="font-mono">just serve</code>.
        </p>
      )}
    </div>
  );
}
