import type { BoxEvent, BoxRow, Peer } from "./types";

/**
 * Agents talking to each other (M7, FLOTTA-54), as the window shows it.
 *
 * No React here, for the reason `timeline.ts` has none: the decisions — which
 * answer belongs to which question, whether a silence is still a wait, whether
 * a terminal step was an agent asking a colleague — are where the bugs will be,
 * and a typechecker cannot see any of them.
 */

/** The control plane's deadline for one delivery (`relay.DEFAULT_TIMEOUT_S`). */
const RELAY_DEADLINE_MS = 240_000;

/**
 * One question between two agents, from the side of the agent whose panel is
 * open.
 *
 * - `asked`: this agent asked `peer`.
 * - `was_asked`: `peer` asked this agent.
 *
 * `outcome` is what is known, not what is hoped:
 * - `answered` — a reply was recorded.
 * - `failed` — the relay recorded that no answer came (asker's side only).
 * - `refused` — the relay would not carry it: not granted, a loop, or over
 *   budget. Never delivered, so it cost nothing; shown because a stopped
 *   runaway is the thing a person most needs to be able to see.
 * - `waiting` — nothing yet, and the relay's deadline has not passed.
 * - `unknown` — nothing recorded and the deadline has passed. The answering
 *   side has no failure event of its own, so this is said plainly rather than
 *   guessed at in either direction.
 */
export type Exchange = {
  direction: "asked" | "was_asked";
  peer: string;
  at: string;
  question: string;
  outcome: "answered" | "failed" | "refused" | "waiting" | "unknown";
  reply: string | null;
  reason: string | null;
  seconds: number | null;
};

const OPENS: Record<string, Exchange["direction"]> = {
  peer_asked: "asked",
  peer_asked_by: "was_asked",
};
const CLOSES: Record<string, Exchange["direction"]> = {
  peer_answered: "asked",
  peer_failed: "asked",
  peer_replied: "was_asked",
};

function str(payload: Record<string, unknown> | null | undefined, key: string): string | null {
  const value = payload?.[key];
  return typeof value === "string" ? value : null;
}

function num(payload: Record<string, unknown> | null | undefined, key: string): number | null {
  const value = payload?.[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/**
 * Every exchange in a timeline, newest first.
 *
 * Each question is written as one event and its answer as a later one, so they
 * are paired here. **Paired by peer and direction, most recent open question
 * first.** That is exact rather than approximate because the relay allows one
 * delivery per answering agent at a time, and the asking agent is blocked on
 * its own tool call while it waits — so there is never more than one open
 * question between the same two agents in the same direction.
 */
export function exchangesIn(events: BoxEvent[], now: number = Date.now()): Exchange[] {
  const done: Exchange[] = [];
  const open: Exchange[] = [];

  for (const event of events) {
    if (event.entity_kind !== "box") continue;
    const payload = event.payload ?? null;
    const peer = str(payload, "peer");
    if (!peer) continue;

    if (event.type === "peer_refused") {
      done.push({
        direction: "asked",
        peer,
        at: event.ts,
        question: str(payload, "message") ?? "",
        outcome: "refused",
        reply: null,
        reason: str(payload, "reason"),
        seconds: null,
      });
      continue;
    }

    const opens = OPENS[event.type];
    if (opens) {
      open.push({
        direction: opens,
        peer,
        at: event.ts,
        question: str(payload, "message") ?? "",
        outcome: "waiting",
        reply: null,
        reason: null,
        seconds: null,
      });
      continue;
    }

    const closes = CLOSES[event.type];
    if (!closes) continue;
    const index = findLastIndex(open, (x) => x.direction === closes && x.peer === peer);
    if (index < 0) continue; // an answer whose question predates the page of events
    const [exchange] = open.splice(index, 1);
    const failed = event.type === "peer_failed";
    done.push({
      ...exchange,
      outcome: failed ? "failed" : "answered",
      reply: failed ? null : str(payload, "reply"),
      reason: failed ? str(payload, "reason") : null,
      seconds: num(payload, "seconds"),
    });
  }

  for (const exchange of open) {
    const started = Date.parse(exchange.at);
    const expired = Number.isFinite(started) && now - started > RELAY_DEADLINE_MS;
    done.push({ ...exchange, outcome: expired ? "unknown" : "waiting" });
  }

  return done.sort((a, b) => Date.parse(b.at) - Date.parse(a.at));
}

function findLastIndex<T>(items: T[], test: (item: T) => boolean): number {
  for (let i = items.length - 1; i >= 0; i--) if (test(items[i])) return i;
  return -1;
}

/** What `flotta-ask` was doing, read off the command a step ran. */
export type Delegation = { kind: "list" } | { kind: "ask"; peer: string; question: string };

/**
 * Whether a terminal step was an agent asking a colleague, and whom.
 *
 * Read from the command line Hermes reports as the step's detail, which is
 * what the model actually typed — so it tolerates what a model types: a
 * `cd … &&` in front, quoted or unquoted questions, `--json` anywhere. The
 * detail is cut at 160 characters, so the question may be too; the peer never
 * is, being the first thing after the command.
 */
export function delegationOf(detail: string): Delegation | null {
  const match = /(?:^|[\s;&|(])flotta-ask(?=\s|$)(.*)$/.exec(detail);
  if (!match) return null;
  const words = match[1]
    .trim()
    .split(/\s+/)
    .filter((w) => w !== "" && w !== "--json");
  if (words.length === 0 || words[0] === "--list" || words[0] === "-l") return { kind: "list" };
  const [peer, ...rest] = words;
  if (!/^[a-z0-9][a-z0-9-]*$/.test(peer)) return null;
  return { kind: "ask", peer, question: unquote(rest.join(" ")) };
}

function unquote(text: string): string {
  const trimmed = text.trim();
  const first = trimmed[0];
  if ((first === '"' || first === "'") && trimmed.endsWith(first) && trimmed.length > 1) {
    return trimmed.slice(1, -1);
  }
  // Cut short by the detail limit: the opening quote is there, the closing
  // one is not.
  if (first === '"' || first === "'") return trimmed.slice(1);
  return trimmed;
}

/** "Reviewer — backend PRs (eng-r)", or just the address when nothing else is known. */
export function peerLabel(peer: Pick<Peer, "name" | "display_name">): string {
  return peer.display_name ? `${peer.display_name} (${peer.name})` : peer.name;
}

/**
 * Who could be granted: every other agent that is not already.
 *
 * Not the agent itself (the control plane refuses that anyway, and offering a
 * control that can only fail is the window lying), and not an agent that is
 * still being built — its address does not answer yet, so a message to it
 * would spend the asking agent's time on a certain failure.
 */
export function grantable(fleet: BoxRow[], self: string, granted: Peer[]): BoxRow[] {
  const have = new Set(granted.map((p) => p.id));
  return fleet
    .filter((b) => b.id !== self && !have.has(b.id) && b.status !== "provisioning")
    .sort((a, b) => a.name.localeCompare(b.name));
}
