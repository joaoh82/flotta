import { describe, expect, it } from "vitest";
import {
  asStoreStatus,
  drift,
  fieldsOf,
  hermesOf,
  hermesStanding,
  imageParts,
  imageStanding,
} from "./machine";
import type { Machine } from "./types";

describe("reading the substrate's vocabulary", () => {
  it("maps a running machine onto the store's word for it", () => {
    expect(asStoreStatus("started")).toBe("running");
  });

  it("treats suspended as asleep, because that is what it is", () => {
    // A suspended machine has its RAM snapshotted and costs disk. From the
    // fleet's point of view that is `stopped` — and calling it drift would
    // flag every box the idle sweep has ever put down.
    expect(asStoreStatus("suspended")).toBe("stopped");
    expect(asStoreStatus("stopped")).toBe("stopped");
  });

  it("says nothing about a machine mid-transition", () => {
    for (const state of ["starting", "stopping", "replacing", "created", "weird"]) {
      expect(asStoreStatus(state)).toBeNull();
    }
  });
});

describe("drift between the row and the machine", () => {
  it("is silent when they agree", () => {
    expect(drift("running", "started")).toBeNull();
    expect(drift("stopped", "suspended")).toBeNull();
  });

  it("is silent while the machine is starting", () => {
    // The common case, and the one that would make the warning worthless:
    // waking a box passes through `starting` on the way to `started`.
    expect(drift("stopped", "starting")).toBeNull();
  });

  it("explains a row that has not caught up with a wake", () => {
    const said = drift("stopped", "started");
    expect(said).toContain("asleep");
    expect(said).toContain("door");
  });

  it("explains a row claiming an agent that is down", () => {
    expect(drift("running", "stopped")).toContain("put it down");
  });

  it("names the case where the machine is simply not there", () => {
    expect(drift("running", "gone")).toContain("not there");
  });
});

describe("reading an image reference", () => {
  it("splits the digest off before the tag", () => {
    // The order is the bug worth pinning: reading the tag first swallows the
    // digest into it and renders a ninety-character "tag".
    const parts = imageParts(
      "registry.fly.io/joaoh82-flotta-images:deployment-01M23SRM@sha256:bbaf60bf20a7",
    );
    expect(parts.repository).toBe("registry.fly.io/joaoh82-flotta-images");
    expect(parts.tag).toBe("deployment-01M23SRM");
    expect(parts.digest).toBe("sha256:bbaf60bf20a7");
  });

  it("handles a reference with no digest", () => {
    const parts = imageParts("registry.fly.io/app:tag");
    expect(parts).toEqual({
      repository: "registry.fly.io/app",
      tag: "tag",
      digest: null,
    });
  });

  it("handles a reference with no tag", () => {
    expect(imageParts("registry.fly.io/app").tag).toBeNull();
  });

  it("does not mistake a registry port for a tag", () => {
    const parts = imageParts("localhost:5000/flotta");
    expect(parts.repository).toBe("localhost:5000/flotta");
    expect(parts.tag).toBeNull();
  });
});

describe("the lines of the panel", () => {
  const full: Machine = {
    state: "started",
    machine_id: "815990c9246728",
    app: "joaoh82-flotta-eng-g",
    region: "ams",
    cpu_kind: "shared",
    cpus: 1,
    memory_mb: 1024,
    volume_id: "vol_vwnl2n2zqend82nv",
    volume_gb: 2,
    volume_path: "/data",
    private_ip: "fdaa:bd::2",
    created_at: "2026-09-08T21:00:43Z",
    updated_at: "2026-09-09T19:58:47Z",
    host_status: "ok",
  };

  const find = (fields: ReturnType<typeof fieldsOf>, label: string) =>
    fields.find((f) => f.label === label)?.value;

  it("reads the size in the units a person uses", () => {
    expect(find(fieldsOf(full), "Size")).toBe("1 shared vCPU, 1 GB RAM");
  });

  it("names the disk as the thing that is the agent", () => {
    expect(find(fieldsOf(full), "Memory disk")).toBe(
      "vol_vwnl2n2zqend82nv · 2 GB at /data",
    );
  });

  it("stays quiet about a healthy host and speaks up about an unhealthy one", () => {
    // "ok" on every panel is a line people stop reading, and the one time it
    // matters it would look like all the others.
    expect(find(fieldsOf(full), "Host")).toBeUndefined();
    expect(find(fieldsOf({ ...full, host_status: "unreachable" }), "Host")).toBe(
      "unreachable",
    );
  });

  it("skips what the substrate did not report, rather than printing undefined", () => {
    // The failure this exists for: an unguarded `{machine.region}` renders the
    // word "undefined" in a panel whose whole job is to be trusted.
    const fields = fieldsOf({ state: "gone" });
    expect(fields).toEqual([]);
    expect(JSON.stringify(fields)).not.toContain("undefined");
  });

  it("does not invent a size from a machine that reported none", () => {
    expect(find(fieldsOf({ state: "started", region: "ams" }), "Size")).toBeUndefined();
  });

  it("treats a reported zero as a value, not as a blank", () => {
    // Not a Fly shape today, but the rule this module keeps is "blank means
    // the substrate did not say". A truthiness check makes `0` indistinguishable
    // from silence, which is the same class of lie as printing "undefined".
    expect(find(fieldsOf({ ...full, cpus: 0, memory_mb: 0 }), "Size")).toBe(
      "0 shared vCPU, 0 MB RAM",
    );
  });
});

describe("whether an agent is on the fleet's image", () => {
  it("says so when it is", () => {
    expect(imageStanding({ image_current: true }).kind).toBe("current");
  });

  it("says so when it is not", () => {
    expect(imageStanding({ image_current: false }).kind).toBe("behind");
  });

  it("does not turn unknown into behind", () => {
    // `behind` puts an Upgrade button on screen, and an upgrade restarts the
    // agent. Offering one on no evidence is the whole failure mode.
    expect(imageStanding({ image_current: null, fleet_image: "r/x:t" }).kind).toBe("unknown");
    expect(imageStanding({}).kind).toBe("unknown");
  });

  it("names the fixable cause when the fleet has no image configured", () => {
    // The live trap: this is a deployment variable, and an unset or stale one
    // is the difference between an Upgrade button that works and one that does
    // nothing.
    expect(imageStanding({ image_current: null }).detail).toContain("FLOTTA_FLY_IMAGE");
  });
});

describe("whether the fleet's Hermes pin is behind", () => {
  it("reports a newer release and how to take it", () => {
    const said = hermesStanding({ pinned: "v2026.8.19", latest: "v2026.9.7", behind: true });
    expect(said.kind).toBe("behind");
    expect(said.detail).toContain("hermes-bump v2026.9.7");
  });

  it("never reports a failed check as up to date", () => {
    // The single most important assertion about this feature. "Up to date"
    // on an unreachable GitHub hides a real upgrade behind a reassuring word.
    const said = hermesStanding({
      pinned: "v2026.8.19",
      latest: null,
      behind: false,
      unavailable: "could not reach GitHub",
    });
    expect(said.kind).toBe("unknown");
    expect(said.detail).toContain("could not reach GitHub");
  });

  it("is up to date only when it has seen the newest", () => {
    expect(hermesStanding({ pinned: "v1", latest: "v1", behind: false }).kind).toBe("current");
  });
});

describe("the Hermes an agent runs", () => {
  it("reports the label when the image has one", () => {
    expect(hermesOf({ hermes_ref: "v2026.8.19" })).toEqual({
      value: "v2026.8.19",
      note: null,
    });
  });

  it("explains a blank instead of calling it unknown", () => {
    // Every agent in the fleet is this case until it is upgraded, so the
    // sentence has to say why and what fixes it — otherwise it reads as a
    // fault.
    for (const machine of [null, {}, { hermes_ref: "  " }, { hermes_ref: null }]) {
      const said = hermesOf(machine);
      expect(said.value).toBeNull();
      expect(said.note).toContain("Upgrading");
    }
  });
});
