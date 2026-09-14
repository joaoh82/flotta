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
 * What each answer means, in words a person can decide on.
 *
 * `always` has no label because the window never offers it (FLOTTA-62): Hermes
 * records it by pattern, permanently, on the agent's volume, so one click
 * beside one command allowed a whole category forever with no way to see or
 * undo it. If it ever arrives anyway it is shown as its raw name rather than
 * dressed up as something reassuring — the Rust side filters it, so reaching
 * the default means the two halves drifted.
 */
export function choiceLabel(choice: string): string {
  switch (choice) {
    case "once":
      return "Allow once";
    case "session":
      return "Allow for this conversation";
    case "deny":
      return "Deny";
    default:
      return choice;
  }
}

/**
 * What "Allow for this conversation" will actually cover, or null when the
 * card does not offer it.
 *
 * The button's label is about *time*; this sentence is about *breadth*, and it
 * is the part a person would get wrong. Hermes grants the pattern, not the
 * command, so allowing one temp-directory delete for the conversation allows
 * every command it classifies as a delete in a root path until the
 * conversation ends. Saying so before the click is FLOTTA-62's acceptance: no
 * button grants more than the card says.
 */
export function sessionScope(request: ApprovalRequest): string | null {
  if (!request.choices.includes("session")) return null;
  const covers = request.pattern
    ? `every command Hermes classifies as “${request.pattern}”`
    : "every command Hermes puts in the same category as this one";
  return `“Allow for this conversation” allows ${covers}, not just this one, until the conversation ends.`;
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
