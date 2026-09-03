import { useCallback, useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
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

function Empty({ error, onSettings }: { error: FleetError | null; onSettings: () => void }) {
  // Three failures, three fixes, three messages. An error string plus an empty
  // list would render all of them as "you have no agents", which is the one
  // reading that is never actionable.
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
  };

  return (
    <div className="p-8">
      <p className="text-sm font-medium text-neutral-900">{TITLES[error.kind]}</p>
      <p className="mt-1 max-w-prose text-xs text-neutral-600">{error.detail}</p>
      {(error.kind === "not_configured" || error.kind === "rejected") && (
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
    const view = await invoke<SettingsView>("load_settings");
    setSettings(view);
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
            onClick={() => setShowSettings((open) => !open)}
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
          <Empty error={error} onSettings={() => setShowSettings(true)} />
        ) : (
          <ul className="divide-y divide-neutral-100">
            {boxes.map((box) => (
              <li
                key={box.id}
                className="flex items-center justify-between px-4 py-3 hover:bg-neutral-50"
              >
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="truncate text-sm font-medium">{box.name}</span>
                    <StatusBadge status={box.status} />
                  </div>
                  <p className="mt-0.5 truncate font-mono text-[11px] text-neutral-500">
                    {box.id}
                    {box.created_at ? ` · ${box.created_at.slice(0, 10)}` : ""}
                  </p>
                </div>
                {/* A stopped agent is addressable — the door wakes it. Saying
                    so here stops `stopped` reading as "unavailable". */}
                <span className="ml-4 shrink-0 text-xs text-neutral-400">
                  {box.status === "stopped" ? "wakes when addressed" : ""}
                </span>
              </li>
            ))}
          </ul>
        )}
      </main>
    </div>
  );
}
