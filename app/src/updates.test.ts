import { describe, expect, it } from "vitest";
import { offerOf } from "./updates";

describe("what the fleet banner offers", () => {
  it("offers an update when a newer Hermes exists", () => {
    const said = offerOf({ pinned: "v1", latest: "v2", behind: true }, null);
    expect(said).toMatchObject({ kind: "offer", ref: "v2" });
  });

  it("offers nothing when up to date", () => {
    expect(offerOf({ pinned: "v2", latest: "v2", behind: false }, null).kind).toBe("none");
  });

  it("offers nothing when the check failed", () => {
    // The assertion that keeps being worth making: an unreachable GitHub must
    // not become an invitation to rebuild for nothing.
    expect(
      offerOf({ pinned: "v1", latest: null, behind: false, unavailable: "no network" }, null).kind,
    ).toBe("none");
  });

  it("offers nothing before the versions have loaded", () => {
    expect(offerOf(null, null).kind).toBe("none");
  });

  it("reports a build in progress over any offer", () => {
    // Even if a newer Hermes appears mid-build. Two builds race for the same
    // release history, and the control plane refuses the second — the window
    // should not put a button in front of that refusal.
    const said = offerOf({ pinned: "v1", latest: "v3", behind: true }, {
      id: "b1",
      hermes_ref: "v2",
      status: "building",
      started_at: "now",
    });
    expect(said).toMatchObject({ kind: "building", ref: "v2" });
  });

  it("keeps a failed build on screen even when nothing is behind", () => {
    // Nothing changed for any agent, and the reason is the only way to know
    // why — losing it because the pin happens to match upstream would hide a
    // failure behind a green state.
    const said = offerOf({ pinned: "v2", latest: "v2", behind: false }, {
      id: "b1",
      hermes_ref: "v2",
      status: "failed",
      error: "no space left",
      started_at: "now",
    });
    expect(said.kind).toBe("failed");
    expect(said.kind === "failed" && said.detail).toContain("no space left");
  });

  it("says nothing about a build that finished cleanly", () => {
    expect(
      offerOf({ pinned: "v2", latest: "v2", behind: false }, {
        id: "b1",
        hermes_ref: "v2",
        status: "done",
        image: "registry/x:t",
        started_at: "now",
      }).kind,
    ).toBe("none");
  });
});
