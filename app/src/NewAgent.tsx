import { useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { isFleetError, type BoxRow } from "./types";

/**
 * Create an agent.
 *
 * One request. The box arrives with its identity already on it — FLOTTA-21
 * injects it at creation — which is why this is a button rather than a button
 * followed by a terminal. That was the point of doing FLOTTA-21 before M8.
 *
 * The request is *not* the provisioning. Since FLOTTA-27 the control plane
 * answers `202` in a moment and builds the machine on a thread, so this form
 * finishes long before the agent exists. Saying "this takes a minute" here
 * would attach that minute to the wrong thing and then stop saying it exactly
 * when it became true — the waiting belongs to the agent, and `AgentTimeline`
 * is where it is described.
 */

/**
 * Why the name rule is spelled out twice.
 *
 * `store.validate_box_name` is the authority — it is what makes this true for
 * the CLI and for anything else that ever calls the API. This copy exists only
 * to answer before the round trip, because on Fly that round trip is a
 * *provision*: typing `Eng-f` once spent one, left a `torn_down` row, and took
 * the name with it, since a terminal row keeps its name forever.
 *
 * Kept deliberately narrow to limit what drift can cost. It never *accepts* on
 * the server's behalf — a name that passes here is still sent and can still be
 * refused, and that refusal is rendered — so the worst a stale copy can do is
 * complain about a name that would have worked.
 *
 * The name is the address: `<name>.<domain>` is a DNS label, and the door
 * lowercases the host it receives.
 */
function nameProblem(name: string): string | null {
  const value = name.trim();
  if (!value) return null;
  if (value.length > 63) return `${value.length} characters — a name has to fit in a DNS label (63)`;
  if (/^[a-z0-9]([a-z0-9-]*[a-z0-9])?$/.test(value)) return null;

  const suggestion = value.toLowerCase().replace(/[_\s]+/g, "-");
  const hint = /^[a-z0-9]([a-z0-9-]*[a-z0-9])?$/.test(suggestion) ? ` — try ${suggestion}` : "";
  return `lowercase letters, digits and dashes, not starting or ending with a dash${hint}`;
}
export function NewAgent({ onCreated }: { onCreated: (box: BoxRow) => void }) {
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Hidden by default. Most agents want the fleet's defaults, and a create
  // form asking three questions to answer one makes the common case worse to
  // serve the rare one.
  const [showOptions, setShowOptions] = useState(false);
  const [volumeGb, setVolumeGb] = useState("");
  const [region, setRegion] = useState("");
  // Who it is (FLOTTA-40). The address is `name`; these are what a person
  // calls it, what it is for, and what it is told. Instructions become the
  // agent's SOUL.md on its own volume and are its to evolve from then on.
  const [displayName, setDisplayName] = useState("");
  const [description, setDescription] = useState("");
  const [instructions, setInstructions] = useState("");

  const problem = nameProblem(name);

  async function create(event: React.FormEvent) {
    event.preventDefault();
    const trimmed = name.trim();
    if (!trimmed || busy || problem) return;
    setBusy(true);
    setError(null);
    try {
      const parsed = Number.parseInt(volumeGb.trim(), 10);
      const box = await invoke<BoxRow>("create_agent", {
        name: trimmed,
        // undefined, not null: absent means "the fleet decides", and that is
        // the answer for almost every agent.
        volumeGb: volumeGb.trim() === "" || Number.isNaN(parsed) ? undefined : parsed,
        region: region.trim() === "" ? undefined : region.trim(),
        displayName: displayName.trim() === "" ? undefined : displayName.trim(),
        description: description.trim() === "" ? undefined : description.trim(),
        instructions: instructions.trim() === "" ? undefined : instructions.trim(),
      });
      setName("");
      setVolumeGb("");
      setRegion("");
      setDisplayName("");
      setDescription("");
      setInstructions("");
      setShowOptions(false);
      onCreated(box);
    } catch (err) {
      setError(isFleetError(err) ? err.detail : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={create} className="border-t border-neutral-200 p-3">
      <div className="flex gap-2">
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="new agent, e.g. eng-b"
          disabled={busy}
          className="min-w-0 flex-1 rounded border border-neutral-300 px-2 py-1.5 text-xs focus:border-neutral-500 focus:outline-none disabled:bg-neutral-50"
        />
        <button
          type="submit"
          disabled={busy || name.trim() === "" || problem !== null}
          className="rounded bg-neutral-900 px-3 py-1.5 text-xs font-medium text-white disabled:opacity-40"
        >
          {busy ? "…" : "Create"}
        </button>
      </div>
      <button
        type="button"
        onClick={() => setShowOptions((open) => !open)}
        className="mt-2 text-[11px] text-neutral-500 underline hover:text-neutral-700"
      >
        {showOptions ? "Use the fleet's defaults" : "Give it a different size or region"}
      </button>

      {/* Who it is. Visible without expanding anything, because an agent with
          no description is one nobody can pick by role — and because the
          address rules above stop needing an apology: call it what you like,
          it lives at `<name>.flotta.dev`. */}
      <div className="mt-2 flex gap-2">
        <input
          value={displayName}
          onChange={(e) => setDisplayName(e.target.value)}
          placeholder="what people call it (optional)"
          maxLength={80}
          disabled={busy}
          className="min-w-0 flex-1 rounded border border-neutral-300 px-2 py-1 text-xs focus:border-neutral-500 focus:outline-none disabled:bg-neutral-50"
        />
        <input
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="what it is for (optional)"
          maxLength={500}
          disabled={busy}
          className="min-w-0 flex-1 rounded border border-neutral-300 px-2 py-1 text-xs focus:border-neutral-500 focus:outline-none disabled:bg-neutral-50"
        />
      </div>

      {/* Always visible, not behind the expander. An agent is defined by
          what it is told at least as much as by its name, and the product
          ask lists instructions beside the name — a field that is easy to
          miss on first create is a field that goes unset. */}
      <div className="mt-2">
        <label className="block">
          <span className="text-[11px] text-neutral-600">Standing instructions</span>
          <textarea
            value={instructions}
            onChange={(e) => setInstructions(e.target.value)}
            placeholder="Its system prompt. Who it is, what it works on, how it should behave."
            rows={4}
            maxLength={16000}
            disabled={busy}
            className="mt-0.5 w-full rounded border border-neutral-300 px-2 py-1 font-mono text-[11px] focus:border-neutral-500 focus:outline-none disabled:bg-neutral-50"
          />
          {/* Seeded once. Said here because it is the one thing about
              instructions that is not obvious: after this the file is the
              agent's, on its volume, and this form never touches it again. */}
          <span className="text-[11px] text-neutral-400">
            Written to the agent&rsquo;s SOUL.md once, at creation. From then on it&rsquo;s
            the agent&rsquo;s own to keep or change.
          </span>
        </label>
      </div>

      {showOptions && (
        <div className="mt-2 space-y-2">
          <label className="block">
            <span className="text-[11px] text-neutral-600">Disk (GB)</span>
            <input
              value={volumeGb}
              onChange={(e) => setVolumeGb(e.target.value)}
              placeholder="the fleet's default"
              inputMode="numeric"
              className="mt-0.5 w-full rounded border border-neutral-300 px-2 py-1 text-xs focus:border-neutral-500 focus:outline-none"
            />
          </label>
          <label className="block">
            <span className="text-[11px] text-neutral-600">Region</span>
            <input
              value={region}
              onChange={(e) => setRegion(e.target.value)}
              placeholder="the fleet's default"
              className="mt-0.5 w-full rounded border border-neutral-300 px-2 py-1 text-xs focus:border-neutral-500 focus:outline-none"
            />
          </label>
          {/* Said here because it is the one thing about this that cannot be
              undone: a volume cannot be resized afterwards. */}
          <p className="text-[11px] text-neutral-400">
            Both are fixed once the agent exists — a volume cannot be resized
            later.
          </p>
        </div>
      )}

      {problem && (
        <p className="mt-2 text-[11px] text-amber-700">
          A name is an agent's address: {problem}
        </p>
      )}
      {busy && (
        <p className="mt-2 text-[11px] text-neutral-500">Asking the control plane…</p>
      )}
      {error && (
        <p className="mt-2 whitespace-pre-wrap rounded bg-red-50 px-2 py-1.5 text-[11px] text-red-800">
          {error}
        </p>
      )}
    </form>
  );
}
