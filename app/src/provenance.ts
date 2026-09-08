import type { FleetSetting } from "./types";

/**
 * Where a fleet setting's value came from, in words.
 *
 * Pure and in its own module for the reason FLOTTA-31 established: this is a
 * decision about what a person is told, and those are where the app's bugs
 * have been. It is also the function that was wrong — see below.
 */

/**
 * Where this value came from, in words.
 *
 * **`store` used to return null**, on the reasoning that a field somebody set
 * needs no explanation. That was wrong in a way only using it showed: the
 * three sources were then distinguished by a grey line *appearing* for two of
 * them and *disappearing* for the third, and absence is not a state anyone can
 * read. "Did I set this to 2, or is it 2 anyway?" had no answer in the window
 * — you had to query the API — which is exactly the question `source` was
 * added to answer.
 *
 * So all three say something now. Never null.
 */
export function sourceNote(setting: FleetSetting): string {
  if (setting.source === "store") return "set here";
  if (setting.source === "env") {
    return "set where the control plane is deployed — saving here takes over";
  }
  return `default: ${setting.default || "unset"}`;
}
