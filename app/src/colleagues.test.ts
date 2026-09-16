import { describe, expect, it } from "vitest";
import fixture from "../src-tauri/tests/peer_timelines.json";
import { delegationOf, exchangesIn, peerLabel } from "./colleagues";
import type { BoxEvent } from "./types";

/**
 * The first real agent-to-agent exchanges, captured off the live fleet the
 * day M7 shipped: eng-g asked eng-r twice, and eng-r answered twice — the
 * second answer being eng-r reporting that its attempt to ask eng-g back was
 * refused as a loop.
 */
const ENG_G = fixture["eng-g"] as unknown as BoxEvent[];
const ENG_R = fixture["eng-r"] as unknown as BoxEvent[];
const LATER = Date.parse("2026-09-16T12:00:00Z");

function event(partial: Partial<BoxEvent> & Pick<BoxEvent, "type">): BoxEvent {
  return { id: 1, ts: "2026-09-16T08:00:00+00:00", entity_kind: "box", payload: null, ...partial };
}

describe("the real exchanges", () => {
  it("pairs each of eng-g's questions with eng-r's answer, newest first", () => {
    const exchanges = exchangesIn(ENG_G, LATER);
    expect(exchanges).toHaveLength(2);
    expect(exchanges.every((x) => x.direction === "asked" && x.peer === "eng-r")).toBe(true);
    expect(exchanges.every((x) => x.outcome === "answered")).toBe(true);
    expect(exchanges[0].question).toMatch(/ask eng-g what 2\+2 is/);
    expect(exchanges[1].question).toMatch(/reviewer check first/);
    expect(exchanges[1].reply).toMatch(/input validation/);
    expect(exchanges[1].seconds).toBe(52.3);
  });

  it("shows the same two from eng-r's side, as questions it was asked", () => {
    const exchanges = exchangesIn(ENG_R, LATER);
    expect(exchanges.map((x) => [x.direction, x.peer, x.outcome])).toEqual([
      ["was_asked", "eng-g", "answered"],
      ["was_asked", "eng-g", "answered"],
    ]);
  });

  it("does not mistake a grant or a revocation for a conversation", () => {
    const kinds = new Set(ENG_G.map((e) => e.type));
    expect(kinds.has("peer_granted") && kinds.has("peer_revoked")).toBe(true);
    expect(exchangesIn(ENG_G, LATER)).toHaveLength(2);
  });
});

describe("what is known and what is not", () => {
  const asked = event({
    type: "peer_asked",
    ts: "2026-09-16T08:00:00+00:00",
    payload: { peer: "eng-r", message: "still there?" },
  });

  it("is still waiting inside the relay's deadline", () => {
    const [x] = exchangesIn([asked], Date.parse("2026-09-16T08:01:00Z"));
    expect(x.outcome).toBe("waiting");
  });

  it("says it does not know once the deadline has passed with nothing recorded", () => {
    // The answering side writes no failure event of its own, so claiming
    // either "failed" or "still going" here would be a guess.
    const [x] = exchangesIn([asked], Date.parse("2026-09-16T08:10:00Z"));
    expect(x.outcome).toBe("unknown");
  });

  it("reports a failure with the relay's reason", () => {
    const [x] = exchangesIn(
      [
        asked,
        event({
          type: "peer_failed",
          ts: "2026-09-16T08:04:00+00:00",
          payload: { peer: "eng-r", reason: "no reply within 240s", seconds: 240 },
        }),
      ],
      LATER,
    );
    expect(x).toMatchObject({ outcome: "failed", reason: "no reply within 240s", reply: null });
  });

  it("shows a refused message, which is what makes a stopped runaway visible", () => {
    const [x] = exchangesIn(
      [
        event({
          type: "peer_refused",
          payload: {
            peer: "eng-g",
            message: "and what do you think?",
            reason: "that would send this conversation back to an agent already in it",
          },
        }),
      ],
      LATER,
    );
    expect(x).toMatchObject({ direction: "asked", outcome: "refused", peer: "eng-g" });
    expect(x.reason).toMatch(/already in it/);
  });

  it("does not pair an answer with a question to somebody else", () => {
    const exchanges = exchangesIn(
      [
        asked,
        event({
          type: "peer_answered",
          ts: "2026-09-16T08:01:00+00:00",
          payload: { peer: "eng-d", reply: "not you" },
        }),
      ],
      Date.parse("2026-09-16T08:02:00Z"),
    );
    expect(exchanges).toHaveLength(1);
    expect(exchanges[0].outcome).toBe("waiting");
  });

  it("skips an answer whose question is older than the events it was given", () => {
    const exchanges = exchangesIn(
      [event({ type: "peer_replied", payload: { peer: "eng-g", reply: "orphan" } })],
      LATER,
    );
    expect(exchanges).toEqual([]);
  });

  it("ignores task and workspace events that happen to carry a peer", () => {
    const exchanges = exchangesIn(
      [{ ...asked, entity_kind: "task" }],
      Date.parse("2026-09-16T08:01:00Z"),
    );
    expect(exchanges).toEqual([]);
  });
});

describe("reading a step as a colleague being asked", () => {
  it("reads the command a model actually typed", () => {
    expect(delegationOf('flotta-ask eng-r "What should a reviewer check first?"')).toEqual({
      kind: "ask",
      peer: "eng-r",
      question: "What should a reviewer check first?",
    });
  });

  it("tolerates what comes before it and flags after it", () => {
    expect(delegationOf("cd /workspace && flotta-ask --json eng-r 'hi there'")).toEqual({
      kind: "ask",
      peer: "eng-r",
      question: "hi there",
    });
  });

  it("keeps an unquoted question whole", () => {
    expect(delegationOf("flotta-ask eng-r what wakes a box")).toMatchObject({
      question: "what wakes a box",
    });
  });

  it("keeps a question the detail limit cut short", () => {
    expect(delegationOf('flotta-ask eng-r "a long question that was cut…')).toMatchObject({
      peer: "eng-r",
      question: "a long question that was cut…",
    });
  });

  it("recognises checking the roster, including a bare invocation", () => {
    expect(delegationOf("flotta-ask --list")).toEqual({ kind: "list" });
    expect(delegationOf("flotta-ask")).toEqual({ kind: "list" });
  });

  it("is not fooled by other commands that mention it", () => {
    expect(delegationOf("ls /workspace")).toBeNull();
    expect(delegationOf("which flotta-asker")).toBeNull();
    expect(delegationOf("cat notflotta-ask.md")).toBeNull();
  });

  it("refuses something that cannot be an agent's address", () => {
    expect(delegationOf("flotta-ask $PEER hello")).toBeNull();
  });
});

describe("labels", () => {
  it("names a colleague by what it is for, with the address it answers to", () => {
    expect(peerLabel({ name: "eng-r", display_name: "Reviewer" })).toBe("Reviewer (eng-r)");
    expect(peerLabel({ name: "eng-d", display_name: null })).toBe("eng-d");
  });
});
