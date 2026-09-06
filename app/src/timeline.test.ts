import { describe, expect, it } from "vitest";
import fixture from "../src-tauri/tests/timeline.json";
import { endingOf, missingOf, ownEvents, reasonOf, warningsIn } from "./timeline";
import type { BoxEvent } from "./types";

/**
 * The frontend's first tests, and they exist because of where the bugs were.
 *
 * FLOTTA-29's review found five things; four were in `.tsx` and all four
 * typechecked. `just check-app` had 30 Rust tests on one side of the app and a
 * typechecker on the other, which is not the same thing — a typechecker cannot
 * see that you picked the wrong `torn_down`.
 *
 * The same fixture the Rust side parses: a real timeline pulled from the
 * deployed control plane rather than hand-written, because both halves decide
 * what to tell somebody by reading exactly these bytes.
 */
const REAL = fixture.events as unknown as BoxEvent[];

function event(partial: Partial<BoxEvent> & Pick<BoxEvent, "type">): BoxEvent {
  return { id: 1, ts: "2026-09-05T08:00:00+00:00", entity_kind: "box", payload: null, ...partial };
}

describe("the real timeline", () => {
  it("is all box-tier, and every event survives the filter", () => {
    expect(REAL).toHaveLength(18);
    expect(ownEvents(REAL)).toHaveLength(18);
  });

  it("says nothing has ended, because eng-e has not", () => {
    // It is `stopped` — idle, not finished, which is the distinction the whole
    // fleet exists to make.
    expect(endingOf(REAL, "stopped")).toEqual({ kind: "unfinished" });
  });

  it("carries reasons on the events that have them and not on the others", () => {
    expect(REAL.filter((e) => reasonOf(e) !== null).length).toBeGreaterThan(0);
    expect(reasonOf(REAL[0])).toBeNull(); // `provisioning` carries name + backend
  });

  it("has no warnings, because that box got its identity", () => {
    expect(warningsIn(REAL)).toEqual([]);
  });
});

describe("which torn_down explains the box", () => {
  // The bug. `get_box_timeline` unions box, task and workspace events, so the
  // last `torn_down` in the list can belong to a workspace — and a workspace
  // teardown writes `"reason": "box <name> torn down"`, which reads perfectly
  // as the agent's own fate. It was right only because `teardown_box` happens
  // to write the workspace events first.
  const timeline: BoxEvent[] = [
    event({ id: 1, type: "provisioning" }),
    event({ id: 2, type: "torn_down", payload: { reason: "create failed: no capacity" } }),
    event({
      id: 3,
      entity_kind: "workspace",
      type: "torn_down",
      payload: { reason: "box eng-f torn down" },
    }),
  ];

  it("is the box's own, whatever ended last", () => {
    expect(endingOf(timeline, "torn_down")).toEqual({
      kind: "never-created",
      reason: "create failed: no capacity",
    });
  });

  it("ignores task events too, not just workspaces", () => {
    const withTask = [
      ...timeline,
      event({ id: 4, entity_kind: "task", type: "failed", payload: { reason: "the task failed" } }),
    ];
    expect(ownEvents(withTask).map((e) => e.id)).toEqual([1, 2]);
  });
});

describe("destroyed, or never created", () => {
  // An agent that ran for a week and was destroyed this morning is `torn_down`
  // too. Telling it "your provision did not finish" is wrong about the only
  // thing this pane is for.
  it("an agent that ran is gone, not un-created", () => {
    const lived = [
      event({ id: 1, type: "provisioning" }),
      event({ id: 2, type: "running", payload: { endpoint: "fly://app/m1" } }),
      event({ id: 3, type: "torn_down", payload: { reason: "destroyed by cli" } }),
    ];
    expect(endingOf(lived, "torn_down")).toEqual({ kind: "gone", reason: "destroyed by cli" });
  });

  it("a machine that was made and never came up also counts as having existed", () => {
    // `BoxNotRunning`: the machine exists and is recorded `stopped`. Saying it
    // was never created leaves someone looking for a machine nobody mentioned.
    const stopped = [
      event({ id: 1, type: "provisioning" }),
      event({ id: 2, type: "stopped", payload: { reason: "machine is 'stopped' after create" } }),
      event({ id: 3, type: "torn_down", payload: { reason: "provisioning never completed" } }),
    ];
    expect(endingOf(stopped, "torn_down").kind).toBe("gone");
  });

  it("a provision that never reached a machine was never created", () => {
    const failed = [
      event({ id: 1, type: "provisioning" }),
      event({ id: 2, type: "torn_down", payload: { reason: "create failed: BackendError" } }),
    ];
    expect(endingOf(failed, "torn_down").kind).toBe("never-created");
  });

  it("nothing terminal is unfinished, whatever the timeline holds", () => {
    expect(endingOf([event({ type: "provisioning" })], "provisioning")).toEqual({
      kind: "unfinished",
    });
    expect(endingOf([], null)).toEqual({ kind: "unfinished" });
  });

  it("an ending with no recorded reason is still an ending", () => {
    const bare = [event({ id: 1, type: "torn_down", payload: null })];
    expect(endingOf(bare, "torn_down")).toEqual({ kind: "never-created", reason: null });
  });
});

describe("what a missing-secrets event actually says", () => {
  // Its payload has `reason` *and* `missing`. The reason is generic advice;
  // the list is the part that names `FLOTTA_BOX_PASSWORD`, and rendering only
  // the reason left the useful half on the floor.
  const missing = event({
    type: "fleet_secrets_missing",
    payload: {
      missing: ["FLOTTA_BOX_PASSWORD", "OPENROUTER_API_KEY"],
      reason: "this box cannot serve without them",
    },
  });

  it("names the secrets, not just the advice", () => {
    expect(missingOf(missing)).toEqual(["FLOTTA_BOX_PASSWORD", "OPENROUTER_API_KEY"]);
    expect(reasonOf(missing)).toBe("this box cannot serve without them");
  });

  it("is a warning worth surfacing, and so is a skipped identity", () => {
    const skipped = event({ id: 2, type: "identity_skipped", payload: { reason: "no key" } });
    expect(warningsIn([missing, skipped, event({ id: 3, type: "running" })])).toHaveLength(2);
  });

  it("survives a payload that is not the shape it should be", () => {
    // Untyped by design — `payload` is whatever the control plane recorded —
    // so every read of it has to tolerate being wrong rather than throw in a
    // render.
    expect(missingOf(event({ type: "fleet_secrets_missing", payload: { missing: "one" } }))).toEqual(
      [],
    );
    expect(missingOf(event({ type: "fleet_secrets_missing", payload: null }))).toEqual([]);
    expect(missingOf(event({ type: "fleet_secrets_missing", payload: { missing: [1, "a"] } }))).toEqual(
      ["a"],
    );
    expect(reasonOf(event({ type: "torn_down", payload: { reason: 42 } }))).toBeNull();
  });

  it("does not warn about another tier's event", () => {
    const workspace = event({ id: 9, entity_kind: "workspace", type: "identity_skipped" });
    expect(warningsIn([workspace])).toEqual([]);
  });
});
