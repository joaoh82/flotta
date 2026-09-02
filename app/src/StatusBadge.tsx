/**
 * Colour carries meaning, so it is always paired with the status word — the
 * badge is never colour-only. Copied deliberately from the dashboard's badge
 * rather than shared: two small components that happen to agree are cheaper
 * than a package neither owns, and the vocabularies could legitimately diverge
 * (the app cares about `stopped` far more than the dashboard does).
 */
const STYLES: Record<string, string> = {
  provisioning: "bg-amber-100 text-amber-800",
  running: "bg-blue-100 text-blue-800",
  // A stopped box is idle, not broken and not finished. Most of the fleet is
  // stopped most of the time — that is the cost argument working, so this must
  // read as calm rather than as a warning.
  stopped: "bg-slate-100 text-slate-700",
  torn_down: "bg-neutral-200 text-neutral-600",
};

export function StatusBadge({ status }: { status: string }) {
  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 font-mono text-xs font-medium ${
        STYLES[status] ?? STYLES.torn_down
      }`}
    >
      {status}
    </span>
  );
}
