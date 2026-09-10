import { useCallback, useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { POLL_MS } from "./poll";
import { isFleetError, type Build, type HermesVersions } from "./types";

/**
 * "There is a new Hermes. Update the agents?" — and then it does it.
 *
 * This is the control the rest of the Hermes work exists to make possible.
 * Before it, the window could tell you a newer Hermes was out and then send
 * you to a terminal to bump a pin, build an image and roll each agent by hand.
 * Everything it needs was already here — the version check, the image label,
 * the backgrounded upgrade — and none of it added up to a thing you could
 * press.
 *
 * It shows only when there is something to do, and says what it is doing while
 * it does it: a build takes minutes, and a button that goes quiet for minutes
 * is indistinguishable from one that did nothing.
 */
export function HermesUpdate({
  versions,
  onChanged,
}: {
  versions: HermesVersions | null;
  /** Re-read the fleet — agents change image as the roll proceeds. */
  onChanged: () => void;
}) {
  const [build, setBuild] = useState<Build | null>(null);
  const [asking, setAsking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const all = await invoke<Build[]>("hermes_builds");
      setBuild(all[0] ?? null);
    } catch {
      // A control plane too old to have this endpoint is not an error worth
      // showing here — the banner simply does not appear.
      setBuild(null);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const building = build?.status === "building";

  // Poll only while something is happening. A build is minutes, and the roll
  // that follows changes agents one at a time — both need the fleet re-read.
  useEffect(() => {
    if (!building) return;
    const timer = setInterval(() => {
      void load();
      onChanged();
    }, POLL_MS);
    return () => clearInterval(timer);
  }, [building, load, onChanged]);

  const start = async () => {
    setError(null);
    setAsking(false);
    try {
      await invoke("update_hermes", { hermesRef: versions?.latest ?? "" });
      await load();
    } catch (e) {
      setError(isFleetError(e) ? e.detail : String(e));
    }
  };

  if (building) {
    return (
      <Bar tone="busy">
        <span>
          Building Hermes {build?.hermes_ref} — a few minutes. Agents are upgraded one at a
          time once it lands.
        </span>
      </Bar>
    );
  }

  // A failed build is worth keeping on screen until something replaces it:
  // nothing changed for any agent, and the reason is the only way to know why.
  if (build?.status === "failed") {
    return (
      <Bar tone="bad">
        <span>
          Building Hermes {build.hermes_ref} failed — no agent was changed. {build.error}
        </span>
        <button onClick={() => void start()} className={BUTTON}>
          Try again
        </button>
      </Bar>
    );
  }

  if (error) {
    return (
      <Bar tone="bad">
        <span>{error}</span>
      </Bar>
    );
  }

  // Nothing to offer: up to date, or the check could not be made. An
  // unreachable GitHub must not become an invitation to rebuild for nothing.
  if (!versions?.behind || !versions.latest) return null;

  if (!asking) {
    return (
      <Bar tone="offer">
        <span>
          Hermes {versions.latest} is available. Your agents run {versions.pinned}.
        </span>
        <button onClick={() => setAsking(true)} className={BUTTON}>
          Update agents
        </button>
      </Bar>
    );
  }

  return (
    <Bar tone="offer">
      <span>
        This builds a new box image on Hermes {versions.latest}, then moves every agent onto
        it. Disks are kept — memories, skills and history all survive. Each agent{" "}
        <strong>restarts</strong>, and if one fails to come up the rest are left alone.
      </span>
      <button onClick={() => void start()} className={BUTTON}>
        Build and update
      </button>
      <button onClick={() => setAsking(false)} className="rounded px-2 py-1 text-xs">
        Cancel
      </button>
    </Bar>
  );
}

const BUTTON =
  "shrink-0 rounded border border-current/30 bg-white/60 px-2.5 py-1 text-xs font-medium hover:bg-white";

function Bar({ tone, children }: { tone: "offer" | "busy" | "bad"; children: React.ReactNode }) {
  const tones = {
    offer: "border-amber-200 bg-amber-50 text-amber-900",
    busy: "border-neutral-200 bg-neutral-50 text-neutral-700",
    bad: "border-red-200 bg-red-50 text-red-900",
  };
  return (
    <div
      className={`flex items-center gap-3 border-b px-4 py-2 text-[11px] ${tones[tone]}`}
    >
      {children}
    </div>
  );
}
