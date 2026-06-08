/**
 * FlowView (/control/flow) — the Flow Command Board, FULLY LIVE.
 *
 * One operator view of every real agent run across Capture → Plan → Execute →
 * Verify → Ship, derived from the live Kanban board (GET /board) and enriched
 * with workers/active, review-verdicts, recent-results and blocked-completions.
 * The receipt rail reads the selected task's real runs + events + deliverables
 * (GET /tasks/{id}). Stage buttons drive real PATCH transitions or show an
 * honest guard. NO mock/demo data — a quiet empty state shows when no runs
 * exist for a stage.
 */
import { useMemo, useState } from "react";
import { AlertTriangle, ArrowRight, Lock, ShieldCheck } from "lucide-react";
import { cn } from "@/lib/utils";
import { de } from "../i18n/de";
import { TONE_HEX, profileLabel, taskStatusLabel } from "../lib/tones";
import { fmtAge } from "../lib/derive";
import { FLEET_STAGES, STAGE_META, groupByStage, roleChip, stageActions, stageGuard, type StageAction } from "../lib/fleet";
import {
  useBoard,
  useHermesBlockedCompletions,
  useHermesRecentResults,
  useHermesReviewVerdicts,
  useHermesWorkers,
  useTaskAction,
  useTaskDetail,
} from "../hooks/useControlData";
import type { BoardTask } from "../lib/types";
import type { TaskDetailResponse } from "../lib/schemas";
import { StatusPill, ToneCallout } from "../components/atoms";
import { Eyebrow, SkeletonCard, Text } from "../components/primitives";
import { FleetPod, FleetEmptyState } from "../components/fleet/atoms";
import { RoleChip } from "../components/fleet/atoms";

const MAX_CARDS = 12;

// Enrichment for a board task, gathered from the live sidecar endpoints.
interface Enriched {
  workerProfile?: string | null;
  workerHeartbeat?: number | null;
  verdict?: string | null;
  verifierEvidenceCount?: number;
  blockedKind?: string | null;
  blockedReason?: string | null;
  resultQualityLabel?: string | null;
  resultQualityTone?: string | null;
  deliverableCount?: number;
}

const EVENT_LABEL: Record<string, string> = {
  created: "Erstellt", claimed: "Worker claimte", completed: "Abgeschlossen", done: "Fertig",
  blocked: "Blockiert", unblocked: "Entblockt", scheduled: "Geplant", promoted: "Befördert",
  submitted_for_review: "Zum Review eingereicht", verified: "Verifiziert", reclaimed: "Zurückgeholt",
  edited: "Bearbeitet", reprioritized: "Neu priorisiert", assigned: "Zugewiesen",
  deliverables_preserved: "Deliverables gesichert", spawn_failed: "Spawn fehlgeschlagen",
};
function eventLabel(kind: string): string {
  return EVENT_LABEL[kind] ?? kind.replace(/_/g, " ");
}

function FlowCardActions({ status, busy, error, onAct }: { status: BoardTask["status"]; busy: boolean; error?: string; onAct: (action: StageAction) => void }) {
  const [pending, setPending] = useState<StageAction | null>(null);
  const actions = stageActions(status);
  const guard = stageGuard(status);
  return (
    <div className="mt-2.5" onClick={(e) => e.stopPropagation()}>
      {pending ? (
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[0.7rem] hc-soft">{pending.confirm}</span>
          <button type="button" disabled={busy} onClick={() => { onAct(pending); setPending(null); }} className="inline-flex min-h-7 items-center rounded-full border border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] px-2.5 text-xs text-[var(--hc-accent-text)] disabled:opacity-40">{busy ? "…" : "Bestätigen"}</button>
          <button type="button" onClick={() => setPending(null)} className="inline-flex min-h-7 items-center rounded-full border border-[var(--hc-border-strong)] px-2.5 text-xs hc-soft">Abbrechen</button>
        </div>
      ) : actions.length ? (
        <div className="flex flex-wrap items-center gap-1.5">
          {actions.map((action) => {
            const color = TONE_HEX[action.tone];
            return (
              <button key={action.key} type="button" disabled={busy} onClick={() => setPending(action)} style={{ borderColor: `${color}55`, color }}
                className={cn("inline-flex min-h-7 items-center gap-1 rounded-full border px-2.5 text-xs font-medium transition disabled:opacity-40", action.intent === "danger" ? "hover:bg-red-500/10" : "hover:bg-white/5")}>
                {action.label}{action.intent === "advance" ? <ArrowRight className="h-3 w-3" /> : null}
              </button>
            );
          })}
        </div>
      ) : guard ? (
        <p className="flex items-center gap-1.5 text-[0.68rem] hc-dim"><Lock className="h-3 w-3" />{guard}</p>
      ) : null}
      {error ? <p className="mt-1.5 flex items-start gap-1 text-[0.7rem] text-red-300"><AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />{error}</p> : null}
    </div>
  );
}

function FlowRunCard({ task, enriched, selected, busy, error, now, onSelect, onAct }: {
  task: BoardTask; enriched: Enriched; selected: boolean; busy: boolean; error?: string; now: number;
  onSelect: (id: string) => void; onAct: (action: StageAction) => void;
}) {
  const role = roleChip(enriched.workerProfile ?? task.assignee, task.status === "review" ? "verification" : null);
  const isBlocked = task.status === "blocked";
  const isReview = task.status === "review";
  const isDone = task.status === "done";
  const ageSec = task.age?.created_age_seconds ?? null;
  return (
    <article
      role="button"
      tabIndex={0}
      aria-pressed={selected}
      onClick={() => onSelect(task.id)}
      onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onSelect(task.id); } }}
      className={cn("cursor-pointer rounded-lg border p-2.5 transition", selected ? "border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)]" : "border-[var(--hc-border)] bg-[var(--hc-panel)] hover:border-[var(--hc-border-strong)]", isBlocked && "border-red-500/40")}
    >
      <div className="flex items-center gap-2">
        <span className="hc-mono text-[0.7rem] hc-dim">{task.id}</span>
        <span className="ml-auto"><RoleChip role={role} /></span>
      </div>
      <p className="mt-1.5 line-clamp-2 text-sm font-semibold leading-snug text-white">{task.title}</p>
      <p className="mt-1 hc-mono text-[0.68rem] hc-dim">
        {ageSec != null ? `⏱ vor ${fmtAge(now - ageSec, now)}` : ""}{task.branch_name ? ` · ${task.branch_name}` : ""}
        {enriched.workerHeartbeat ? ` · ♥ ${fmtAge(enriched.workerHeartbeat, now)}` : ""}
      </p>
      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        <StatusPill tone={isBlocked ? "red" : isDone ? "emerald" : isReview ? "amber" : task.status === "running" ? "cyan" : "zinc"} label={taskStatusLabel[task.status] ?? task.status} dot={task.status === "running" ? "live" : isBlocked ? "error" : isDone ? "ready" : isReview ? "warn" : "idle"} />
        {task.priority >= 2 ? <span className="rounded-full border border-rose-400/30 bg-rose-400/10 px-2 py-0.5 text-[0.7rem] text-rose-200">High</span> : null}
        {enriched.verdict ? <span className="rounded-full border border-cyan-400/30 bg-cyan-400/10 px-2 py-0.5 text-[0.7rem] text-cyan-100">{enriched.verdict}</span> : null}
        {isDone && enriched.resultQualityLabel ? <span className="rounded-full border border-emerald-400/30 bg-emerald-400/10 px-2 py-0.5 text-[0.7rem] text-emerald-100">{enriched.resultQualityLabel}</span> : null}
      </div>
      {task.latest_summary ? <p className="mt-2 line-clamp-2 text-xs hc-soft">{task.latest_summary}</p> : null}
      {isBlocked && enriched.blockedReason ? (
        <p className="mt-2 flex items-start gap-1.5 rounded-md border border-red-500/30 bg-red-500/10 px-2 py-1 text-[0.7rem] text-red-200"><Lock className="mt-0.5 h-3 w-3 shrink-0" />{enriched.blockedReason}</p>
      ) : null}
      {isReview ? (
        <p className="mt-2 flex items-center gap-1.5 text-[0.68rem] hc-dim"><ShieldCheck className="h-3 w-3 text-cyan-300" />Verifier-Gate — Ship nimmt ab, Rework schickt zurück.</p>
      ) : null}
      {enriched.deliverableCount ? <p className="mt-1.5 text-[0.68rem] text-emerald-300">{enriched.deliverableCount} Deliverable{enriched.deliverableCount === 1 ? "" : "s"}</p> : null}
      <FlowCardActions status={task.status} busy={busy} error={error} onAct={onAct} />
    </article>
  );
}

function FlowReceiptRail({ taskId, task, detail, loading, error, now }: {
  taskId: string | null; task?: BoardTask; detail?: TaskDetailResponse; loading: boolean; error?: string; now: number;
}) {
  if (!taskId) {
    return (
      <aside className="hc-surface-card h-fit p-4 xl:sticky xl:top-4">
        <Eyebrow>{de.flow.selectedChain}</Eyebrow>
        <p className="mt-3 text-sm hc-soft">{de.flow.noSelection}</p>
      </aside>
    );
  }
  const runs = detail?.runs ?? [];
  const events = (detail?.events ?? []).slice(-12).reverse();
  const deliverables = detail?.deliverables ?? [];
  const empty = !loading && !error && runs.length === 0 && events.length === 0 && deliverables.length === 0;
  return (
    <aside className="hc-surface-card h-fit p-4 xl:sticky xl:top-4">
      <Eyebrow>{de.flow.selectedChain}</Eyebrow>
      <div className="mt-2">
        <p className="hc-mono text-[0.7rem] hc-dim">{taskId}</p>
        <p className="mt-1 text-sm font-semibold text-white">{detail?.task?.title ?? task?.title ?? ""}</p>
        {task ? (
          <div className="mt-2 flex flex-wrap items-center gap-1.5">
            <RoleChip role={roleChip(task.assignee, task.status === "review" ? "verification" : null)} />
            <StatusPill tone={task.status === "done" ? "emerald" : task.status === "blocked" ? "red" : "zinc"} label={taskStatusLabel[task.status] ?? task.status} />
          </div>
        ) : null}
      </div>

      {error ? <div className="mt-3"><ToneCallout tone="red">{error}</ToneCallout></div> : null}
      {loading && !detail ? <div className="mt-3"><SkeletonCard rows={3} /></div> : null}
      {empty ? <div className="mt-3"><FleetEmptyState title={de.flow.noReceipts} desc={de.flow.noReceiptsDesc} /></div> : null}

      {runs.length ? (
        <div className="mt-4">
          <Eyebrow>{de.flow.receipts}</Eyebrow>
          <div className="mt-2 space-y-2">
            {runs.map((run) => {
              const role = roleChip(run.profile, run.run_role);
              const ok = run.outcome === "completed";
              return (
                <div key={run.id} className="rounded-lg border border-[var(--hc-border)] bg-[var(--hc-panel)] p-2.5">
                  <div className="flex items-center gap-2">
                    <RoleChip role={role} />
                    <span className="ml-auto inline-flex items-center gap-1 text-[0.7rem]" style={{ color: ok ? "#4ade80" : run.error ? "#fb2c36" : "#9ca3af" }}>
                      <span className="h-1.5 w-1.5 rounded-full" style={{ background: ok ? "#4ade80" : run.error ? "#fb2c36" : "#9ca3af" }} />{run.outcome ?? run.status}
                    </span>
                  </div>
                  <p className="mt-1 hc-mono text-[0.68rem] hc-dim">Run {run.id} · {run.run_role_label ?? profileLabel[run.profile ?? ""] ?? run.profile ?? "—"}{run.ended_at ? ` · vor ${fmtAge(run.ended_at, now)}` : run.started_at ? ` · seit ${fmtAge(run.started_at, now)}` : ""}</p>
                  {run.summary ? <p className="mt-1 line-clamp-3 text-[0.78rem] text-zinc-100">{run.summary}</p> : null}
                  {run.error ? <p className="mt-1 line-clamp-2 text-[0.72rem] text-red-300">{run.error}</p> : null}
                </div>
              );
            })}
          </div>
        </div>
      ) : null}

      {deliverables.length ? (
        <div className="mt-4">
          <Eyebrow>Deliverables</Eyebrow>
          <ul className="mt-2 space-y-1.5">
            {deliverables.map((d) => (
              <li key={d.relative_path} className="flex items-center justify-between gap-2 rounded-md border border-emerald-400/20 bg-emerald-500/[.06] px-2.5 py-1.5">
                <span className="min-w-0 truncate text-[0.78rem] text-white">{d.relative_path}</span>
                <a className="shrink-0 text-[0.7rem] text-emerald-200 hover:text-emerald-100" href={d.url} target="_blank" rel="noreferrer">öffnen</a>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {events.length ? (
        <div className="mt-4">
          <Eyebrow>{de.flow.liveFlow}</Eyebrow>
          <div className="mt-2 space-y-2">
            {events.map((ev, i) => (
              <div key={ev.id} className="flex gap-2.5">
                <span className={cn("mt-1 h-2 w-2 shrink-0 rounded-full", i === 0 && "animate-pulse")} style={{ background: i === 0 ? "#4ade80" : "#52525b" }} />
                <div className="min-w-0">
                  <p className="text-[0.82rem] text-white">{eventLabel(ev.kind)}</p>
                  <p className="text-[0.68rem] hc-dim">vor {fmtAge(ev.created_at, now)}</p>
                </div>
              </div>
            ))}
          </div>
        </div>
      ) : null}
    </aside>
  );
}

export function FlowView() {
  const board = useBoard();
  const workers = useHermesWorkers();
  const reviews = useHermesReviewVerdicts();
  const results = useHermesRecentResults();
  const blocked = useHermesBlockedCompletions();
  const { run: runAction, busyId, errorById } = useTaskAction(board.reload);
  const taskDetail = useTaskDetail();
  const [fallbackNow] = useState(() => Math.floor(Date.now() / 1000));
  const now = board.data?.now ?? fallbackNow;
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const allTasks: BoardTask[] = useMemo(() => board.data?.columns.flatMap((c) => c.tasks) ?? [], [board.data]);
  const columns = useMemo(() => groupByStage(allTasks), [allTasks]);

  const enrichmentById = useMemo(() => {
    const map: Record<string, Enriched> = {};
    for (const w of workers.data?.workers ?? []) {
      map[w.task_id] = { ...map[w.task_id], workerProfile: w.profile, workerHeartbeat: w.last_heartbeat_at || null };
    }
    for (const r of reviews.data?.reviews ?? []) {
      map[r.task_id] = { ...map[r.task_id], verdict: r.verifier_verdict, verifierEvidenceCount: r.verifier_evidence?.length ?? 0 };
    }
    for (const b of blocked.data?.blocked ?? []) {
      map[b.task_id] = { ...map[b.task_id], blockedKind: b.kind, blockedReason: b.fix_summary || b.summary_preview || null };
    }
    for (const res of results.data?.results ?? []) {
      map[res.task_id] = { ...map[res.task_id], resultQualityLabel: res.result_quality?.label ?? null, resultQualityTone: res.result_quality?.tone ?? null, deliverableCount: res.deliverables?.length ?? 0 };
    }
    return map;
  }, [workers.data, reviews.data, blocked.data, results.data]);

  const selectTask = (id: string) => {
    setSelectedId(id);
    if (!taskDetail.detailById[id]) void taskDetail.fetch(id);
  };
  const onAct = (taskId: string, action: StageAction) => {
    void runAction(taskId, action.target, action.key === "rework" ? { block_reason: "Operator-Rework aus dem Flow-Board" } : undefined);
    if (selectedId === taskId) void taskDetail.fetch(taskId);
  };

  const counts = useMemo(() => ({
    running: columns.execute.filter((t) => t.status === "running").length,
    plan: columns.plan.length,
    review: columns.verify.length,
    total: allTasks.filter((t) => t.status !== "done" && t.status !== "archived").length,
  }), [columns, allTasks]);

  const selectedTask = selectedId ? allTasks.find((t) => t.id === selectedId) : undefined;
  const loadingFirst = board.loading && board.data == null;
  const hasAnyRun = allTasks.length > 0;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <Eyebrow>{de.flow.eyebrow}</Eyebrow>
          <h1 className="hc-type-title mt-1 text-white">{de.flow.title}</h1>
          <Text variant="body" className="mt-1 max-w-2xl hc-soft">{de.flow.subtitle}</Text>
        </div>
        <div className="grid grid-cols-3 gap-2">
          <FleetPod label={de.flow.runsActive} dot="live" value={loadingFirst ? "—" : counts.running} />
          <FleetPod label={de.flow.wip} value={loadingFirst ? "—" : counts.total} />
          <FleetPod label={de.flow.review} dot="warn" value={loadingFirst ? "—" : counts.review} />
        </div>
      </div>

      {board.error ? <ToneCallout tone="red">{de.flow.loadError}<br />{board.error}</ToneCallout> : null}

      {loadingFirst ? (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3"><SkeletonCard rows={4} /><SkeletonCard rows={4} /><SkeletonCard rows={4} /></div>
      ) : !hasAnyRun ? (
        <FleetEmptyState title={de.flow.emptyTitle} desc={de.flow.emptyDesc} />
      ) : (
        <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_340px]">
          <div className="overflow-x-auto pb-2">
            <div className="flex min-w-max gap-3">
              {FLEET_STAGES.map((stage, idx) => {
                const meta = STAGE_META[stage];
                const items = columns[stage];
                const hex = TONE_HEX[meta.tone];
                const shown = items.slice(0, MAX_CARDS);
                const fill = items.length ? Math.min(1, items.length / 6) : 0;
                return (
                  <section key={stage} className="flex w-[15.5rem] shrink-0 flex-col">
                    <div className="flex items-center gap-2">
                      <span className="hc-mono text-[0.7rem] hc-dim">{String(idx + 1).padStart(2, "0")}</span>
                      <span className="text-xs font-semibold uppercase tracking-wide text-white">{meta.label}</span>
                      <span className="ml-auto hc-mono rounded-full border border-[var(--hc-border)] px-1.5 text-[0.7rem] hc-soft">{items.length}</span>
                    </div>
                    <p className="mt-1 text-[0.7rem] hc-dim">{meta.purpose}</p>
                    <div className="hc-stage-rail mt-1.5" style={{ "--hc-role": hex } as React.CSSProperties}><i style={{ width: `${fill * 100}%` }} /></div>
                    <div className="mt-2.5 flex flex-col gap-2">
                      {items.length === 0 ? (
                        <p className="rounded-lg border border-dashed border-[var(--hc-border)] px-2.5 py-4 text-center text-[0.7rem] hc-dim">{de.flow.stageEmpty}</p>
                      ) : (
                        <>
                          {shown.map((task) => (
                            <FlowRunCard key={task.id} task={task} enriched={enrichmentById[task.id] ?? {}} selected={task.id === selectedId} busy={busyId === task.id} error={errorById[task.id] || undefined} now={now} onSelect={selectTask} onAct={(a) => onAct(task.id, a)} />
                          ))}
                          {items.length > shown.length ? <p className="px-1 py-1 text-center text-[0.68rem] hc-dim">+ {items.length - shown.length} weitere</p> : null}
                        </>
                      )}
                    </div>
                  </section>
                );
              })}
            </div>
          </div>
          <FlowReceiptRail taskId={selectedId} task={selectedTask} detail={selectedId ? taskDetail.detailById[selectedId] : undefined} loading={taskDetail.loadingId === selectedId} error={selectedId ? taskDetail.errorById[selectedId] : undefined} now={now} />
        </div>
      )}
    </div>
  );
}
