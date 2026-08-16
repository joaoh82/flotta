<!-- BEGIN:nextjs-agent-rules -->
# This is NOT the Next.js you know

This version has breaking changes — APIs, conventions, and file structure may all differ from your training data. Read the relevant guide in `node_modules/next/dist/docs/` before writing any code. Heed deprecation notices.
<!-- END:nextjs-agent-rules -->

## Flotta

The warning above is real and was worth heeding — this app is on **Next 16**,
where `params` and `searchParams` are Promises that must be awaited (the
synchronous access Next 15 tolerated is gone).

One trap specific to this app: **do not enable `cacheComponents`.** Next 16
treats synchronous SQLite reads as deterministic work that can complete during
prerendering, which would bake a snapshot of the fleet store into the build. It
also turns `dynamic = 'force-dynamic'` into a no-op. The escape hatch, if it is
ever enabled, is `await connection()` before each query.

Working guidelines for this repo — never-touch-`main`, task management,
reserved ports — live in the root [`../CLAUDE.md`](../CLAUDE.md), which is the
source of truth for every harness. See [`README.md`](./README.md) for what this
dashboard is, how it reads the store, and its v0.1 limits.
