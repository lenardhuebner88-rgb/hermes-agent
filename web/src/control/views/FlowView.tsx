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
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, ArrowRight, FileText, Lock, Play, RefreshCw, ShieldCheck } from "lucide-react";
import { cn } from "@/lib/utils";
import { de } from "../i18n/de";
import { TONE_HEX, profileLabel, taskStatusLabel } from "../lib/tones";
import { fmtAge, freshness } from "../lib/derive";
import { FLEET_STAGES, STAGE_META, flowCounts, groupByStage, roleChip, stageActions, stageGuard, type StageAction } from "../lib/fleet";
import { getFlowSubtaskStatusExplanation } from "../lib/flowStatus";
import { getHeldFlowDispatchGuard, type HeldFlowDispatchGuard } from "../lib/flowDispatchGuard";
import {
  useBoard,
  useFlowRelease,
  useHermesBlockedCompletions,
  useHermesRecentResults,
  useHermesReviewVerdicts,
  useHermesWorkers,
  useTaskAction,
  useTaskDetail,
} from "../hooks/useControlData";
import type { BoardTask, TaskStatus } from "../lib/types";
import type { TaskDetailResponse } from "../lib/schemas";
import { StatusPill, ToneCallout } from "../components/atoms";
import { Eyebrow, SkeletonCard, Text } from "../components/primitives";
import { FleetPod, FleetEmptyState } from "../components/fleet/atoms";
import { RoleChip } from "../components/fleet/atoms";
import { FlowCapture } from "../components/fleet/FlowCapture";

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
  decomposed: "In Subtasks zerlegt", specified: "Spezifiziert", linked: "Verknüpft",
  commented: "Kommentiert", flow_plan: "Plan-Spec geschrieben",
};
function eventLabel(kind: string): string {
  return EVENT_LABEL[kind] ?? kind.replace(/_/g, " ");
}

function statusTone(s: TaskStatus) {
  return s === "done" ? "emerald" : s === "blocked" ? "red" : s === "review" ? "amber" : s === "running" ? "cyan" : s === "scheduled" ? "violet" : "zinc";
}

interface FlowDispatchChoice extends HeldFlowDispatchGuard {
  taskId: string;
}

function FlowCardActions({ status, busy, error, dispatchChoice, onReleaseChain, onDispatchSingle, onCancelDispatchChoice, onAct }: {
  status: BoardTask["status"]; busy: boolean; error?: string; dispatchChoice?: FlowDispatchChoice | null;
  onReleaseChain?: () => void; onDispatchSingle?: () => void; onCancelDispatchChoice?: () => void; onAct: (action: StageAction) => void;
}) {
  const [pending, setPending] = useState<StageAction | null>(null);
  const actions = stageActions(status);
  const guard = stageGuard(status);
  return (
    <div className="mt-2.5" onClick={(e) => e.stopPropagation()}>
      {dispatchChoice ? (
        <div className="rounded-md border border-emerald-400/30 bg-emerald-400/10 p-2">
          <p className="text-[0.72rem] font-medium text-emerald-100">{de.flow.singleDispatch.prompt}</p>
          <p className="mt-1 text-[0.68rem] hc-soft">{de.flow.singleDispatch.heldSiblings(dispatchChoice.heldSiblingIds.length)}</p>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <button type="button" disabled={busy} onClick={onReleaseChain} className="inline-flex min-h-11 sm:min-h-8 items-center rounded-full border border-emerald-300/50 bg-emerald-300/15 px-3 text-xs font-semibold text-emerald-100 disabled:opacity-40">
              {busy ? de.flow.plan.releaseBusy : de.flow.singleDispatch.startChain}
            </button>
            <button type="button" disabled={busy} onClick={onDispatchSingle} className="inline-flex min-h-11 sm:min-h-8 items-center rounded-full border border-amber-300/40 bg-amber-300/10 px-3 text-xs text-amber-100 disabled:opacity-40">
              {de.flow.singleDispatch.startSingle}
            </button>
            <button type="button" onClick={onCancelDispatchChoice} className="inline-flex min-h-11 sm:min-h-8 items-center rounded-full border border-[var(--hc-border-strong)] px-3 text-xs hc-soft">{de.flow.singleDispatch.cancel}</button>
          </div>
        </div>
      ) : pending ? (
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[0.7rem] hc-soft">{pending.confirm}</span>
          <button type="button" disabled={busy} onClick={() => { onAct(pending); setPending(null); }} className="inline-flex min-h-11 sm:min-h-7 items-center rounded-full border border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] px-2.5 text-xs text-[var(--hc-accent-text)] disabled:opacity-40">{busy ? "…" : "Bestätigen"}</button>
          <button type="button" onClick={() => setPending(null)} className="inline-flex min-h-11 sm:min-h-7 items-center rounded-full border border-[var(--hc-border-strong)] px-2.5 text-xs hc-soft">Abbrechen</button>
        </div>
      ) : actions.length ? (
        <div className="flex flex-wrap items-center gap-1.5">
          {actions.map((action) => {
            const color = TONE_HEX[action.tone];
            return (
              <button key={action.key} type="button" disabled={busy} onClick={() => setPending(action)} style={{ borderColor: `${color}55`, color }}
                className={cn("inline-flex min-h-11 sm:min-h-7 items-center gap-1 rounded-full border px-2.5 text-xs font-medium transition disabled:opacity-40", action.intent === "danger" ? "hover:bg-red-500/10" : "hover:bg-white/5")}>
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

function FlowRunCard({ task, enriched, selected, busy, error, now, dispatchChoice, onSelect, onReleaseChain, onDispatchSingle, onCancelDispatchChoice, onAct }: {
  task: BoardTask; enriched: Enriched; selected: boolean; busy: boolean; error?: string; now: number; dispatchChoice?: FlowDispatchChoice | null;
  onSelect: (id: string) => void; onReleaseChain?: () => void; onDispatchSingle?: () => void; onCancelDispatchChoice?: () => void; onAct: (action: StageAction) => void;
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
        {task.progress && task.progress.total > 0 ? <span className="rounded-full border border-sky-400/30 bg-sky-400/10 px-2 py-0.5 text-[0.7rem] text-sky-100">{task.progress.done}/{task.progress.total} {de.flow.plan.subtasksHeading}</span> : null}
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
      <FlowCardActions
        status={task.status}
        busy={busy}
        error={error}
        dispatchChoice={dispatchChoice}
        onReleaseChain={onReleaseChain}
        onDispatchSingle={onDispatchSingle}
        onCancelDispatchChoice={onCancelDispatchChoice}
        onAct={onAct}
      />
    </article>
  );
}

// The Plan panel: when the selected task is a decompose root, surface its real
// subtask GROUP (resolved live from the board), the durable Vault plan-spec link
// (documented method), and a "Kette starten" release when subtasks are gate-held
// in `scheduled`. This is the visible+operative proof — the same child ids run
// on through Execute → Verify → Ship.
function FlowPlanPanel({ rootId, detail, boardTasks, onRelease, releaseBusy, releaseError, released }: {
  rootId: string; detail?: TaskDetailResponse; boardTasks: BoardTask[];
  onRelease: (rootId: string, n: number) => void; releaseBusy: boolean; releaseError?: string; released?: number;
}) {
  const [confirming, setConfirming] = useState(false);
  const events = detail?.events ?? [];
  const decomposed = [...events].reverse().find((e) => e.kind === "decomposed");
  const hasSpec = events.some((e) => e.kind === "flow_plan");
  const rawIds = (decomposed?.payload?.child_ids as unknown);
  const childIds: string[] = Array.isArray(rawIds) ? rawIds.filter((x): x is string => typeof x === "string") : [];
  if (!childIds.length && !hasSpec) return null;

  const byId = new Map(boardTasks.map((t) => [t.id, t]));
  const children = childIds.map((id) => byId.get(id)).filter((t): t is BoardTask => !!t);
  const heldCount = children.filter((c) => c.status === "scheduled").length;
  const specUrl = `/api/plugins/kanban/tasks/${encodeURIComponent(rootId)}/flow-plan`;

  return (
    <div className="mt-4 rounded-lg border border-[var(--hc-border)] bg-[var(--hc-panel)] p-3">
      <div className="flex items-center justify-between gap-2">
        <Eyebrow>{childIds.length ? de.flow.plan.decomposedInto(childIds.length) : de.flow.plan.subtasksHeading}</Eyebrow>
        {hasSpec ? (
          <a href={specUrl} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-[0.72rem] text-sky-200 hover:text-sky-100">
            <FileText className="h-3.5 w-3.5" />{de.flow.plan.openSpec}
          </a>
        ) : null}
      </div>

      {children.length ? (
        <ul className="mt-2 space-y-1.5">
          {children.map((c) => {
            const statusExplanation = getFlowSubtaskStatusExplanation(c.status, c.status === "blocked" ? c.latest_summary : null);
            return (
              <li key={c.id} className="flex flex-wrap items-center gap-2 rounded-md border border-[var(--hc-border)] px-2 py-1.5">
                <span className="hc-mono text-[0.66rem] hc-dim">{c.id}</span>
                <span className="min-w-0 flex-1 basis-36 truncate text-[0.78rem] text-white">{c.title}</span>
                <div className="ml-auto flex max-w-full flex-wrap items-center justify-end gap-1.5">
                  <StatusPill tone={statusTone(c.status)} label={taskStatusLabel[c.status] ?? c.status} />
                  <span className={cn("max-w-full text-[0.68rem] leading-snug hc-dim sm:max-w-[13rem] sm:text-right", c.status === "blocked" && "text-red-200")} title={statusExplanation}>
                    {statusExplanation}
                  </span>
                </div>
              </li>
            );
          })}
        </ul>
      ) : null}

      {heldCount > 0 ? (
        <div className="mt-2.5">
          {confirming ? (
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-[0.7rem] hc-soft">{de.flow.plan.releaseConfirm(heldCount)}</span>
              <button type="button" disabled={releaseBusy} onClick={() => { onRelease(rootId, heldCount); setConfirming(false); }} className="inline-flex min-h-9 items-center rounded-full border border-emerald-400/40 bg-emerald-400/10 px-3 text-xs text-emerald-100 disabled:opacity-40">{releaseBusy ? de.flow.plan.releaseBusy : de.flow.plan.releaseConfirmButton}</button>
              <button type="button" onClick={() => setConfirming(false)} className="inline-flex min-h-9 items-center rounded-full border border-[var(--hc-border-strong)] px-3 text-xs hc-soft">Abbrechen</button>
            </div>
          ) : (
            <button type="button" disabled={releaseBusy} onClick={() => setConfirming(true)} className="inline-flex min-h-9 items-center gap-1.5 rounded-full border border-emerald-400/40 bg-emerald-400/10 px-3 text-xs font-medium text-emerald-100 transition hover:brightness-110 disabled:opacity-40">
              <Play className="h-3.5 w-3.5" />{de.flow.plan.release} · {de.flow.plan.subtasksOf(heldCount)}
            </button>
          )}
          <p className="mt-1.5 flex items-center gap-1.5 text-[0.68rem] hc-dim"><Lock className="h-3 w-3" />{de.flow.plan.heldGate}</p>
          <p className="mt-1 text-[0.68rem] hc-dim">Kette starten gibt gehaltene Subtasks frei; Queue/Assignee und Dependencies entscheiden den tatsächlichen Start.</p>
        </div>
      ) : null}

      {released ? <p className="mt-1.5 text-[0.7rem] text-emerald-300">{de.flow.plan.released(released)}</p> : null}
      {releaseError ? <p className="mt-1.5 flex items-start gap-1 text-[0.7rem] text-red-300"><AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />{releaseError}</p> : null}
    </div>
  );
}

function FlowChainInsight({ task, detail, boardTasks, snapshotLabel }: { task?: BoardTask; detail?: TaskDetailResponse; boardTasks: BoardTask[]; snapshotLabel: string }) {
  const links = detail?.links;
  const parentIds = links?.parents ?? [];
  const childIds = links?.children ?? [];
  if (!links || (parentIds.length === 0 && childIds.length === 0)) return null;

  const byId = new Map(boardTasks.map((t) => [t.id, t]));
  const linkedTasks = [...parentIds, ...childIds].map((id) => byId.get(id)).filter((t): t is BoardTask => !!t);
  const parentTasks = parentIds.map((id) => byId.get(id)).filter((t): t is BoardTask => !!t);
  const childTasks = childIds.map((id) => byId.get(id)).filter((t): t is BoardTask => !!t);
  const heldTasks = linkedTasks.filter((t) => t.status === "scheduled");
  const readyTasks = linkedTasks.filter((t) => t.status === "ready");
  const runningTasks = linkedTasks.filter((t) => t.status === "running");
  const waitingDependents = childTasks.filter((t) => ["triage", "todo", "scheduled", "blocked"].includes(t.status));
  const unknownIds = [...parentIds, ...childIds].filter((id) => !byId.has(id));
  const unknownPredecessorIds = parentIds.filter((id) => !byId.has(id));
  const possibleActivePredecessors = parentTasks.filter((t) => !["done", "archived"].includes(t.status));
  const hasAmbiguousTodo = task?.status === "todo" || linkedTasks.some((t) => t.status === "todo");
  const parallelNote = childIds.length > 1
    ? `Direkte Verknüpfungen: ${childIds.length} mögliche Nachfolger im Detail-Link; Ausführung bleibt je Assignee serialisiert.`
    : parentIds.length > 1
      ? `Direkte Verknüpfungen: ${parentIds.length} mögliche Vorgänger im Detail-Link; der Snapshot belegt keine Blockierungsursache.`
      : "Direkte Verknüpfungen: 1-Hop-Vorgänger/Nachfolger aus dem Task-Detail.";
  const taskLine = (t: BoardTask) => `${t.id} · ${taskStatusLabel[t.status] ?? t.status}${t.assignee ? ` · ${t.assignee}` : ""}`;
  const predecessorHintLine = parentIds.length === 0
    ? "Keine möglichen Vorgänger im Detail-Link."
    : unknownPredecessorIds.length
      ? `Unklar: mögliche Vorgänger fehlen im Board-Snapshot (${unknownPredecessorIds.join(", ")}).`
      : possibleActivePredecessors.length
        ? `Mögliche aktive Vorgänger im Snapshot: ${possibleActivePredecessors.map(taskLine).join(" · ")}`
        : "Mögliche Vorgänger sind done/archived; Ursache eines Wartestatus bleibt im Snapshot nicht eindeutig.";

  return (
    <div className="mt-4 rounded-lg border border-sky-400/25 bg-sky-500/[.06] p-3">
      <div className="flex flex-wrap items-center gap-2">
        <Eyebrow>Ketten-Kontext</Eyebrow>
        <span className="rounded-full border border-[var(--hc-border)] px-2 py-0.5 text-[0.66rem] hc-dim">Snapshot · read-only</span>
        <span className="rounded-full border border-[var(--hc-border)] px-2 py-0.5 text-[0.66rem] hc-dim">Snapshot-Alter: {snapshotLabel}</span>
      </div>
      <p className="mt-1.5 text-[0.72rem] hc-dim">{parallelNote}</p>
      <div className="mt-2 grid gap-2">
        <div className="rounded-md border border-[var(--hc-border)] bg-[var(--hc-panel)] px-2.5 py-2">
          <p className="text-[0.68rem] font-semibold uppercase tracking-wide text-sky-100">Gehalten</p>
          <p className="mt-1 text-[0.75rem] hc-soft">{heldTasks.length ? heldTasks.map(taskLine).join(" · ") : "Keine gehaltenen direkten Nachbarn im Snapshot."}</p>
        </div>
        <div className="rounded-md border border-[var(--hc-border)] bg-[var(--hc-panel)] px-2.5 py-2">
          <p className="text-[0.68rem] font-semibold uppercase tracking-wide text-emerald-100">Ready-Nachbar im Snapshot</p>
          <p className="mt-1 text-[0.75rem] hc-soft">{readyTasks.length ? readyTasks.map(taskLine).join(" · ") : "Kein ready-Nachbar im Snapshot; keine Scheduler-Zusage."}</p>
        </div>
        <div className="rounded-md border border-[var(--hc-border)] bg-[var(--hc-panel)] px-2.5 py-2">
          <p className="text-[0.68rem] font-semibold uppercase tracking-wide text-cyan-100">Läuft bereits</p>
          <p className="mt-1 text-[0.75rem] hc-soft">{runningTasks.length ? runningTasks.map(taskLine).join(" · ") : "Kein direkter Nachbar läuft im Snapshot."}</p>
        </div>
        <div className="rounded-md border border-[var(--hc-border)] bg-[var(--hc-panel)] px-2.5 py-2">
          <p className="text-[0.68rem] font-semibold uppercase tracking-wide text-amber-100">Mögliche Vorgänger</p>
          <p className="mt-1 text-[0.75rem] hc-soft">{predecessorHintLine}</p>
        </div>
        <div className="rounded-md border border-[var(--hc-border)] bg-[var(--hc-panel)] px-2.5 py-2">
          <p className="text-[0.68rem] font-semibold uppercase tracking-wide text-amber-100">Mögliche Nachfolger mit Wartestatus</p>
          <p className="mt-1 text-[0.75rem] hc-soft">{waitingDependents.length ? waitingDependents.map(taskLine).join(" · ") : "Keine möglichen Nachfolger mit Wartestatus im Snapshot."}</p>
        </div>
      </div>
      <div className="mt-2 flex flex-wrap gap-1.5">
        {parentTasks.map((p) => <StatusPill key={`p-${p.id}`} tone={statusTone(p.status)} label={`Möglicher Vorgänger ${taskStatusLabel[p.status] ?? p.status}`} />)}
        {childTasks.map((c) => <StatusPill key={`c-${c.id}`} tone={statusTone(c.status)} label={`Möglicher Nachfolger ${taskStatusLabel[c.status] ?? c.status}`} />)}
      </div>
      {unknownIds.length ? <p className="mt-2 text-[0.68rem] hc-dim">Nicht im Board-Snapshot: {unknownIds.join(", ")}</p> : null}
      {parentIds.length ? <p className="mt-2 text-[0.68rem] hc-dim">Snapshot-Hinweis: rohe Detail-Links zeigen Nähe, aber keinen sicheren Blockierungsgrund.</p> : null}
      {hasAmbiguousTodo ? <p className="mt-2 text-[0.68rem] text-amber-200">Hinweis: todo ist uneindeutig; es kann Dependency-Warten, manuelles Backlog oder Dispatcher-Queue sein.</p> : null}
    </div>
  );
}

function FlowReceiptRail({ taskId, task, detail, loading, error, now, boardTasks, snapshotLabel, onRelease, releaseBusy, releaseError, released }: {
  taskId: string | null; task?: BoardTask; detail?: TaskDetailResponse; loading: boolean; error?: string; now: number;
  boardTasks: BoardTask[]; snapshotLabel: string; onRelease: (rootId: string, n: number) => void; releaseBusy: boolean; releaseError?: string; released?: number;
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

      {taskId ? (
        <FlowPlanPanel
          rootId={taskId}
          detail={detail}
          boardTasks={boardTasks}
          onRelease={onRelease}
          releaseBusy={releaseBusy}
          releaseError={releaseError}
          released={released}
        />
      ) : null}

      <FlowChainInsight task={task} detail={detail} boardTasks={boardTasks} snapshotLabel={snapshotLabel} />

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
  const flowRelease = useFlowRelease(board.reload);
  const [releasedById, setReleasedById] = useState<Record<string, number>>({});
  const [fallbackNow] = useState(() => Math.floor(Date.now() / 1000));
  const now = board.data?.now ?? fallbackNow;
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [dispatchChoice, setDispatchChoice] = useState<FlowDispatchChoice | null>(null);
  const [checkingDispatchId, setCheckingDispatchId] = useState<string | null>(null);

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

  const railRef = useRef<HTMLDivElement>(null);
  const reloadBoard = board.reload;
  const fetchDetail = taskDetail.fetch;

  const selectTask = (id: string) => {
    setSelectedId(id);
    if (!taskDetail.detailById[id]) void fetchDetail(id);
    // On mobile/tablet the receipt rail stacks below the board (xl breakpoint),
    // so a tap would otherwise change something off-screen — bring it into view.
    if (typeof window !== "undefined" && window.matchMedia("(max-width: 1279px)").matches) {
      window.setTimeout(() => railRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }), 60);
    }
  };
  const runTaskAction = useCallback((taskId: string, action: StageAction) => {
    void runAction(taskId, action.target, action.key === "rework" ? { block_reason: "Operator-Rework aus dem Flow-Board" } : undefined);
    if (selectedId === taskId) void fetchDetail(taskId);
  }, [runAction, selectedId, fetchDetail]);
  const onAct = (task: BoardTask, action: StageAction) => {
    if (action.key !== "dispatch") {
      runTaskAction(task.id, action);
      return;
    }

    setSelectedId(task.id);
    setDispatchChoice(null);
    setCheckingDispatchId(task.id);
    void (async () => {
      const detail = taskDetail.detailById[task.id] ?? await fetchDetail(task.id);
      const rootId = detail?.events
        .map((event) => event.payload?.from_decompose_of)
        .find((value): value is string => typeof value === "string" && !!value.trim()) ?? null;
      const rootDetail = rootId ? (taskDetail.detailById[rootId] ?? await fetchDetail(rootId)) : null;
      const guard = getHeldFlowDispatchGuard(task, detail, rootDetail, allTasks);
      if (guard) {
        setDispatchChoice({ taskId: task.id, ...guard });
      } else {
        runTaskAction(task.id, action);
      }
      setCheckingDispatchId(null);
    })();
  };
  // Release a gated plan: unblock the held subtasks, then refresh the board +
  // this root's detail so the held banner clears and the children move on.
  const onReleasePlan = useCallback((rootId: string, n: number) => {
    void flowRelease.release(rootId).then((res) => {
      if (res.ok) {
        setReleasedById((prev) => ({ ...prev, [rootId]: res.released ?? n }));
        void fetchDetail(rootId);
        if (selectedId && selectedId !== rootId) void fetchDetail(selectedId);
      }
    });
  }, [flowRelease, fetchDetail, selectedId]);
  const onReleaseChain = useCallback(() => {
    if (!dispatchChoice) return;
    onReleasePlan(dispatchChoice.rootId, dispatchChoice.heldSiblingIds.length + 1);
    setDispatchChoice(null);
  }, [dispatchChoice, onReleasePlan]);
  const onDispatchSingle = useCallback(() => {
    if (!dispatchChoice) return;
    const dispatchAction = stageActions("scheduled").find((action) => action.key === "dispatch");
    if (dispatchAction) runTaskAction(dispatchChoice.taskId, dispatchAction);
    setDispatchChoice(null);
  }, [dispatchChoice, runTaskAction]);
  // A freshly captured task: select it so its receipt rail is ready, and pull the
  // board now so the new card appears without waiting for the next poll.
  const onCaptured = useCallback((taskId: string) => {
    setSelectedId(taskId);
    void reloadBoard();
    void fetchDetail(taskId);
  }, [reloadBoard, fetchDetail]);

  const counts = useMemo(() => flowCounts(allTasks), [allTasks]);
  const fresh = freshness(board.lastUpdated, 8000, now);

  const selectedTask = selectedId ? allTasks.find((t) => t.id === selectedId) : undefined;
  const selectedStatus = selectedTask?.status;
  const loadingFirst = board.loading && board.data == null;
  const hasAnyRun = allTasks.length > 0;

  // Keep the receipt rail live while a non-terminal task is selected: re-fetch
  // its detail every 8s so runs/events/the verifier verdict stream in during a
  // run. Pauses when the tab is hidden (same contract as usePolling).
  useEffect(() => {
    if (!selectedId || selectedStatus === "done" || selectedStatus === "archived") return;
    const handle = window.setInterval(() => {
      if (!document.hidden) void fetchDetail(selectedId);
    }, 8000);
    return () => window.clearInterval(handle);
  }, [selectedId, selectedStatus, fetchDetail]);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <Eyebrow>{de.flow.eyebrow}</Eyebrow>
          <h1 className="hc-type-title mt-1 text-white">{de.flow.title}</h1>
          <Text variant="body" className="mt-1 max-w-2xl hc-soft">{de.flow.subtitle}</Text>
        </div>
        <div className="flex flex-col items-stretch gap-2 sm:items-end">
          <div className="flex items-center gap-2 self-end">
            <span className="flex items-center gap-1.5 text-[0.68rem] hc-dim" title={fresh.stale ? de.flow.paused : undefined}>
              <span className={cn("h-1.5 w-1.5 rounded-full", fresh.stale ? "bg-amber-400" : "bg-emerald-400 animate-pulse")} />
              {board.lastUpdated ? (fresh.stale ? de.flow.paused : de.flow.updated(fresh.label.replace("vor ", ""))) : ""}
            </span>
            <button type="button" onClick={() => void board.reload()} aria-label={de.flow.refresh} className="inline-flex h-7 w-7 items-center justify-center rounded-full border border-[var(--hc-border)] hc-soft transition hover:border-[var(--hc-border-strong)]"><RefreshCw className="h-3.5 w-3.5" /></button>
            <FlowCapture onCreated={onCaptured} />
          </div>
          <div className={cn("grid gap-2", counts.blocked > 0 ? "grid-cols-2 sm:grid-cols-4" : "grid-cols-3")}>
            <FleetPod label={de.flow.runsActive} dot="live" value={loadingFirst ? "—" : counts.running} />
            <FleetPod label={de.flow.wip} value={loadingFirst ? "—" : counts.wip} />
            <FleetPod label={de.flow.review} dot="warn" value={loadingFirst ? "—" : counts.review} />
            {counts.blocked > 0 ? <FleetPod label={de.flow.rework} dot="error" value={loadingFirst ? "—" : counts.blocked} /> : null}
          </div>
        </div>
      </div>

      {board.error ? <ToneCallout tone="red">{de.flow.loadError}<br />{board.error}</ToneCallout> : null}

      {loadingFirst ? (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3"><SkeletonCard rows={4} /><SkeletonCard rows={4} /><SkeletonCard rows={4} /></div>
      ) : !hasAnyRun ? (
        <FleetEmptyState title={de.flow.emptyTitle} desc={de.flow.emptyDesc} />
      ) : (
        <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_340px]">
          <div className="snap-x overflow-x-auto pb-2">
            <div className="flex min-w-max gap-3">
              {FLEET_STAGES.map((stage, idx) => {
                const meta = STAGE_META[stage];
                const items = columns[stage];
                const hex = TONE_HEX[meta.tone];
                const shown = items.slice(0, MAX_CARDS);
                const fill = items.length ? Math.min(1, items.length / 6) : 0;
                return (
                  <section key={stage} className="flex w-[15.5rem] shrink-0 snap-start flex-col">
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
                          {shown.map((task) => {
                            const taskDispatchChoice = dispatchChoice?.taskId === task.id ? dispatchChoice : null;
                            const isBusy = busyId === task.id || checkingDispatchId === task.id || (taskDispatchChoice != null && flowRelease.busyId === taskDispatchChoice.rootId);
                            return (
                              <FlowRunCard
                                key={task.id}
                                task={task}
                                enriched={enrichmentById[task.id] ?? {}}
                                selected={task.id === selectedId}
                                busy={isBusy}
                                error={errorById[task.id] || undefined}
                                now={now}
                                dispatchChoice={taskDispatchChoice}
                                onSelect={selectTask}
                                onReleaseChain={onReleaseChain}
                                onDispatchSingle={onDispatchSingle}
                                onCancelDispatchChoice={() => setDispatchChoice(null)}
                                onAct={(a) => onAct(task, a)}
                              />
                            );
                          })}
                          {items.length > shown.length ? <p className="px-1 py-1 text-center text-[0.68rem] hc-dim">+ {items.length - shown.length} weitere</p> : null}
                        </>
                      )}
                    </div>
                  </section>
                );
              })}
            </div>
          </div>
          <div ref={railRef} className="scroll-mt-4">
            <FlowReceiptRail
              taskId={selectedId}
              task={selectedTask}
              detail={selectedId ? taskDetail.detailById[selectedId] : undefined}
              loading={taskDetail.loadingId === selectedId}
              error={selectedId ? taskDetail.errorById[selectedId] : undefined}
              now={now}
              boardTasks={allTasks}
              snapshotLabel={board.lastUpdated ? (fresh.stale ? de.flow.paused : fresh.label) : "unbekannt"}
              onRelease={onReleasePlan}
              releaseBusy={flowRelease.busyId === selectedId}
              releaseError={selectedId ? flowRelease.errorById[selectedId] : undefined}
              released={selectedId ? releasedById[selectedId] : undefined}
            />
          </div>
        </div>
      )}
    </div>
  );
}
