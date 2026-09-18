/**
 * The shapes Rust hands across the boundary.
 *
 * Kept in step with `src-tauri/src/fleet.rs` by hand — there is no codegen, and
 * adding one for three structs would be more machinery than it saves. The
 * cost of drift is a runtime `undefined`, so anything optional is typed
 * optional here rather than assumed present.
 */

/**
 * An agent another agent may message (M7). Mirrors `Peer` in `fleet.rs`.
 *
 * `id` is what the grant is recorded against — a name can be reused once its
 * agent is destroyed — and `name` is what the asking agent has to type.
 */
export type Peer = {
  id: string;
  name: string;
  display_name?: string | null;
  description?: string | null;
};

/** Mirrors `Colleagues` in `fleet.rs`. Everyone is a colleague unless blocked. */
export type Colleagues = {
  peers: Peer[];
  blocked: Peer[];
};

export type BoxRow = {
  id: string;
  /** The ADDRESS — a DNS label, immutable. Not what a person calls it. */
  name: string;
  status: string;
  endpoint?: string | null;
  created_at?: string | null;
  /**
   * What a person calls it (FLOTTA-40). `null` is "not set" and the window
   * falls back to the address; a missing key would render as `undefined`.
   */
  display_name?: string | null;
  description?: string | null;
  /**
   * The standing instructions Flotta last wrote to the agent's volume. The
   * copy it reads is `SOUL.md` there, which the agent may have changed since,
   * so this is the record rather than a claim about the live prompt.
   *
   * Editable from the Info panel — but only through a verb that reaches the
   * volume *and* restarts the conversation, because a session keeps the system
   * prompt it was born with.
   */
  instructions?: string | null;
  /** The model the agent runs (FLOTTA-39) — its own, or the fleet's. */
  model?: string | null;
  /** `agent` or `fleet`; null when the fleet has no model configured. */
  model_source?: "agent" | "fleet" | null;
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
  /**
   * Which Hermes this image carries, from a label baked in at build time.
   *
   * Absent for an image built before that label existed — which is not the
   * same as "no Hermes", and every agent in the fleet is one until upgraded.
   */
  hermes_ref?: string | null;
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
  /**
   * Where this agent clones projects (`FLOTTA_WORKDIR` on the machine).
   * Absent on agents created before the setting was wired; they still use
   * `/workspace`, which is the entrypoint default.
   */
  workdir?: string | null;
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
  /** What an upgrade with no argument would move this agent onto. */
  fleet_image?: string | null;
  /**
   * Whether it is already on that image.
   *
   * **`null` is not `false`.** One side being unknown must not offer an
   * upgrade. Decided on the control plane with `_same_image`, because Fly
   * reports a digest the configured image does not carry, and comparing the
   * two as strings has been wrong three times in this repo.
   */
  image_current?: boolean | null;
};

/**
 * Which Hermes the fleet builds, and whether upstream has a newer one.
 *
 * Three facts, kept apart on purpose: `pinned` is what the *next* image would
 * be built from, `latest` is what upstream has, and what an individual agent
 * runs is neither — that is `Machine.hermes_ref`. They were one line of
 * justfile output before, and they have never been the same fact.
 */
/** One attempt to build the box image. Mirrors `Build` in `store.py`. */
export type Build = {
  id: string;
  hermes_ref: string;
  /** `building`, `rolling`, `done` or `failed`. `rolling` is still running. */
  status: string;
  image?: string | null;
  error?: string | null;
  started_at: string;
  finished_at?: string | null;
};

export type HermesVersions = {
  /**
   * What the *next* image would be built at if nobody said otherwise. An
   * intention — it says nothing about any image that exists, and after an
   * update started from the app it stays put while the fleet moves on.
   */
  pinned: string;
  /** What the fleet's newest image was actually built at. The fact. */
  fleet_ref?: string | null;
  /** `build` when a build produced it, `pin` when nothing has been built. */
  fleet_ref_source?: string | null;
  /** `null` when GitHub could not be reached: unknown, never "up to date". */
  latest?: string | null;
  behind?: boolean;
  unavailable?: string | null;
  fleet_image?: string | null;
  /** `env`, `release` or `none` — where `fleet_image` came from. */
  fleet_image_source?: string | null;
  /**
   * What the build app last released.
   *
   * Reported even when the environment won, because the two disagreeing is
   * the trap: a deployment variable naming an image older than the newest
   * build makes the window offer the wrong upgrade with total confidence.
   */
  newest_release?: string | null;
  /** Which app releases are read from, or null when none is configured. */
  fleet_image_app?: string | null;
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
  /**
   * Whether the app may change it. A shown-but-not-settable value is one a
   * person needs to *see* — the provider endpoint, which decides where the
   * fleet's API key is sent and so cannot be written over the API.
   */
  editable?: boolean;
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
  /**
   * The conversation was started over and the transcript is now empty.
   *
   * Deliberately not `ready` with an empty `resumed`: that arrives from a
   * resync too, and clearing on it wiped the transcript of an agent that was
   * mid-conversation. "Nothing to show yet" and "nothing any more" are
   * different instructions.
   */
  | { kind: "reset"; box_name: string }
  /**
   * The agent wants to run something Hermes considers risky and is waiting for
   * a person to decide. Ignored until FLOTTA-60, which meant a turn could sit
   * on "thinking…" for minutes, blocked on a question the window never asked.
   */
  | { kind: "approval"; box_name: string; request: ApprovalRequest }
  /**
   * The question is no longer open: Hermes's timeout denied it, or the turn
   * was interrupted. The card comes down; the turn goes on (FLOTTA-64).
   */
  | { kind: "approval_withdrawn"; box_name: string }
  | { kind: "thinking"; box_name: string }
  /**
   * What the agent is doing, while it does it (FLOTTA-61). Before this the
   * window showed "thinking…" from submit to reply, and a turn that ran seven
   * commands looked identical to one that had hung.
   */
  | { kind: "progress"; box_name: string; step: Step }
  | { kind: "reply"; box_name: string; text: string }
  | { kind: "failed"; box_name: string; detail: string }
  | { kind: "closed"; box_name: string };

/**
 * What Hermes asked permission for. Mirrors `ApprovalRequest` in
 * `src-tauri/src/agent.rs`.
 *
 * `command` is already redacted on the box: a credential-shaped value is
 * masked before it leaves. `choices` are Hermes's own — a command its
 * classifier judged dangerous is offered fewer of them, and the window must
 * not offer them back.
 */
export type ApprovalRequest = {
  request_id: string | null;
  command: string;
  description: string;
  /**
   * The category Hermes matched, e.g. `delete in root path`. **This, not the
   * command, is what "Allow for this conversation" grants** — Hermes records
   * approvals by pattern — so the card names it before the click (FLOTTA-62).
   */
  pattern: string | null;
  /** Never contains `always`: the Rust side drops it on purpose. */
  choices: string[];
};

/** One line of a conversation the box already had. */
export type HistoryLine = { role: string; text: string };

/**
 * One step of a turn in progress. Mirrors `Step` in `src-tauri/src/agent.rs`,
 * whose payload shapes were captured off a live gateway.
 */
export type Step =
  /** The next piece of the model's reasoning. */
  | { kind: "reasoning"; text: string }
  /** The model is writing a call to this tool. */
  | { kind: "preparing"; tool: string }
  /** The next piece of what the agent is saying. */
  | { kind: "text"; text: string }
  /**
   * Something the agent said before using a tool, now finished. The final
   * reply does not repeat it — measured — so it is a transcript line of its own.
   */
  | { kind: "said"; text: string }
  | { kind: "tool_started"; id: string; tool: string; detail: string }
  | {
      kind: "tool_finished";
      id: string;
      tool: string;
      seconds: number | null;
      failed: boolean;
    };

/** A tool the agent used, as the transcript shows it. */
export type WorkStep = {
  id: string;
  tool: string;
  /** One bounded line: what the tool was asked to do. */
  detail: string;
  state: "running" | "done" | "failed";
  seconds: number | null;
};

/** One line of a transcript. */
export type Turn =
  | { from: "you" | "agent" | "system"; text: string }
  /**
   * The tools used between two things said. Shown while the turn runs and
   * kept after it — but not on a resumed conversation, whose history comes
   * from the box and carries only what was said.
   */
  | { from: "work"; steps: WorkStep[] };

/**
 * What is streaming right now and not yet a line of the transcript: the reply
 * as it is written, the reasoning behind the next step, the tool being
 * prepared.
 */
export type Live = {
  text: string;
  reasoning: string;
  preparing: string | null;
};
