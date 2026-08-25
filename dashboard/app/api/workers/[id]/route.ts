/**
 * GET /api/workers/:id — one box, its tasks and its timeline.
 * DELETE /api/workers/:id — destroy it.
 *
 * Both proxy the control plane since M4.5.
 *
 * The DELETE used to shell out to `flotta kill` as a subprocess, because
 * tearing a box down needs Modal or Fly credentials this process does not
 * have. The control plane has them, so a DELETE is enough — and decision D10
 * ("only code that can reach the substrate writes to the store") is preserved
 * by the API boundary rather than by spawning python.
 *
 * The id validation goes with it: the value is no longer heading for a command
 * line, so there is nothing to inject into. It is URL-encoded on the way out
 * and the control plane 404s an unknown box.
 */
import {
  controlPlaneUrl,
  ControlPlaneError,
  ControlPlaneUnreachableError,
  getEvents,
  getWorker,
  killWorker,
} from "@/lib/control";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";
export const revalidate = 0;

function errorResponse(error: unknown): Response {
  if (error instanceof ControlPlaneUnreachableError) {
    return Response.json(
      {
        error: "control_plane_unreachable",
        message: error.message,
        control_plane: controlPlaneUrl(),
      },
      { status: 503 },
    );
  }
  if (error instanceof ControlPlaneError) {
    return Response.json(
      { error: "control_plane_error", message: error.message },
      { status: 502 },
    );
  }
  return Response.json(
    {
      error: "unreadable",
      message: error instanceof Error ? error.message : String(error),
    },
    { status: 500 },
  );
}

export async function GET(
  _request: Request,
  { params }: { params: Promise<{ id: string }> },
) {
  const { id } = await params;
  try {
    const worker = await getWorker(id);
    if (!worker) {
      return Response.json({ error: "not_found", id }, { status: 404 });
    }
    return Response.json({ worker, events: await getEvents(id) });
  } catch (error) {
    return errorResponse(error);
  }
}

export async function DELETE(
  _request: Request,
  { params }: { params: Promise<{ id: string }> },
) {
  const { id } = await params;
  try {
    return Response.json({ result: await killWorker(id) });
  } catch (error) {
    if (error instanceof ControlPlaneError && error.status === 404) {
      return Response.json({ error: "not_found", id }, { status: 404 });
    }
    return errorResponse(error);
  }
}
