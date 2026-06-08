import { useEffect, useState } from "react";
import { cn } from "@/lib/utils";
import { fetchJSON } from "@/lib/api";
import { de } from "../i18n/de";
import {
  useBoard,
  useHermesBlockedCompletions,
  useHermesRecentResults,
  useHermesReviewVerdicts,
  useHermesTodayDigest,
  useHermesWorkers,
  useRosterCount,
  useRunInspect,
} from "../hooks/useControlData";
import { nowSec, workerHealth, workerSortRank } from "../lib/derive";
import { KEYMAP } from "../lib/keymap";
import type { Density } from "../hooks/useDensity";
import type { BoardTask } from "../lib/types";
import { WorkerCard } from "../components/WorkerCard";
import { HermesTodayDigestCard } from "../components/HermesTodayDigestCard";
import { HermesBlockedCard } from "../components/HermesBlockedCard";
import { HermesReviewCard } from "../components/HermesReviewCard";
import { ToneCallout } from "../components/atoms";
import { SkeletonCard, Text } from "../components/primitives";
import { FleetPod, FleetPanel, FleetEmptyState } from "../components/fleet/atoms";
import { FleetResultCard } from "../components/fleet/FleetResultCard";
import { FleetPipeline } from "../components/fleet/FleetPipeline";

const GRID = "grid gap-3 lg:grid-cols-2";

export function HermesFleet({ density }: { density: Density }) {
  const board = useBoard();
  const roster = useRosterCount();
  const workers = useHermesWorkers();
  const results = useHermesRecentResults();
  const digest = useHermesTodayDigest();
  const reviews = useHermesReviewVerdicts();
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

  const boardTasks: BoardTask[] = board.data?.columns.flatMap((c) => c.tasks) ?? [];
  const colCount = (name: string) => board.data?.columns.find((c) => c.name === name)?.tasks.length ?? 0;
  const runningCount = colCount("running");
  const reviewCount = board.data ? colCount("review") : (reviews.data?.count ?? 0);
  const activeRuns = board.data ? runningCount : (workers.data?.count ?? list.length);

  const resultsList = results.data?.results ?? [];
  const resultsLoadingFirst = results.loading && results.data == null;
  const digestList = digest.data?.items ?? [];
  const digestLoadingFirst = digest.loading && digest.data == null;
  const reviewList = reviews.data?.reviews ?? [];
  const reviewsLoadingFirst = reviews.loading && reviews.data == null;
  const blockedList = blocked.data?.blocked ?? [];
  const verifierRejectedList = blockedList.filter((item) => item.kind === "verifier_request_changes");
  const blockedWarningList = blockedList.filter((item) => item.kind !== "verifier_request_changes");

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

  const inspectError = list[activeIndex] ? errorByRun[list[activeIndex].run_id] : "";

  return (
    <div className="space-y-5">
      {/* Header */}
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h1 className="hc-type-title text-white">{de.fleet.title}</h1>
        <span className="hc-mono text-sm hc-dim">{de.fleet.subtitle}</span>
      </div>

      {/* KPI pods */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <FleetPod label={de.fleet.podActiveRuns} dot="live" value={board.loading && board.data == null ? "—" : activeRuns} />
        <FleetPod
          label={de.fleet.podWorkers}
          value={workersLoadingFirst ? "—" : (workers.data?.count ?? list.length)}
          suffix={roster.data != null ? `/ ${roster.data}` : undefined}
        />
        <FleetPod label={de.fleet.podInReview} dot="warn" value={reviewsLoadingFirst && !board.data ? "—" : reviewCount} />
        <FleetPod label={de.fleet.podDoneToday} value={digestLoadingFirst ? "—" : (digest.data?.count ?? 0)} />
      </div>

      {/* Pipeline — real stage mechanics (Ziel 2) */}
      {board.error ? <ToneCallout tone="red">Board konnte nicht geladen werden.<br />{board.error}</ToneCallout> : null}
      <FleetPipeline tasks={boardTasks} reload={board.reload} />

      {/* Worker fleet panel */}
      <FleetPanel eyebrow={de.fleet.workerEyebrow} meta={de.fleet.workerHint}>
        {workers.error ? <ToneCallout tone="red">{workers.error}</ToneCallout> : null}
        {actionError ? <div className="mb-3"><ToneCallout tone="red">{actionError}</ToneCallout></div> : null}
        {inspectError ? <div className="mb-3"><ToneCallout tone="amber">{de.worker.actionFailed}: {inspectError}</ToneCallout></div> : null}
        {workersLoadingFirst ? (
          <div className={GRID}><SkeletonCard rows={4} /><SkeletonCard rows={4} /></div>
        ) : list.length === 0 ? (
          <FleetEmptyState title={de.fleet.workerEmptyTitle} desc={de.fleet.workerEmptyDesc} />
        ) : (
          <div className={GRID}>
            {list.map((worker, index) => (
              <div key={worker.run_id} aria-selected={activeIndex === index} className={cn(activeIndex === index && "rounded-xl ring-1 ring-[var(--hc-accent-border)]")}>
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
            ))}
          </div>
        )}
      </FleetPanel>

      {/* Review verdicts + today digest — 2-column row with quiet empty states */}
      <div className={GRID}>
        <FleetPanel eyebrow={de.fleet.reviewEyebrow} meta={de.fleet.reviewMeta(reviewsLoadingFirst ? 0 : (reviews.data?.count ?? 0))}>
          {reviews.error ? <ToneCallout tone="red">{reviews.error}</ToneCallout> : reviewsLoadingFirst ? (
            <SkeletonCard rows={3} />
          ) : reviewList.length === 0 ? (
            <FleetEmptyState ok title={de.fleet.reviewEmptyTitle} desc={de.fleet.reviewEmptyDesc} />
          ) : (
            <div className="space-y-3">
              {reviewList.map((review) => <HermesReviewCard key={review.task_id} review={review} now={now} />)}
            </div>
          )}
        </FleetPanel>

        <FleetPanel eyebrow={de.fleet.digestEyebrow} meta={de.fleet.digestMeta}>
          {digest.error ? <ToneCallout tone="red">{de.hermes.todayDigestError}<br />{digest.error}</ToneCallout> : digestLoadingFirst ? (
            <SkeletonCard rows={3} />
          ) : digestList.length === 0 ? (
            <FleetEmptyState title={de.fleet.digestEmptyTitle} desc={de.fleet.digestEmptyDesc} />
          ) : (
            <div className="space-y-3">
              {digestList.map((item) => <HermesTodayDigestCard key={item.run_id} item={item} now={now} />)}
            </div>
          )}
        </FleetPanel>
      </div>

      {/* Letzte Ergebnisse — verdichtetes 2-column grid */}
      <FleetPanel eyebrow={de.fleet.resultsEyebrow} meta={de.fleet.resultsMeta(resultsLoadingFirst ? 0 : (results.data?.count ?? 0))}>
        {results.error ? <ToneCallout tone="red">{de.hermes.resultsError}<br />{results.error}</ToneCallout> : resultsLoadingFirst ? (
          <div className={GRID}><SkeletonCard rows={3} /><SkeletonCard rows={3} /></div>
        ) : resultsList.length === 0 ? (
          <FleetEmptyState title={de.fleet.resultsEmptyTitle} desc={de.fleet.resultsEmptyDesc} />
        ) : (
          <div className={GRID}>
            {resultsList.map((result) => <FleetResultCard key={result.run_id} result={result} now={now} />)}
          </div>
        )}
      </FleetPanel>

      {/* Rework surfaces — verifier-rejected + hard-blocked completions */}
      <div className={GRID}>
        <FleetPanel eyebrow={de.fleet.rework} meta={blocked.loading && blocked.data == null ? "…" : `${verifierRejectedList.length}`}>
          <p className="mb-3 hc-type-label hc-dim">{de.fleet.reworkHint}</p>
          {blocked.error ? <ToneCallout tone="red">{de.hermes.blockedError}<br />{blocked.error}</ToneCallout> : verifierRejectedList.length === 0 ? (
            <FleetEmptyState ok title={de.fleet.reworkEmpty} desc="" />
          ) : (
            <div className="space-y-3">
              {verifierRejectedList.map((item) => <HermesBlockedCard key={`verifier-${item.run_id ?? item.event_id}`} blocked={item} now={now} />)}
            </div>
          )}
        </FleetPanel>

        <FleetPanel eyebrow={de.fleet.blockedTitle} meta={blocked.loading && blocked.data == null ? "…" : `${blockedWarningList.length}`}>
          <p className="mb-3 hc-type-label hc-dim">{de.fleet.blockedHint}</p>
          {blocked.error ? <ToneCallout tone="red">{de.hermes.blockedError}<br />{blocked.error}</ToneCallout> : blockedWarningList.length === 0 ? (
            <FleetEmptyState ok title={de.fleet.blockedEmpty} desc="" />
          ) : (
            <div className="space-y-3">
              {blockedWarningList.map((item) => <HermesBlockedCard key={item.event_id} blocked={item} now={now} />)}
            </div>
          )}
        </FleetPanel>
      </div>

      <Text variant="label" className="hc-dim">j/k bewegt die Worker-Auswahl · Enter inspiziert · Aktionen schreiben echten Kanban-Status.</Text>
    </div>
  );
}
