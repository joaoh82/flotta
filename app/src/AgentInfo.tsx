import { useCallback, useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { StatusBadge } from "./StatusBadge";
import { drift, fieldsOf, imageParts } from "./machine";
import { isFleetError, type BoxRow, type MachineView } from "./types";

/**
 * What is actually true about an agent's machine.
 *
 * Everything else in this window reads the fleet store, which is a *belief*:
 * a row written by whatever last reached the substrate. That is the right
 * thing for a list — it is cheap, and every verb that touches a box updates
 * it — but it means the app has never been able to answer "what image is this
 * agent on", "how big is its disk", or "is the row even right".
 *
 * So this panel asks the substrate, and shows the answer **beside** the row
 * rather than instead of it. The two usually agree. When they do not, that is
 * the most useful thing on the screen — and a single reconciled status would
 * be the window quietly picking a winner and telling you a tidy story.
 *
 * It is fetched on open and on Recheck, never on a timer: it costs a `flyctl`
 * subprocess on the control plane.
 */
export function AgentInfo({ box, onClose }: { box: BoxRow; onClose: () => void }) {
  const [view, setView] = useState<MachineView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setView(await invoke<MachineView>("agent_machine", { id: box.id }));
      setError(null);
    } catch (e) {
      setError(isFleetError(e) ? e.detail : String(e));
    } finally {
      setLoading(false);
    }
  }, [box.id]);

  useEffect(() => {
    void load();
  }, [load]);

  const machine = view?.machine ?? null;
  // The row from this response when there is one — it was read at the same
  // moment as the machine, so comparing them is comparing two things that were
  // true together. Falling back to the list's row would compare the substrate
  // now against a row from up to a poll ago and invent drift out of the gap.
  const row = view?.box ?? box;
  const disagreement = machine ? drift(row.status, machine.state) : null;
  const image = machine?.image ? imageParts(machine.image) : null;

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between border-b border-neutral-200 px-4 py-2">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium">{box.name}</span>
          <span className="text-xs text-neutral-500">Info</span>
        </div>
        <div className="flex items-center gap-1">
          <button
            onClick={() => void load()}
            disabled={loading}
            className="rounded px-2 py-1 text-xs text-neutral-600 hover:bg-neutral-100 disabled:opacity-40"
          >
            {loading ? "Asking…" : "Recheck"}
          </button>
          <button
            onClick={onClose}
            className="rounded px-2 py-1 text-xs text-neutral-500 hover:bg-neutral-100"
          >
            Close
          </button>
        </div>
      </div>

      <div className="min-h-0 flex-1 space-y-5 overflow-auto p-4">
        {/* The two sources, side by side and labelled as sources. Which one a
            number came from is the question this panel exists to answer. */}
        <section className="grid grid-cols-2 gap-3">
          <div className="rounded border border-neutral-200 p-3">
            <p className="text-[11px] uppercase tracking-wide text-neutral-400">Fleet record</p>
            <div className="mt-1.5">
              <StatusBadge status={row.status} />
            </div>
            <p className="mt-2 text-[11px] text-neutral-500">
              What the control plane believes, and what the list shows.
            </p>
          </div>
          <div className="rounded border border-neutral-200 p-3">
            <p className="text-[11px] uppercase tracking-wide text-neutral-400">Machine</p>
            <p className="mt-1.5 font-mono text-sm">
              {machine ? machine.state : <span className="text-neutral-400">—</span>}
            </p>
            <p className="mt-2 text-[11px] text-neutral-500">
              What the substrate said just now.
            </p>
          </div>
        </section>

        {disagreement && (
          <p className="rounded border border-amber-200 bg-amber-50 px-3 py-2 text-[11px] text-amber-900">
            {disagreement}
          </p>
        )}

        {/* Not an error: a box being built has no machine yet, and saying so is
            the truth. Rendering a substrate failure here instead would be
            FLOTTA-29 again — a scary screen for a creation going perfectly
            well. */}
        {view?.unavailable && (
          <p className="rounded border border-neutral-200 bg-neutral-50 px-3 py-2 text-[11px] text-neutral-600">
            {view.unavailable}
          </p>
        )}

        {error && (
          <p className="rounded border border-red-200 bg-red-50 px-3 py-2 text-[11px] text-red-800">
            {error}
          </p>
        )}

        {image && (
          <section>
            <h3 className="text-[11px] uppercase tracking-wide text-neutral-400">Image</h3>
            <dl className="mt-1.5 space-y-1">
              <Line label="Repository" value={image.repository} mono />
              {image.tag && <Line label="Tag" value={image.tag} mono />}
              {/* The digest is the only part nobody can move under you: an
                  upgrade that changes nothing else changes this, which is what
                  makes it the thing to compare after one. */}
              {image.digest && <Line label="Digest" value={image.digest} mono />}
            </dl>
            <p className="mt-1.5 text-[11px] text-neutral-400">
              Which Hermes this image carries is not recorded anywhere yet — the tag is
              the closest thing to a version until it is.
            </p>
          </section>
        )}

        {machine && (
          <section>
            <h3 className="text-[11px] uppercase tracking-wide text-neutral-400">Machine</h3>
            <dl className="mt-1.5 space-y-1">
              {fieldsOf(machine).map((field) => (
                <Line key={field.label} label={field.label} value={field.value} mono={field.mono} />
              ))}
            </dl>
          </section>
        )}

        <section>
          <h3 className="text-[11px] uppercase tracking-wide text-neutral-400">Agent</h3>
          <dl className="mt-1.5 space-y-1">
            <Line label="Id" value={row.id} mono />
            <Line label="Address" value={`${row.name}.flotta.dev`} mono />
            {row.endpoint && <Line label="Endpoint" value={row.endpoint} mono />}
            {row.created_at && <Line label="Agent created" value={row.created_at} />}
          </dl>
        </section>
      </div>
    </div>
  );
}

function Line({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex gap-3 text-[12px]">
      <dt className="w-32 shrink-0 text-neutral-500">{label}</dt>
      <dd className={`min-w-0 break-all ${mono ? "font-mono text-[11px]" : ""}`}>{value}</dd>
    </div>
  );
}
