import { useEffect, useState } from "react";
import { Bot } from "lucide-react";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { cn } from "@/lib/utils";
import { useHermesWorkers, useRunInspect } from "../hooks/useControlData";
import { nowSec, workerHealth, workerSortRank } from "../lib/derive";
import { KEYMAP } from "../lib/keymap";
import type { Density } from "../hooks/useDensity";
import { WorkerCard } from "../components/WorkerCard";
import { ToneCallout } from "../components/atoms";

export function HermesFleet({ density }: { density: Density }) {
  const workers = useHermesWorkers();
  const { inspectByRun, loadingRun, inspect } = useRunInspect();
  const now = nowSec();
  const [selected, setSelected] = useState(0);
  const list = (workers.data?.workers ?? [])
    .map((worker) => ({ ...worker, inspect: inspectByRun[worker.run_id] ?? worker.inspect }))
    .sort((a, b) => workerSortRank(b, now) - workerSortRank(a, now));
  const activeIndex = Math.min(selected, Math.max(0, list.length - 1));

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target?.closest("input,textarea,[contenteditable='true'],[role='dialog']")) return;
      const key = event.key.toLowerCase();
      if (KEYMAP.list.next.includes(key as "j")) { event.preventDefault(); setSelected((idx) => Math.min(list.length - 1, idx + 1)); }
      if (KEYMAP.list.prev.includes(key as "k")) { event.preventDefault(); setSelected((idx) => Math.max(0, idx - 1)); }
      if (KEYMAP.list.open.includes(event.key as "Enter") && list[activeIndex]) { event.preventDefault(); void inspect(list[activeIndex].run_id); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [activeIndex, inspect, list]);

  return (
    <div className="space-y-5">
      <section className="hc-card flex flex-col gap-2 p-4 sm:flex-row sm:items-center sm:justify-between">
        <div><p className="hc-eyebrow">Hermes-Worker</p><h2 className="mt-1 text-xl font-semibold text-white">{workers.data?.count ?? list.length} aktive Läufe</h2></div>
        {workers.loading ? <Spinner /> : <span className="text-sm hc-soft">Inspect lädt CPU/RAM pro Worker auf Knopfdruck.</span>}
      </section>
      {workers.error ? <ToneCallout tone="red">{workers.error}</ToneCallout> : null}
      {list.length === 0 && !workers.loading ? <div className="hc-card flex items-center gap-3 p-4 text-sm hc-soft"><Bot className="h-5 w-5" />Keine aktiven Worker.</div> : null}
      <div className={cn("grid gap-4", density === "compact" ? "xl:grid-cols-2" : "lg:grid-cols-2")}>
        {list.map((worker, index) => <div key={worker.run_id} aria-selected={activeIndex === index} className={cn(activeIndex === index && "rounded-xl ring-1 ring-[var(--hc-accent-border)]")}><WorkerCard worker={worker} health={workerHealth(worker, now)} density={density} now={now} inspectLoading={loadingRun === worker.run_id} onInspect={inspect} /></div>)}
      </div>
    </div>
  );
}
