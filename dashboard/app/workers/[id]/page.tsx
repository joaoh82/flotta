import { WorkerView } from "@/app/components/WorkerView";

/**
 * `params` is a Promise in Next 16 — the synchronous access that Next 15
 * tolerated with a warning was removed.
 */
export default async function WorkerPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  return <WorkerView id={id} />;
}
