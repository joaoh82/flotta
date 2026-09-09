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
 * What the substrate says about a machine. Mirrors `Machine` in
 * `src-tauri/src/fleet.rs`, which mirrors `MachineInfo` in `backend.py`.
 *
 * Everything but `state` is optional at all three layers, deliberately: this
 * is `flyctl`'s JSON, and a key that moves should blank one line of a panel
 * rather than break the read.
 */
export type Machine = {
  state: string;
  machine_id?: string | null;
  app?: string | null;
  image?: string | null;
  region?: string | null;
  cpu_kind?: string | null;
  cpus?: number | null;
  memory_mb?: number | null;
  volume_id?: string | null;
  volume_gb?: number | null;
  volume_path?: string | null;
  private_ip?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  host_status?: string | null;
};

/**
 * The row and the machine, unmerged.
 *
 * Both, because they can disagree and the disagreement is the interesting
 * part. `unavailable` says why there is no machine — no substrate reached, or
 * no machine yet — which is the difference between "Info is broken" and "this
 * agent is still being built".
 */
export type MachineView = {
  box: BoxRow;
  machine?: Machine | null;
  unavailable?: string | null;
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
  /**
   * `box`, `task` or `workspace`.
   *
   * The endpoint is box-scoped but the timeline is not — `get_box_timeline`
   * unions all three tiers, because "what has this agent been doing" spans
   * them. Anything asking "why did this box end" has to filter on this, or it
   * is really asking "what ended last, whatever it was".
   */
  entity_kind: string;
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

/**
 * One fleet setting, as `GET /api/settings` reports it. Mirrors `FleetSetting`
 * in `src-tauri/src/fleet.rs`.
 *
 * The catalogue travels with the value — label, help, kind and default all come
 * from the control plane — so the form is rendered rather than hardcoded, and a
 * setting added server-side appears without the app being rebuilt.
 *
 * `source` says whether the value came from the store (somebody set it), the
 * environment (a deployment variable is still deciding) or the default.
 */
export type FleetSetting = {
  key: string;
  label: string;
  help: string;
  kind: string;
  default: string;
  value: string;
  source: string;
};

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
