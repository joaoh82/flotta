import { useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { isFleetError } from "./types";

/**
 * Start this agent's conversation over.
 *
 * **Why the window needs this at all.** Hermes renders an agent's system
 * prompt once, when a conversation starts, and keeps it for that
 * conversation's life. The standing instructions on the agent's disk are read
 * at that moment and never again, so an agent that has already talked keeps
 * whatever persona it was born with. Starting over is the boundary that lets
 * new instructions take effect, and until this button existed there was no way
 * to ask for one from the app.
 *
 * Two clicks, because the messages on screen go away. Nothing is destroyed:
 * the agent's memory lives on its own disk rather than in the conversation, so
 * it keeps what it learned and loses only the visible back-and-forth.
 */
export function StartOver({ boxName }: { boxName: string }) {
  const [asking, setAsking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function confirm() {
    setError(null);
    try {
      await invoke("reset_conversation", { boxName });
      setAsking(false);
    } catch (err) {
      // Stays open on failure. Closing the confirm on an error would leave the
      // old transcript on screen with nothing to say why it is still there.
      setError(isFleetError(err) ? err.detail : String(err));
    }
  }

  if (!asking) {
    return (
      <button
        onClick={() => setAsking(true)}
        className="rounded px-2 py-1 text-xs text-neutral-500 hover:bg-neutral-100"
      >
        Start over
      </button>
    );
  }

  return (
    <div className="flex items-center gap-2">
      <span className="text-xs text-neutral-500">
        {error ?? `Clear this conversation? ${boxName} keeps its memory.`}
      </span>
      <button
        onClick={() => void confirm()}
        className="rounded bg-neutral-900 px-2 py-1 text-xs font-medium text-white"
      >
        {error ? "Try again" : "Start over"}
      </button>
      <button
        onClick={() => {
          setAsking(false);
          setError(null);
        }}
        className="rounded px-2 py-1 text-xs text-neutral-500 hover:bg-neutral-100"
      >
        Cancel
      </button>
    </div>
  );
}
