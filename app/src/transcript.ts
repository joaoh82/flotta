import type { AgentEvent, ApprovalRequest, Turn } from "./types";

/**
 * What an event does to the transcript on screen.
 *
 * Pure, and here rather than inline in `Conversation.tsx`, for the reason
 * FLOTTA-31 moved the timeline filter out: these branches decide what a person
 * sees, they are a function of data alone, and nothing ran them. A typechecker
 * is happy with a branch that clears the wrong thing.
 */
export function turnsAfter(current: Turn[], event: AgentEvent): Turn[] {
  switch (event.kind) {
    case "ready":
      // Replace rather than append: this is what the box says was said, and it
      // is more authoritative than anything on screen.
      //
      // An **empty** resume is not an instruction to clear. It also arrives
      // from a resync that found nothing yet, and clearing on it emptied the
      // transcript of an agent that was mid-conversation.
      return event.resumed.length > 0
        ? event.resumed.map((line) => ({
            from: line.role === "user" ? "you" : "agent",
            text: line.text,
          }))
        : current;
    case "reset":
      // The conversation was started over on the box. This is the one event
      // that means "there is nothing any more", as opposed to "nothing yet".
      return [];
    case "reply":
      return [...current, { from: "agent", text: event.text }];
    case "failed":
      // Failures read in place, in order, rather than replacing the
      // conversation they interrupted — what was already said still happened.
      return [...current, { from: "system", text: event.detail }];
    default:
      return current;
  }
}

/**
 * The status the pane should show for an event.
 *
 * A reset is not a state the pane has anything to say about: the socket is
 * open and the agent is waiting, which is `ready`. Carrying `reset` through as
 * a status would add a value that every other branch — busy, reconnect, the
 * composer's disabled state — has to remember to ignore.
 */
export function statusAfter(event: AgentEvent): AgentEvent["kind"] {
  return event.kind === "reset" ? "ready" : event.kind;
}

/**
 * The approval waiting on a person, after an event.
 *
 * **Cleared by anything that ends the turn**, not only by an answer. Hermes's
 * gate times out on its own and denies, and the turn then finishes with a
 * reply or a failure; a card left on screen after that would offer buttons for
 * a question that was already decided — and a click would land on nothing, or
 * worse, on the next turn's approval.
 *
 * A second `approval` replaces the first rather than stacking. The turn loop
 * answers one at a time, and two cards would invite answering the wrong one.
 */
export function approvalAfter(
  current: ApprovalRequest | null,
  event: AgentEvent,
): ApprovalRequest | null {
  switch (event.kind) {
    case "approval":
      return event.request;
    case "reply":
    case "failed":
    case "closed":
    case "reset":
    case "ready":
    case "waking":
      return null;
    default:
      // `thinking` arrives again when the pane remounts mid-turn, *before* the
      // pending approval is re-sent. Clearing on it would flash the card away
      // and back; keeping it is what a remount needs.
      return current;
  }
}

/**
 * What each of Hermes's answers means, in words a person can decide on.
 *
 * The raw choices are protocol vocabulary: "session" and "always" do not say
 * what they grant or for how long, and those are exactly the two a person
 * should read before clicking. An unknown choice is shown as itself rather
 * than hidden — the Rust side has already filtered to answers the gateway
 * accepts, so reaching the default means the two drifted.
 */
export function choiceLabel(choice: string): string {
  switch (choice) {
    case "once":
      return "Allow once";
    case "session":
      return "Allow for this conversation";
    case "always":
      return "Always allow";
    case "deny":
      return "Deny";
    default:
      return choice;
  }
}

/**
 * Whether the composer is busy — no sending while this is true.
 *
 * Waiting on an approval counts. The agent is mid-turn, and a message typed
 * now would be deferred behind the very turn it seems to be answering, which
 * reads as the app eating it.
 */
export function isBusy(status: AgentEvent["kind"]): boolean {
  return status === "waking" || status === "thinking" || status === "approval";
}
