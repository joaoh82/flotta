/**
 * GET    /api/workers/:id — worker row + its event timeline
 * DELETE /api/workers/:id — tear the worker down (the kill button)
 *
 * Note `params` is a Promise and must be awaited: Next 16 removed the
 * synchronous compatibility shim that Next 15 still allowed.
 */
import type { NextRequest } from "next/server";

import {
  getEvents,
  getWorker,
  StoreMissingError,
  StoreOnPostgresError,
} from "@/lib/store";
import { InvalidWorkerIdError, killWorker } from "@/lib/teardown";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";
export const revalidate = 0;

function storeErrorResponse(error: unknown): Response {
  if (error instanceof StoreOnPostgresError) {
    // 501, not 503: the fleet is reachable and healthy — this reader is the
    // thing that cannot reach it. "Not implemented" is the honest status for a
    // capability the dashboard has not grown yet (§8.3's API rewrite).
    return Response.json(
      { error: "store_on_postgres", message: error.message },
      { status: 501 },
    );
  }
  if (error instanceof StoreMissingError) {
    return Response.json(
      { error: "store_missing", message: error.message, store: error.storePath },
      { status: 503 },
    );
  }
  return Response.json(
    {
      error: "store_unreadable",
      message: error instanceof Error ? error.message : String(error),
    },
    { status: 500 },
  );
}

export async function GET(
  _request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const { id } = await params;
  try {
    const worker = getWorker(id);
    if (!worker) {
      return Response.json({ error: "not_found", id }, { status: 404 });
    }
    return Response.json({ worker, events: getEvents(id) });
  } catch (error) {
    return storeErrorResponse(error);
  }
}

export async function DELETE(
  _request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const { id } = await params;
  try {
    // Confirm the worker exists before shelling out, so a typo comes back as a
    // clean 404 rather than a CLI stderr dump.
    if (!getWorker(id)) {
      return Response.json({ error: "not_found", id }, { status: 404 });
    }
    return Response.json({ result: await killWorker(id) });
  } catch (error) {
    if (error instanceof InvalidWorkerIdError) {
      return Response.json(
        { error: "invalid_id", message: error.message },
        { status: 400 },
      );
    }
    if (error instanceof StoreMissingError || error instanceof StoreOnPostgresError) {
      return storeErrorResponse(error);
    }

    // The CLI failed. Surface its stderr — a teardown that did not happen must
    // never be reported to the user as success.
    const stderr =
      typeof error === "object" && error !== null && "stderr" in error
        ? String((error as { stderr: unknown }).stderr)
        : "";
    return Response.json(
      {
        error: "teardown_failed",
        message: error instanceof Error ? error.message : String(error),
        detail: stderr.trim(),
      },
      { status: 500 },
    );
  }
}
