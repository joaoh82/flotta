import { useCallback, useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { isFleetError, type FleetSetting } from "./types";

/**
 * How the fleet behaves, changed from the window.
 *
 * Distinct from `Settings`, which is about *this app* — where the control
 * plane is, and whether a token is saved. These live on the other side of the
 * network and are shared by everything that talks to it, so changing one here
 * changes it for the CLI and the dashboard too.
 *
 * **The form is not hardcoded.** Label, help text, kind and default all come
 * from the control plane's catalogue, so a setting added there appears here
 * without the app being rebuilt. Which is also why this renders whatever it is
 * given rather than knowing what a "sweep interval" is.
 *
 * Nothing here is a credential. The catalogue is an allowlist of
 * configuration — the signing key and the provider key are not in it and could
 * not be added without tripping a test that says so — which is what keeps the
 * rule that made this a desktop app intact: the webview never holds a secret.
 */

/** An error from the Rust side, as a sentence. */
function describe(err: unknown): string {
  if (isFleetError(err)) return err.detail;
  return typeof err === "string" ? err : JSON.stringify(err);
}

/** Where a value came from, said plainly enough to act on. */
function sourceNote(setting: FleetSetting): string | null {
  if (setting.source === "store") return null; // it is simply set; no note needed
  if (setting.source === "env")
    return "set where the control plane is deployed — saving here takes over";
  return `default: ${setting.default || "unset"}`;
}

export function FleetSettings() {
  const [settings, setSettings] = useState<FleetSetting[] | null>(null);
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  const load = useCallback(async () => {
    try {
      setSettings(await invoke<FleetSetting[]>("fleet_settings"));
      setError(null);
    } catch (err) {
      setError(describe(err));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function save(event: React.FormEvent) {
    event.preventDefault();
    if (saving) return;
    setSaving(true);
    setError(null);
    setSaved(false);
    try {
      // The whole edited map in one request. The control plane validates it as
      // one and refuses the lot if any value is bad, so a rejected save leaves
      // the fleet exactly as it was rather than half-applied.
      const next = await invoke<FleetSetting[]>("set_fleet_settings", { values: edits });
      setSettings(next);
      // Cleared rather than kept: what comes back is what was *stored*, and
      // holding the typed values on top of it would show a form that disagrees
      // with the fleet.
      setEdits({});
      setSaved(true);
    } catch (err) {
      setError(describe(err));
    } finally {
      setSaving(false);
    }
  }

  if (error && settings === null) {
    return (
      <div className="rounded border border-neutral-200 p-4">
        <p className="text-xs font-medium text-neutral-900">
          Could not read the fleet's settings
        </p>
        <p className="mt-1 text-xs text-neutral-600">{error}</p>
      </div>
    );
  }

  if (settings === null) {
    return <p className="text-xs text-neutral-500">Loading fleet settings…</p>;
  }

  const dirty = Object.keys(edits).length > 0;

  return (
    <form onSubmit={save} className="space-y-4">
      <div>
        <h3 className="text-sm font-semibold text-neutral-900">Fleet</h3>
        <p className="mt-1 text-xs text-neutral-500">
          How your agents behave. These live on the control plane, so they apply
          everywhere — this window, the CLI, the dashboard.
        </p>
      </div>

      {settings.map((setting) => {
        const note = sourceNote(setting);
        const current = edits[setting.key] ?? setting.value;
        return (
          <label key={setting.key} className="block">
            <span className="text-xs font-medium text-neutral-700">{setting.label}</span>
            <input
              value={current}
              onChange={(e) =>
                setEdits((prev) => ({ ...prev, [setting.key]: e.target.value }))
              }
              placeholder={setting.default || "unset"}
              className="mt-1 w-full rounded border border-neutral-300 px-2 py-1.5 font-mono text-xs focus:border-neutral-500 focus:outline-none"
            />
            <span className="mt-1 block text-xs text-neutral-500">
              {setting.help}
              {note && <span className="text-neutral-400"> · {note}</span>}
            </span>
          </label>
        );
      })}

      <p className="text-xs text-neutral-400">
        Empty a field to stop overriding it.
      </p>

      {error && (
        <p className="rounded bg-red-50 px-3 py-2 text-xs text-red-800">{error}</p>
      )}
      {saved && !dirty && (
        <p className="text-xs text-neutral-500">Saved. Takes effect on the next sweep.</p>
      )}

      <button
        type="submit"
        disabled={saving || !dirty}
        className="rounded bg-neutral-900 px-3 py-1.5 text-xs font-medium text-white disabled:opacity-40"
      >
        {saving ? "Saving…" : "Save fleet settings"}
      </button>
    </form>
  );
}
