import { describe, expect, it } from "vitest";
import { statusAfter, turnsAfter } from "./transcript";
import type { Turn } from "./types";

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
    expect(statusAfter({ kind: "reset", box_name: "eng-r" })).toBe("ready");
  });

  it("passes every other event through as its own kind", () => {
    expect(statusAfter({ kind: "waking", box_name: "eng-r" })).toBe("waking");
    expect(statusAfter({ kind: "thinking", box_name: "eng-r" })).toBe("thinking");
    expect(
      statusAfter({ kind: "failed", box_name: "eng-r", detail: "x" }),
    ).toBe("failed");
  });
});
