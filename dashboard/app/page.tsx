import { FleetTable } from "./components/FleetTable";

export default function FleetPage() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Fleet</h1>
        <p className="mt-1 text-sm text-neutral-500">
          Refreshes every 3 seconds.
        </p>
      </div>
      <FleetTable />
    </div>
  );
}
