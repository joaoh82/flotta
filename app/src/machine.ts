/**
 * What the Info panel decides, with no React in sight.
 *
 * The frontend had no tests and that is where the bugs were (FLOTTA-31), and
 * this panel is made almost entirely of decisions: whether two sources agree,
 * how to read an image reference, what to call a machine that is not there.
 * Every one of them is a pure function of data, so every one of them is
 * testable — and none of them typechecks its way to being right.
 */

import type { Machine } from "./types";

/**
 * The substrate's vocabulary, mapped onto the store's.
 *
 * Fly says `started`; the store says `running`. Fly says `suspended` for a
 * machine whose RAM was snapshotted, which the store also calls `stopped` —
 * correctly, because from the fleet's point of view both are "asleep, costs
 * disk, wakes when addressed".
 *
 * `null` means *transitional*: the machine is between states, and comparing it
 * to a row would report drift for a box that is merely starting up. A panel
 * that cries drift during a normal wake teaches people to ignore it.
 */
export function asStoreStatus(state: string): string | null {
  switch (state) {
    case "started":
      return "running";
    case "stopped":
    case "suspended":
      return "stopped";
    case "gone":
    case "destroyed":
      return "torn_down";
    default:
      // created, starting, stopping, replacing, destroying, unknown.
      return null;
  }
}

/**
 * Do the row and the substrate tell the same story?
 *
 * This is the question the panel exists to answer. The store is a *belief*,
 * written by whatever last reached the machine; when nothing has for a while,
 * it is a stale sentence presented with total confidence. That is exactly what
 * the fleet list shows, so the disagreement has to be visible somewhere.
 *
 * Returns `null` when there is nothing to say — agreement, or a transition.
 */
export function drift(rowStatus: string, machineState: string): string | null {
  const equivalent = asStoreStatus(machineState);
  if (equivalent === null || equivalent === rowStatus) return null;

  if (equivalent === "torn_down") {
    return `The fleet has a row for this agent, but its machine is not there. Nothing is running, and nothing is billing.`;
  }
  if (rowStatus === "stopped" && equivalent === "running") {
    return `The fleet thinks this agent is asleep; its machine is up. Something woke it — anyone talking to it through the door does — and the row has not caught up.`;
  }
  if (rowStatus === "running" && equivalent === "stopped") {
    return `The fleet thinks this agent is up; its machine is not. Something put it down — the idle sweep, or Fly itself during a host drain — and the row has not caught up.`;
  }
  return `The fleet says ${rowStatus}; the machine says ${machineState}.`;
}

/** An OCI reference, split for display. */
export type ImageParts = {
  /** `registry.fly.io/joaoh82-flotta-images` */
  repository: string;
  /** `deployment-01M23…`, when there is one. */
  tag: string | null;
  /** `sha256:bbaf60…`, when there is one. */
  digest: string | null;
};

/**
 * Split an image reference into its parts.
 *
 * Both halves matter and they answer different questions. The **tag** says
 * which deploy this is, and is the thing a person compares with what `fly
 * deploy` last printed. The **digest** says which bytes, and is the only part
 * that cannot be moved under you — an upgrade that changes nothing else
 * changes this.
 *
 * Order is deliberate: a Fly reference is `repo:tag@sha256:…`, so the digest
 * splits off first. Reading the tag first would swallow the digest into it and
 * show a 90-character "tag".
 */
export function imageParts(image: string): ImageParts {
  const [left, digest] = splitOnce(image, "@");
  // Only a colon *after* the last slash is a tag; `registry:5000/x` is a port.
  const slash = left.lastIndexOf("/");
  const colon = left.lastIndexOf(":");
  if (colon > slash) {
    return { repository: left.slice(0, colon), tag: left.slice(colon + 1), digest };
  }
  return { repository: left, tag: null, digest };
}

function splitOnce(value: string, sep: string): [string, string | null] {
  const at = value.indexOf(sep);
  return at === -1 ? [value, null] : [value.slice(0, at), value.slice(at + sep.length)];
}

/** One line of the panel. */
export type Field = { label: string; value: string; mono?: boolean };

/**
 * The machine as an ordered list of lines, skipping what the substrate did not
 * report.
 *
 * Built here rather than as fifteen conditionals in JSX for the reason the
 * whole module exists: which lines appear, and what a blank looks like, are
 * decisions. A missing key blanks a line — it must never render the word
 * "undefined", which is what an unguarded `{machine.region}` does.
 */
export function fieldsOf(machine: Machine): Field[] {
  const fields: Field[] = [];
  const add = (label: string, value: string | number | null | undefined, mono = false) => {
    // `== null` catches both null and undefined and nothing else. A truthiness
    // check would treat `0` as unreported, which for a size is the difference
    // between "the substrate did not say" and "it said zero".
    if (value == null || value === "") return;
    fields.push({ label, value: String(value), mono });
  };

  add("Machine", machine.machine_id, true);
  add("Fly app", machine.app, true);
  add("Region", machine.region);
  if (machine.cpus != null || machine.memory_mb != null) {
    const cpu =
      machine.cpus != null ? `${machine.cpus} ${machine.cpu_kind ?? ""} vCPU`.trim() : null;
    const ram = machine.memory_mb != null ? `${memory(machine.memory_mb)} RAM` : null;
    add("Size", [cpu, ram].filter(Boolean).join(", "));
  }
  // The disk, named as what it is. On this fleet the volume *is* the agent:
  // `/data/hermes` holds its memories, its skills and its conversations, and
  // it is what survives an upgrade or a rebuilt machine.
  if (machine.volume_id) {
    const size = machine.volume_gb ? ` · ${machine.volume_gb} GB` : "";
    const path = machine.volume_path ? ` at ${machine.volume_path}` : "";
    add("Memory disk", `${machine.volume_id}${size}${path}`, true);
  }
  add("Private address", machine.private_ip, true);
  add("Machine created", machine.created_at);
  add("Last changed", machine.updated_at);
  // Only when it is *not* fine: "ok" on every panel is a line people stop
  // reading, and the one time it matters it would look like all the others.
  if (machine.host_status && machine.host_status !== "ok") {
    add("Host", machine.host_status);
  }
  return fields;
}

function memory(mb: number): string {
  return mb >= 1024 && mb % 1024 === 0 ? `${mb / 1024} GB` : `${mb} MB`;
}

/** A verdict the panel can render, with the sentence that goes under it. */
export type Standing = {
  kind: "current" | "behind" | "unknown";
  detail: string;
};

/**
 * Is this agent on the image the fleet builds?
 *
 * The verdict comes from the control plane (`image_current`), which compares
 * with `_same_image` — Fly reports a digest the configured image does not
 * carry, and that comparison has been wrong three times here. This only turns
 * it into words.
 *
 * `unknown` is a real answer and must stay one. Rendering it as `behind` would
 * offer an Upgrade button on no evidence, and an upgrade restarts the agent.
 */
export function imageStanding(view: {
  image_current?: boolean | null;
  fleet_image?: string | null;
  machine?: { image?: string | null } | null;
}): Standing {
  if (view.image_current === true) {
    return { kind: "current", detail: "On the image this fleet builds." };
  }
  if (view.image_current === false) {
    return {
      kind: "behind",
      detail: "This agent runs an older image than the one the fleet builds.",
    };
  }
  if (!view.fleet_image) {
    return {
      kind: "unknown",
      detail:
        "The control plane has no fleet image configured, so there is nothing to compare " +
        "against. Set FLOTTA_FLY_IMAGE where it runs.",
    };
  }
  return {
    kind: "unknown",
    detail: "The substrate did not say which image this machine runs.",
  };
}

/**
 * Is the fleet's Hermes pin behind the newest release?
 *
 * Note what this is *not* about: any running agent. It compares what the next
 * image would be built from against what upstream has. An agent can be on an
 * older Hermes than the pin — that is `imageStanding`, and the two answer
 * different questions on purpose.
 */
export function hermesStanding(versions: {
  pinned: string;
  latest?: string | null;
  behind?: boolean;
  unavailable?: string | null;
}): Standing {
  if (versions.unavailable || !versions.latest) {
    return {
      kind: "unknown",
      detail:
        versions.unavailable ??
        "Could not check for a newer Hermes. This is not the same as being up to date.",
    };
  }
  if (versions.behind) {
    return {
      kind: "behind",
      detail:
        `Hermes ${versions.latest} is out; this fleet builds ${versions.pinned}. ` +
        `Moving the pin rebuilds the image — \`just hermes-bump ${versions.latest}\` — ` +
        "and is not something the app does, because it re-runs the live checks.",
    };
  }
  return { kind: "current", detail: `The newest Hermes is ${versions.latest}.` };
}

/**
 * What to show for the Hermes an agent is running.
 *
 * The blank case is the interesting one and it is temporary: an image built
 * before `fly/Dockerfile` grew its version label says nothing about what is
 * inside it. Saying "unknown" invites "so is it broken?"; saying *why*, and
 * that upgrading fixes it, is the true and useful sentence — upgrading swaps
 * in a labelled image.
 */
export function hermesOf(machine: { hermes_ref?: string | null } | null): {
  value: string | null;
  note: string | null;
} {
  const ref = machine?.hermes_ref?.trim();
  if (ref) return { value: ref, note: null };
  return {
    value: null,
    note: "This image was built before Flotta recorded its Hermes version. Upgrading replaces it with one that does.",
  };
}

/**
 * Is the control plane pinned to something older than the newest build?
 *
 * The specific failure this exists for: `FLOTTA_FLY_IMAGE` is a deployment
 * variable naming an image in another Fly app, and it has gone stale twice —
 * most recently still naming an app that had been *deleted*, which made this
 * panel offer to downgrade the one agent that had been upgraded and report the
 * stale ones as current.
 *
 * Nothing was wrong with the panel; the configuration was lying, and there was
 * no way to see that from here. Now there is.
 *
 * Silent unless there is something to say — a `release` source cannot be
 * stale, and an unknown newest build is not evidence of anything.
 */
export function fleetImageNote(versions: {
  fleet_image?: string | null;
  fleet_image_source?: string | null;
  newest_release?: string | null;
  fleet_image_app?: string | null;
}): string | null {
  const {
    fleet_image: image,
    fleet_image_source: source,
    newest_release: newest,
    fleet_image_app: app,
  } = versions;

  // Two causes with two different fixes, and they used to render as one
  // sentence: nothing configured at all, versus an app named whose releases
  // cannot be read — a wrong name, a deleted app, a flyctl that cannot reach
  // it. Saying which one turns "it does not work" into an action.
  if (source === "none" || (!image && !newest)) {
    if (!app) {
      return (
        "No fleet image, and no build app configured to find one. Set FLOTTA_FLY_APP " +
        "on the control plane to the app `just fly-up` deploys into."
      );
    }
    return (
      `No fleet image: nothing could be read from the app ${app}. Either that is not ` +
      "the app `just fly-up` deploys into, or it has no completed release."
    );
  }
  if (source !== "env" || !newest || !image) return null;
  // Compared as written. The control plane does the digest-aware comparison
  // where it matters (`image_current`); here both strings come from the same
  // side and a difference is a difference.
  if (image === newest) return null;
  return (
    "This control plane is pinned to an image by FLOTTA_FLY_IMAGE, and it is not " +
    "the newest one built. Upgrading an agent would move it onto the pinned image, " +
    "not the latest."
  );
}
