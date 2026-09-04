import { useCallback, useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { Conversation } from "./Conversation";
import { Settings } from "./Settings";
import { StatusBadge } from "./StatusBadge";
import { isFleetError, type BoxRow, type FleetError, type SettingsView } from "./types";

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
          Create one with <code>flotta create eng-a</code> — from M8.3, there is
          a button here.
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
        ) : boxes.length === 0 ? (
          <Empty
            error={error}
            loading={loading}
            onSettings={() => setShowSettings(true)}
          />
        ) : (
          <div className="flex h-full">
            {/* Agents on the left, the conversation beside them. The agent is
                the primary object, not a row in a table of machines — which is
                why selecting one opens a conversation rather than a detail
                page. */}
            <ul className="w-64 shrink-0 divide-y divide-neutral-100 overflow-auto border-r border-neutral-200">
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
                  </button>
                </li>
              ))}
            </ul>

            <div className="min-w-0 flex-1">
              {selected ? (
                <Conversation key={selected} boxName={selected} />
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
