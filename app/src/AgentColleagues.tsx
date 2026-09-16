import { useCallback, useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { exchangesIn, peerLabel, type Exchange } from "./colleagues";
import { POLL_MS } from "./poll";
import { isFleetError, type BoxEvent, type Colleagues, type Peer } from "./types";

/**
 * Which agents this one may ask, and what they have said to each other (M7).
 *
 * **Everyone, unless blocked** (FLOTTA-65). FLOTTA-54 made this a list you
 * added to, and a new agent was nobody's colleague until someone went round
 * every panel. Now the list is the fleet, and the control here is Block.
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
export function AgentColleagues({ boxId, boxName }: { boxId: string; boxName: string }) {
  const [lists, setLists] = useState<Colleagues | null>(null);
  /** Which row's button is in flight, so only that one greys out. */
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [exchanges, setExchanges] = useState<Exchange[] | null>(null);

  const load = useCallback(async () => {
    try {
      setLists(await invoke<Colleagues>("agent_colleagues", { id: boxId }));
      setError(null);
    } catch (e) {
      // Unknown is not "none": the same rule as the repository list.
      setLists(null);
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

  async function change(command: "block_peer" | "allow_peer", peer: string) {
    setBusy(peer);
    setError(null);
    try {
      setLists(await invoke<Colleagues>(command, { id: boxId, peer }));
    } catch (e) {
      setError(isFleetError(e) ? e.detail : String(e));
    } finally {
      setBusy(null);
    }
  }

  return (
    <section>
      <h3 className="text-[11px] uppercase tracking-wide text-neutral-400">Colleagues</h3>

      {lists === null && !error && (
        <p className="mt-1.5 text-[11px] text-neutral-500">Loading…</p>
      )}

      {lists !== null && lists.peers.length === 0 && (
        <p className="mt-1.5 text-[11px] text-neutral-500">
          {lists.blocked.length === 0
            ? `No other agents yet. Every agent you create can be asked by ${boxName}.`
            : `${boxName} is blocked from asking every other agent.`}
        </p>
      )}

      {lists !== null && lists.peers.length > 0 && (
        <PeerList
          peers={lists.peers}
          action="Block"
          danger
          busy={busy}
          onAction={(peer) => void change("block_peer", peer.id)}
        />
      )}

      {lists !== null && lists.blocked.length > 0 && (
        <>
          <h4 className="mt-3 text-[11px] uppercase tracking-wide text-neutral-400">
            Blocked — {boxName} may not ask
          </h4>
          <PeerList
            peers={lists.blocked}
            action="Allow"
            busy={busy}
            onAction={(peer) => void change("allow_peer", peer.id)}
          />
        </>
      )}

      {error && <p className="mt-1 text-[11px] text-red-700">{error}</p>}

      {/* The two limits a person will otherwise discover by surprise: grants
          are one-way, and there is a brake. */}
      <p className="mt-2 text-[11px] leading-relaxed text-neutral-400">
        {boxName} can ask any agent on this fleet and gets the reply back —
        including agents created later. Blocking is one way: it stops {boxName}
        asking that agent, not the other way round. Flotta carries every
        message and stops loops and runaway back-and-forth on its own.{" "}
        {boxName} cannot change this list itself. Mention an agent with @ in a
        chat to suggest asking it.
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

function PeerList({
  peers,
  action,
  danger,
  busy,
  onAction,
}: {
  peers: Peer[];
  action: string;
  danger?: boolean;
  busy: string | null;
  onAction: (peer: Peer) => void;
}) {
  return (
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
            onClick={() => onAction(peer)}
            disabled={busy !== null}
            className={`shrink-0 text-[11px] text-neutral-500 disabled:opacity-40 ${
              danger ? "hover:text-red-700" : "hover:text-neutral-900"
            }`}
          >
            {busy === peer.id ? "…" : action}
          </button>
        </li>
      ))}
    </ul>
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
