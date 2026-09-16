import { useCallback, useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { credentialOf, renewalReason, type Credential } from "./timeline";
import { isFleetError, type BoxEvent } from "./types";

type Rotated = { box_id: string; name: string; expires_at: number; scopes: string[] };

/**
 * The agent's identity token: when it runs out, and a way to renew it.
 *
 * The token is what lets an agent fetch GitHub credentials and ask its
 * colleagues. Renewing it used to be `just box-identity`, which wrote the new
 * token to the wrong Fly app on a per-agent fleet and so renewed nothing. It
 * is a button now because the control plane is the only thing that knows
 * where each agent lives.
 */
export function AgentIdentity({
  boxId,
  boxName,
  running,
}: {
  boxId: string;
  boxName: string;
  /** Renewing restarts a running agent, which the confirmation has to say. */
  running: boolean;
}) {
  const [credential, setCredential] = useState<Credential | null | undefined>(undefined);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const events = await invoke<BoxEvent[]>("agent_timeline", { id: boxId });
      setCredential(credentialOf(events));
    } catch {
      setCredential(null);
    }
  }, [boxId]);

  useEffect(() => {
    void load();
  }, [load]);

  async function renew() {
    setBusy(true);
    setError(null);
    try {
      const rotated = await invoke<Rotated>("rotate_identity", { id: boxId });
      setDone(`Renewed. Valid until ${day(rotated.expires_at)}.`);
      setConfirming(false);
      await load();
    } catch (e) {
      setError(isFleetError(e) ? e.detail : String(e));
    } finally {
      setBusy(false);
    }
  }

  const reason = renewalReason(credential ?? null, Date.now() / 1000);

  return (
    <div className="text-[12px]">
      <div className="flex gap-3">
        <dt className="w-32 shrink-0 text-neutral-500">Identity</dt>
        <dd className="min-w-0 flex-1">
          {credential === undefined ? (
            <span className="text-neutral-400">…</span>
          ) : credential === null ? (
            <span className="text-neutral-500">No token recorded by Flotta</span>
          ) : (
            <span>
              Valid until {day(credential.expiresAt)}
              {reason && <span className="ml-1 text-amber-700">— {reason}</span>}
            </span>
          )}
          {!confirming && (
            <button
              onClick={() => {
                setConfirming(true);
                setDone(null);
              }}
              className="ml-2 text-[11px] text-neutral-500 hover:text-neutral-900"
            >
              Renew
            </button>
          )}
        </dd>
      </div>

      {confirming && (
        <div className="ml-35 mt-1.5 rounded border border-neutral-200 bg-neutral-50 p-2.5 text-[11px] text-neutral-700">
          <p>
            Issues {boxName} a new token on its own machine.{" "}
            {running
              ? `${boxName} is running, so it restarts to pick the token up — about a minute, and any turn in progress is interrupted.`
              : `${boxName} is asleep and stays asleep; it picks the token up next time it wakes.`}{" "}
            Its memory is not touched.
          </p>
          <div className="mt-2 flex gap-2">
            <button
              onClick={() => void renew()}
              disabled={busy}
              className="rounded bg-neutral-900 px-3 py-1 text-xs font-medium text-white disabled:opacity-40"
            >
              {busy ? (running ? "Renewing and restarting…" : "Renewing…") : "Renew identity"}
            </button>
            <button
              onClick={() => setConfirming(false)}
              disabled={busy}
              className="rounded border border-neutral-300 px-3 py-1 text-xs hover:bg-white disabled:opacity-40"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {error && <p className="mt-1 text-[11px] text-red-700">{error}</p>}
      {done && <p className="mt-1 text-[11px] text-emerald-700">{done}</p>}
    </div>
  );
}

function day(epochSeconds: number): string {
  return new Date(epochSeconds * 1000).toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}
