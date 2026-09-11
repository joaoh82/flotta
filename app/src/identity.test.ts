import { describe, expect, it } from "vitest";
import { labelOf } from "./identity";

describe("what the sidebar calls an agent", () => {
  it("uses the display name when there is one, and keeps the address visible", () => {
    // Renaming must never look like moving: the address moves to the small
    // line, it does not vanish.
    expect(labelOf({ id: "b-1", name: "eng-f", display_name: "Reviewer — backend PRs" })).toEqual(
      { primary: "Reviewer — backend PRs", secondary: "eng-f" },
    );
  });

  it("falls back to the address, with the id underneath, when nothing is set", () => {
    for (const display_name of [undefined, null, "", "   "]) {
      expect(labelOf({ id: "b-1", name: "eng-f", display_name })).toEqual({
        primary: "eng-f",
        secondary: "b-1",
      });
    }
  });
});
