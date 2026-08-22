/**
 * Startup banner for the dev server.
 *
 * Wired as npm's `predev` hook rather than into the justfile, so it fires for
 * `npm run dev` too — the dashboard README documents that form, and a warning
 * only the `just` path prints would miss exactly the reader who skipped the
 * docs.
 *
 * It says three things, in the order they matter: there is no authentication,
 * which store is about to be read, and which URL is about to be served. The
 * store line is not filler — `spawn` creates the store in whatever directory
 * it ran from, so "which fleet am I looking at" is a real question, and an
 * empty dashboard and the wrong file look identical without it.
 */
import { existsSync } from "node:fs";
import path from "node:path";

const useColor = process.env.NO_COLOR === undefined && process.stdout.isTTY;
const paint = (code, s) => (useColor ? `[${code}m${s}[0m` : s);
const bold = (s) => paint("1", s);
const yellow = (s) => paint("33", s);
const dim = (s) => paint("2", s);

// Mirrors lib/store.ts: $FLOTTA_STORE, else ../fleet.db relative to dashboard/.
const storePath = process.env.FLOTTA_STORE
  ? path.resolve(process.env.FLOTTA_STORE)
  : path.resolve(process.cwd(), "..", "fleet.db");

const storeNote = existsSync(storePath)
  ? ""
  : dim("   (none yet — spawn a worker)");

console.log(
  [
    "",
    yellow(bold("  ⚠  No authentication. This dashboard can kill workers.")),
    yellow("     Localhost only. Do not expose it without putting auth in front."),
    "",
    `  ${dim("store")}  ${storePath}${storeNote}`,
    `  ${dim("url")}    http://localhost:3001`,
    "",
  ].join("\n"),
);
