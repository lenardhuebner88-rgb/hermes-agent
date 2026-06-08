import { useEffect, useState } from "react";
import { Bot } from "lucide-react";
import { cn } from "@/lib/utils";
import { fetchJSON } from "@/lib/api";
import { de } from "../i18n/de";
import { useHermesBlockedCompletions, useHermesRecentResults, useHermesWorkers, useRunInspect } from "../hooks/useControlData";
import { nowSec, workerHealth, workerSortRank } from "../lib/derive";
import { KEYMAP } from "../lib/keymap";
import type { Density } from "../hooks/useDensity";
import { WorkerCard } from "../components/WorkerCard";
import { HermesResultCard } from "../components/HermesResultCard";
import { HermesBlockedCard } from "../components/HermesBlockedCard";
import { ToneCallout } from "../components/atoms";
import { Panel, Section, SkeletonCard, Stagger, StaggerItem, Stat, Text } from "../components/primitives";

export function HermesFleet({ density }: { density: Density }) {
  const workers = useHermesWorkers();
  const results = useHermesRecentResults();
  const blocked = useHermesBlockedCompletions();
  const { inspectByRun, errorByRun, loadingRun, inspect } = useRunInspect();
  const now = nowSec();
  const [selected, setSelected] = useState(0);
  const [busyRun, setBusyRun] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const onAction = async (runId: string, action: string) => {
    setBusyRun(runId);
    setActionError(null);
    try {
      const res = await fetchJSON<{ ok?: boolean; detail?: string }>(
        `/api/plugins/kanban/workers/${encodeURIComponent(runId)}/action`,
        { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action, confirm: true }) },
      );
      if (res.ok === false) setActionError(res.detail || de.worker.actionFailed);
    } catch (e) {
      setActionError(`${de.worker.actionFailed}: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusyRun(null);
      await workers.reload();
    }
  };
  const list = (workers.data?.workers ?? [])
    .map((worker) => ({ ...worker, inspect: inspectByRun[worker.run_id] ?? worker.inspect }))
    .sort((a, b) => workerSortRank(b, now) - workerSortRank(a, now));
  const activeIndex = Math.min(selected, Math.max(0, list.length - 1));
  const workersLoadingFirst = workers.loading && workers.data == null;
  const resultsList = results.data?.results ?? [];
  const resultsLoadingFirst = results.loading && results.data == null;
  const blockedList = blocked.data?.blocked ?? [];
  const blockedLoadingFirst = blocked.loading && blocked.data == null;

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

  const gridClass = cn("grid gap-4", density === "compact" ? "xl:grid-cols-2" : "lg:grid-cols-2");
  const inspectError = list[activeIndex] ? errorByRun[list[activeIndex].run_id] : "";

  return (
    <div className="space-y-5">
      <Panel eyebrow="Hermes-Worker" title="Flotte">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
          <Stat
            label="Aktive Läufe"
            value={workersLoadingFirst ? "—" : (workers.data?.count ?? list.length)}
            hint="Inspect lädt CPU/RAM pro Worker auf Knopfdruck."
            accent
            className="min-w-0"
          />
        </div>
      </Panel>

      {workers.error ? <ToneCallout tone="red">{workers.error}</ToneCallout> : null}
      {actionError ? <ToneCallout tone="red">{actionError}</ToneCallout> : null}
      {inspectError ? <ToneCallout tone="amber">{de.worker.actionFailed}: {inspectError}</ToneCallout> : null}

      {workersLoadingFirst ? (
        <div className={gridClass}>
          <SkeletonCard rows={4} />
          <SkeletonCard rows={4} />
        </div>
      ) : list.length === 0 ? (
        <div className="hc-surface-card flex items-center gap-3 p-4 text-sm hc-soft">
          <Bot className="h-5 w-5" />Keine aktiven Worker.
        </div>
      ) : (
        <Stagger className={gridClass}>
          {list.map((worker, index) => (
            <StaggerItem
              key={worker.run_id}
              className={cn(activeIndex === index && "rounded-xl ring-1 ring-[var(--hc-accent-border)]")}
            >
              <div aria-selected={activeIndex === index}>
                <WorkerCard
                  worker={worker}
                  health={workerHealth(worker, now)}
                  density={density}
                  now={now}
                  inspectLoading={loadingRun === worker.run_id}
                  onInspect={inspect}
                  onAction={onAction}
                  actionBusy={busyRun === worker.run_id}
                />
              </div>
            </StaggerItem>
          ))}
        </Stagger>
      )}

      <Section
        eyebrow={de.hermes.recentResults}
        title={`${resultsLoadingFirst ? "…" : (results.data?.count ?? 0)} abgeschlossene Läufe`}
        actions={<Text variant="label" className="hc-soft">{de.hermes.recentResultsHint}</Text>}
      >
        {results.error ? <ToneCallout tone="red">{de.hermes.resultsError}<br />{results.error}</ToneCallout> : null}
        {resultsLoadingFirst ? (
          <div className={gridClass}>
            <SkeletonCard rows={3} />
            <SkeletonCard rows={3} />
          </div>
        ) : resultsList.length === 0 && !results.error ? (
          <div className="hc-surface-card p-4 text-sm hc-soft">{de.hermes.emptyResults}</div>
        ) : (
          <Stagger className={gridClass}>
            {resultsList.map((result) => (
              <StaggerItem key={result.run_id}>
                <HermesResultCard result={result} now={now} />
              </StaggerItem>
            ))}
          </Stagger>
        )}
      </Section>

      <Section
        eyebrow={de.hermes.blockedCompletions}
        title={`${blockedLoadingFirst ? "…" : (blocked.data?.count ?? 0)} blockierte Abschluesse`}
        actions={<Text variant="label" className="hc-soft">{de.hermes.blockedHint}</Text>}
      >
        {blocked.error ? <ToneCallout tone="red">{de.hermes.blockedError}<br />{blocked.error}</ToneCallout> : null}
        {blockedLoadingFirst ? (
          <div className={gridClass}>
            <SkeletonCard rows={3} />
            <SkeletonCard rows={3} />
          </div>
        ) : blockedList.length === 0 && !blocked.error ? (
          <div className="hc-surface-card p-4 text-sm hc-soft">{de.hermes.emptyBlocked}</div>
        ) : (
          <Stagger className={gridClass}>
            {blockedList.map((item) => (
              <StaggerItem key={item.event_id}>
                <HermesBlockedCard blocked={item} now={now} />
              </StaggerItem>
            ))}
          </Stagger>
        )}
      </Section>
    </div>
  );
}
