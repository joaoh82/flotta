import { useCallback, useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { exchangesIn, grantable, peerLabel, type Exchange } from "./colleagues";
import { POLL_MS } from "./poll";
import { isFleetError, type BoxEvent, type BoxRow, type Peer } from "./types";

/**
 * Which agents this one may ask, and what they have said to each other (M7).
 *
 * **Named "Colleagues" because the agents are told to look for it.** A refusal
 * from the control plane and the skill on every box both send the person to
 * "this agent's Info panel, Colleagues". The first version of M7 shipped that
 * sentence before this section existed, which is the window lying from the
 * other side — the label here is the other half of that promise, so renaming
 * it means changing the copy in `control/app.py` and the skill too.
 *
 * The conversations are here rather than in the transcript because the
 * answering agent holds each one in a **separate session**: a colleague's
 * question is not part of the thread a person was having with it. So this is
 * the only place eng-r's side of an exchange is visible at all.
 */
export function AgentColleagues({
  boxId,
  boxName,
  fleet,
}: {
  boxId: string;
  boxName: string;
  fleet: BoxRow[];
}) {
  const [peers, setPeers] = useState<Peer[] | null>(null);
  const [choice, setChoice] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [exchanges, setExchanges] = useState<Exchange[] | null>(null);

  const load = useCallback(async () => {
    try {
      setPeers(await invoke<Peer[]>("agent_peers", { id: boxId }));
      setError(null);
    } catch (e) {
      // Unknown is not "none": the same rule as the repository list.
      setPeers(null);
      setError(isFleetError(e) ? e.detail : String(e));
    }
  }, [boxId]);

  useEffect(() => {
    void load();
  }, [load]);

  // Polled while the panel is open: an exchange takes tens of seconds, and
  // watching one arrive is the point of showing them. Failures are kept quiet
  // and leave the last good list up — this is a secondary view, and flashing
  // an error every five seconds over a flaky network would bury the grants.
  useEffect(() => {
    let live = true;
    async function tick() {
      try {
        const events = await invoke<BoxEvent[]>("agent_timeline", { id: boxId });
        if (live) setExchanges(exchangesIn(events));
      } catch {
        /* keep what we had */
      }
    }
    void tick();
    const timer = setInterval(() => void tick(), POLL_MS);
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, [boxId]);

  async function change(command: "grant_peer" | "revoke_peer", peer: string) {
    setBusy(true);
    setError(null);
    try {
      setPeers(await invoke<Peer[]>(command, { id: boxId, peer }));
      if (command === "grant_peer") setChoice("");
    } catch (e) {
      setError(isFleetError(e) ? e.detail : String(e));
    } finally {
      setBusy(false);
    }
  }

  const offered = peers ? grantable(fleet, boxId, peers) : [];

  return (
    <section>
      <h3 className="text-[11px] uppercase tracking-wide text-neutral-400">Colleagues</h3>

      {peers === null && !error && (
        <p className="mt-1.5 text-[11px] text-neutral-500">Loading…</p>
      )}

      {peers !== null && peers.length === 0 && (
        <p className="mt-1.5 text-[11px] text-neutral-500">
          None yet. {boxName} cannot ask any other agent for help.
        </p>
      )}

      {peers !== null && peers.length > 0 && (
        <ul className="mt-1.5 divide-y divide-neutral-100 rounded border border-neutral-200">
          {peers.map((peer) => (
            <li key={peer.id} className="flex items-center justify-between gap-3 px-2.5 py-1.5">
              <div className="min-w-0">
                <div className="truncate text-[12px] text-neutral-800">{peerLabel(peer)}</div>
                {peer.description && (
                  <div className="truncate text-[11px] text-neutral-500">{peer.description}</div>
                )}
              </div>
              <button
                onClick={() => void change("revoke_peer", peer.id)}
                disabled={busy}
                className="shrink-0 text-[11px] text-neutral-500 hover:text-red-700 disabled:opacity-40"
              >
                Revoke
              </button>
            </li>
          ))}
        </ul>
      )}

      {peers !== null && (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            if (choice && !busy) void change("grant_peer", choice);
          }}
          className="mt-2 flex gap-2"
        >
          <select
            value={choice}
            onChange={(e) => setChoice(e.target.value)}
            disabled={offered.length === 0}
            className="flex-1 rounded border border-neutral-300 bg-white px-2 py-1.5 text-[12px] focus:border-neutral-500 focus:outline-none disabled:text-neutral-400"
          >
            <option value="">
              {offered.length === 0 ? "No other agents to add" : "Choose an agent…"}
            </option>
            {offered.map((box) => (
              <option key={box.id} value={box.id}>
                {peerLabel(box)}
              </option>
            ))}
          </select>
          <button
            type="submit"
            disabled={busy || choice === ""}
            className="rounded bg-neutral-900 px-3 py-1.5 text-xs font-medium text-white disabled:opacity-40"
          >
            Let it ask
          </button>
        </form>
      )}

      {error && <p className="mt-1 text-[11px] text-red-700">{error}</p>}

      {/* The two limits a person will otherwise discover by surprise: grants
          are one-way, and there is a brake. */}
      <p className="mt-2 text-[11px] leading-relaxed text-neutral-400">
        {boxName} can send these agents a question and gets their reply back.
        It is one way: for them to ask {boxName}, add it from their own panel.
        Flotta carries every message and stops loops and runaway back-and-forth
        on its own. {boxName} cannot add colleagues itself.
      </p>

      {exchanges !== null && exchanges.length > 0 && (
        <>
          <h4 className="mt-3 text-[11px] uppercase tracking-wide text-neutral-400">
            Recent conversations
          </h4>
          <ul className="mt-1.5 space-y-2">
            {exchanges.slice(0, 10).map((x) => (
              <ExchangeRow key={`${x.at}-${x.direction}-${x.peer}`} exchange={x} self={boxName} />
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

const OUTCOME: Record<Exchange["outcome"], { label: string; tone: string }> = {
  answered: { label: "answered", tone: "text-emerald-700" },
  waiting: { label: "waiting for a reply…", tone: "animate-pulse text-neutral-500" },
  failed: { label: "no answer", tone: "text-red-700" },
  refused: { label: "stopped by Flotta", tone: "text-amber-700" },
  unknown: { label: "no reply recorded", tone: "text-neutral-500" },
};

function ExchangeRow({ exchange: x, self }: { exchange: Exchange; self: string }) {
  const outcome = OUTCOME[x.outcome];
  const [from, to] = x.direction === "asked" ? [self, x.peer] : [x.peer, self];
  const when = new Date(x.at);
  return (
    <li className="rounded border border-neutral-200 p-2 text-[11px]">
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-neutral-700">
          <span className="font-medium">{from}</span> asked{" "}
          <span className="font-medium">{to}</span>
        </span>
        <span className="shrink-0 text-neutral-400">
          <span className={outcome.tone}>{outcome.label}</span>
          {x.seconds !== null && ` · ${Math.round(x.seconds)}s`}
          {" · "}
          {Number.isNaN(when.getTime()) ? x.at : when.toLocaleTimeString()}
        </span>
      </div>
      {x.question && <p className="mt-1 whitespace-pre-wrap text-neutral-800">{x.question}</p>}
      {x.reply && (
        <p className="mt-1 whitespace-pre-wrap border-l-2 border-neutral-200 pl-2 text-neutral-600">
          {x.reply}
        </p>
      )}
      {x.reason && <p className="mt-1 text-neutral-500">{x.reason}</p>}
    </li>
  );
}
