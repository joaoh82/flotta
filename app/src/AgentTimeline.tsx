import { useCallback, useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { POLL_MS } from "./poll";
import { endingOf, missingOf, reasonOf, warningsIn } from "./timeline";
import { isAddressable, isFleetError, type BoxEvent, type BoxRow } from "./types";

/**
 * What is happening to an agent you cannot talk to yet, or can no longer.
 *
 * The pane beside the sidebar shows a conversation for an agent that is
 * addressable. This is what it shows for one that is not — which, until
 * FLOTTA-29, was a conversation too: selecting a box the moment it was created
 * opened a socket to a machine that did not exist, and the app reported a
 * connection error for a creation that was succeeding.
 *
 * Three endings share this component because they are one story. A box being
 * built has nothing to say yet and a minute or two in which to say it; a box
 * whose provision failed has exactly one thing to say, and it is in the
 * timeline; a box that lived and was destroyed has a different thing to say,
 * and telling the two apart is the difference between "was not created" and
 * "is gone". Blocking selection instead would have been fewer lines, but a row
 * you just made that does nothing when clicked reads as broken, and the
 * failure would still have had nowhere to appear.
 *
 * The room is not only for this. §M7's delegation traffic — one agent handing
 * work to another — is the same kind of thing: events against a box, with no
 * other surface in the app to land on.
 */

/** Time of day, which is all that is useful over a couple of minutes. */
function timeOf(ts: string): string {
  const when = new Date(ts);
  return Number.isNaN(when.getTime())
    ? ts
    : when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function AgentTimeline({
  boxId,
  boxName,
  /** The status from the fleet list, or null when the box has left it. */
  status,
  /**
   * Ask for the fleet list to be re-read.
   *
   * Called when this box turns out to be addressable after all — it left the
   * list, `get_agent` says `running`, and only the *list* can promote the pane
   * to a conversation. Without it the pane would sit here until somebody
   * pressed Refresh.
   *
   * Must be a stable reference; it is an effect dependency.
   */
  onRefresh,
}: {
  boxId: string;
  boxName: string;
  status: string | null;
  onRefresh: () => void;
}) {
  const [events, setEvents] = useState<BoxEvent[] | null>(null);
  const [fetched, setFetched] = useState<BoxRow | null>(null);
  const [error, setError] = useState<string | null>(null);

  // The list is the truth while the box is in it. Once it is not — which is
  // what a failed provision looks like, because `GET /api/boxes` filters
  // terminal boxes — the box has to be asked for by id.
  const effective = status ?? fetched?.status ?? null;
  const ending = endingOf(events ?? [], effective);
  const settled = ending.kind !== "unfinished";

  const load = useCallback(async () => {
    try {
      const timeline = await invoke<BoxEvent[]>("agent_timeline", { id: boxId });
      setEvents(timeline);
      if (status === null) setFetched(await invoke<BoxRow>("get_agent", { id: boxId }));
      setError(null);
    } catch (err) {
      setError(isFleetError(err) ? err.detail : String(err));
    }
  }, [boxId, status]);

  useEffect(() => {
    void load();
    // Nothing further will happen to a torn-down box, so stop asking. Every
    // other state here is one that changes on its own.
    if (settled) return;
    const timer = setInterval(() => void load(), POLL_MS);
    return () => clearInterval(timer);
  }, [load, settled]);

  // The list said this agent was gone and the box itself says otherwise — a
  // lagging list, or a status this pane does not treat as terminal. Only the
  // list can promote the pane to a conversation, so ask for it to be re-read.
  useEffect(() => {
    if (status === null && fetched && isAddressable(fetched.status)) onRefresh();
  }, [status, fetched, onRefresh]);

  const failure = ending.kind === "unfinished" ? null : ending.reason;
  const warnings = warningsIn(events ?? []);

  return (
    <div className="flex h-full flex-col overflow-auto p-5">
      {ending.kind === "gone" ? (
        <>
          <h2 className="text-sm font-medium text-neutral-900">{boxName} is gone</h2>
          <p className="mt-1 max-w-prose text-xs text-neutral-600">
            {failure ??
              "It was destroyed. Its machine and the volume its memory was on " +
                "went with it."}
          </p>
        </>
      ) : ending.kind === "never-created" ? (
        <>
          <h2 className="text-sm font-medium text-neutral-900">
            {boxName} was not created
          </h2>
          <p className="mt-1 max-w-prose text-xs text-neutral-600">
            {failure ??
              "Its provision did not finish. The timeline below is everything " +
                "the control plane recorded about it."}
          </p>
          {/* Deliberately not "try again with the same name". Releasing a
              name happens in `teardown_box`, and a provision that fails does
              not go through it — the row is closed where it broke. So the
              name is still taken by a box that no longer exists, and creating
              it again answers 409. Saying so beats sending someone into that
              wall. Filed as FLOTTA-30. */}
          <p className="mt-2 max-w-prose text-xs text-neutral-500">
            Its machine is torn down with it, so nothing is left running. The
            name <span className="font-mono">{boxName}</span> is still held by
            this record, though — create the next one under a different name.
          </p>
        </>
      ) : (
        <>
          <h2 className="text-sm font-medium text-neutral-900">Building {boxName}</h2>
          <p className="mt-1 max-w-prose text-xs text-neutral-600">
            An app, a volume, a machine and a boot — a minute or two. Its
            identity and the fleet's secrets go on before it starts, so it
            arrives able to work rather than needing a second command.
          </p>
          <p className="mt-2 max-w-prose text-xs text-neutral-500">
            This opens into a conversation on its own when the box is up;
            nothing here needs clicking. If it never finishes, the control
            plane closes it out after twenty minutes and says why.
          </p>
        </>
      )}

      {warnings.length > 0 && (
        <div className="mt-4 rounded border border-amber-200 bg-amber-50 px-3 py-2">
          <p className="text-[11px] font-medium text-amber-900">
            {settled
              ? `Recorded while ${boxName} was being built`
              : `${boxName} is being created, but something is missing`}
          </p>
          {warnings.map((event) => (
            <div key={event.id} className="mt-1">
              <p className="text-[11px] text-amber-800">{reasonOf(event) ?? event.type}</p>
              {missingOf(event).length > 0 && (
                <p className="mt-0.5 font-mono text-[11px] text-amber-900">
                  {missingOf(event).join(", ")}
                </p>
              )}
            </div>
          ))}
        </div>
      )}

      {error && (
        <p className="mt-4 rounded bg-red-50 px-3 py-2 text-[11px] text-red-800">
          Could not read the timeline: {error}
        </p>
      )}

      <div className="mt-5">
        <p className="font-mono text-[11px] text-neutral-400">timeline</p>
        {events === null ? (
          <p className="mt-2 text-xs text-neutral-500">Loading…</p>
        ) : events.length === 0 ? (
          <p className="mt-2 text-xs text-neutral-500">Nothing recorded yet.</p>
        ) : (
          <ul className="mt-2 space-y-1.5">
            {events.map((event) => (
              <li key={event.id} className="flex gap-3 text-xs">
                <span className="shrink-0 font-mono text-[11px] text-neutral-400">
                  {timeOf(event.ts)}
                </span>
                {/* The tier, when it is not the agent itself. A task `failed`
                    and a box `addressed` are otherwise identical rows about
                    very different things. */}
                {event.entity_kind !== "box" && (
                  <span className="shrink-0 font-mono text-[11px] text-neutral-400">
                    {event.entity_kind}
                  </span>
                )}
                <span className="shrink-0 font-mono text-[11px] text-neutral-700">
                  {event.type}
                </span>
                <span className="min-w-0 text-[11px] text-neutral-500">
                  {reasonOf(event) ?? ""}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
