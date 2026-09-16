import { useCallback, useEffect, useRef, useState } from "react";
import { delegationOf } from "./colleagues";
import { Markdown } from "./Markdown";
import {
  complete,
  mentionAt,
  segments,
  suggest,
  withNote,
  withoutNote,
  type MentionQuery,
} from "./mentions";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import {
  isFleetError,
  type AgentEvent,
  type ApprovalRequest,
  type Colleagues,
  type Live,
  type Peer,
  type Turn,
  type WorkStep,
} from "./types";
import {
  approvalAfter,
  choiceLabel,
  doingNow,
  duration,
  isBusy,
  liveAfter,
  NOTHING_LIVE,
  reasoningTail,
  sessionScope,
  statusAfter,
  turnsAfter,
  type Status,
} from "./transcript";

/** An error from the Rust side, as a sentence rather than an object. */
function describe(err: unknown): string {
  return isFleetError(err) ? err.detail : String(err);
}

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
  const [status, setStatus] = useState<Status>("waking");
  /** What is streaming and not yet a line of the transcript. */
  const [live, setLive] = useState<Live>(NOTHING_LIVE);
  const [draft, setDraft] = useState("");
  /** The question the agent is blocked on, if any. */
  const [approval, setApproval] = useState<ApprovalRequest | null>(null);
  const [answering, setAnswering] = useState(false);
  // Bumped to re-run the effect and reconnect. A failed conversation is
  // forgotten on the Rust side, so opening again really does reconnect rather
  // than returning success against a dead sender.
  const [attempt, setAttempt] = useState(0);
  const bottom = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLInputElement>(null);
  /**
   * Who this agent may ask — what an `@` offers, and what a mention in a sent
   * message is checked against. The control plane's list rather than the
   * fleet's, so a blocked agent or one still being built is never suggested.
   */
  const [peers, setPeers] = useState<Peer[]>([]);
  const [caret, setCaret] = useState(0);
  /** Which suggestion the arrow keys are on. */
  const [pick, setPick] = useState(0);
  /** Escape closes the list until the mention being typed changes. */
  const [dismissed, setDismissed] = useState<number | null>(null);

  const loadPeers = useCallback(async () => {
    try {
      const lists = await invoke<Colleagues>("agent_colleagues", { id: boxName });
      setPeers(lists.peers);
    } catch {
      // Mentions are a convenience. Without the list the message still sends;
      // it just goes without a note, which is what it would have been anyway.
    }
  }, [boxName]);

  useEffect(() => {
    void loadPeers();
  }, [loadPeers]);

  const at: MentionQuery | null = mentionAt(draft, caret);
  const open = at !== null && at.start !== dismissed;
  const offered = open ? suggest(peers, at.query) : [];

  // A new `@` refreshes the list, so an agent created a minute ago is offered.
  const atStart = at?.start ?? null;
  useEffect(() => {
    if (atStart !== null) void loadPeers();
    else setDismissed(null);
    setPick(0);
  }, [atStart, loadPeers]);

  function choose(peer: Peer) {
    if (!at) return;
    const next = complete(draft, at, caret, peer.name);
    setDraft(next.text);
    setCaret(next.caret);
    // After React has put the new value in, so the caret lands after the name.
    requestAnimationFrame(() => {
      input.current?.focus();
      input.current?.setSelectionRange(next.caret, next.caret);
    });
  }

  function onKeyDown(event: React.KeyboardEvent<HTMLInputElement>) {
    if (offered.length === 0) return;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const step = event.key === "ArrowDown" ? 1 : -1;
      setPick((i) => (i + step + offered.length) % offered.length);
    } else if (event.key === "Enter" || event.key === "Tab") {
      // Choosing, not sending: an Enter with the list open that sent the
      // message would send "@en" to the agent.
      event.preventDefault();
      choose(offered[Math.min(pick, offered.length - 1)]);
    } else if (event.key === "Escape") {
      event.preventDefault();
      setDismissed(at?.start ?? null);
    }
  }

  const peerNames = new Set(peers.map((p) => p.name));

  // Keyed by box so switching agents starts a clean transcript rather than
  // showing one agent's words under another's name.
  useEffect(() => {
    setTurns([]);
    setStatus("waking");
    setApproval(null);
    setLive(NOTHING_LIVE);

    let alive = true;
    let off: (() => void) | undefined;

    // **Listen before asking.** When the socket is already up,
    // `open_conversation` does not reconnect — it asks the live conversation
    // to resync, and the `ready` that carries the transcript arrives as an
    // event. Registering the listener afterwards races that event: losing the
    // race leaves the UI on "Waking…" with a conversation that is fine.
    void (async () => {
      off = await listen<AgentEvent>("agent://event", (event) => {
        const payload = event.payload;
        // Events carry the box they belong to because several conversations
        // run at once (M8.3). Without this check, one agent's reply lands in
        // another's transcript.
        if (!alive || payload.box_name !== boxName) return;

        setStatus((s) => statusAfter(s, payload));
        setTurns((t) => turnsAfter(t, payload));
        setLive((l) => liveAfter(l, payload));
        setApproval((a) => approvalAfter(a, payload));
      });
      if (!alive) {
        off();
        return;
      }
      try {
        await invoke("open_conversation", { boxName });
      } catch (err) {
        if (!alive) return;
        setStatus("failed");
        setTurns((t) => [...t, { from: "system", text: describe(err) }]);
      }
    })();

    return () => {
      alive = false;
      off?.();
    };
  }, [boxName, attempt]);

  /** Try again after a failure, without having to switch agents and back. */
  async function reconnect() {
    setStatus("waking");
    setAttempt((n) => n + 1);
  }

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth" });
  }, [turns, status, live]);

  const busy = isBusy(status);

  /**
   * Send the person's decision to the agent that is waiting on it.
   *
   * The card is taken down **after** the answer is accepted, not before: an
   * answer that failed to send must leave the question on screen, or the agent
   * sits blocked with nothing left to click.
   */
  async function answer(choice: string) {
    if (!approval || answering) return;
    setAnswering(true);
    try {
      await invoke("respond_approval", {
        boxName,
        requestId: approval.request_id,
        choice,
      });
      setApproval(null);
      setStatus("thinking");
    } catch (err) {
      setTurns((t) => [...t, { from: "system", text: describe(err) }]);
    } finally {
      setAnswering(false);
    }
  }

  async function send(event: React.FormEvent) {
    event.preventDefault();
    const text = draft.trim();
    if (!text || busy) return;
    setDraft("");
    setCaret(0);
    setDismissed(null);
    setLive(NOTHING_LIVE);
    setTurns((t) => [...t, { from: "you", text }]);
    setStatus("thinking");
    try {
      // The agent gets a note about anyone mentioned; the transcript shows
      // what was typed. See `mentions.ts`.
      await invoke("send_prompt", { boxName, text: withNote(text, peers) });
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

        {turns.map((turn, i) =>
          turn.from === "work" ? (
            <Work key={i} steps={turn.steps} busy={busy} />
          ) : (
            <div key={i} className="text-sm">
              <div className="mb-0.5 font-mono text-[11px] text-neutral-400">
                {turn.from === "you" ? "you" : turn.from === "agent" ? boxName : "flotta"}
              </div>
              {/* Only the agent's words are Markdown. What the person typed
                  is shown as typed, and an error is Flotta's, not prose. */}
              {turn.from === "agent" ? (
                <Markdown text={turn.text} />
              ) : (
                <div
                  className={
                    turn.from === "system"
                      ? "whitespace-pre-wrap rounded bg-red-50 px-3 py-2 text-red-800"
                      : "whitespace-pre-wrap text-neutral-900"
                  }
                >
                  {turn.from === "you" ? (
                    // Without the note: a resumed conversation's history comes
                    // back from Hermes with it still attached.
                    <Mentioned text={withoutNote(turn.text)} names={peerNames} />
                  ) : (
                    turn.text
                  )}
                </div>
              )}
            </div>
          ),
        )}

        {/* The answer as it is written. Replaced by the finished reply, which
            is why it is not a line of the transcript. */}
        {busy && live.text.trim() && (
          <div className="text-sm">
            <div className="mb-0.5 font-mono text-[11px] text-neutral-400">{boxName}</div>
            {/* Rendered as it streams: half a table is still easier to read
                as a table than as pipes, and the finished reply replaces it. */}
            <Markdown text={live.text.trimStart()} />
            <span className="mt-1 inline-block h-3.5 w-1.5 animate-pulse bg-neutral-400" />
          </div>
        )}

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
          <div className="space-y-1">
            {/* The agent's train of thought, while there is nothing else to
                show. Its tail, on one line: enough to see it is getting
                somewhere, not a wall to read. */}
            {live.reasoning && !live.text.trim() && (
              <p className="text-xs italic text-neutral-400">{reasoningTail(live.reasoning)}</p>
            )}
            <p className="flex items-center gap-1.5 text-xs text-neutral-500">
              <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-neutral-400" />
              {boxName} is {doingNow(turns, live)}
            </p>
          </div>
        )}

        {/* The agent is blocked on this. Shown in the flow of the
            conversation, where the person is already looking, rather than as a
            modal that could open over another agent's pane. */}
        {approval && (
          <div className="rounded border border-amber-300 bg-amber-50 p-3 text-sm">
            <div className="mb-1 font-medium text-amber-900">
              {boxName} is waiting for your approval
            </div>
            {approval.description && (
              <div className="mb-2 text-xs text-amber-900">{approval.description}</div>
            )}
            {approval.command && (
              <pre className="mb-3 max-h-40 overflow-auto whitespace-pre-wrap rounded bg-white p-2 font-mono text-[11px] text-neutral-900">
                {approval.command}
              </pre>
            )}
            <div className="flex flex-wrap gap-2">
              {approval.choices.map((choice) => (
                <button
                  key={choice}
                  onClick={() => void answer(choice)}
                  disabled={answering}
                  className={
                    choice === "deny"
                      ? "rounded border border-neutral-300 bg-white px-3 py-1.5 text-xs font-medium hover:bg-neutral-50 disabled:opacity-40"
                      : "rounded bg-neutral-900 px-3 py-1.5 text-xs font-medium text-white disabled:opacity-40"
                  }
                >
                  {choiceLabel(choice)}
                </button>
              ))}
            </div>
            {sessionScope(approval) && (
              <p className="mt-2 text-[11px] text-amber-900">{sessionScope(approval)}</p>
            )}
            <p className="mt-2 text-[11px] text-amber-800">
              Unanswered, it is denied automatically — silence is not consent.
            </p>
          </div>
        )}
        {(status === "failed" || status === "closed") && (
          <button
            onClick={() => void reconnect()}
            className="rounded border border-neutral-300 px-3 py-1.5 text-xs font-medium hover:bg-neutral-50"
          >
            Reconnect to {boxName}
          </button>
        )}
        <div ref={bottom} />
      </div>

      <form onSubmit={send} className="relative border-t border-neutral-200 p-3">
        {offered.length > 0 && (
          <ul
            role="listbox"
            className="absolute bottom-full left-3 z-20 mb-1 w-80 overflow-hidden rounded border border-neutral-200 bg-white py-1 shadow-lg"
          >
            {offered.map((peer, i) => (
              <li key={peer.id} role="option" aria-selected={i === pick}>
                <button
                  type="button"
                  // mousedown, not click: a click blurs the input first, and
                  // the caret it was tracking goes with it.
                  onMouseDown={(e) => {
                    e.preventDefault();
                    choose(peer);
                  }}
                  onMouseEnter={() => setPick(i)}
                  className={`block w-full px-3 py-1.5 text-left ${
                    i === pick ? "bg-neutral-100" : ""
                  }`}
                >
                  <span className="font-mono text-xs text-violet-700">@{peer.name}</span>
                  {(peer.display_name || peer.description) && (
                    <span className="ml-2 truncate text-[11px] text-neutral-500">
                      {peer.display_name ?? peer.description}
                    </span>
                  )}
                </button>
              </li>
            ))}
          </ul>
        )}
        <div className="flex gap-2">
          <input
            ref={input}
            value={draft}
            onChange={(e) => {
              setDraft(e.target.value);
              setCaret(e.target.selectionStart ?? e.target.value.length);
            }}
            onSelect={(e) => setCaret(e.currentTarget.selectionStart ?? 0)}
            onKeyDown={onKeyDown}
            placeholder={busy ? "…" : `Message ${boxName} — @ to mention another agent`}
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

/**
 * The tools an agent used between two things it said.
 *
 * A running step only pulses while the turn is live. After a failure or a
 * closed socket nothing will ever finish it, and a spinner there would claim
 * work that is not happening.
 */
/** A person's message with the colleagues it names picked out. */
function Mentioned({ text, names }: { text: string; names: Set<string> }) {
  return (
    <>
      {segments(text, names).map((part, i) =>
        part.mention ? (
          <span key={i} className="rounded bg-violet-50 px-0.5 font-medium text-violet-700">
            {part.text}
          </span>
        ) : (
          <span key={i}>{part.text}</span>
        ),
      )}
    </>
  );
}

/**
 * What a step did, in words.
 *
 * A `flotta-ask` call is a terminal command to Hermes and a conversation with
 * a colleague to the person watching, so it is shown as the second: "asking
 * eng-r" with the question, rather than a shell line. Everything else is the
 * tool and what it was asked to do. The command is still one hover away.
 */
function StepLabel({ step, busy }: { step: WorkStep; busy: boolean }) {
  const delegation = step.tool === "terminal" ? delegationOf(step.detail) : null;
  if (delegation?.kind === "ask") {
    const verb =
      step.state === "running"
        ? busy
          ? "asking"
          : "asked"
        : step.state === "failed"
          ? "could not ask"
          : "asked";
    return (
      <span className="flex min-w-0 items-baseline gap-2" title={step.detail}>
        <span className="shrink-0 text-violet-700">
          {verb} <span className="font-medium">{delegation.peer}</span>
        </span>
        {delegation.question && (
          <span className="min-w-0 truncate text-[11px] text-neutral-600">
            {delegation.question}
          </span>
        )}
      </span>
    );
  }
  if (delegation?.kind === "list") {
    return (
      <span className="shrink-0 text-violet-700" title={step.detail}>
        checked who it can ask
      </span>
    );
  }
  return (
    <>
      <span className="shrink-0 text-neutral-500">{step.tool}</span>
      {step.detail && (
        <code className="min-w-0 truncate font-mono text-[11px] text-neutral-700" title={step.detail}>
          {step.detail}
        </code>
      )}
    </>
  );
}

function Work({ steps, busy }: { steps: WorkStep[]; busy: boolean }) {
  return (
    <ul className="space-y-0.5 border-l-2 border-neutral-200 pl-3">
      {steps.map((step) => (
        <li key={step.id} className="flex items-baseline gap-2 text-xs">
          <span
            className={
              step.state === "failed"
                ? "w-3 text-red-600"
                : step.state === "done"
                  ? "w-3 text-emerald-600"
                  : busy
                    ? "w-3 animate-pulse text-neutral-400"
                    : "w-3 text-neutral-300"
            }
            aria-label={step.state}
          >
            {step.state === "failed" ? "✕" : step.state === "done" ? "✓" : "•"}
          </span>
          <StepLabel step={step} busy={busy} />
          {step.seconds !== null && (
            <span className="ml-auto shrink-0 font-mono text-[10px] text-neutral-400">
              {duration(step.seconds)}
            </span>
          )}
        </li>
      ))}
    </ul>
  );
}
