import type { Peer } from "./types";

/**
 * `@eng-g` in a chat (FLOTTA-65): a person pointing an agent at a colleague.
 *
 * Three jobs, all pure so they can be tested without a window:
 *
 * 1. **Completing** — what has been typed after an `@`, and which colleagues
 *    it could mean.
 * 2. **Telling the agent** — `@eng-g` means nothing to a model on its own, so
 *    a message that names colleagues is sent with a short note saying who they
 *    are and how to ask them.
 * 3. **Not showing the note** — the person typed the message, not the note.
 *    The note comes back inside the history Hermes returns when a conversation
 *    is resumed, so it is removed wherever a person's message is rendered,
 *    not only when it is first sent.
 */

/**
 * The note's opening. Distinctive enough that nobody types it by accident,
 * since everything from it to the end of a message is hidden.
 */
export const NOTE_OPEN = "\n\n(Flotta note: ";
const NOTE_CLOSE = ")";

/** Same rule as a box name, which is what follows the `@`. */
const NAME = "[a-z0-9](?:[a-z0-9-]*[a-z0-9])?";

/** An `@` being typed right now, and what follows it so far. */
export type MentionQuery = { start: number; query: string };

/**
 * The mention the caret is in, if any.
 *
 * Only at the start of the text or after whitespace, so `me@example.com` does
 * not open a list of agents.
 */
export function mentionAt(text: string, caret: number): MentionQuery | null {
  const before = text.slice(0, caret);
  const match = /(^|\s)@([a-z0-9-]*)$/i.exec(before);
  if (!match) return null;
  return { start: before.length - match[2].length - 1, query: match[2].toLowerCase() };
}

/**
 * Which colleagues a partial mention could mean, best first.
 *
 * By address first, since that is what is being typed — then by what the
 * agent is called, so "@rev" finds the reviewer whatever its address is.
 */
export function suggest(peers: Peer[], query: string, limit = 6): Peer[] {
  const q = query.toLowerCase();
  const rank = (peer: Peer): number => {
    if (peer.name.startsWith(q)) return 0;
    if (peer.name.includes(q)) return 1;
    if ((peer.display_name ?? "").toLowerCase().includes(q)) return 2;
    return -1;
  };
  return peers
    .map((peer) => ({ peer, r: rank(peer) }))
    .filter((x) => x.r >= 0)
    .sort((a, b) => a.r - b.r || a.peer.name.localeCompare(b.peer.name))
    .slice(0, limit)
    .map((x) => x.peer);
}

/** Replace the mention being typed with the chosen name, and a space. */
export function complete(
  text: string,
  at: MentionQuery,
  caret: number,
  name: string,
): { text: string; caret: number } {
  const after = text.slice(caret).replace(/^[a-z0-9-]*/i, "");
  const inserted = `@${name} `;
  const rest = after.startsWith(" ") ? after.slice(1) : after;
  return {
    text: text.slice(0, at.start) + inserted + rest,
    caret: at.start + inserted.length,
  };
}

/** The colleagues a message names, once each, in the order they appear. */
export function mentionsIn(text: string, peers: Peer[]): Peer[] {
  const byName = new Map(peers.map((p) => [p.name, p]));
  const found: Peer[] = [];
  const pattern = new RegExp(`(?:^|[^\\w@.])@(${NAME})(?![\\w-])`, "g");
  for (const match of text.matchAll(pattern)) {
    const peer = byName.get(match[1]);
    if (peer && !found.includes(peer)) found.push(peer);
  }
  return found;
}

/**
 * What is actually sent: the message, plus a note when it names colleagues.
 *
 * The note says who each one is and how to ask — and nothing about *whether*
 * to. The person's own words carry that; "@eng-g might know" and "don't bother
 * @eng-g" are both theirs to write.
 */
export function withNote(text: string, peers: Peer[]): string {
  const named = mentionsIn(text, peers);
  if (named.length === 0) return text;
  const who = named
    .map((p) => {
      const about = [p.display_name, p.description].filter(Boolean).join(", ");
      return about ? `@${p.name} (${about})` : `@${p.name}`;
    })
    .join("; ");
  const noun = named.length === 1 ? "is another agent" : "are other agents";
  return (
    text +
    NOTE_OPEN +
    `${who} ${noun} on this fleet. To ask one, run ` +
    `flotta-ask <name> "<your question>" with everything it needs in the question, ` +
    `and tell me what it said.` +
    NOTE_CLOSE
  );
}

/** The message as the person typed it. */
export function withoutNote(text: string): string {
  const at = text.lastIndexOf(NOTE_OPEN);
  if (at < 0 || !text.trimEnd().endsWith(NOTE_CLOSE)) return text;
  return text.slice(0, at);
}

/** A message cut into plain text and mentions, for highlighting. */
export function segments(text: string, names: Set<string>): { text: string; mention: boolean }[] {
  const out: { text: string; mention: boolean }[] = [];
  const pattern = new RegExp(`(^|[^\\w@.])@(${NAME})(?![\\w-])`, "g");
  let last = 0;
  for (const match of text.matchAll(pattern)) {
    if (!names.has(match[2])) continue;
    const start = (match.index ?? 0) + match[1].length;
    if (start > last) out.push({ text: text.slice(last, start), mention: false });
    out.push({ text: `@${match[2]}`, mention: true });
    last = start + match[2].length + 1;
  }
  if (last < text.length) out.push({ text: text.slice(last), mention: false });
  return out;
}
