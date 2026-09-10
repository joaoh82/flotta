/**
 * Named `updates`, not `hermesUpdate`.
 *
 * macOS filesystems are case-insensitive, so `hermesUpdate.ts` and
 * `HermesUpdate.tsx` are one path to the compiler and `tsc` refuses the pair.
 * `provenance.ts` exists for the same reason — it could not be `settings.ts`
 * beside `Settings.tsx`. Worth knowing before naming the next one.
 *
 * What the fleet banner should say — decided here, rendered in the component.
 *
 * Four states with four different meanings, and the differences are all
 * decisions rather than rendering: an unreachable GitHub must not read as an
 * invitation to rebuild, a build in progress outranks any offer, and a failed
 * build has to stay visible even once the fleet looks up to date — otherwise
 * a failure hides behind a green state.
 */

import type { Build, HermesVersions } from "./types";

export type Offer =
  | { kind: "none" }
  | { kind: "offer"; ref: string; detail: string }
  | { kind: "building"; ref: string; detail: string }
  | { kind: "failed"; ref: string; detail: string };

export function offerOf(versions: HermesVersions | null, build: Build | null): Offer {
  // A build in progress beats everything, including a newer Hermes appearing
  // mid-build: the control plane refuses a second build, and offering a button
  // that will be refused is worse than saying what is happening.
  if (build?.status === "building") {
    return {
      kind: "building",
      ref: build.hermes_ref,
      detail: `Building Hermes ${build.hermes_ref}. Agents are upgraded one at a time once it lands.`,
    };
  }

  if (build?.status === "failed") {
    return {
      kind: "failed",
      ref: build.hermes_ref,
      detail:
        `Building Hermes ${build.hermes_ref} failed — no agent was changed. ` +
        (build.error ?? "No reason was recorded."),
    };
  }

  // `behind` is only ever true on evidence; `latest` is null when GitHub could
  // not be reached. Both have to hold before a button appears.
  if (!versions?.behind || !versions.latest) return { kind: "none" };

  return {
    kind: "offer",
    ref: versions.latest,
    detail: `Hermes ${versions.latest} is available. Your agents run ${versions.pinned}.`,
  };
}
