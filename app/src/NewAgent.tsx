import { useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { isFleetError, type BoxRow } from "./types";

/**
 * Create an agent.
 *
 * One request. The box arrives with its identity already on it — FLOTTA-21
 * injects it at creation — which is why this is a button rather than a button
 * followed by a terminal. That was the point of doing FLOTTA-21 before M8.
 */
export function NewAgent({ onCreated }: { onCreated: (box: BoxRow) => void }) {
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function create(event: React.FormEvent) {
    event.preventDefault();
    const trimmed = name.trim();
    if (!trimmed || busy) return;
    setBusy(true);
    setError(null);
    try {
      const box = await invoke<BoxRow>("create_agent", { name: trimmed });
      setName("");
      onCreated(box);
    } catch (err) {
      setError(isFleetError(err) ? err.detail : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={create} className="border-t border-neutral-200 p-3">
      <div className="flex gap-2">
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="new agent, e.g. eng-b"
          disabled={busy}
          className="min-w-0 flex-1 rounded border border-neutral-300 px-2 py-1.5 text-xs focus:border-neutral-500 focus:outline-none disabled:bg-neutral-50"
        />
        <button
          type="submit"
          disabled={busy || name.trim() === ""}
          className="rounded bg-neutral-900 px-3 py-1.5 text-xs font-medium text-white disabled:opacity-40"
        >
          {busy ? "…" : "Create"}
        </button>
      </div>
      {busy && (
        <p className="mt-2 text-[11px] text-neutral-500">
          Provisioning a machine and a volume. This takes a minute.
        </p>
      )}
      {error && (
        <p className="mt-2 whitespace-pre-wrap rounded bg-red-50 px-2 py-1.5 text-[11px] text-red-800">
          {error}
        </p>
      )}
    </form>
  );
}
