import type { AgentEvent, ApprovalRequest, Live, Step, Turn, WorkStep } from "./types";

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
    case "progress":
      return withStep(current, event.step);
    default:
      return current;
  }
}

/** The transcript after one step: only the steps that are lines of it. */
function withStep(current: Turn[], step: Step): Turn[] {
  switch (step.kind) {
    case "said":
      return [...current, { from: "agent", text: step.text }];
    case "tool_started": {
      if (hasStep(current, step.id)) {
        // A step seen twice — replayed after a remount — is not two steps.
        return current;
      }
      return withWork(current, {
        id: step.id,
        tool: step.tool,
        detail: step.detail,
        state: "running",
        seconds: null,
      });
    }
    case "tool_finished": {
      const state: WorkStep["state"] = step.failed ? "failed" : "done";
      for (let i = current.length - 1; i >= 0; i--) {
        const turn = current[i];
        if (turn.from !== "work" || !turn.steps.some((s) => s.id === step.id)) continue;
        const steps = turn.steps.map((s) =>
          s.id === step.id ? { ...s, state, seconds: step.seconds } : s,
        );
        return [...current.slice(0, i), { from: "work", steps }, ...current.slice(i + 1)];
      }
      // Finished without ever being seen starting — a pane that mounted in
      // between. Shown anyway: a step that happened is not dropped for
      // arriving out of order.
      return withWork(current, {
        id: step.id,
        tool: step.tool,
        detail: "",
        state,
        seconds: step.seconds,
      });
    }
    default:
      return current;
  }
}

function hasStep(turns: Turn[], id: string): boolean {
  return turns.some((t) => t.from === "work" && t.steps.some((s) => s.id === id));
}

/**
 * Add a step to the work block at the end, or start one.
 *
 * Consecutive tools share a block, so seven commands read as one piece of work
 * rather than seven interruptions.
 */
function withWork(current: Turn[], step: WorkStep): Turn[] {
  const last = current[current.length - 1];
  return last?.from === "work"
    ? [...current.slice(0, -1), { from: "work", steps: [...last.steps, step] }]
    : [...current, { from: "work", steps: [step] }];
}

export const NOTHING_LIVE: Live = { text: "", reasoning: "", preparing: null };

/**
 * What is streaming, after an event.
 *
 * **Cleared by anything that ends the turn**, like the approval card: the reply
 * replaces the streamed text with the finished one, and leaving the stream on
 * screen as well would show the answer twice.
 *
 * Cleared too when a tool starts or narration is finished. What streamed
 * before that is now a line of its own (`said`) or led to the step on screen,
 * and the next piece of the answer starts from nothing.
 */
export function liveAfter(current: Live, event: AgentEvent): Live {
  switch (event.kind) {
    case "progress":
      switch (event.step.kind) {
        case "text":
          return { ...current, text: current.text + event.step.text, preparing: null };
        case "reasoning":
          return { ...current, reasoning: current.reasoning + event.step.text };
        case "preparing":
          return { ...current, preparing: event.step.tool };
        case "said":
        case "tool_started":
          return NOTHING_LIVE;
        case "tool_finished":
          return { ...current, preparing: null };
        default:
          return current;
      }
    case "reply":
    case "failed":
    case "closed":
    case "reset":
    case "ready":
    case "waking":
      return NOTHING_LIVE;
    default:
      return current;
  }
}

/**
 * One short phrase for what the agent is doing now.
 *
 * FLOTTA-61's acceptance is that nothing sits on a bare "thinking…" without
 * saying what it is doing, and this is where that is decided. Most specific
 * first: a tool being written, one running, the answer, then reasoning.
 */
export function doingNow(turns: Turn[], live: Live): string {
  if (live.preparing) return `preparing ${live.preparing}…`;
  const last = turns[turns.length - 1];
  const running =
    last?.from === "work" ? last.steps.find((s) => s.state === "running") : undefined;
  if (running) return `running ${running.tool}…`;
  if (live.text) return "writing…";
  if (live.reasoning) return "reasoning…";
  return "thinking…";
}

/**
 * The end of the reasoning, as one line: the agent's train of thought without
 * a wall of it.
 */
export function reasoningTail(reasoning: string, chars = 240): string {
  const flat = reasoning.replace(/\s+/g, " ").trim();
  return flat.length <= chars ? flat : `…${flat.slice(flat.length - (chars - 1))}`;
}

/** A tool's duration in the words a person reads it in. */
export function duration(seconds: number | null): string {
  if (seconds === null) return "";
  if (seconds < 1) return `${Math.round(seconds * 1000)}ms`;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
}

/**
 * The status the pane should show for an event.
 *
 * A reset is not a state the pane has anything to say about: the socket is
 * open and the agent is waiting, which is `ready`. Carrying `reset` through as
 * a status would add a value that every other branch — busy, reconnect, the
 * composer's disabled state — has to remember to ignore.
 */
export function statusAfter(current: Status, event: AgentEvent): Status {
  switch (event.kind) {
    case "reset":
      return "ready";
    case "approval_withdrawn":
      // The agent carries on without the answer it asked for.
      return "thinking";
    case "progress":
      // Progress is the agent working, which is `thinking` — except while a
      // card is up. Reasoning can keep streaming around an approval, and
      // flipping to `thinking` would read as the question having been answered.
      return current === "approval" ? "approval" : "thinking";
    default:
      return event.kind;
  }
}

/** The states the pane can be in. `reset` and `progress` are events, not states. */
export type Status = Exclude<AgentEvent["kind"], "reset" | "progress" | "approval_withdrawn">;

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
    case "approval_withdrawn":
      // Hermes stopped waiting. A card left up would offer buttons for a
      // question already decided — by its timeout, as a denial.
      return null;
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
export function isBusy(status: Status): boolean {
  return status === "waking" || status === "thinking" || status === "approval";
}
