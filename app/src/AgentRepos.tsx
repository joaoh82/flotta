import { useCallback, useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { isFleetError } from "./types";

/**
 * The repositories an agent may clone, commit to and push.
 *
 * **The capability shipped long before this panel did.** A box has held a git
 * credential helper since FLOTTA-20: it asks the control plane per repository,
 * per invocation, and holds no GitHub credential of its own. What was missing
 * was any way to grant one without a terminal — which is the thing this
 * project keeps saying it is not.
 *
 * The list always comes from the answer to the last call rather than being
 * patched locally. Every endpoint returns the whole list after the change, so
 * a pasted URL comes back as `owner/name` and what is displayed is what was
 * actually granted, not what was typed.
 */
export function AgentRepos({ boxId }: { boxId: string }) {
  const [repos, setRepos] = useState<string[] | null>(null);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setRepos(await invoke<string[]>("agent_repos", { id: boxId }));
      setError(null);
    } catch (e) {
      // A failed read must not render as "no repositories" — that is the same
      // confidently-wrong empty state the fleet list had to stop showing.
      setRepos(null);
      setError(isFleetError(e) ? e.detail : String(e));
    }
  }, [boxId]);

  useEffect(() => {
    void load();
  }, [load]);

  async function grant(event: React.FormEvent) {
    event.preventDefault();
    const repo = draft.trim();
    if (!repo || busy) return;
    setBusy(true);
    setError(null);
    try {
      setRepos(await invoke<string[]>("grant_repo", { id: boxId, repo }));
      setDraft("");
    } catch (e) {
      setError(isFleetError(e) ? e.detail : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function revoke(repo: string) {
    setBusy(true);
    setError(null);
    try {
      setRepos(await invoke<string[]>("revoke_repo", { id: boxId, repo }));
    } catch (e) {
      setError(isFleetError(e) ? e.detail : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section>
      <h3 className="text-[11px] uppercase tracking-wide text-neutral-400">
        Repositories
      </h3>

      {repos === null && !error && (
        <p className="mt-1.5 text-[11px] text-neutral-500">Loading…</p>
      )}

      {repos !== null && repos.length === 0 && (
        <p className="mt-1.5 text-[11px] text-neutral-500">
          None yet. Public repositories work without a grant; a private one
          needs to be listed here.
        </p>
      )}

      {repos !== null && repos.length > 0 && (
        <ul className="mt-1.5 divide-y divide-neutral-100 rounded border border-neutral-200">
          {repos.map((repo) => (
            <li key={repo} className="flex items-center justify-between px-2.5 py-1.5">
              <span className="font-mono text-[11px] text-neutral-800">{repo}</span>
              <button
                onClick={() => void revoke(repo)}
                disabled={busy}
                className="text-[11px] text-neutral-500 hover:text-red-700 disabled:opacity-40"
              >
                Revoke
              </button>
            </li>
          ))}
        </ul>
      )}

      <form onSubmit={grant} className="mt-2 flex gap-2">
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="owner/name, or paste a GitHub URL"
          className="flex-1 rounded border border-neutral-300 px-2.5 py-1.5 font-mono text-[11px] focus:border-neutral-500 focus:outline-none"
        />
        <button
          type="submit"
          disabled={busy || draft.trim() === ""}
          className="rounded bg-neutral-900 px-3 py-1.5 text-xs font-medium text-white disabled:opacity-40"
        >
          Grant
        </button>
      </form>

      {error && <p className="mt-1 text-[11px] text-red-700">{error}</p>}

      {/* The sentence that stops this panel promising more than the system
          keeps. The first half is the capability; the second is the limit, and
          it runs in **both** directions — which the first version of this said
          only half of. Flotta narrows what its token reaches and cannot widen
          it, so a repository that token has no access to is refused above
          rather than stored and left to fail on a machine (FLOTTA-59). When
          FLOTTA-22 lands, this is the paragraph that changes. */}
      <p className="mt-2 text-[11px] leading-relaxed text-neutral-400">
        This agent can clone, commit and push to these repositories. It holds
        no GitHub credential — it asks the control plane for one per
        repository, per use, and revoking takes effect on its next fetch.
        Flotta narrows what its own GitHub token can reach and cannot widen it,
        so a repository that token has no access to is refused here.
      </p>
    </section>
  );
}
