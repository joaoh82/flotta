/**
 * The shapes Rust hands across the boundary.
 *
 * Kept in step with `src-tauri/src/fleet.rs` by hand — there is no codegen, and
 * adding one for three structs would be more machinery than it saves. The
 * cost of drift is a runtime `undefined`, so anything optional is typed
 * optional here rather than assumed present.
 */

export type BoxRow = {
  id: string;
  name: string;
  status: string;
  endpoint?: string | null;
  created_at?: string | null;
};

/**
 * One line of a box's timeline. Mirrors `BoxEvent` in `src-tauri/src/fleet.rs`.
 *
 * `payload` is untyped on purpose — its shape depends on `type`, and the app
 * reads two keys out of it. See `reasonOf`.
 */
export type BoxEvent = {
  id: number;
  ts: string;
  type: string;
  payload?: Record<string, unknown> | null;
};

/**
 * Can you talk to this agent right now?
 *
 * `running` obviously. `stopped` too — the door wakes a sleeping box, and most
 * of the fleet is stopped most of the time, which is the cost argument
 * working rather than an outage.
 *
 * `provisioning` is the one this function exists for. A box in that state has
 * a row and no machine, so the door cannot resolve it; opening a conversation
 * against one shows a connection error for a creation that is going perfectly
 * well. That was FLOTTA-29 — the same class of lie the `202` removed from the
 * API, reintroduced one layer up.
 */
export function isAddressable(status: string): boolean {
  return status === "running" || status === "stopped";
}

/**
 * Why this is a tagged union and not a string.
 *
 * "No agents", "cannot reach the control plane" and "your token was refused"
 * all render as an empty list if the UI is handed only an error string — and
 * they have three different fixes. Modelling the difference is what stops the
 * app quietly telling you your fleet is empty when it is actually unreachable.
 */
export type FleetError =
  | { kind: "not_configured"; detail: string }
  | { kind: "unreachable"; detail: string }
  | { kind: "rejected"; detail: string }
  | { kind: "unexpected"; detail: string }
  /** Local, and nothing to do with the control plane. */
  | { kind: "keychain"; detail: string };

export type SettingsView = {
  control_url: string;
  domain: string;
  /** Whether a token is in the keychain. Never the token itself. */
  has_token: boolean;
  /**
   * Set when the keychain could not be read at all — which is not the same as
   * it being empty, and used to be reported as if it were.
   */
  token_error?: string | null;
};

export function isFleetError(value: unknown): value is FleetError {
  return typeof value === "object" && value !== null && "kind" in value;
}

/**
 * What the agent task reports as it happens.
 *
 * Mirrors `AgentEvent` in `src-tauri/src/agent.rs`. Tagged for the same reason
 * `FleetError` is: "waking", "thinking" and "failed" are different states, and
 * a UI that cannot tell them apart shows one spinner for all three — which
 * reads as a hang at exactly the moment the box genuinely is not there yet.
 */
export type AgentEvent =
  | { kind: "waking"; box_name: string }
  /**
   * `resumed` is the conversation the box already had — empty for a new one.
   *
   * The transcript comes **from the box**, not from anything the UI kept: the
   * agent's memory is the source of truth, and a cached copy would keep being
   * shown after the agent had moved on.
   */
  | { kind: "ready"; box_name: string; resumed: HistoryLine[] }
  | { kind: "thinking"; box_name: string }
  | { kind: "reply"; box_name: string; text: string }
  | { kind: "failed"; box_name: string; detail: string }
  | { kind: "closed"; box_name: string };

/** One line of a conversation the box already had. */
export type HistoryLine = { role: string; text: string };

/** One line of a transcript. */
export type Turn = {
  from: "you" | "agent" | "system";
  text: string;
};
