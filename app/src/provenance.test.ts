import { describe, expect, it } from "vitest";
import { sourceNote } from "./provenance";
import type { FleetSetting } from "./types";

function setting(partial: Partial<FleetSetting>): FleetSetting {
  return {
    key: "FLOTTA_IDLE_AFTER_S",
    label: "Sleep agents after",
    help: "Seconds of quiet before an agent suspends itself.",
    kind: "seconds",
    default: "1800",
    value: "1800",
    source: "default",
    ...partial,
  };
}

describe("where a setting's value came from", () => {
  // The bug this file exists for. `store` returned null, so a value somebody
  // had deliberately set was shown by a grey note *disappearing* — and absence
  // is not a state anyone can read. "Did I set this, or is it just what it is?"
  // had no answer in the window, which is the exact question `source` was added
  // to the API to answer.
  it("says something for every source, including one that is set", () => {
    for (const source of ["store", "env", "default"]) {
      const note = sourceNote(setting({ source }));
      expect(note, `${source} said nothing`).toBeTruthy();
    }
  });

  it("distinguishes a value somebody chose from one that merely is", () => {
    const chosen = sourceNote(setting({ source: "store", value: "300" }));
    const inherited = sourceNote(setting({ source: "default", value: "1800" }));
    expect(chosen).not.toEqual(inherited);
    expect(chosen).toContain("set here");
  });

  it("points at the deployment when a variable is still deciding", () => {
    // The most useful of the three: "why is my fleet not using the number I
    // set" is almost always this, and the answer is not in the app at all.
    expect(sourceNote(setting({ source: "env" }))).toContain("control plane is deployed");
  });

  it("names the default rather than showing a blank", () => {
    expect(sourceNote(setting({ source: "default", default: "1800" }))).toContain("1800");
    // A setting whose default is nothing still has to say so — an empty
    // "default: " reads as a rendering bug.
    expect(sourceNote(setting({ source: "default", default: "" }))).toContain("unset");
  });
});
