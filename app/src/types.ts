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
  | { kind: "ready"; box_name: string }
  | { kind: "thinking"; box_name: string }
  | { kind: "reply"; box_name: string; text: string }
  | { kind: "failed"; box_name: string; detail: string }
  | { kind: "closed"; box_name: string };

/** One line of a transcript. `pending` marks a turn still in flight. */
export type Turn = {
  from: "you" | "agent" | "system";
  text: string;
};
