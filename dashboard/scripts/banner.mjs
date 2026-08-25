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

const useColor = process.env.NO_COLOR === undefined && process.stdout.isTTY;
const paint = (code, s) => (useColor ? `[${code}m${s}[0m` : s);
const bold = (s) => paint("1", s);
const yellow = (s) => paint("33", s);
const dim = (s) => paint("2", s);

// The dashboard reads the control plane, not a file (M4.5). Announcing a
// store path here would be the same stale lie the API routes just stopped
// telling — it named a SQLite file even when the fleet lived on Postgres.
const controlUrl = (process.env.FLOTTA_CONTROL_URL || "http://127.0.0.1:8080").replace(
  /\/$/,
  "",
);

console.log(
  [
    "",
    yellow(bold("  ⚠  No authentication. This dashboard can destroy boxes.")),
    yellow("     Localhost only, and so is the control plane it talks to."),
    "",
    `  ${dim("control")}  ${controlUrl}  ${dim("(start it with `flotta serve`)")}`,
    `  ${dim("url")}    http://localhost:3001`,
    "",
  ].join("\n"),
);
