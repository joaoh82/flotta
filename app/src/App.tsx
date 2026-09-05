import { useCallback, useEffect, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { AgentTimeline } from "./AgentTimeline";
import { Conversation } from "./Conversation";
import { DestroyAgent } from "./DestroyAgent";
import { NewAgent } from "./NewAgent";
import { Settings } from "./Settings";
import { StatusBadge } from "./StatusBadge";
import {
  isAddressable,
  isFleetError,
  type BoxRow,
  type FleetError,
  type SettingsView,
} from "./types";

/**
 * How often to re-read the fleet while something in it is still being built.
 *
 * Seconds, not sub-second: provisioning is minutes of work and the control
 * plane is not free.
 */
const POLL_MS = 5000;

/**
 * The Flotta app.
 *
 * M8.1 is the list. The conversation (M8.2) hangs off selecting an agent here,
 * which is why an agent row is already the primary object rather than a row in
 * a table of machines.
 */

/**
 * The host, or the raw string, or a placeholder — but never a thrown error.
 *
 * `new URL(x).host` throws on anything without a scheme, and this is rendered
 * in the header, above everything. One bad value in settings.json took the
 * whole window down on every launch, with no way back to the settings form
 * that would have fixed it. A chrome element must not be able to do that.
 */
function hostOf(url: string | undefined): string {
  if (!url) return "not configured";
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}

function Empty({
  error,
  loading,
  onSettings,
}: {
  error: FleetError | null;
  loading: boolean;
  onSettings: () => void;
}) {
  // Three failures, three fixes, three messages. An error string plus an empty
  // list would render all of them as "you have no agents", which is the one
  // reading that is never actionable.
  if (loading) {
    return <div className="p-8 text-center text-sm text-neutral-500">Loading…</div>;
  }
  if (!error) {
    return (
      <div className="p-8 text-center">
        <p className="text-sm text-neutral-700">No agents yet.</p>
        <p className="mt-1 text-xs text-neutral-500">
          Name one below and it will be built for you — a machine, a volume and
          a memory of its own.
        </p>
      </div>
    );
  }

  const TITLES: Record<FleetError["kind"], string> = {
    not_configured: "Not set up yet",
    unreachable: "Cannot reach the control plane",
    rejected: "That token was refused",
    unexpected: "Unexpected answer from the control plane",
    // Deliberately not blamed on the control plane, which has not been
    // contacted at this point. Saying otherwise sent the first person who saw
    // it looking at a Railway deployment that was fine.
    keychain: "The keychain would not release the token",
  };

  return (
    <div className="p-8">
      <p className="text-sm font-medium text-neutral-900">{TITLES[error.kind]}</p>
      <p className="mt-1 max-w-prose text-xs text-neutral-600">{error.detail}</p>
      {(error.kind === "not_configured" ||
        error.kind === "rejected" ||
        error.kind === "keychain") && (
        <button
          onClick={onSettings}
          className="mt-3 rounded bg-neutral-900 px-3 py-1.5 text-xs font-medium text-white"
        >
          Open settings
        </button>
      )}
    </div>
  );
}

export default function App() {
  const [settings, setSettings] = useState<SettingsView | null>(null);
  const [showSettings, setShowSettings] = useState(false);
  const [boxes, setBoxes] = useState<BoxRow[]>([]);
  const [error, setError] = useState<FleetError | null>(null);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<string | null>(null);
  const [destroying, setDestroying] = useState<BoxRow | null>(null);
  /**
   * The agent we just made, until it is either talkable-to or gone.
   *
   * Kept as well as reading `provisioning` off the list because a failed
   * provision *leaves* the list — `GET /api/boxes` hides terminal boxes — so
   * "is anything still being built" cannot be answered by the list alone at
   * exactly the moment it matters.
   */
  const [watching, setWatching] = useState<string | null>(null);

  /**
   * The last row seen for each name.
   *
   * A box that leaves the list has to still be explainable: without this, an
   * agent whose provision failed would simply vanish from the window mid-look,
   * which is the silent version of the bug this is all fixing. Written after
   * render, so the render where a box disappears still sees the row it had.
   */
  const lastSeen = useRef(new Map<string, BoxRow>());

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      setBoxes(await invoke<BoxRow[]>("list_boxes"));
      setError(null);
    } catch (err) {
      // A failed refresh must not leave a stale list looking current.
      setBoxes([]);
      setError(
        isFleetError(err)
          ? err
          : { kind: "unexpected", detail: String(err) },
      );
    } finally {
      setLoading(false);
    }
  }, []);

  /**
   * The same read, quietly.
   *
   * Two differences from `refresh`, both deliberate. It does not touch
   * `loading`, so a background tick does not flicker the header or disable the
   * button. And it swallows failures rather than emptying the list: a manual
   * refresh means "tell me the truth now" and must report a failure, but a
   * timer that wiped the fleet because one request timed out would turn a blip
   * into "you have no agents" while you were reading a conversation.
   */
  const poll = useCallback(async () => {
    try {
      const rows = await invoke<BoxRow[]>("list_boxes");
      setBoxes(rows);
      setError(null);
    } catch {
      // Left as it was, on purpose. See above.
    }
  }, []);

  const reload = useCallback(async () => {
    let view: SettingsView;
    try {
      view = await invoke<SettingsView>("load_settings");
    } catch (err) {
      // Unhandled, this left `loading` true and the list empty forever, which
      // renders as "No agents yet" — the most confidently wrong thing the app
      // can say. A screen that cannot load its own settings must say so.
      setLoading(false);
      setError(
        isFleetError(err) ? err : { kind: "unexpected", detail: String(err) },
      );
      return;
    }
    setSettings(view);

    // A keychain that cannot be read is not a keychain with nothing in it.
    // Sending this to the settings form would say "None saved yet" about a
    // token that is sitting right there.
    if (view.token_error) {
      setLoading(false);
      setError({
        kind: "keychain",
        detail:
          `${view.token_error}. On macOS a rebuilt binary is a different ` +
          "binary, so an item saved by an earlier build can be refused to this " +
          "one. Saving the token again in Settings stores it fresh for this " +
          "build and fixes it.",
      });
      return;
    }

    // Straight to settings on a first run: an empty list with a "configure me"
    // message is a worse first screen than the form itself.
    if (!view.control_url || !view.has_token) {
      setShowSettings(true);
      setLoading(false);
      return;
    }
    await refresh();
  }, [refresh]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const building = boxes.some((box) => box.status === "provisioning");

  // Poll while anything is unfinished, and only then. A fleet that is entirely
  // running or asleep changes when somebody changes it, and the Refresh button
  // is how you ask.
  useEffect(() => {
    if (showSettings || (!building && !watching)) return;
    const timer = setInterval(() => void poll(), POLL_MS);
    return () => clearInterval(timer);
  }, [building, watching, showSettings, poll]);

  // Stop watching once the answer is in. Both endings settle it: the agent is
  // addressable, or it is no longer in the fleet and the timeline says why.
  useEffect(() => {
    if (!watching) return;
    const row = boxes.find((box) => box.name === watching);
    if (!row || row.status !== "provisioning") setWatching(null);
  }, [boxes, watching]);

  const selectedBox = boxes.find((b) => b.name === selected) ?? null;
  // A selected agent that has left the list. Not "nothing selected" — that
  // renders as the neutral "pick an agent", which is how a failed creation
  // used to disappear without ever saying so.
  const selectedGone = selected && !selectedBox ? (lastSeen.current.get(selected) ?? null) : null;
  /** The agent the right-hand pane is about, present in the fleet or not. */
  const shown = selectedBox ?? selectedGone;

  useEffect(() => {
    for (const box of boxes) lastSeen.current.set(box.name, box);
  }, [boxes]);

  /**
   * A newly created agent.
   *
   * It is selected, but it is `provisioning`, so the pane shows what is
   * happening to it rather than a conversation with a machine that does not
   * exist yet. The row from the `202` goes straight into the list: it is a
   * real row, and waiting a poll to display what we just made is a second of
   * looking like nothing happened.
   */
  const created = useCallback(
    (box: BoxRow) => {
      setBoxes((rows) => (rows.some((row) => row.id === box.id) ? rows : [...rows, box]));
      setError(null);
      setWatching(box.name);
      setSelected(box.name);
    },
    [],
  );

  return (
    <div className="flex h-full flex-col bg-white text-neutral-900">
      <header className="flex items-center justify-between border-b border-neutral-200 px-4 py-2.5">
        <div className="flex items-baseline gap-2">
          <h1 className="text-sm font-semibold tracking-tight">Flotta</h1>
          <span className="text-xs text-neutral-500">
            {hostOf(settings?.control_url)}
          </span>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => void refresh()}
            disabled={loading || showSettings}
            className="rounded px-2 py-1 text-xs text-neutral-600 hover:bg-neutral-100 disabled:opacity-40"
          >
            {loading ? "Loading…" : "Refresh"}
          </button>
          <button
            onClick={() => {
              // Closing re-reads rather than revealing a stale list: settings
              // are exactly the thing that changes what the list should show.
              if (showSettings) void reload();
              setShowSettings((open) => !open);
            }}
            className="rounded px-2 py-1 text-xs text-neutral-600 hover:bg-neutral-100"
          >
            {showSettings ? "Close" : "Settings"}
          </button>
        </div>
      </header>

      <main className="flex-1 overflow-auto">
        {showSettings && settings ? (
          <Settings
            initial={settings}
            onSaved={() => {
              setShowSettings(false);
              void reload();
            }}
          />
        ) : boxes.length === 0 && !selectedGone ? (
          <div className="flex flex-col items-center">
            <Empty
              error={error}
              loading={loading}
              onSettings={() => setShowSettings(true)}
            />
            {/* The first agent is created from the same form as the tenth. An
                empty fleet used to point at the CLI instead, which is the one
                screen where a person has no agent to fall back on. */}
            {!loading && !error && (
              <div className="w-72">
                <NewAgent onCreated={created} />
              </div>
            )}
          </div>
        ) : (
          <div className="flex h-full">
            {/* Agents on the left, the conversation beside them. The agent is
                the primary object, not a row in a table of machines — which is
                why selecting one opens a conversation rather than a detail
                page. */}
            <div className="flex w-64 shrink-0 flex-col border-r border-neutral-200">
              <ul className="min-h-0 flex-1 divide-y divide-neutral-100 overflow-auto">
              {boxes.map((box) => (
                <li key={box.id}>
                  <button
                    onClick={() => setSelected(box.name)}
                    className={`w-full px-4 py-3 text-left hover:bg-neutral-50 ${
                      selected === box.name ? "bg-neutral-100" : ""
                    }`}
                  >
                    <div className="flex items-center gap-2">
                      <span className="truncate text-sm font-medium">{box.name}</span>
                      <StatusBadge status={box.status} />
                    </div>
                    <p className="mt-0.5 truncate font-mono text-[11px] text-neutral-500">
                      {box.id}
                    </p>
                    {/* A stopped agent is addressable — the door wakes it.
                        Saying so stops `stopped` reading as "unavailable". */}
                    {box.status === "stopped" && (
                      <p className="mt-0.5 text-[11px] text-neutral-400">
                        wakes when addressed
                      </p>
                    )}
                    {/* Said plainly, because the row looks talkable-to and is
                        not: there is no machine behind it yet. */}
                    {box.status === "provisioning" && (
                      <p className="mt-0.5 text-[11px] text-neutral-400">
                        building — a minute or two
                      </p>
                    )}
                  </button>
                </li>
              ))}
              </ul>
              <NewAgent onCreated={created} />
            </div>

            <div className="flex min-w-0 flex-1 flex-col">
              {destroying ? (
                <DestroyAgent
                  box={destroying}
                  onCancel={() => setDestroying(null)}
                  onDestroyed={() => {
                    if (selected === destroying.name) setSelected(null);
                    setDestroying(null);
                    void refresh();
                  }}
                />
              ) : selectedBox && isAddressable(selectedBox.status) ? (
                <>
                  <div className="flex items-center justify-between border-b border-neutral-200 px-4 py-2">
                    <span className="font-mono text-[11px] text-neutral-500">
                      {selectedBox.name}.flotta.dev
                    </span>
                    <button
                      onClick={() => setDestroying(selectedBox)}
                      className="rounded px-2 py-1 text-xs text-neutral-500 hover:bg-red-50 hover:text-red-700"
                    >
                      Destroy
                    </button>
                  </div>
                  {/* Keyed by name so switching agents remounts: a transcript
                      belongs to one agent, and the box is asked for it again
                      rather than the UI carrying it across. */}
                  <div className="min-h-0 flex-1">
                    <Conversation key={selectedBox.name} boxName={selectedBox.name} />
                  </div>
                </>
              ) : shown ? (
                <>
                  <div className="flex items-center justify-between border-b border-neutral-200 px-4 py-2">
                    <span className="font-mono text-[11px] text-neutral-500">
                      {shown.name}
                    </span>
                    {/* No Destroy while a box is being built. Cancelling would
                        race the thread that is provisioning it, which is how
                        you get a machine nothing has a row for — the exact
                        failure FLOTTA-27 handed to the reconcile loop. It
                        closes a stranded provision itself. */}
                    <button
                      onClick={() => setSelected(null)}
                      className="rounded px-2 py-1 text-xs text-neutral-500 hover:bg-neutral-100"
                    >
                      Dismiss
                    </button>
                  </div>
                  <div className="min-h-0 flex-1">
                    <AgentTimeline
                      key={shown.id}
                      boxId={shown.id}
                      boxName={shown.name}
                      status={selectedBox?.status ?? null}
                    />
                  </div>
                </>
              ) : (
                <p className="p-8 text-sm text-neutral-500">
                  Pick an agent to talk to.
                </p>
              )}
            </div>
          </div>
        )}
      </main>
    </div>
  );
}
