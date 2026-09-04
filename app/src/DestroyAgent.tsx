import { useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { isFleetError, type BoxRow } from "./types";

/**
 * Destroy an agent, and everything it remembers.
 *
 * Confirmed by typing the name, not by clicking through a dialog — and checked
 * again in Rust, because this is the verb that deletes months of an agent's
 * memory. Typing a name is a deliberate act; clicking "OK" is a reflex.
 */
export function DestroyAgent({
  box,
  onDestroyed,
  onCancel,
}: {
  box: BoxRow;
  onDestroyed: () => void;
  onCancel: () => void;
}) {
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function destroy(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await invoke("destroy_agent", {
        id: box.id,
        name: box.name,
        confirm,
      });
      onDestroyed();
    } catch (err) {
      setError(isFleetError(err) ? err.detail : String(err));
      setBusy(false);
    }
  }

  return (
    <form onSubmit={destroy} className="space-y-3 p-5">
      <div>
        <h2 className="text-sm font-semibold">Destroy {box.name}?</h2>
        <p className="mt-1 max-w-prose text-xs text-neutral-600">
          This deletes the machine <em>and its volume</em>. Everything{" "}
          {box.name} has learned — its memory, its skills, every conversation —
          goes with it, and none of it can be recovered.
        </p>
        <p className="mt-2 max-w-prose text-xs text-neutral-600">
          To stop paying for CPU without losing any of that, let it sleep
          instead: an idle agent suspends on its own and wakes when addressed.
        </p>
      </div>

      <label className="block">
        <span className="text-xs text-neutral-700">
          Type <code className="font-mono font-medium">{box.name}</code> to
          confirm
        </span>
        <input
          value={confirm}
          onChange={(e) => setConfirm(e.target.value)}
          className="mt-1 w-full rounded border border-neutral-300 px-2 py-1.5 font-mono text-xs focus:border-neutral-500 focus:outline-none"
        />
      </label>

      {error && (
        <p className="whitespace-pre-wrap rounded bg-red-50 px-3 py-2 text-xs text-red-800">
          {error}
        </p>
      )}

      <div className="flex gap-2">
        <button
          type="submit"
          disabled={busy || confirm !== box.name}
          className="rounded bg-red-700 px-3 py-1.5 text-xs font-medium text-white disabled:opacity-40"
        >
          {busy ? "Destroying…" : "Destroy forever"}
        </button>
        <button
          type="button"
          onClick={onCancel}
          className="rounded border border-neutral-300 px-3 py-1.5 text-xs font-medium hover:bg-neutral-50"
        >
          Keep it
        </button>
      </div>
    </form>
  );
}
