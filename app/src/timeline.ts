import type { BoxEvent } from "./types";

/**
 * What a box's timeline says about it.
 *
 * Pure, and in its own module with no React import, because this is where the
 * app's bugs have actually been. FLOTTA-29's review found four, and every one
 * of them was a `filter` predicate or a branch on data — all four typechecked,
 * because a typechecker cannot see that you picked the wrong `torn_down`.
 */

/** The reason an event carries, when it carries one. */
export function reasonOf(event: BoxEvent): string | null {
  const value = event.payload?.reason;
  return typeof value === "string" ? value : null;
}

/**
 * The secrets a `fleet_secrets_missing` event names.
 *
 * Its payload has `reason` *and* `missing`, and the reason is generic advice —
 * the list is the part that says which ones. Rendering only the reason left
 * the useful half on the floor.
 */
export function missingOf(event: BoxEvent): string[] {
  const value = event.payload?.missing;
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];
}

/**
 * Only the box's own events.
 *
 * **This filter is the whole reason `entity_kind` is carried.**
 * `store.get_box_timeline` unions the box's events with those of its tasks
 * *and* its workspaces, because "what has this agent been doing" spans all
 * three tiers. Without the filter, "why did this box end" silently means "what
 * ended last, whatever it was" — and a workspace teardown writes
 * `"reason": "box <name> torn down"`, which reads perfectly as the agent's own
 * fate. It gave the right answer only because `teardown_box` happens to write
 * the workspace events first.
 */
export function ownEvents(events: readonly BoxEvent[]): BoxEvent[] {
  return events.filter((event) => event.entity_kind === "box");
}

/**
 * Events that are worth saying out loud rather than only listing.
 *
 * A box can finish provisioning and still be unable to work: no provider key
 * means it answers every turn with "No inference provider configured", and no
 * signing key means it cannot fetch a git credential. Both are recorded and
 * neither changes the status, so a box that is `running` and useless looks
 * exactly like one that is fine.
 *
 * They matter *more* on a box that failed, not less: a machine that will not
 * boot for want of `FLOTTA_BOX_PASSWORD` ends as "provisioning never
 * completed", and this is the only place the actual cause appears.
 */
export const WARNINGS = new Set(["fleet_secrets_missing", "identity_skipped"]);

export function warningsIn(events: readonly BoxEvent[]): BoxEvent[] {
  return ownEvents(events).filter((event) => WARNINGS.has(event.type));
}

/**
 * How the agent's story ends, as far as the timeline knows.
 *
 * `unfinished` is not "fine" — it is "nothing terminal has happened yet", which
 * for the pane that shows this means still being built. Distinguishing the
 * other two is what stops an agent that ran for a week and was destroyed this
 * morning being told its provision did not finish.
 */
export type Ending =
  | { kind: "unfinished" }
  /** It existed. A machine ran, and then it was destroyed. */
  | { kind: "gone"; reason: string | null }
  /** It never became a machine. */
  | { kind: "never-created"; reason: string | null };

/**
 * `status` is the box's status, or null when it has left the fleet list.
 *
 * `torn_down` is the only terminal box status — the store's transition table
 * gives boxes no `failed`, because machines get destroyed rather than fail.
 */
export function endingOf(events: readonly BoxEvent[], status: string | null): Ending {
  if (status !== "torn_down") return { kind: "unfinished" };

  const own = ownEvents(events);
  const reason =
    own
      .filter((event) => event.type === "torn_down")
      .map(reasonOf)
      .filter((value): value is string => value !== null)
      .pop() ?? null;

  // A machine having existed is what separates "destroyed" from "never made".
  // `stopped` counts as well as `running`: `BoxNotRunning` is the path where a
  // machine *was* created and did not come up, and telling someone that never
  // happened would leave them looking for a machine nobody mentioned.
  const lived = own.some((event) => event.type === "running" || event.type === "stopped");
  return lived ? { kind: "gone", reason } : { kind: "never-created", reason };
}
