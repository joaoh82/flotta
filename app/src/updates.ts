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
  // `rolling` counts too, and that is the point of it existing. `done` used
  // to be set the moment the image was built, so the button came back while
  // agents were still moving — and pressing it would have been refused by a
  // control plane that knows the operation is not over.
  if (build?.status === "building" || build?.status === "rolling") {
    return {
      kind: "building",
      ref: build.hermes_ref,
      detail:
        build.status === "rolling"
          ? `Hermes ${build.hermes_ref} is built. Upgrading agents one at a time.`
          : `Building Hermes ${build.hermes_ref}. Agents are upgraded one at a time once it lands.`,
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

  // `fleet_ref`, not `pinned`. The pin is what the next build *would* use;
  // what the agents run is what the last build actually used, and after an
  // update started here the two never converge again.
  return {
    kind: "offer",
    ref: versions.latest,
    detail:
      `Hermes ${versions.latest} is available. Your agents run ` +
      `${versions.fleet_ref ?? versions.pinned}.`,
  };
}
