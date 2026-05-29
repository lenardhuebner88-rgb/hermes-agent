import { Bot } from "lucide-react";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { cn } from "@/lib/utils";
import { useHermesWorkers, useRunInspect } from "../hooks/useControlData";
import { nowSec, workerHealth, workerSortRank } from "../lib/derive";
import type { Density } from "../hooks/useDensity";
import { WorkerCard } from "../components/WorkerCard";
import { ToneCallout } from "../components/atoms";

export function HermesFleet({ density }: { density: Density }) {
  const workers = useHermesWorkers();
  const { inspectByRun, loadingRun, inspect } = useRunInspect();
  const now = nowSec();
  const list = (workers.data?.workers ?? [])
    .map((worker) => ({ ...worker, inspect: inspectByRun[worker.run_id] ?? worker.inspect }))
    .sort((a, b) => workerSortRank(b, now) - workerSortRank(a, now));

  return (
    <div className="space-y-5">
      <section className="hc-card flex flex-col gap-2 p-4 sm:flex-row sm:items-center sm:justify-between">
        <div><p className="hc-eyebrow">Hermes-Worker</p><h2 className="mt-1 text-xl font-semibold text-white">{workers.data?.count ?? list.length} aktive L?ufe</h2></div>
        {workers.loading ? <Spinner /> : <span className="text-sm hc-soft">Inspect l?dt CPU/RAM pro Worker auf Knopfdruck.</span>}
      </section>
      {workers.error ? <ToneCallout tone="red">{workers.error}</ToneCallout> : null}
      {list.length === 0 && !workers.loading ? <div className="hc-card flex items-center gap-3 p-4 text-sm hc-soft"><Bot className="h-5 w-5" />Keine aktiven Worker.</div> : null}
      <div className={cn("grid gap-4", density === "compact" ? "xl:grid-cols-2" : "lg:grid-cols-2")}>
        {list.map((worker) => <WorkerCard key={worker.run_id} worker={worker} health={workerHealth(worker, now)} density={density} now={now} inspectLoading={loadingRun === worker.run_id} onInspect={inspect} />)}
      </div>
    </div>
  );
}
