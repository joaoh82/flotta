/**
 * GET /api/workers — every worker, newest first.
 *
 * `force-dynamic` + `revalidate = 0` are belt and braces: route handlers are
 * already uncached by default in Next 16, but this endpoint reads a live file
 * that changes underneath us, and a cached fleet view would be actively
 * misleading rather than merely stale.
 */
import { listWorkers, resolveStorePath, StoreMissingError } from "@/lib/store";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";
export const revalidate = 0;

export async function GET() {
  try {
    return Response.json({ workers: listWorkers() });
  } catch (error) {
    if (error instanceof StoreMissingError) {
      // 503, not an empty list: "no store here" and "no workers yet" are very
      // different facts and the UI must be able to tell them apart.
      return Response.json(
        {
          error: "store_missing",
          message: error.message,
          store: error.storePath,
        },
        { status: 503 },
      );
    }
    return Response.json(
      {
        error: "store_unreadable",
        message: error instanceof Error ? error.message : String(error),
        store: resolveStorePath(),
      },
      { status: 500 },
    );
  }
}
