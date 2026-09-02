import { useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import type { SettingsView } from "./types";

/**
 * Where the fleet is, and the token to reach it.
 *
 * The token field is write-only. `load_settings` reports whether one is stored
 * and never returns it — so this screen can say "a token is saved" but cannot
 * show it, and neither can anything else that gets script execution in the
 * webview. Leaving the field blank keeps the stored token; that is why editing
 * a URL does not mean pasting a token again.
 */
export function Settings({
  initial,
  onSaved,
}: {
  initial: SettingsView;
  onSaved: () => void;
}) {
  const [controlUrl, setControlUrl] = useState(initial.control_url);
  const [domain, setDomain] = useState(initial.domain || "flotta.dev");
  const [token, setToken] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function save(event: React.FormEvent) {
    event.preventDefault();
    setSaving(true);
    setError(null);
    try {
      await invoke("save_settings", {
        controlUrl,
        domain,
        // undefined, not "": an empty string is a deliberate clear.
        token: token === "" ? undefined : token,
      });
      setToken("");
      onSaved();
    } catch (err) {
      setError(typeof err === "string" ? err : JSON.stringify(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <form onSubmit={save} className="mx-auto max-w-lg space-y-5 p-6">
      <div>
        <h2 className="text-sm font-semibold text-neutral-900">Settings</h2>
        <p className="mt-1 text-xs text-neutral-500">
          The app talks to your control plane. Nothing here leaves this machine
          except the requests it makes on your behalf.
        </p>
      </div>

      <label className="block">
        <span className="text-xs font-medium text-neutral-700">
          Control plane URL
        </span>
        <input
          value={controlUrl}
          onChange={(e) => setControlUrl(e.target.value)}
          placeholder="https://your-fleet.up.railway.app"
          className="mt-1 w-full rounded border border-neutral-300 px-2 py-1.5 font-mono text-xs focus:border-neutral-500 focus:outline-none"
        />
      </label>

      <label className="block">
        <span className="text-xs font-medium text-neutral-700">Box domain</span>
        <input
          value={domain}
          onChange={(e) => setDomain(e.target.value)}
          placeholder="flotta.dev"
          className="mt-1 w-full rounded border border-neutral-300 px-2 py-1.5 font-mono text-xs focus:border-neutral-500 focus:outline-none"
        />
        <span className="mt-1 block text-xs text-neutral-500">
          Agents are reached at <code>&lt;name&gt;.{domain || "…"}</code>.
        </span>
      </label>

      <label className="block">
        <span className="text-xs font-medium text-neutral-700">
          Access token
        </span>
        <input
          type="password"
          value={token}
          onChange={(e) => setToken(e.target.value)}
          placeholder={
            initial.has_token ? "saved — leave blank to keep it" : "flotta_…"
          }
          className="mt-1 w-full rounded border border-neutral-300 px-2 py-1.5 font-mono text-xs focus:border-neutral-500 focus:outline-none"
        />
        <span className="mt-1 block text-xs text-neutral-500">
          Stored in your keychain, never in a file.{" "}
          {initial.has_token ? "One is saved." : "None saved yet."} Mint one
          with{" "}
          <code className="text-[11px]">
            flotta token mint you --scope fleet:read --scope box:chat
          </code>
          .
        </span>
      </label>

      {error && (
        <p className="rounded bg-red-50 px-3 py-2 text-xs text-red-800">
          {error}
        </p>
      )}

      <button
        type="submit"
        disabled={saving}
        className="rounded bg-neutral-900 px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50"
      >
        {saving ? "Saving…" : "Save"}
      </button>
    </form>
  );
}
