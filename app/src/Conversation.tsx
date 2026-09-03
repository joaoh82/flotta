import { useEffect, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import type { AgentEvent, Turn } from "./types";

/**
 * A conversation with one agent.
 *
 * The socket lives in Rust and outlives this component, so everything here is
 * a projection of events rather than a request/response cycle. That is not an
 * implementation detail leaking upward — it is the protocol: a reply arrives
 * as an event, never as the answer to the submit.
 */
export function Conversation({ boxName }: { boxName: string }) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [status, setStatus] = useState<AgentEvent["kind"]>("waking");
  const [draft, setDraft] = useState("");
  const bottom = useRef<HTMLDivElement>(null);

  // Keyed by box so switching agents starts a clean transcript rather than
  // showing one agent's words under another's name.
  useEffect(() => {
    setTurns([]);
    setStatus("waking");

    let alive = true;
    const unlisten = listen<AgentEvent>("agent://event", (event) => {
      const payload = event.payload;
      // Events carry the box they belong to because several conversations run
      // at once (M8.3). Without this check, one agent's reply lands in
      // another's transcript.
      if (!alive || payload.box_name !== boxName) return;

      setStatus(payload.kind);
      if (payload.kind === "reply") {
        setTurns((t) => [...t, { from: "agent", text: payload.text }]);
      } else if (payload.kind === "failed") {
        setTurns((t) => [...t, { from: "system", text: payload.detail }]);
      }
    });

    void invoke("open_conversation", { boxName }).catch((err) => {
      if (!alive) return;
      setStatus("failed");
      setTurns((t) => [
        ...t,
        { from: "system", text: typeof err === "object" && err && "detail" in err ? String((err as { detail: unknown }).detail) : String(err) },
      ]);
    });

    return () => {
      alive = false;
      void unlisten.then((off) => off());
    };
  }, [boxName]);

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth" });
  }, [turns, status]);

  const busy = status === "waking" || status === "thinking";

  async function send(event: React.FormEvent) {
    event.preventDefault();
    const text = draft.trim();
    if (!text || busy) return;
    setDraft("");
    setTurns((t) => [...t, { from: "you", text }]);
    setStatus("thinking");
    try {
      await invoke("send_prompt", { boxName, text });
    } catch (err) {
      setStatus("failed");
      setTurns((t) => [...t, { from: "system", text: String(err) }]);
    }
  }

  return (
    <div className="flex h-full flex-col">
      <div className="flex-1 space-y-4 overflow-auto p-5">
        {turns.length === 0 && !busy && (
          <p className="text-xs text-neutral-500">
            {boxName} is listening. Its memory is on its own disk, so it
            remembers what you told it last time.
          </p>
        )}

        {turns.map((turn, i) => (
          <div key={i} className="text-sm">
            <div className="mb-0.5 font-mono text-[11px] text-neutral-400">
              {turn.from === "you" ? "you" : turn.from === "agent" ? boxName : "flotta"}
            </div>
            <div
              className={
                turn.from === "system"
                  ? "whitespace-pre-wrap rounded bg-red-50 px-3 py-2 text-red-800"
                  : "whitespace-pre-wrap text-neutral-900"
              }
            >
              {turn.text}
            </div>
          </div>
        ))}

        {/* Waking is not a hang, and saying so is the difference between a
            slow app and a broken one. A box is asleep most of the time —
            that is the cost model working. */}
        {status === "waking" && (
          <p className="text-xs text-neutral-500">
            Waking {boxName}… the machine starts in under a second, then Hermes
            loads. First contact takes 10–60 seconds.
          </p>
        )}
        {status === "thinking" && (
          <p className="text-xs text-neutral-500">{boxName} is thinking…</p>
        )}
        <div ref={bottom} />
      </div>

      <form onSubmit={send} className="border-t border-neutral-200 p-3">
        <div className="flex gap-2">
          <input
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder={busy ? "…" : `Message ${boxName}`}
            disabled={busy}
            className="flex-1 rounded border border-neutral-300 px-3 py-2 text-sm focus:border-neutral-500 focus:outline-none disabled:bg-neutral-50"
          />
          <button
            type="submit"
            disabled={busy || draft.trim() === ""}
            className="rounded bg-neutral-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-40"
          >
            Send
          </button>
        </div>
      </form>
    </div>
  );
}
