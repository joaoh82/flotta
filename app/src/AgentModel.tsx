import { useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { isFleetError, type BoxRow } from "./types";

/**
 * Which model an agent runs, and a way to change it (FLOTTA-39).
 *
 * The row says whose choice the model is — the agent's own or the fleet's —
 * because "why is this one on a different model" is the first question a
 * fleet with several of them raises.
 */
export function AgentModel({
  box,
  onChanged,
}: {
  box: BoxRow;
  onChanged: (box: BoxRow) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState<null | "set" | "reset">(null);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);

  const running = box.status === "running";
  const ownModel = box.model_source === "agent";

  async function change(model: string | null) {
    setBusy(model === null ? "reset" : "set");
    setError(null);
    try {
      const updated = await invoke<BoxRow>("set_agent_model", { id: box.id, model });
      onChanged(updated);
      setDone(`Now runs ${updated.model ?? "the fleet's model"}.`);
      setEditing(false);
    } catch (e) {
      setError(isFleetError(e) ? e.detail : String(e));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="text-[12px]">
      <div className="flex gap-3">
        <dt className="w-32 shrink-0 text-neutral-500">Model</dt>
        <dd className="min-w-0 flex-1">
          {box.model ? (
            <>
              <span className="font-mono text-[11px]">{box.model}</span>
              <span className="ml-1.5 text-[11px] text-neutral-400">
                {ownModel ? "its own" : "the fleet's"}
              </span>
            </>
          ) : (
            <span className="text-neutral-500">None configured for this fleet</span>
          )}
          {!editing && (
            <button
              onClick={() => {
                setDraft(ownModel ? (box.model ?? "") : "");
                setEditing(true);
                setDone(null);
              }}
              className="ml-2 text-[11px] text-neutral-500 hover:text-neutral-900"
            >
              Change
            </button>
          )}
        </dd>
      </div>

      {editing && (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            if (draft.trim() && !busy) void change(draft.trim());
          }}
          className="ml-35 mt-1.5 rounded border border-neutral-200 bg-neutral-50 p-2.5 text-[11px] text-neutral-700"
        >
          <input
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="anthropic/claude-sonnet-4.5"
            spellCheck={false}
            autoFocus
            disabled={busy !== null}
            className="w-full rounded border border-neutral-300 bg-white px-2 py-1 font-mono text-[11px] focus:border-neutral-500 focus:outline-none"
          />
          <p className="mt-1.5">
            {running
              ? `${box.name} is running, so it restarts to switch — about a minute, and any turn in progress is interrupted.`
              : `${box.name} is asleep and stays asleep; it switches when it next wakes.`}{" "}
            Its memory and conversations are kept.
          </p>
          <div className="mt-2 flex flex-wrap gap-2">
            <button
              type="submit"
              disabled={busy !== null || draft.trim() === ""}
              className="rounded bg-neutral-900 px-3 py-1 text-xs font-medium text-white disabled:opacity-40"
            >
              {busy === "set" ? "Switching…" : "Use this model"}
            </button>
            {ownModel && (
              <button
                type="button"
                onClick={() => void change(null)}
                disabled={busy !== null}
                className="rounded border border-neutral-300 bg-white px-3 py-1 text-xs hover:bg-neutral-50 disabled:opacity-40"
              >
                {busy === "reset" ? "Switching…" : "Use the fleet's model"}
              </button>
            )}
            <button
              type="button"
              onClick={() => setEditing(false)}
              disabled={busy !== null}
              className="rounded px-2 py-1 text-xs text-neutral-500 hover:text-neutral-900 disabled:opacity-40"
            >
              Cancel
            </button>
          </div>
        </form>
      )}

      {error && <p className="ml-35 mt-1 text-[11px] text-red-700">{error}</p>}
      {done && <p className="ml-35 mt-1 text-[11px] text-emerald-700">{done}</p>}
    </div>
  );
}
