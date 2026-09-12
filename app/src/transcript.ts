import type { AgentEvent, Turn } from "./types";

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
