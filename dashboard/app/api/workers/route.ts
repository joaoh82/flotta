/**
 * GET /api/workers — every box, newest first.
 *
 * A thin proxy to the control plane since M4.5. It used to open the fleet's
 * SQLite file directly, which §8.3 required to go: "reads the API, not the DB
 * directly". The `force-dynamic` pair stays for the same reason it was added —
 * this endpoint reflects a fleet that changes underneath us, and a cached view
 * is actively misleading rather than merely stale.
 */
import {
  controlPlaneUrl,
  ControlPlaneError,
  ControlPlaneUnreachableError,
  listWorkers,
} from "@/lib/control";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";
export const revalidate = 0;

export async function GET() {
  try {
    return Response.json({ workers: await listWorkers() });
  } catch (error) {
    if (error instanceof ControlPlaneUnreachableError) {
      // 503, not an empty list: "the control plane is down" and "you have no
      // boxes" are very different facts, and rendering the second for the
      // first is the confusion this project keeps having to kill.
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
}
