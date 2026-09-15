import { describe, expect, it } from "vitest";
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
} from "./transcript";
import type { AgentEvent, ApprovalRequest, Step, Turn } from "./types";

const said: Turn[] = [
  { from: "you", text: "who are you?" },
  { from: "agent", text: "the backend reviewer" },
];

describe("turnsAfter", () => {
  it("replaces the transcript with what the box says was said", () => {
    // The box is more authoritative than anything on screen: its memory is the
    // source of truth, and a UI copy would keep being shown after the agent
    // had moved on.
    expect(
      turnsAfter(said, {
        kind: "ready",
        box_name: "eng-r",
        resumed: [{ role: "user", text: "hello" }],
      }),
    ).toEqual([{ from: "you", text: "hello" }]);
  });

  it("leaves the transcript alone when a resume carries nothing", () => {
    // An empty `ready` means "nothing to show yet", not "nothing any more".
    // It also arrives from a resync, and clearing on it emptied the transcript
    // of an agent that was mid-conversation.
    expect(
      turnsAfter(said, { kind: "ready", box_name: "eng-r", resumed: [] }),
    ).toEqual(said);
  });

  it("clears the transcript when the conversation was started over", () => {
    // The pair that must not collapse into one branch: this is the event that
    // means "nothing any more", and it is the whole reason `reset` exists as a
    // separate kind rather than an empty `ready`.
    expect(turnsAfter(said, { kind: "reset", box_name: "eng-r" })).toEqual([]);
  });

  it("anything but the agent's own words is attributed to the agent", () => {
    // `role` is Hermes's vocabulary and only `user` is ours; assistant, tool
    // and anything added later all read as the agent talking rather than as
    // the person.
    expect(
      turnsAfter([], {
        kind: "ready",
        box_name: "eng-r",
        resumed: [
          { role: "assistant", text: "a" },
          { role: "tool", text: "b" },
        ],
      }),
    ).toEqual([
      { from: "agent", text: "a" },
      { from: "agent", text: "b" },
    ]);
  });

  it("appends a reply rather than replacing what came before", () => {
    expect(
      turnsAfter(said, { kind: "reply", box_name: "eng-r", text: "more" }),
    ).toEqual([...said, { from: "agent", text: "more" }]);
  });

  it("shows a failure in place, keeping the conversation it interrupted", () => {
    expect(
      turnsAfter(said, { kind: "failed", box_name: "eng-r", detail: "gone" }),
    ).toEqual([...said, { from: "system", text: "gone" }]);
  });

  it("does not touch the transcript while waking, thinking or closing", () => {
    for (const event of [
      { kind: "waking", box_name: "eng-r" },
      { kind: "thinking", box_name: "eng-r" },
      { kind: "closed", box_name: "eng-r" },
    ] as const) {
      expect(turnsAfter(said, event)).toEqual(said);
    }
  });

  it("never mutates the transcript it was given", () => {
    const before = [...said];
    turnsAfter(said, { kind: "reply", box_name: "eng-r", text: "more" });
    turnsAfter(said, { kind: "reset", box_name: "eng-r" });
    expect(said).toEqual(before);
  });
});

describe("statusAfter", () => {
  it("reports a reset as ready", () => {
    // Not a state the pane has anything to say about: the socket is open and
    // the agent is waiting. Carrying `reset` through would add a value that
    // the busy check, the reconnect button and the composer all have to
    // remember to ignore.
    expect(statusAfter("thinking", { kind: "reset", box_name: "eng-r" })).toBe("ready");
  });

  it("passes every other event through as its own kind", () => {
    expect(statusAfter("ready", { kind: "waking", box_name: "eng-r" })).toBe("waking");
    expect(statusAfter("ready", { kind: "thinking", box_name: "eng-r" })).toBe("thinking");
    expect(
      statusAfter("thinking", { kind: "failed", box_name: "eng-r", detail: "x" }),
    ).toBe("failed");
  });

  it("reports progress as thinking", () => {
    // A pane that remounted on `waking` and then hears a tool start is looking
    // at an agent at work, not one still waking up.
    expect(statusAfter("waking", progress({ kind: "text", text: "hi" }))).toBe("thinking");
  });

  it("keeps an approval up while reasoning streams around it", () => {
    // Flipping to `thinking` would read as the question having been answered.
    expect(statusAfter("approval", progress({ kind: "reasoning", text: "hm" }))).toBe(
      "approval",
    );
  });
});

function progress(step: Step): AgentEvent {
  return { kind: "progress", box_name: "eng-r", step };
}

const started = (id: string, detail = "ls /workspace"): Step => ({
  kind: "tool_started",
  id,
  tool: "terminal",
  detail,
});

const finished = (id: string, failed = false): Step => ({
  kind: "tool_finished",
  id,
  tool: "terminal",
  seconds: 0.24,
  failed,
});

const last = (turns: Turn[]) => turns[turns.length - 1];

/** Run a sequence of steps through `turnsAfter`, as the pane would. */
function replay(turns: Turn[], steps: Step[]): Turn[] {
  return steps.reduce((t, step) => turnsAfter(t, progress(step)), turns);
}

describe("turnsAfter, while the agent works", () => {
  it("shows a tool running, then how it finished", () => {
    const running = replay(said, [started("a")]);
    expect(running).toEqual([
      ...said,
      {
        from: "work",
        steps: [{ id: "a", tool: "terminal", detail: "ls /workspace", state: "running", seconds: null }],
      },
    ]);
    expect(last(replay(running, [finished("a")]))).toEqual({
      from: "work",
      steps: [{ id: "a", tool: "terminal", detail: "ls /workspace", state: "done", seconds: 0.24 }],
    });
  });

  it("shows a command that failed as failed", () => {
    // eng-r's slow turn: three commands failed and every one looked like
    // progress on screen.
    const turns = replay([], [started("a"), finished("a", true)]);
    expect(turns).toMatchObject([{ from: "work", steps: [{ state: "failed" }] }]);
  });

  it("groups consecutive tools into one block", () => {
    const turns = replay(said, [started("a"), finished("a"), started("b"), finished("b")]);
    expect(turns).toHaveLength(said.length + 1);
    expect(last(turns)).toMatchObject({ from: "work", steps: [{ id: "a" }, { id: "b" }] });
  });

  it("puts what the agent said between tools in order, as its own line", () => {
    const turns = replay(
      [],
      [
        { kind: "said", text: "Checking the machine now." },
        started("a"),
        finished("a"),
        { kind: "said", text: "Now the disk." },
        started("b"),
      ],
    );
    expect(turns.map((t) => t.from)).toEqual(["agent", "work", "agent", "work"]);
  });

  it("does not add a step twice when a remounted pane is shown the turn again", () => {
    const once = replay([], [started("a"), finished("a")]);
    expect(replay(once, [started("a"), finished("a")])).toEqual(once);
  });

  it("finishes a step in an earlier block, not a new one", () => {
    // Parallel tools can finish after narration has started a new block.
    const turns = replay([], [started("a"), { kind: "said", text: "meanwhile" }, finished("a")]);
    expect(turns).toMatchObject([
      { from: "work", steps: [{ id: "a", state: "done" }] },
      { from: "agent", text: "meanwhile" },
    ]);
  });

  it("still shows a step whose start it never saw", () => {
    expect(replay([], [finished("z")])).toMatchObject([
      { from: "work", steps: [{ id: "z", state: "done", detail: "" }] },
    ]);
  });

  it("keeps streaming text and reasoning out of the transcript", () => {
    // They are `Live`, not lines: the reply arrives whole and would otherwise
    // be shown twice.
    expect(
      replay(said, [
        { kind: "text", text: "partial" },
        { kind: "reasoning", text: "hm" },
        { kind: "preparing", tool: "terminal" },
      ]),
    ).toEqual(said);
  });
});

describe("liveAfter", () => {
  const after = (steps: Step[]) =>
    steps.reduce((live, step) => liveAfter(live, progress(step)), NOTHING_LIVE);

  it("streams the answer as it is written", () => {
    expect(
      after([
        { kind: "text", text: "`/workspace" },
        { kind: "text", text: "` is empty," },
      ]).text,
    ).toBe("`/workspace` is empty,");
  });

  it("starts again from nothing when a tool starts", () => {
    // What streamed before it is now a line (`said`) or led to the step shown.
    expect(
      after([
        { kind: "reasoning", text: "I will list it" },
        { kind: "text", text: "Let me check." },
        { kind: "said", text: "Let me check." },
        started("a"),
      ]),
    ).toEqual(NOTHING_LIVE);
  });

  it("names the tool being prepared until it starts or text arrives", () => {
    expect(after([{ kind: "preparing", tool: "terminal" }]).preparing).toBe("terminal");
    expect(
      after([
        { kind: "preparing", tool: "terminal" },
        { kind: "text", text: "a" },
      ]).preparing,
    ).toBeNull();
  });

  it("is cleared by anything that ends the turn, so the answer is not shown twice", () => {
    const streaming = after([
      { kind: "text", text: "the answer" },
      { kind: "reasoning", text: "because" },
    ]);
    const ends: AgentEvent[] = [
      { kind: "reply", box_name: "eng-r", text: "the answer" },
      { kind: "failed", box_name: "eng-r", detail: "x" },
      { kind: "closed", box_name: "eng-r" },
      { kind: "reset", box_name: "eng-r" },
      { kind: "ready", box_name: "eng-r", resumed: [] },
      { kind: "waking", box_name: "eng-r" },
    ];
    for (const event of ends) {
      expect(liveAfter(streaming, event), event.kind).toEqual(NOTHING_LIVE);
    }
  });

  it("survives a remount's thinking, which arrives before the replayed steps", () => {
    const streaming = after([{ kind: "text", text: "half" }]);
    expect(liveAfter(streaming, { kind: "thinking", box_name: "eng-r" })).toEqual(streaming);
  });
});

describe("doingNow", () => {
  it("never leaves a working agent on a bare thinking when it knows more", () => {
    const running = replay([], [started("a")]);
    expect(doingNow([], { ...NOTHING_LIVE, preparing: "terminal" })).toBe("preparing terminal…");
    expect(doingNow(running, NOTHING_LIVE)).toBe("running terminal…");
    expect(doingNow([], { ...NOTHING_LIVE, text: "a" })).toBe("writing…");
    expect(doingNow([], { ...NOTHING_LIVE, reasoning: "a" })).toBe("reasoning…");
    expect(doingNow([], NOTHING_LIVE)).toBe("thinking…");
  });

  it("does not report a finished tool as running", () => {
    expect(doingNow(replay([], [started("a"), finished("a")]), NOTHING_LIVE)).toBe("thinking…");
  });
});

describe("reasoningTail", () => {
  it("shows short reasoning whole, on one line", () => {
    expect(reasoningTail(" The user wants\n two commands. ")).toBe("The user wants two commands.");
  });

  it("keeps the end of long reasoning, where the current thought is", () => {
    const tail = reasoningTail("a".repeat(500) + " latest", 20);
    expect(tail).toHaveLength(20);
    expect(tail.startsWith("…")).toBe(true);
    expect(tail.endsWith("latest")).toBe(true);
  });
});

describe("duration", () => {
  it("reads the way a person would say it", () => {
    expect(duration(null)).toBe("");
    expect(duration(0.2417)).toBe("242ms");
    expect(duration(3.94)).toBe("3.9s");
    expect(duration(125)).toBe("2m 5s");
  });
});


const asked: ApprovalRequest = {
  request_id: "r-1",
  command: "rm -rf /workspace/old",
  description: "recursive delete",
  pattern: "delete in root path",
  choices: ["once", "deny"],
};

describe("approvalAfter", () => {
  it("shows the approval the agent is waiting on", () => {
    expect(
      approvalAfter(null, { kind: "approval", box_name: "eng-r", request: asked }),
    ).toEqual(asked);
  });

  it("clears it when the turn ends, however it ends", () => {
    // Hermes's gate times out on its own and denies. A card left up after
    // that offers buttons for a question already decided.
    const endings: AgentEvent[] = [
      { kind: "reply", box_name: "eng-r", text: "done" },
      { kind: "failed", box_name: "eng-r", detail: "x" },
      { kind: "closed", box_name: "eng-r" },
      { kind: "reset", box_name: "eng-r" },
      { kind: "ready", box_name: "eng-r", resumed: [] },
      { kind: "waking", box_name: "eng-r" },
    ];
    for (const event of endings) {
      expect(approvalAfter(asked, event), event.kind).toBeNull();
    }
  });

  it("keeps it through a remount's thinking, which arrives before the re-sent approval", () => {
    expect(approvalAfter(asked, { kind: "thinking", box_name: "eng-r" })).toEqual(asked);
  });

  it("replaces rather than stacks a second approval", () => {
    const second = { ...asked, request_id: "r-2", command: "git push --force" };
    expect(
      approvalAfter(asked, { kind: "approval", box_name: "eng-r", request: second }),
    ).toEqual(second);
  });
});

describe("choiceLabel", () => {
  it("labels the three answers the window offers", () => {
    expect(choiceLabel("session")).toBe("Allow for this conversation");
    expect(choiceLabel("once")).toBe("Allow once");
    expect(choiceLabel("deny")).toBe("Deny");
  });

  it("gives always no reassuring label, because it should never reach the card", () => {
    // FLOTTA-62. If it arrives, the two halves drifted — show the raw word
    // rather than dress a permanent, pattern-wide grant up as a friendly button.
    expect(choiceLabel("always")).toBe("always");
  });

  it("shows an unexpected choice as itself rather than hiding it", () => {
    expect(choiceLabel("mystery")).toBe("mystery");
  });
});

describe("isBusy", () => {
  it("counts waiting on an approval as busy", () => {
    // A message typed now would queue behind the turn it seems to answer.
    expect(isBusy("approval")).toBe(true);
    expect(isBusy("thinking")).toBe(true);
    expect(isBusy("waking")).toBe(true);
  });

  it("is free once the agent has answered or the conversation is idle", () => {
    for (const status of ["ready", "reply", "failed", "closed"] as const) {
      expect(isBusy(status), status).toBe(false);
    }
  });
});

describe("sessionScope", () => {
  const session: ApprovalRequest = { ...asked, choices: ["once", "session", "deny"] };

  it("names the pattern a conversation-wide allow would cover", () => {
    // The breadth is the part a person gets wrong: the label says how long,
    // this says how much.
    const text = sessionScope(session);
    expect(text).toContain("delete in root path");
    expect(text).toContain("not just this one");
  });

  it("still warns about breadth when Hermes sent no pattern", () => {
    const text = sessionScope({ ...session, pattern: null });
    expect(text).toContain("same category");
  });

  it("says nothing when the card does not offer a conversation-wide allow", () => {
    // A smart-denied command gets once and deny only; a warning about a
    // button that is not there would just be noise.
    expect(sessionScope({ ...asked, choices: ["once", "deny"] })).toBeNull();
  });
});
