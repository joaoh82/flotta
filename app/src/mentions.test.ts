import { describe, expect, it } from "vitest";
import {
  complete,
  mentionAt,
  mentionsIn,
  NOTE_OPEN,
  segments,
  suggest,
  withNote,
  withoutNote,
} from "./mentions";
import type { Peer } from "./types";

const PEERS: Peer[] = [
  { id: "b-g", name: "eng-g", display_name: null, description: null },
  {
    id: "b-r",
    name: "eng-r",
    display_name: "Reviewer - backend PRs",
    description: "Reviews backend pull requests",
  },
  { id: "b-d", name: "eng-d", display_name: "Docs", description: null },
];

describe("completing a mention", () => {
  it("opens on an @ at the start or after a space", () => {
    expect(mentionAt("@", 1)).toEqual({ start: 0, query: "" });
    expect(mentionAt("ask @en", 7)).toEqual({ start: 4, query: "en" });
  });

  it("does not open inside an email address", () => {
    expect(mentionAt("mail me@example", 15)).toBeNull();
  });

  it("closes once the mention is followed by a space", () => {
    expect(mentionAt("ask @eng-g ", 11)).toBeNull();
  });

  it("follows the caret, not the end of the text", () => {
    expect(mentionAt("ask @en about x", 7)).toEqual({ start: 4, query: "en" });
  });

  it("offers colleagues by address first, then by what they are called", () => {
    expect(suggest(PEERS, "").map((p) => p.name)).toEqual(["eng-d", "eng-g", "eng-r"]);
    expect(suggest(PEERS, "eng-r").map((p) => p.name)).toEqual(["eng-r"]);
    expect(suggest(PEERS, "rev").map((p) => p.name)).toEqual(["eng-r"]);
    expect(suggest(PEERS, "zzz")).toEqual([]);
  });

  it("replaces what was typed with the name and a space, and says where the caret goes", () => {
    const at = mentionAt("ask @en", 7)!;
    expect(complete("ask @en", at, 7, "eng-r")).toEqual({ text: "ask @eng-r ", caret: 11 });
  });

  it("replaces the rest of a partial name when completing mid-word", () => {
    const at = mentionAt("ask @en about x", 7)!;
    // Caret after "@en", with "g-x" still to its right.
    expect(complete("ask @eng-x about x", at, 7, "eng-r").text).toBe("ask @eng-r about x");
  });
});

describe("finding mentions in a message", () => {
  it("finds real colleagues, once each, in order", () => {
    const named = mentionsIn("@eng-r and @eng-g, then @eng-r again", PEERS);
    expect(named.map((p) => p.name)).toEqual(["eng-r", "eng-g"]);
  });

  it("ignores names that are not colleagues", () => {
    expect(mentionsIn("@eng-zz and @someone", PEERS)).toEqual([]);
  });

  it("ignores email addresses and partial names", () => {
    expect(mentionsIn("write to x@eng-g.dev", PEERS)).toEqual([]);
    expect(mentionsIn("@eng-gx", PEERS)).toEqual([]);
  });

  it("allows punctuation after a mention", () => {
    expect(mentionsIn("ask @eng-g.", PEERS).map((p) => p.name)).toEqual(["eng-g"]);
    expect(mentionsIn("(@eng-d)", PEERS).map((p) => p.name)).toEqual(["eng-d"]);
  });
});

describe("the note the agent gets", () => {
  it("is not added when nobody is mentioned", () => {
    expect(withNote("just a question", PEERS)).toBe("just a question");
  });

  it("names each colleague, what it is for, and the command", () => {
    const sent = withNote("@eng-r can help with this", PEERS);
    expect(sent.startsWith("@eng-r can help with this")).toBe(true);
    expect(sent).toContain("@eng-r (Reviewer - backend PRs, Reviews backend pull requests)");
    expect(sent).toContain('flotta-ask <name> "<your question>"');
  });

  it("says nothing about whether to ask — that is the person's call", () => {
    const note = withNote("don't bother @eng-g", PEERS).slice("don't bother @eng-g".length);
    expect(note).not.toMatch(/\byou should\b|\bmust\b/i);
  });

  it("is removed again, so the transcript shows what was typed", () => {
    const typed = "@eng-r and @eng-d, what do you think?\n\nsecond paragraph";
    expect(withoutNote(withNote(typed, PEERS))).toBe(typed);
  });

  it("leaves a message alone that merely contains the marker's words", () => {
    const typed = `quote: ${NOTE_OPEN.trim()} not really`;
    expect(withoutNote(typed)).toBe(typed);
  });
});

describe("highlighting", () => {
  const names = new Set(PEERS.map((p) => p.name));

  it("cuts a message into text and mentions", () => {
    expect(segments("ask @eng-r now", names)).toEqual([
      { text: "ask ", mention: false },
      { text: "@eng-r", mention: true },
      { text: " now", mention: false },
    ]);
  });

  it("leaves unknown names as text", () => {
    expect(segments("ask @nobody", names)).toEqual([{ text: "ask @nobody", mention: false }]);
  });

  it("handles a mention at the very start and the very end", () => {
    expect(segments("@eng-g", names)).toEqual([{ text: "@eng-g", mention: true }]);
  });
});
