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
import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, ArrowRight, ChevronDown, ChevronRight, Copy, FileText, Lock, Play, RefreshCw, ShieldCheck, X } from "lucide-react";
import { useSearchParams } from "react-router-dom";
import { cn } from "@/lib/utils";
import { fetchJSON, openAuthedApiFile } from "@/lib/api";
import { de } from "../i18n/de";
import { TONE_HEX, profileLabel, taskStatusLabel } from "../lib/tones";
import { fmtAge, fmtDur, fmtTokens, freshness, workerHealth, workerSortRank } from "../lib/derive";
import {
  FLEET_STAGES,
  STAGE_META,
  buildChains,
  flowCounts,
  groupChainsByEpic,
  projectKey,
  projectLabel,
  projectOptions,
  roleChip,
  stageActions,
  stageGuard,
  type ChainModel,
  type StageAction,
} from "../lib/fleet";
import { getFlowSubtaskStatusExplanation } from "../lib/flowStatus";
import { getHeldFlowDispatchGuard, type HeldFlowDispatchGuard } from "../lib/flowDispatchGuard";
import {
  useBoard,
  useEpicActions,
  useEpics,
  useFlowGate,
  useFlowRelease,
  useKanbanDecisionQueue,
  useHermesBlockedCompletions,
  useHermesRecentResults,
  useHermesReviewVerdicts,
  usePlanSpecs,
  useSystemHealth,
  useHermesWorkers,
  useRunInspect,
  useTaskAction,
  useTaskDetail,
} from "../hooks/useControlData";
import type { BoardTask, FlowGateReleaseLevel, FlowReleaseOptions, PlanSpecIngestResponse, PlanSpecPromptResponse, PlanSpecRecord, TaskArtifactLink, TaskDeliverable, TaskStatus } from "../lib/types";
import { isIsolatedWorkspace } from "../lib/types";
import type { Epic, TaskDetailResponse } from "../lib/schemas";
import { StaleBadge, StatusPill, ToneCallout } from "../components/atoms";
import { TriageStrip } from "../components/TriageStrip";
import { FunnelFreigaben } from "../components/FunnelFreigaben";
import { Hero } from "../components/Hero";
import { Eyebrow, SkeletonCard } from "../components/primitives";
import { FleetPod, FleetEmptyState, FleetPanel } from "../components/fleet/atoms";
import { RoleChip } from "../components/fleet/atoms";
import { EpicCreate } from "../components/fleet/EpicCreate";
import { FlowCapture } from "../components/fleet/FlowCapture";
import { WorkerCard, type WorkerActionKey } from "../components/WorkerCard";
import { useClientNowSeconds } from "../lib/clock";

const MAX_CARDS = 12;
const MAX_DELIVERED = 8;
const VERIFIER_GATE_TERMINAL_GRACE_MS = 60_000;

function flowTaskDomId(taskId: string): string {
  return `flow-task-${encodeURIComponent(taskId)}`;
}

function flowChainDomId(rootId: string): string {
  return `flow-chain-${encodeURIComponent(rootId)}`;
}

function scrollToFlowTask(taskId: string): void {
  window.setTimeout(() => {
    const target = document.getElementById(flowTaskDomId(taskId)) ?? document.getElementById(flowChainDomId(taskId));
    target?.scrollIntoView({ behavior: "smooth", block: "center" });
  }, 80);
}

// Enrichment for a board task, gathered from the live sidecar endpoints.
export interface Enriched {
  workerProfile?: string | null;
  workerHeartbeat?: number | null;
  verdict?: string | null;
  verifierEvidenceCount?: number;
  activeVerifier?: boolean;
  activeRunId?: string | null;
  reviewRunState?: "active" | "approved" | "request_changes" | "pending";
  reviewVerdictAt?: number | null;
  blockedKind?: string | null;
  blockedReason?: string | null;
  resultQualityLabel?: string | null;
  resultQualityTone?: string | null;
  deliverableCount?: number;
  resultArtifactLinks?: TaskArtifactLink[];
}

// A single frozen empty-enrichment object: cards with no sidecar data get this
// stable reference (not a fresh `{}` per render) so React.memo can skip them.
const EMPTY_ENRICHED: Enriched = Object.freeze({});

function recoveryAgeLabel(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  return `${Math.round(minutes / 60)}h`;
}

function RecoveryStrip() {
  const health = useSystemHealth();
  const decisions = useKanbanDecisionQueue();
  const dispatcher = health.data?.subsystems.kanban_dispatcher;
  const recoveryRows = (decisions.data?.decisions ?? []).filter((d) =>
    d.kind === "operator_escalation" ||
    d.kind === "integration_parked" ||
    d.kind === "rate_limited_loop",
  );
  const tone = dispatcher?.status === "healthy"
    ? "emerald"
    : dispatcher?.status === "degraded"
      ? "amber"
      : "red";

  if (!dispatcher && recoveryRows.length === 0 && !health.error && !decisions.error) {
    return null;
  }

  return (
    <FleetPanel
      eyebrow="Recovery"
      meta={`Dispatcher · ${recoveryAgeLabel(dispatcher?.heartbeat_age_s)} · ${recoveryRows.length} offen`}
    >
      <div className="flex flex-wrap items-center gap-2">
        <StatusPill
          tone={tone}
          label={dispatcher?.detail || dispatcher?.status || "unbekannt"}
          dot={dispatcher?.status === "healthy" ? "live" : dispatcher?.status === "degraded" ? "warn" : "error"}
        />
        {health.error ? <span className="text-xs text-amber-200">{health.error}</span> : null}
        {decisions.error ? <span className="text-xs text-amber-200">{decisions.error}</span> : null}
      </div>
      {recoveryRows.length ? (
        <ul className="mt-3 grid gap-2 lg:grid-cols-3">
          {recoveryRows.slice(0, 3).map((row) => (
            <li key={`${row.kind}:${row.task_id}`} className="rounded-lg border border-white/10 bg-white/[.03] px-3 py-2">
              <div className="flex items-center justify-between gap-2">
                <span className="text-xs font-semibold text-white">{row.title}</span>
                <span className="hc-mono text-[10px] hc-dim">{row.kind}</span>
              </div>
              <p className="mt-1 line-clamp-2 text-xs hc-soft">{row.reason}</p>
              {row.operator_escalation?.recommended_human_action ? (
                <p className="mt-1 line-clamp-2 text-xs text-amber-100">{row.operator_escalation.recommended_human_action}</p>
              ) : null}
              {row.operator_escalation?.blocked_action_boundary?.length ? (
                <p className="mt-1 truncate hc-mono text-[10px] hc-dim">Grenze: {row.operator_escalation.blocked_action_boundary.join(", ")}</p>
              ) : null}
            </li>
          ))}
        </ul>
      ) : (
        <p className="mt-3 text-sm hc-dim">Keine Recovery-Parks.</p>
      )}
    </FleetPanel>
  );
}

function PlanSpecHub({ onIngested }: { onIngested: (rootTaskId: string) => void }) {
  const plans = usePlanSpecs();
  const [plansOpen, setPlansOpen] = useState(false);
  const [busyPath, setBusyPath] = useState<string | null>(null);
  const [errorByPath, setErrorByPath] = useState<Record<string, string>>({});
  const [promptByPath, setPromptByPath] = useState<Record<string, string>>({});
  const items = (plans.data?.planspecs ?? []).slice(0, 8);
  const validCount = items.filter((item) => item.valid).length;

  const setRowError = useCallback((path: string, message: string | null) => {
    setErrorByPath((current) => {
      const next = { ...current };
      if (message) next[path] = message;
      else delete next[path];
      return next;
    });
  }, []);

  const ingest = useCallback(async (item: PlanSpecRecord) => {
    setBusyPath(`${item.path}:ingest`);
    setRowError(item.path, null);
    try {
      const result = await fetchJSON<PlanSpecIngestResponse>("/api/plugins/kanban/planspecs/ingest", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: item.path, author: "dashboard" }),
      });
      await plans.reload();
      onIngested(result.root_task_id);
    } catch (e) {
      setRowError(item.path, e instanceof Error ? e.message : String(e));
    } finally {
      setBusyPath(null);
    }
  }, [onIngested, plans, setRowError]);

  const buildPrompt = useCallback(async (item: PlanSpecRecord) => {
    setBusyPath(`${item.path}:prompt`);
    setRowError(item.path, null);
    try {
      const result = await fetchJSON<PlanSpecPromptResponse>("/api/plugins/kanban/planspecs/sprint-prompt", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: item.path, author: "dashboard" }),
      });
      setPromptByPath((current) => ({ ...current, [item.path]: result.prompt }));
      if (navigator.clipboard) {
        await navigator.clipboard.writeText(result.prompt).catch(() => undefined);
      }
    } catch (e) {
      setRowError(item.path, e instanceof Error ? e.message : String(e));
    } finally {
      setBusyPath(null);
    }
  }, [setRowError]);

  if (!plans.loading && !plans.error && items.length === 0) return null;

  return (
    <FleetPanel eyebrow="Planspec-Hub" meta={`${validCount}/${items.length} offen · Vault`}>
      <button
        type="button"
        aria-expanded={plansOpen}
        onClick={() => setPlansOpen((value) => !value)}
        className="flex min-h-11 w-full min-w-0 items-center gap-2 rounded-md border border-[var(--hc-border)] px-3 py-2 text-left transition hover:border-[var(--hc-border-strong)]"
      >
        {plansOpen ? <ChevronDown className="h-4 w-4 shrink-0 hc-soft" /> : <ChevronRight className="h-4 w-4 shrink-0 hc-soft" />}
        <span className="min-w-0 flex-1 break-words text-sm font-semibold text-white">Offene PlanSpecs</span>
        <span className="shrink-0 rounded-full border border-[var(--hc-border)] px-2 py-0.5 hc-mono hc-type-label hc-soft">{items.length}</span>
      </button>
      {plans.error ? <ToneCallout tone="amber">{plans.error}</ToneCallout> : null}
      {plansOpen && plans.loading && plans.data == null ? <SkeletonCard rows={3} /> : null}
      {plansOpen ? <div className="mt-3 grid gap-2">
        {items.map((item) => {
          const ingestBusy = busyPath === `${item.path}:ingest`;
          const promptBusy = busyPath === `${item.path}:prompt`;
          const rowError = errorByPath[item.path];
          const prompt = promptByPath[item.path];
          return (
            <div key={item.path} className="min-w-0 rounded-lg border border-[var(--hc-border)] bg-[var(--hc-panel)] p-3">
              <div className="flex min-w-0 flex-wrap items-start gap-2">
                <FileText className="mt-0.5 h-4 w-4 shrink-0 text-[var(--hc-accent-text)]" />
                <div className="min-w-0 flex-1">
                  <p className="break-words text-sm font-semibold leading-snug text-white">{item.topic}</p>
                  <p className="mt-1 break-all hc-mono hc-type-label hc-dim sm:line-clamp-1 sm:break-normal">{item.path}</p>
                </div>
                <StatusPill tone={item.valid ? "emerald" : "amber"} label={item.valid ? "offen" : "blocked"} dot={item.valid ? "live" : "warn"} />
              </div>
              <div className="mt-2 flex flex-wrap items-center gap-1.5">
                <span className="max-w-full rounded-full border border-[var(--hc-border)] px-2 py-0.5 hc-type-label hc-soft">{item.freigabe || "ohne Freigabe"}</span>
                <span className="max-w-full rounded-full border border-[var(--hc-border)] px-2 py-0.5 hc-type-label hc-soft">{item.live_test_depth || "smoke"}</span>
                <span className="max-w-full rounded-full border border-[var(--hc-border)] px-2 py-0.5 hc-type-label hc-soft">{item.subtask_count} Subtasks</span>
                <span className="max-w-full rounded-full border border-[var(--hc-border)] px-2 py-0.5 hc-type-label hc-dim">{item.agent}</span>
              </div>
              {item.errors.length ? <p className="mt-2 break-words text-[0.75rem] text-amber-200">{item.errors.join(" · ")}</p> : null}
              {rowError ? <p className="mt-2 break-words text-[0.75rem] text-red-300">{rowError}</p> : null}
              <div className="mt-3 flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  disabled={!item.valid || ingestBusy || promptBusy}
                  onClick={() => void ingest(item)}
                  className="inline-flex min-h-11 items-center gap-1.5 rounded-full border border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] px-3 text-sm text-[var(--hc-accent-text)] transition hover:brightness-110 disabled:opacity-40 sm:min-h-9"
                >
                  {ingestBusy ? <RefreshCw className="h-3.5 w-3.5 animate-spin" /> : <ArrowRight className="h-3.5 w-3.5" />}
                  Kanban
                </button>
                <button
                  type="button"
                  disabled={!item.valid || ingestBusy || promptBusy}
                  onClick={() => void buildPrompt(item)}
                  className="inline-flex min-h-11 items-center gap-1.5 rounded-full border border-[var(--hc-border-strong)] px-3 text-sm hc-soft transition hover:bg-white/5 disabled:opacity-40 sm:min-h-9"
                >
                  {promptBusy ? <RefreshCw className="h-3.5 w-3.5 animate-spin" /> : <Copy className="h-3.5 w-3.5" />}
                  Sprint-Prompt
                </button>
              </div>
              {prompt ? (
                <textarea
                  readOnly
                  value={prompt}
                  className="mt-3 min-h-28 w-full resize-y rounded-md border border-[var(--hc-border)] bg-black/20 p-2 hc-mono text-[0.72rem] text-zinc-100 outline-none"
                />
              ) : null}
            </div>
          );
        })}
      </div> : null}
    </FleetPanel>
  );
}

// Shallow field compare so enrichmentById can keep the PREVIOUS object reference
// for an entry whose content didn't change across a poll tick — without it every
// workers/reviews/results tick hands each card a new `enriched` object and the
// memo never holds.
function artifactKey(item: Pick<TaskDeliverable, "relative_path" | "url"> & { path?: string }): string {
  return item.url || item.path || item.relative_path;
}

function sameArtifactLinks(a?: TaskArtifactLink[], b?: TaskArtifactLink[]): boolean {
  const left = a ?? [];
  const right = b ?? [];
  if (left.length !== right.length) return false;
  return left.every((item, i) => artifactKey(item) === artifactKey(right[i]));
}

function preferredResultArtifactLink(links?: TaskArtifactLink[]): TaskArtifactLink | null {
  const items = links ?? [];
  if (!items.length) return null;
  return items.find((item) => /(^|\/)RESULT\.md$/i.test(item.relative_path) || /(^|\/)RESULT\.md$/i.test(item.path)) ?? items[0];
}

function reviewGateStatus(enriched: Enriched): string | null {
  if (enriched.activeVerifier) {
    return `Verifier läuft${enriched.activeRunId ? ` · Run ${enriched.activeRunId}` : ""}`;
  }
  if (enriched.reviewRunState === "approved") return "Verifier APPROVED — wartet auf Board-Refresh";
  if (enriched.reviewRunState === "request_changes") return "Verifier REQUEST_CHANGES — Nacharbeit folgt";
  return null;
}

function terminalVerifierGateKey(enriched: Enriched): string | null {
  if (enriched.activeVerifier) return null;
  if (enriched.reviewRunState === "approved" || enriched.reviewRunState === "request_changes") return enriched.reviewRunState;
  return null;
}

function shouldExposeManualReviewFallback(enriched: Enriched, now: number): boolean {
  if (enriched.activeVerifier) return false;
  if (terminalVerifierGateKey(enriched) == null) return false;
  // Zeitanker = Verdict-Zeitpunkt des Review-Runs (Server-`submitted_at`),
  // nicht eine clientseitige "erste Sichtung": das ist rein ableitbar (keine
  // Ref-/State-Mutation im Render — REQUEST_CHANGES-Befund Run 1018/1021)
  // und überlebt Reloads. Ist das Verdict beim Öffnen der Seite schon älter
  // als die Grace, ist der Übergang nachweislich ausgeblieben — der manuelle
  // Pfad darf dann sofort erscheinen.
  const verdictAt = enriched.reviewVerdictAt;
  return verdictAt != null && verdictAt > 0 && now - verdictAt >= VERIFIER_GATE_TERMINAL_GRACE_MS / 1000;
}

function sameEnriched(a: Enriched, b: Enriched): boolean {
  return (
    a.workerProfile === b.workerProfile &&
    a.workerHeartbeat === b.workerHeartbeat &&
    a.verdict === b.verdict &&
    a.verifierEvidenceCount === b.verifierEvidenceCount &&
    a.activeVerifier === b.activeVerifier &&
    a.activeRunId === b.activeRunId &&
    a.reviewRunState === b.reviewRunState &&
    a.reviewVerdictAt === b.reviewVerdictAt &&
    a.blockedKind === b.blockedKind &&
    a.blockedReason === b.blockedReason &&
    a.resultQualityLabel === b.resultQualityLabel &&
    a.resultQualityTone === b.resultQualityTone &&
    a.deliverableCount === b.deliverableCount &&
    sameArtifactLinks(a.resultArtifactLinks, b.resultArtifactLinks)
  );
}

const EVENT_LABEL: Record<string, string> = {
  created: "Erstellt", claimed: "Worker claimte", completed: "Abgeschlossen", done: "Fertig",
  blocked: "Blockiert", unblocked: "Entblockt", scheduled: "Geplant", promoted: "Befördert",
  submitted_for_review: "Zur Prüfung eingereicht", verified: "Verifiziert", reclaimed: "Zurückgeholt",
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

function FlowCardActions({ status, busy, error, dispatchChoice, verifierGateStatus, manualReviewFallback, onReleaseChain, onDispatchSingle, onCancelDispatchChoice, onAct }: {
  status: BoardTask["status"]; busy: boolean; error?: string; dispatchChoice?: FlowDispatchChoice | null;
  verifierGateStatus?: string | null; manualReviewFallback?: boolean;
  onReleaseChain?: () => void; onDispatchSingle?: () => void; onCancelDispatchChoice?: () => void; onAct: (action: StageAction) => void;
}) {
  const [pending, setPending] = useState<StageAction | null>(null);
  const actions = stageActions(status);
  const guard = stageGuard(status);
  const reviewIsVerifierDriven = status === "review" && verifierGateStatus;
  const reviewActionsLockedByActiveVerifier = reviewIsVerifierDriven && !manualReviewFallback;
  return (
    <div className="mt-2.5" onClick={(e) => e.stopPropagation()}>
      {reviewIsVerifierDriven ? (
        <div className="space-y-1.5">
          <p className="flex items-center gap-1.5 rounded-md border border-cyan-400/25 bg-cyan-400/10 px-2 py-1 hc-type-label text-cyan-100">
            <ShieldCheck className="h-3 w-3" />{verifierGateStatus}
          </p>
          {manualReviewFallback ? <p className="hc-type-label hc-soft">Übergang ausgeblieben — manuell abnehmen</p> : null}
        </div>
      ) : null}
      {reviewActionsLockedByActiveVerifier ? null : dispatchChoice ? (
        <div className="rounded-md border border-emerald-400/30 bg-emerald-400/10 p-2">
          <p className="hc-type-label text-emerald-100">{de.flow.singleDispatch.prompt}</p>
          <p className="mt-1 hc-type-label hc-soft">{de.flow.singleDispatch.heldSiblings(dispatchChoice.heldSiblingIds.length)}</p>
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
          <span className="hc-type-label hc-soft">{pending.confirm}</span>
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
        <p className="flex items-center gap-1.5 hc-type-label hc-dim"><Lock className="h-3 w-3" />{guard}</p>
      ) : null}
      {error ? <p className="mt-1.5 flex items-start gap-1 hc-type-label text-red-300"><AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />{error}</p> : null}
    </div>
  );
}

interface FlowRunCardProps {
  task: BoardTask; enriched: Enriched; selected: boolean; busy: boolean; error?: string; now: number; dispatchChoice?: FlowDispatchChoice | null; manualReviewFallback?: boolean;
  onSelect: (id: string) => void; onReleaseChain?: () => void; onDispatchSingle?: () => void; onCancelDispatchChoice?: () => void; onAct: (task: BoardTask, action: StageAction) => void;
}

// Re-render a card only when something it shows actually changed. `enriched` is a
// fresh object every poll tick (enrichmentById rebuilds), so compare it by
// content; everything else is stable by identity (callbacks are useCallback'd;
// `task`/`now` only change on a board tick). Without this, a workers/reviews/
// results tick reconciles all ~60 cards every 5 s.
function flowCardPropsEqual(a: FlowRunCardProps, b: FlowRunCardProps): boolean {
  return (
    a.task === b.task &&
    a.selected === b.selected &&
    a.busy === b.busy &&
    a.error === b.error &&
    a.now === b.now &&
    a.dispatchChoice === b.dispatchChoice &&
    a.manualReviewFallback === b.manualReviewFallback &&
    a.onSelect === b.onSelect &&
    a.onReleaseChain === b.onReleaseChain &&
    a.onDispatchSingle === b.onDispatchSingle &&
    a.onCancelDispatchChoice === b.onCancelDispatchChoice &&
    a.onAct === b.onAct &&
    sameEnriched(a.enriched, b.enriched)
  );
}

export const FlowRunCard = memo(function FlowRunCard({ task, enriched, selected, busy, error, now, dispatchChoice, manualReviewFallback, onSelect, onReleaseChain, onDispatchSingle, onCancelDispatchChoice, onAct }: FlowRunCardProps) {
  const role = roleChip(enriched.workerProfile ?? task.assignee, task.status === "review" ? "verification" : null);
  const isBlocked = task.status === "blocked";
  const isReview = task.status === "review";
  const isDone = task.status === "done";
  const verifierGate = reviewGateStatus(enriched);
  const resultArtifact = preferredResultArtifactLink(enriched.resultArtifactLinks);
  const ageSec = task.age?.created_age_seconds ?? null;
  // mobileOverflowGuard: all phone-width rows below need min-w-0 + wrapping or
  // long task ids / branch names can push the Flow tab off the right edge.
  return (
    <article
      id={flowTaskDomId(task.id)}
      role="button"
      tabIndex={0}
      aria-pressed={selected}
      onClick={() => onSelect(task.id)}
      onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onSelect(task.id); } }}
      className={cn("min-w-0 max-w-full cursor-pointer overflow-hidden rounded-lg border p-2.5 transition", selected ? "border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)]" : "border-[var(--hc-border)] bg-[var(--hc-panel)] hover:border-[var(--hc-border-strong)]", isBlocked && "border-red-500/40")}
    >
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <span className="hc-mono min-w-0 max-w-full truncate hc-type-label hc-dim">{task.id}</span>
        <span className="ml-auto shrink-0"><RoleChip role={role} /></span>
      </div>
      <p className="mt-1.5 line-clamp-2 text-sm font-semibold leading-snug text-white">{task.title}</p>
      <p className="mt-1 truncate hc-mono hc-type-label hc-dim">
        {ageSec != null ? `⏱ vor ${fmtAge(now - ageSec, now)}` : ""}{task.branch_name ? ` · ${task.branch_name}` : ""}
        {enriched.workerHeartbeat ? ` · ♥ ${fmtAge(enriched.workerHeartbeat, now)}` : ""}
      </p>
      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        <StatusPill tone={isBlocked ? "red" : isDone ? "emerald" : isReview ? "amber" : task.status === "running" ? "cyan" : "zinc"} label={taskStatusLabel[task.status] ?? task.status} dot={task.status === "running" ? "live" : isBlocked ? "error" : isDone ? "ready" : isReview ? "warn" : "idle"} />
        {task.priority >= 2 ? <span className="rounded-full border border-rose-400/30 bg-rose-400/10 px-2 py-0.5 hc-type-label text-rose-200">Hoch</span> : null}
        {(task.auto_retry_count ?? 0) > 0 ? <span className="rounded-full border border-amber-400/30 bg-amber-400/10 px-2 py-0.5 hc-type-label text-amber-100">Auto-Retry {Math.min(task.auto_retry_count ?? 0, 2)}/2</span> : null}
        {task.progress && task.progress.total > 0 ? <span className="rounded-full border border-sky-400/30 bg-sky-400/10 px-2 py-0.5 hc-type-label text-sky-100">{task.progress.done}/{task.progress.total} {de.flow.plan.subtasksHeading}</span> : null}
        {enriched.verdict ? <span className="rounded-full border border-cyan-400/30 bg-cyan-400/10 px-2 py-0.5 hc-type-label text-cyan-100">{enriched.verdict}</span> : null}
        {isIsolatedWorkspace(task) ? <span title={task.workspace_path ?? undefined} className="rounded-full border border-violet-400/30 bg-violet-400/10 px-2 py-0.5 hc-type-label text-violet-200">⧉ Worktree</span> : null}
        {isDone && enriched.resultQualityLabel ? <span className="rounded-full border border-emerald-400/30 bg-emerald-400/10 px-2 py-0.5 hc-type-label text-emerald-100">{enriched.resultQualityLabel}</span> : null}
      </div>
      {task.latest_summary ? <p className="mt-2 line-clamp-2 text-xs hc-soft">{task.latest_summary}</p> : null}
      {isBlocked && enriched.blockedReason ? (
        <p className="mt-2 flex items-start gap-1.5 rounded-md border border-red-500/30 bg-red-500/10 px-2 py-1 hc-type-label text-red-200"><Lock className="mt-0.5 h-3 w-3 shrink-0" />{enriched.blockedReason}</p>
      ) : null}
      {isReview ? (
        <p className="mt-2 flex items-center gap-1.5 hc-type-label hc-dim"><ShieldCheck className="h-3 w-3 text-cyan-300" />Verifier-Gate — {verifierGate ?? "wartet auf Verifier; Nacharbeit schickt zurück."}</p>
      ) : null}
      {enriched.deliverableCount ? <p className="mt-1.5 hc-type-label text-emerald-300">{enriched.deliverableCount} Deliverable{enriched.deliverableCount === 1 ? "" : "s"}</p> : null}
      {resultArtifact ? (
        <div className="mt-2 flex items-center justify-between gap-2 rounded-md border border-emerald-400/20 bg-emerald-500/[.06] px-2 py-1.5">
          <span className="min-w-0 truncate hc-type-label text-emerald-100">Spec-Draft / RESULT · {resultArtifact.relative_path}</span>
          <DeliverableOpenButton url={resultArtifact.url} label="RESULT öffnen" />
        </div>
      ) : null}
      <FlowCardActions
        status={task.status}
        busy={busy}
        error={error}
        dispatchChoice={dispatchChoice}
        verifierGateStatus={verifierGate}
        manualReviewFallback={manualReviewFallback}
        onReleaseChain={onReleaseChain}
        onDispatchSingle={onDispatchSingle}
        onCancelDispatchChoice={onCancelDispatchChoice}
        onAct={(action) => onAct(task, action)}
      />
    </article>
  );
}, flowCardPropsEqual);

// The Plan panel: when the selected task is a decompose root, surface its real
// subtask GROUP (resolved live from the board), the durable Vault plan-spec link
// (documented method), and a "Kette starten" release when subtasks are gate-held
// in `scheduled`. This is the visible+operative proof — the same child ids run
// on through Execute → Verify → Ship.
function FlowPlanPanel({ rootId, detail, boardTasks, now, onRelease, releaseBusy, releaseError, released, onGateChanged }: {
  rootId: string; detail?: TaskDetailResponse; boardTasks: BoardTask[];
  now: number; onRelease: (rootId: string, n: number, options?: FlowReleaseOptions) => void; releaseBusy: boolean; releaseError?: string; released?: number; onGateChanged?: () => void | Promise<void>;
}) {
  const [confirming, setConfirming] = useState(false);
  const [releaseLevel, setReleaseLevel] = useState<FlowGateReleaseLevel>("merge");
  const [assigneeOverrides, setAssigneeOverrides] = useState<Record<string, string>>({});
  const [selectedSizing, setSelectedSizing] = useState<string[]>([]);
  const [splitTitle, setSplitTitle] = useState("");
  const autoSweepRef = useRef<string | null>(null);
  const gate = useFlowGate(rootId, onGateChanged);
  const events = detail?.events ?? [];
  const decomposed = [...events].reverse().find((e) => e.kind === "decomposed");
  const hasSpec = events.some((e) => e.kind === "flow_plan");
  const rawIds = (decomposed?.payload?.child_ids as unknown);
  const gateChildren = gate.data?.children ?? [];
  const childIds: string[] = gateChildren.length
    ? gateChildren.map((c) => c.id).filter(Boolean)
    : (Array.isArray(rawIds) ? rawIds.filter((x): x is string => typeof x === "string") : []);
  useEffect(() => {
    if (!gate.data?.auto_dispatch_eligible || gate.data.held_count <= 0) return;
    const key = `${rootId}:${gate.data.timeout_at ?? "now"}`;
    if (autoSweepRef.current === key) return;
    autoSweepRef.current = key;
    void gate.sweepTimeouts(gate.data.timeout_seconds);
  }, [gate, gate.data?.auto_dispatch_eligible, gate.data?.held_count, gate.data?.timeout_at, gate.data?.timeout_seconds, rootId]);
  if (!childIds.length && !hasSpec) return null;

  const byId = new Map(boardTasks.map((t) => [t.id, t]));
  const gateById = new Map(gateChildren.map((c) => [c.id, c]));
  const children = childIds.map((id) => byId.get(id)).filter((t): t is BoardTask => !!t);
  const heldCount = gate.data?.held_count ?? children.filter((c) => c.status === "scheduled").length;
  const specUrl = `/api/plugins/kanban/tasks/${encodeURIComponent(rootId)}/flow-plan`;
  const profiles = Array.from(new Set((gate.data?.lanes ?? []).flatMap((lane) => lane.profiles))).filter(Boolean).sort();
  const estimate = gate.data?.cost_estimate;
  const timeoutAt = gate.data?.timeout_at ?? null;
  const timeoutCopy = timeoutAt == null
    ? null
    : (timeoutAt <= now ? "Timeout erreicht" : `Auto in ${fmtDur(timeoutAt - now)}`);
  const overridePayload: Record<string, string | null> = {};
  for (const child of gateChildren) {
    const next = assigneeOverrides[child.id];
    if (next !== undefined && next !== (child.assignee ?? "")) {
      overridePayload[child.id] = next || null;
    }
  }
  const toggleSizing = (id: string) => {
    setSelectedSizing((prev) => prev.includes(id)
      ? prev.filter((x) => x !== id)
      : [...prev, id].slice(-2));
  };
  const mergeSelected = () => {
    if (selectedSizing.length !== 2) return;
    void gate.sizing("merge", selectedSizing).then((res) => {
      if (res) setSelectedSizing([]);
    });
  };
  const splitSelected = () => {
    if (selectedSizing.length !== 1 || !splitTitle.trim()) return;
    void gate.sizing("split", selectedSizing, { title: splitTitle.trim() }).then((res) => {
      if (res) {
        setSelectedSizing([]);
        setSplitTitle("");
      }
    });
  };
  const releaseOptions: FlowReleaseOptions = {
    release_level: releaseLevel,
    assignee_overrides: overridePayload,
  };

  return (
    <div className="mt-4 rounded-lg border border-[var(--hc-border)] bg-[var(--hc-panel)] p-3">
      <div className="flex items-center justify-between gap-2">
        <Eyebrow>{childIds.length ? de.flow.plan.decomposedInto(childIds.length) : de.flow.plan.subtasksHeading}</Eyebrow>
        {hasSpec ? (
          // Über den authentifizierten Opener wie alle Deliverables: ein
          // plain <a target="_blank"> trägt im Session-Token-Modus keinen
          // Auth-Header und lief dort auf ein 401.
          <DeliverableOpenButton url={specUrl} label={de.flow.plan.openSpec} />
        ) : null}
      </div>

      {gate.data ? (
        <div className="mt-2.5 rounded-md border border-[var(--hc-border)] bg-black/10 p-2">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <Eyebrow>Freigabe</Eyebrow>
            {estimate ? (
              <StatusPill
                tone={estimate.warning ? "amber" : "emerald"}
                label={`~$${estimate.estimated_cost_usd.toFixed(3)} · ${fmtTokens(estimate.estimated_tokens)}`}
              />
            ) : null}
            {timeoutCopy ? <span className="hc-type-label hc-dim">{timeoutCopy}</span> : null}
            <button
              type="button"
              disabled={gate.busy}
              onClick={() => void gate.sweepTimeouts(gate.data?.timeout_seconds)}
              className="ml-auto inline-flex min-h-9 items-center gap-1 rounded-full border border-[var(--hc-border-strong)] px-2.5 hc-type-label hc-soft disabled:opacity-40"
            >
              <RefreshCw className="h-3.5 w-3.5" />Sweep
            </button>
          </div>
          {estimate?.warning ? (
            <p className="mt-1.5 flex items-start gap-1.5 hc-type-label text-amber-200">
              <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />Soft-Limit ${estimate.soft_limit_usd.toFixed(2)} überschritten.
            </p>
          ) : null}
          <div className="mt-2 flex flex-wrap gap-1.5">
            {(["merge", "live"] as FlowGateReleaseLevel[]).map((level) => (
              <button
                key={level}
                type="button"
                onClick={() => setReleaseLevel(level)}
                className={cn(
                  "inline-flex min-h-9 items-center rounded-full border px-3 hc-type-label transition",
                  releaseLevel === level ? "border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] text-[var(--hc-accent-text)]" : "border-[var(--hc-border)] hc-soft hover:border-[var(--hc-border-strong)]",
                )}
              >
                {level === "merge" ? "Merge" : "Live"}
              </button>
            ))}
          </div>
        </div>
      ) : gate.loading ? (
        <p className="mt-2 hc-type-label hc-dim">Gate wird geladen…</p>
      ) : null}

      {children.length ? (
        <ul className="mt-2 space-y-1.5">
          {children.map((c) => {
            const statusExplanation = getFlowSubtaskStatusExplanation(c.status, c.status === "blocked" ? c.latest_summary : null);
            const gateChild = gateById.get(c.id);
            const risk = gateChild?.risk;
            const riskTone = risk?.tone === "high" ? "red" : risk?.tone === "medium" ? "amber" : "emerald";
            const selected = selectedSizing.includes(c.id);
            return (
              <li key={c.id} className={cn("flex min-w-0 flex-wrap items-center gap-2 rounded-md border px-2 py-1.5", selected ? "border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)]" : "border-[var(--hc-border)]")}>
                <button
                  type="button"
                  onClick={() => toggleSizing(c.id)}
                  className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-full border border-[var(--hc-border-strong)] hc-type-label hc-soft"
                  title="Sizing-Auswahl"
                  aria-pressed={selected}
                >
                  {selected ? "x" : "+"}
                </button>
                <span className="hc-mono min-w-0 max-w-full truncate hc-type-label hc-dim">{c.id}</span>
                <span className="min-w-0 flex-1 basis-36 truncate hc-type-label text-white">{c.title}</span>
                <div className="ml-auto flex max-w-full flex-wrap items-center justify-end gap-1.5">
                  {risk ? <StatusPill tone={riskTone} label={`Risiko ${risk.tone}`} /> : null}
                  <StatusPill tone={statusTone(c.status)} label={taskStatusLabel[c.status] ?? c.status} />
                  {profiles.length ? (
                    <select
                      value={assigneeOverrides[c.id] ?? gateChild?.assignee ?? c.assignee ?? ""}
                      onChange={(e) => setAssigneeOverrides((prev) => ({ ...prev, [c.id]: e.target.value }))}
                      className="min-h-8 max-w-40 rounded-md border border-[var(--hc-border)] bg-[var(--hc-panel)] px-2 hc-type-label text-white"
                    >
                      <option value="">Unassigned</option>
                      {profiles.map((profile) => <option key={profile} value={profile}>{profileLabel[profile] ?? profile}</option>)}
                    </select>
                  ) : null}
                  <span className={cn("max-w-full hc-type-label hc-dim sm:max-w-[13rem] sm:text-right", c.status === "blocked" && "text-red-200")} title={statusExplanation}>
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
          <div className="mb-2 flex min-w-0 flex-wrap items-center gap-2">
            <button
              type="button"
              disabled={gate.busy || selectedSizing.length !== 2}
              onClick={mergeSelected}
              className="inline-flex min-h-9 items-center rounded-full border border-[var(--hc-border-strong)] px-3 hc-type-label hc-soft disabled:opacity-40"
            >
              Merge
            </button>
            <input
              value={splitTitle}
              onChange={(e) => setSplitTitle(e.target.value)}
              placeholder="Split-Titel"
              className="min-h-9 min-w-0 flex-1 rounded-md border border-[var(--hc-border)] bg-[var(--hc-panel)] px-2 text-xs text-white placeholder:text-zinc-500"
            />
            <button
              type="button"
              disabled={gate.busy || selectedSizing.length !== 1 || !splitTitle.trim()}
              onClick={splitSelected}
              className="inline-flex min-h-9 items-center rounded-full border border-[var(--hc-border-strong)] px-3 hc-type-label hc-soft disabled:opacity-40"
            >
              Split
            </button>
          </div>
          {confirming ? (
            <div className="flex flex-wrap items-center gap-2">
              <span className="hc-type-label hc-soft">{de.flow.plan.releaseConfirm(heldCount)}</span>
              <button type="button" disabled={releaseBusy} onClick={() => { onRelease(rootId, heldCount, releaseOptions); setConfirming(false); }} className="inline-flex min-h-9 items-center rounded-full border border-emerald-400/40 bg-emerald-400/10 px-3 text-xs text-emerald-100 disabled:opacity-40">{releaseBusy ? de.flow.plan.releaseBusy : `${de.flow.plan.releaseConfirmButton} · ${releaseLevel}`}</button>
              <button type="button" onClick={() => setConfirming(false)} className="inline-flex min-h-9 items-center rounded-full border border-[var(--hc-border-strong)] px-3 text-xs hc-soft">Abbrechen</button>
            </div>
          ) : (
            <button type="button" disabled={releaseBusy} onClick={() => setConfirming(true)} className="inline-flex min-h-9 items-center gap-1.5 rounded-full border border-emerald-400/40 bg-emerald-400/10 px-3 text-xs font-medium text-emerald-100 transition hover:brightness-110 disabled:opacity-40">
              <Play className="h-3.5 w-3.5" />{de.flow.plan.release} · {de.flow.plan.subtasksOf(heldCount)}
            </button>
          )}
          <p className="mt-1.5 flex items-center gap-1.5 hc-type-label hc-dim"><Lock className="h-3 w-3" />{de.flow.plan.heldGate}</p>
          <p className="mt-1 hc-type-label hc-dim">Kette starten gibt gehaltene Subtasks frei; Queue/Assignee und Dependencies entscheiden den tatsächlichen Start.</p>
        </div>
      ) : null}

      {released ? <p className="mt-1.5 hc-type-label text-emerald-300">{de.flow.plan.released(released)}</p> : null}
      {gate.error ? <p className="mt-1.5 flex items-start gap-1 hc-type-label text-red-300"><AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />{gate.error}</p> : null}
      {releaseError ? <p className="mt-1.5 flex items-start gap-1 hc-type-label text-red-300"><AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />{releaseError}</p> : null}
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
        <span className="rounded-full border border-[var(--hc-border)] px-2 py-0.5 hc-type-label hc-dim">Snapshot · read-only</span>
        <span className="rounded-full border border-[var(--hc-border)] px-2 py-0.5 hc-type-label hc-dim">Snapshot-Alter: {snapshotLabel}</span>
      </div>
      <p className="mt-1.5 hc-type-label hc-dim">{parallelNote}</p>
      <div className="mt-2 grid gap-2">
        <div className="rounded-md border border-[var(--hc-border)] bg-[var(--hc-panel)] px-2.5 py-2">
          <p className="hc-eyebrow text-sky-100">Gehalten</p>
          <p className="mt-1 break-words text-[0.75rem] hc-soft">{heldTasks.length ? heldTasks.map(taskLine).join(" · ") : "Keine gehaltenen direkten Nachbarn im Snapshot."}</p>
        </div>
        <div className="rounded-md border border-[var(--hc-border)] bg-[var(--hc-panel)] px-2.5 py-2">
          <p className="hc-eyebrow text-emerald-100">Ready-Nachbar im Snapshot</p>
          <p className="mt-1 break-words text-[0.75rem] hc-soft">{readyTasks.length ? readyTasks.map(taskLine).join(" · ") : "Kein ready-Nachbar im Snapshot; keine Scheduler-Zusage."}</p>
        </div>
        <div className="rounded-md border border-[var(--hc-border)] bg-[var(--hc-panel)] px-2.5 py-2">
          <p className="hc-eyebrow text-cyan-100">Läuft bereits</p>
          <p className="mt-1 break-words text-[0.75rem] hc-soft">{runningTasks.length ? runningTasks.map(taskLine).join(" · ") : "Kein direkter Nachbar läuft im Snapshot."}</p>
        </div>
        <div className="rounded-md border border-[var(--hc-border)] bg-[var(--hc-panel)] px-2.5 py-2">
          <p className="hc-eyebrow text-amber-100">Mögliche Vorgänger</p>
          <p className="mt-1 break-words text-[0.75rem] hc-soft">{predecessorHintLine}</p>
        </div>
        <div className="rounded-md border border-[var(--hc-border)] bg-[var(--hc-panel)] px-2.5 py-2">
          <p className="hc-eyebrow text-amber-100">Mögliche Nachfolger mit Wartestatus</p>
          <p className="mt-1 break-words text-[0.75rem] hc-soft">{waitingDependents.length ? waitingDependents.map(taskLine).join(" · ") : "Keine möglichen Nachfolger mit Wartestatus im Snapshot."}</p>
        </div>
      </div>
      <div className="mt-2 flex flex-wrap gap-1.5">
        {parentTasks.map((p) => <StatusPill key={`p-${p.id}`} tone={statusTone(p.status)} label={`Möglicher Vorgänger ${taskStatusLabel[p.status] ?? p.status}`} />)}
        {childTasks.map((c) => <StatusPill key={`c-${c.id}`} tone={statusTone(c.status)} label={`Möglicher Nachfolger ${taskStatusLabel[c.status] ?? c.status}`} />)}
      </div>
      {unknownIds.length ? <p className="mt-2 break-words hc-type-label hc-dim">Nicht im Board-Snapshot: {unknownIds.join(", ")}</p> : null}
      {parentIds.length ? <p className="mt-2 hc-type-label hc-dim">Snapshot-Hinweis: rohe Detail-Links zeigen Nähe, aber keinen sicheren Blockierungsgrund.</p> : null}
      {hasAmbiguousTodo ? <p className="mt-2 hc-type-label text-amber-200">Hinweis: todo ist uneindeutig; es kann Dependency-Warten, manuelles Backlog oder Dispatcher-Queue sein.</p> : null}
    </div>
  );
}

function DeliverableOpenButton({ url, label = "öffnen" }: { url: string; label?: string }) {
  const [openError, setOpenError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const onOpen = useCallback(async () => {
    setBusy(true);
    setOpenError(null);
    try {
      await openAuthedApiFile(url);
    } catch (err) {
      setOpenError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }, [url]);
  return (
    <div className="shrink-0 text-right">
      <button
        type="button"
        className="hc-type-label text-emerald-200 hover:text-emerald-100 disabled:cursor-wait disabled:opacity-60"
        onClick={onOpen}
        disabled={busy}
        title={url}
        aria-label={`${label}: ${url}`}
      >
        {busy ? "öffnet…" : label}
      </button>
      {openError ? <p className="mt-1 max-w-32 hc-type-label text-red-300">{openError}</p> : null}
    </div>
  );
}

export function FlowReceiptRail({ taskId, task, detail, enriched = EMPTY_ENRICHED, loading, error, now, boardTasks, snapshotLabel, onRelease, releaseBusy, releaseError, released, onGateChanged }: {
  taskId: string | null; task?: BoardTask; detail?: TaskDetailResponse; enriched?: Enriched; loading: boolean; error?: string; now: number;
  boardTasks: BoardTask[]; snapshotLabel: string; onRelease: (rootId: string, n: number, options?: FlowReleaseOptions) => void; releaseBusy: boolean; releaseError?: string; released?: number; onGateChanged?: () => void | Promise<void>;
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
  const resultArtifactLinks = enriched.resultArtifactLinks ?? [];
  const artifactLinksOnly = resultArtifactLinks.filter((link) => !deliverables.some((deliverable) => artifactKey(deliverable) === artifactKey(link)));
  const empty = !loading && !error && runs.length === 0 && events.length === 0 && deliverables.length === 0 && artifactLinksOnly.length === 0;
  return (
    <aside className="hc-surface-card h-fit p-4 xl:sticky xl:top-4">
      <Eyebrow>{de.flow.selectedChain}</Eyebrow>
      <div className="mt-2">
        <p className="hc-mono hc-type-label hc-dim">{taskId}</p>
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
          key={taskId}
          rootId={taskId}
          detail={detail}
          boardTasks={boardTasks}
          now={now}
          onRelease={onRelease}
          releaseBusy={releaseBusy}
          releaseError={releaseError}
          released={released}
          onGateChanged={onGateChanged}
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
                <div key={run.id} className="min-w-0 max-w-full overflow-hidden rounded-lg border border-[var(--hc-border)] bg-[var(--hc-panel)] p-2.5">
                  <div className="flex min-w-0 items-center gap-2">
                    <RoleChip role={role} />
                    <span className={cn("ml-auto inline-flex shrink-0 items-center gap-1 hc-type-label", ok ? "text-[var(--hc-emerald)]" : run.error ? "text-[var(--hc-red)]" : "hc-dim")}>
                      <span className={cn("h-1.5 w-1.5 rounded-full", ok ? "bg-[var(--hc-emerald)]" : run.error ? "bg-[var(--hc-red)]" : "bg-[var(--hc-text-dim)]")} />{run.outcome ?? run.status}
                    </span>
                  </div>
                  <p className="mt-1 truncate hc-mono hc-type-label hc-dim">Run {run.id} · {run.run_role_label ?? profileLabel[run.profile ?? ""] ?? run.profile ?? "—"}{run.ended_at ? ` · vor ${fmtAge(run.ended_at, now)}` : run.started_at ? ` · seit ${fmtAge(run.started_at, now)}` : ""}</p>
                  {run.summary ? <p className="mt-1 line-clamp-3 text-[0.78rem] text-zinc-100">{run.summary}</p> : null}
                  {run.error ? <p className="mt-1 line-clamp-2 hc-type-label text-red-300">{run.error}</p> : null}
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
                <DeliverableOpenButton url={d.url} />
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {artifactLinksOnly.length ? (
        <div className="mt-4">
          <Eyebrow>Spec-Draft / RESULT</Eyebrow>
          <ul className="mt-2 space-y-1.5">
            {artifactLinksOnly.map((d) => (
              <li key={`${d.source}-${artifactKey(d)}`} className="flex items-center justify-between gap-2 rounded-md border border-emerald-400/20 bg-emerald-500/[.06] px-2.5 py-1.5">
                <span className="min-w-0 truncate text-[0.78rem] text-white" title={d.path}>{d.relative_path || d.filename || d.path}</span>
                <DeliverableOpenButton url={d.url} label={/(^|\/)RESULT\.md$/i.test(d.relative_path) ? "RESULT öffnen" : "Artifact öffnen"} />
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
                <span className={cn("mt-1 h-2 w-2 shrink-0 rounded-full", i === 0 ? "bg-[var(--hc-emerald)]" : "bg-[var(--hc-border-strong)]", i === 0 && "animate-pulse")} />
                <div className="min-w-0">
                  <p className="text-[0.82rem] text-white">{eventLabel(ev.kind)}</p>
                  <p className="hc-type-label hc-dim">vor {fmtAge(ev.created_at, now)}</p>
                </div>
              </div>
            ))}
          </div>
        </div>
      ) : null}
    </aside>
  );
}

/* ── Ketten-Board (Phase 2) ────────────────────────────────────────────────
   Die primäre Einheit ist die Root-Kette (board root_id): eine Karte pro
   Kette mit Mini-Pipeline (Capture→Ship-Zählern), expandierbar zu den
   Mitglieds-Tasks mit allen Stage-Aktionen. Einzeltasks laufen als eigene
   Gruppe; Geliefertes sammelt sich kompakt unten. */

function ChainStagePills({ chain }: { chain: ChainModel<BoardTask> }) {
  return (
    <div className="flex flex-wrap items-center gap-1">
      {FLEET_STAGES.map((stage) => {
        const meta = STAGE_META[stage];
        const n = chain.stageCounts[stage];
        const hex = TONE_HEX[meta.tone];
        return (
          <span
            key={stage}
            title={`${meta.label}: ${n}`}
            className={cn("hc-mono inline-flex items-center gap-1 rounded-full border px-1.5 py-0.5 hc-type-label", n === 0 && "opacity-40")}
            style={n > 0 ? { borderColor: `${hex}55`, color: hex } : { borderColor: "var(--hc-border)", color: "var(--hc-text-dim)" }}
          >
            {meta.label[0]}<span>{n}</span>
          </span>
        );
      })}
    </div>
  );
}

// Epic-Picker am Kettenkopf (Phase 3): die Zuordnung gilt IMMER für die ganze
// Kette (alle Mitglieder werden gepatcht) — bewusst kein Einzel-Task-Picker
// (Grill-Entscheid 3). Zwei-Schritt-Muster wie die Fleet-Aktionen.
function ChainEpicPicker({ chain, openEpics, busy, onAssign }: {
  chain: ChainModel<BoardTask>; openEpics: Epic[]; busy: boolean;
  onAssign: (epicId: string | null) => void;
}) {
  const [picking, setPicking] = useState(false);
  const [choice, setChoice] = useState<string>("");
  if (!openEpics.length && !chain.epicId) return null;
  if (!picking) {
    return (
      <button
        type="button"
        disabled={busy}
        onClick={() => { setChoice(chain.epicId ?? ""); setPicking(true); }}
        className="inline-flex min-h-8 items-center rounded-full border border-indigo-400/25 px-2.5 text-xs text-indigo-200 transition hover:bg-indigo-400/10 disabled:opacity-40"
      >
        {busy ? de.flow.epicAssignBusy : chain.epicId ? de.flow.epicChange : de.flow.epicAssign}
      </button>
    );
  }
  return (
    <div className="flex flex-wrap items-center gap-2">
      <select
        value={choice}
        onChange={(e) => setChoice(e.target.value)}
        aria-label={de.flow.epicAssign}
        className="min-h-8 rounded-md border border-[var(--hc-border)] bg-[var(--hc-panel)] px-2 text-xs text-white outline-none focus:border-[var(--hc-accent-border)]"
      >
        <option value="">{de.flow.epicNoneOption}</option>
        {openEpics.map((e) => <option key={e.id} value={e.id}>{e.title}</option>)}
      </select>
      <span className="hc-type-label hc-dim">{de.flow.epicAssignNote(chain.total)}</span>
      <button
        type="button"
        disabled={busy || (choice || null) === chain.epicId}
        onClick={() => { onAssign(choice || null); setPicking(false); }}
        className="inline-flex min-h-8 items-center rounded-full border border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] px-2.5 text-xs text-[var(--hc-accent-text)] disabled:opacity-40"
      >
        Bestätigen
      </button>
      <button type="button" onClick={() => setPicking(false)} className="inline-flex min-h-8 items-center rounded-full border border-[var(--hc-border-strong)] px-2.5 text-xs hc-soft">Abbrechen</button>
    </div>
  );
}

function ChainCard({ chain, epicTitle, onEpicClick, openEpics, epicBusy, onAssignEpic, expanded, onToggle, selectedId, onSelect, now, renderTask }: {
  chain: ChainModel<BoardTask>; epicTitle?: string | null; onEpicClick?: (epicId: string) => void;
  openEpics: Epic[]; epicBusy: boolean; onAssignEpic: (chain: ChainModel<BoardTask>, epicId: string | null) => void;
  expanded: boolean; onToggle: () => void;
  selectedId: string | null; onSelect: (id: string) => void; now: number;
  renderTask: (task: BoardTask) => React.ReactNode;
}) {
  const openMembers = chain.members.filter((m) => m.status !== "done");
  const doneMembers = chain.members.filter((m) => m.status === "done");
  const title = chain.root?.title ?? chain.members[0]?.title ?? chain.rootId;
  return (
    <article id={flowChainDomId(chain.rootId)} className={cn("hc-surface-card min-w-0 max-w-full overflow-hidden scroll-mt-4 p-3", chain.blockedCount > 0 && "border-red-500/40")}>
      <div
        role="button"
        tabIndex={0}
        aria-expanded={expanded}
        onClick={onToggle}
        onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onToggle(); } }}
        className="cursor-pointer"
      >
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <span className="hc-mono min-w-0 max-w-full truncate hc-type-label hc-dim">{chain.rootId}</span>
          <span className="rounded-full border border-[var(--hc-border)] px-2 py-0.5 hc-type-label hc-soft">{projectLabel(chain.tenant)}</span>
          {chain.epicId ? (
            <button
              type="button"
              title={de.flow.epicGroupToggle}
              onClick={(e) => { e.stopPropagation(); onEpicClick?.(chain.epicId!); }}
              className="rounded-full border border-indigo-400/25 bg-indigo-400/10 px-2 py-0.5 hc-type-label text-indigo-200 transition hover:bg-indigo-400/20"
            >
              {de.flow.epicBadge(epicTitle || chain.epicId)}
            </button>
          ) : null}
          <span className="ml-auto inline-flex shrink-0 items-center gap-1 hc-type-label hc-soft">
            {expanded ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
            {expanded ? de.flow.chainCollapse : de.flow.chainExpand}
          </span>
        </div>
        <p className="mt-1.5 line-clamp-2 text-sm font-semibold leading-snug text-white">{title}</p>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <ChainStagePills chain={chain} />
          <span className="hc-mono hc-type-label hc-soft">{de.flow.chainMembers(chain.doneCount, chain.total)}</span>
          {chain.blockedCount > 0 ? <StatusPill tone="red" label={`${chain.blockedCount} blockiert`} dot="error" /> : null}
          {chain.runningCount > 0 ? <StatusPill tone="cyan" label={`${chain.runningCount} läuft`} dot="live" /> : null}
        </div>
      </div>
      {expanded ? (
        <div className="mt-3 space-y-2">
          <div onClick={(e) => e.stopPropagation()}>
            <ChainEpicPicker chain={chain} openEpics={openEpics} busy={epicBusy} onAssign={(epicId) => onAssignEpic(chain, epicId)} />
          </div>
          {openMembers.length ? (
            <div className="grid gap-2 sm:grid-cols-2">{openMembers.map((m) => <div key={m.id}>{renderTask(m)}</div>)}</div>
          ) : null}
          {doneMembers.length ? (
            <ul className="space-y-1">
              {doneMembers.map((m) => (
                <li key={m.id}>
                  <button
                    id={flowTaskDomId(m.id)}
                    type="button"
                    onClick={() => onSelect(m.id)}
                    className={cn(
                      "flex w-full min-w-0 flex-wrap items-center gap-2 rounded-md border px-2 py-1.5 text-left transition",
                      selectedId === m.id ? "border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)]" : "border-[var(--hc-border)] hover:border-[var(--hc-border-strong)]",
                    )}
                  >
                    <span className="hc-mono min-w-0 max-w-full truncate hc-type-label hc-dim">{m.id}</span>
                    <span className="min-w-0 flex-1 truncate text-[0.78rem] text-zinc-200">{m.title}</span>
                    <span className="hc-type-label text-emerald-300">✓{m.completed_at ? ` vor ${fmtAge(m.completed_at, now)}` : ""}</span>
                  </button>
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
    </article>
  );
}

// Epic-Gruppen-Header (Gruppier-Toggle): Titel statt roher ID, Fortschritt +
// Token-Burn aus dem GET /epics-Rollup — board-weit, nicht nur die gefilterte
// Sicht. Ohne Rollup (Epics-Endpoint down) degradiert er auf die rohe ID.
// "Epic schließen" ist confirm-gated (Zwei-Schritt wie die Fleet-Aktionen);
// Schließen ist ein organisatorischer Akt, die Tasks bleiben unberührt.
function EpicGroupHeader({ epicId, epic, closeBusy, onClose }: {
  epicId: string | null; epic?: Epic; closeBusy?: boolean; onClose?: (epicId: string) => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const tokens = epic ? (epic.input_tokens ?? 0) + (epic.output_tokens ?? 0) : 0;
  return (
    <div id={epicId ? `epic-group-${epicId}` : undefined} className="flex scroll-mt-4 flex-wrap items-center gap-2">
      <span className="rounded-full border border-indigo-400/25 bg-indigo-400/10 px-2.5 py-0.5 hc-type-label text-indigo-200">
        {epicId ? (epic?.title || epicId) : de.flow.epicGroupNone}
      </span>
      {epic ? (
        <>
          <span className="hc-mono hc-type-label hc-soft">{de.flow.epicGroupProgress(epic.done_tasks, epic.task_count)}</span>
          <span className="hc-mono hc-type-label hc-dim">{tokens > 0 ? de.flow.epicGroupTokens(fmtTokens(tokens)) : de.flow.epicGroupNoTokens}</span>
        </>
      ) : null}
      {epicId && epic?.status === "open" && onClose ? (
        confirming ? (
          <span className="ml-auto flex flex-wrap items-center gap-2">
            <span className="hc-type-label hc-soft">{de.flow.epicCloseConfirm}</span>
            <button
              type="button"
              disabled={closeBusy}
              onClick={() => { onClose(epicId); setConfirming(false); }}
              className="inline-flex min-h-8 items-center rounded-full border border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] px-2.5 text-xs text-[var(--hc-accent-text)] disabled:opacity-40"
            >
              {closeBusy ? de.flow.epicCloseBusy : "Bestätigen"}
            </button>
            <button type="button" onClick={() => setConfirming(false)} className="inline-flex min-h-8 items-center rounded-full border border-[var(--hc-border-strong)] px-2.5 text-xs hc-soft">Abbrechen</button>
          </span>
        ) : (
          <button
            type="button"
            disabled={closeBusy}
            onClick={() => setConfirming(true)}
            className="ml-auto inline-flex min-h-8 items-center rounded-full border border-[var(--hc-border)] px-2.5 text-xs hc-soft transition hover:border-[var(--hc-border-strong)] disabled:opacity-40"
          >
            {closeBusy ? de.flow.epicCloseBusy : de.flow.epicClose}
          </button>
        )
      ) : null}
    </div>
  );
}

type DeliveredItem =
  | { kind: "chain"; at: number; chain: ChainModel<BoardTask> }
  | { kind: "single"; at: number; task: BoardTask };

function DeliveredList({ items, selectedId, onSelect, now, enrichmentById }: {
  items: DeliveredItem[]; selectedId: string | null; onSelect: (id: string) => void; now: number;
  enrichmentById: Record<string, Enriched>;
}) {
  // Historie, nicht Steuerung: standardmäßig eingeklappt (Zähler bleibt),
  // aufklappen nur bei Bedarf — hält die Schaltzentrale kurz.
  const [open, setOpen] = useState(false);
  const shown = items.slice(0, MAX_DELIVERED);
  return (
    <div>
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className="flex min-h-9 items-center gap-2 rounded-md px-1 text-left hover:bg-white/5"
      >
        {open ? <ChevronDown className="h-3.5 w-3.5 hc-soft" /> : <ChevronRight className="h-3.5 w-3.5 hc-soft" />}
        <Eyebrow>{de.flow.deliveredHeading}</Eyebrow>
        <span className="hc-mono rounded-full border border-[var(--hc-border)] px-1.5 hc-type-label hc-soft">{items.length}</span>
      </button>
      {!open ? null : (
      <ul className="mt-2 space-y-1.5">
        {shown.map((item) => {
          const id = item.kind === "chain" ? item.chain.rootId : item.task.id;
          const title = item.kind === "chain" ? (item.chain.root?.title ?? id) : item.task.title;
          // Effektivitäts-Signale (Phase 4): Verifier-Qualität + Liefergegen-
          // stand aus den recent-results — nur gezeigt, wenn real vorhanden.
          const enriched = enrichmentById[id];
          return (
            <li key={`${item.kind}-${id}`}>
              <button
                id={flowTaskDomId(id)}
                type="button"
                onClick={() => onSelect(id)}
                className={cn(
                  "hc-decision hc-sev-calm flex w-full min-w-0 flex-wrap items-center gap-2 px-3 py-2 text-left",
                  selectedId === id && "border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)]",
                )}
              >
                <span className="hc-mono min-w-0 max-w-full truncate hc-type-label hc-dim">{id}</span>
                <span className="min-w-0 flex-1 truncate text-[0.8rem] text-zinc-100">{title}</span>
                {enriched?.resultQualityLabel ? (
                  <span className={cn(
                    "shrink-0 rounded-full border px-1.5 py-0.5 hc-type-label",
                    enriched.resultQualityTone === "emerald" ? "border-emerald-500/25 bg-emerald-500/10 text-emerald-200" : "border-amber-500/25 bg-amber-500/10 text-amber-200",
                  )}>{enriched.resultQualityLabel}</span>
                ) : null}
                {enriched?.deliverableCount ? (
                  <span className="shrink-0 rounded-full border border-cyan-500/25 bg-cyan-500/10 px-1.5 py-0.5 hc-type-label text-cyan-200">
                    {enriched.deliverableCount} Deliverable{enriched.deliverableCount === 1 ? "" : "s"}
                  </span>
                ) : null}
                {item.kind === "chain" ? <span className="hc-mono hc-type-label hc-soft">{item.chain.total} Tasks</span> : null}
                <span className="shrink-0 hc-type-label text-emerald-300">{item.at ? `vor ${fmtAge(item.at, now)}` : ""}</span>
              </button>
            </li>
          );
        })}
      </ul>
      )}
      {open && items.length > shown.length ? <p className="mt-1.5 px-1 hc-type-label hc-dim">{de.flow.deliveredMore(items.length - shown.length)}</p> : null}
    </div>
  );
}

// Bottom-Sheet für die Receipt-Kette unter xl (Handy/Tablet): ersetzt das
// frühere Auto-Scrollen zur unten gestapelten Leiste — der Tap holt das Detail
// zum Operator statt den Operator ans Seitenende zu schieben.
function FlowDetailSheet({ taskId, onClose, children }: { taskId: string; onClose: () => void; children: React.ReactNode }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    // Hintergrund-Scroll sperren, solange das Sheet offen ist (nur dort, wo es
    // sichtbar ist — unter xl; auf Desktop rendert das Sheet gar nicht erst).
    const prevOverflow = document.body.style.overflow;
    if (window.matchMedia("(max-width: 1279px)").matches) document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = prevOverflow;
    };
  }, [onClose]);
  return (
    <div className="fixed inset-0 z-50 xl:hidden">
      <button type="button" aria-label={de.flow.detailClose} onClick={onClose} className="absolute inset-0 bg-black/60" />
      <div
        role="dialog"
        aria-modal="true"
        aria-label={de.flow.selectedChain}
        className="absolute inset-x-0 bottom-0 flex max-h-[85dvh] flex-col rounded-t-2xl border border-b-0 border-[var(--hc-border)] bg-[var(--hc-panel)] shadow-2xl"
      >
        <div className="flex items-center justify-between gap-2 border-b border-[var(--hc-border)] px-4 py-2.5">
          <span className="hc-mono hc-type-label hc-dim truncate">{taskId}</span>
          <button
            type="button"
            onClick={onClose}
            aria-label={de.flow.detailClose}
            className="grid h-9 w-9 shrink-0 place-items-center rounded-lg border border-[var(--hc-border)] hc-soft transition hover:border-[var(--hc-border-strong)]"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain p-3 pb-[calc(0.75rem+env(safe-area-inset-bottom,0px))]">{children}</div>
      </div>
    </div>
  );
}

export function FlowView() {
  const [searchParams, setSearchParams] = useSearchParams();
  const taskParam = searchParams.get("task")?.trim() || null;
  const board = useBoard();
  const workers = useHermesWorkers();
  const reviews = useHermesReviewVerdicts();
  const results = useHermesRecentResults();
  const blocked = useHermesBlockedCompletions();
  const { run: runAction, busyId, errorById } = useTaskAction(board.reload);
  const taskDetail = useTaskDetail();
  const flowRelease = useFlowRelease(board.reload);
  const [releasedById, setReleasedById] = useState<Record<string, number>>({});
  // board.data.now freezes on browser 304 revalidations (the cached body is
  // replayed verbatim), which froze every "vor X" label on an idle board —
  // anchor to the client clock, never regressing below the server stamp.
  // Die Client-Uhr kommt aus einem externen Store (useSyncExternalStore),
  // nicht aus `Date.now()` im Render: Render muss idempotent bleiben.
  const clientNow = useClientNowSeconds();
  const now = Math.max(board.data?.now ?? 0, clientNow);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  // Unter xl öffnet die Task-Auswahl die Receipt-Kette als Bottom-Sheet
  // (Desktop behält die sticky Seitenleiste; dort bleibt das Sheet unsichtbar).
  const [detailSheetOpen, setDetailSheetOpen] = useState(false);
  const [dispatchChoice, setDispatchChoice] = useState<FlowDispatchChoice | null>(null);
  const [checkingDispatchId, setCheckingDispatchId] = useState<string | null>(null);

  const allTasks: BoardTask[] = useMemo(() => board.data?.columns.flatMap((c) => c.tasks) ?? [], [board.data]);

  // Projekt-Filter (tenant-Achse). "all" = kein Filter; Altlasten ohne tenant
  // laufen unter "Unsortiert" (projectKey).
  const [projectFilter, setProjectFilter] = useState<string>("all");
  const projects = useMemo(() => projectOptions(allTasks), [allTasks]);
  const filteredTasks = useMemo(
    () => (projectFilter === "all" ? allTasks : allTasks.filter((t) => projectKey(t.tenant) === projectFilter)),
    [allTasks, projectFilter],
  );
  const chainBoard = useMemo(() => buildChains(filteredTasks), [filteredTasks]);
  const allChainBoard = useMemo(() => buildChains(allTasks), [allTasks]);
  const [expandedRoot, setExpandedRoot] = useState<string | null>(null);
  // Ohne manuelle Wahl ist die dringendste Kette aufgeklappt — aber GEPINNT:
  // die Auto-Wahl wird einmal getroffen und behalten, solange die Kette aktiv
  // bleibt. Vorher folgte sie jedem Urgency-Reorder des 8s-Polls, eine Kette
  // klappte zu, eine andere auf, und die Seite sprang unterm Finger weg.
  // Render-Phase-Anpassung (React-Doku "adjusting state when props change")
  // statt Ref-Mutation im useMemo — Refs dürfen im Render weder gelesen noch
  // geschrieben werden (react-hooks/refs).
  const [autoExpand, setAutoExpand] = useState<string | null>(null);
  const autoExpandValid = autoExpand != null && chainBoard.active.some((c) => c.rootId === autoExpand);
  if (!autoExpandValid) {
    const fallbackRoot = chainBoard.active[0]?.rootId ?? null;
    if (fallbackRoot !== autoExpand) setAutoExpand(fallbackRoot);
  }
  const effectiveExpanded = expandedRoot != null ? (expandedRoot || null) : (autoExpandValid ? autoExpand : chainBoard.active[0]?.rootId ?? null);

  // Epic-Gruppierung (Toggle, default AUS — das Board sieht aus wie immer).
  const epics = useEpics();
  const [groupByEpic, setGroupByEpic] = useState(false);
  const epicsById = useMemo(() => new Map((epics.data?.epics ?? []).map((e) => [e.id, e])), [epics.data]);
  const epicGroups = useMemo(
    () => (groupByEpic ? groupChainsByEpic(chainBoard.active) : null),
    [groupByEpic, chainBoard.active],
  );
  // Badge-Klick: Gruppierung an + zur Epic-Gruppe scrollen (nach dem Re-Render).
  const onEpicBadgeClick = useCallback((epicId: string) => {
    setGroupByEpic(true);
    window.setTimeout(() => document.getElementById(`epic-group-${epicId}`)?.scrollIntoView({ behavior: "smooth", block: "start" }), 80);
  }, []);

  // Epic-Aktionen (Phase 3): anlegen / Kette zuordnen / schließen — nach
  // Erfolg Epics + Board neu laden, damit Badges und Rollups ehrlich bleiben.
  const epicsReload = epics.reload;
  const boardReloadRef = board.reload;
  const epicActions = useEpicActions(useCallback(async () => {
    await Promise.all([epicsReload(), boardReloadRef()]);
  }, [epicsReload, boardReloadRef]));
  const openEpics = useMemo(() => (epics.data?.epics ?? []).filter((e) => e.status === "open"), [epics.data]);
  const onAssignEpic = useCallback((chain: ChainModel<BoardTask>, epicId: string | null) => {
    void epicActions.assignChain(chain.rootId, chain.members.map((m) => m.id), epicId);
  }, [epicActions]);
  const onCloseEpic = useCallback((epicId: string) => {
    void epicActions.closeEpic(epicId);
  }, [epicActions]);

  // Worker-Strip (Fleet-Absorption): Live-Läufe mit Laufzeit-Budget + Aktionen.
  const { inspectByRun, errorByRun, loadingRun, inspect } = useRunInspect();
  const [busyRun, setBusyRun] = useState<string | null>(null);
  const [workerActionError, setWorkerActionError] = useState<string | null>(null);
  const workerList = useMemo(
    () =>
      (workers.data?.workers ?? [])
        .map((w) => ({ ...w, inspect: inspectByRun[w.run_id] ?? w.inspect }))
        // Deterministischer Tie-Break (Start, dann run_id): die Kartenreihen-
        // folge darf nicht mit der Payload-Reihenfolge des Polls wackeln.
        .sort((a, b) =>
          workerSortRank(b, now) - workerSortRank(a, now) ||
          a.started_at - b.started_at ||
          a.run_id.localeCompare(b.run_id)),
    [workers.data, inspectByRun, now],
  );
  const workersReload = workers.reload;
  const onWorkerAction = useCallback(async (runId: string, action: WorkerActionKey) => {
    setBusyRun(runId);
    setWorkerActionError(null);
    try {
      if (action === "terminate") {
        // Eigener Endpoint: SIGTERM→SIGKILL + Task-Reclaim (kein workers/action-Verb).
        await fetchJSON<{ ok?: boolean }>(
          `/api/plugins/kanban/runs/${encodeURIComponent(runId)}/terminate`,
          { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ reason: "Operator-Terminate aus dem Flow-Board" }) },
        );
      } else {
        const res = await fetchJSON<{ ok?: boolean; detail?: string }>(
          `/api/plugins/kanban/workers/${encodeURIComponent(runId)}/action`,
          { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action, confirm: true }) },
        );
        if (res.ok === false) setWorkerActionError(res.detail || de.worker.actionFailed);
      }
    } catch (e) {
      setWorkerActionError(`${de.worker.actionFailed}: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusyRun(null);
      await workersReload();
    }
  }, [workersReload]);

  const enrichmentById = useMemo(() => {
    const map: Record<string, Enriched> = {};
    for (const w of workers.data?.workers ?? []) {
      map[w.task_id] = { ...map[w.task_id], workerProfile: w.profile, workerHeartbeat: w.last_heartbeat_at || null };
    }
    for (const r of reviews.data?.reviews ?? []) {
      map[r.task_id] = {
        ...map[r.task_id],
        verdict: r.active_verifier ? "Verifier läuft" : r.verifier_verdict,
        verifierEvidenceCount: r.verifier_evidence?.length ?? 0,
        activeVerifier: r.active_verifier,
        activeRunId: r.active_run_id,
        reviewRunState: r.review_run_state,
        reviewVerdictAt: r.active_verifier ? null : r.submitted_at ?? null,
      };
    }
    for (const b of blocked.data?.blocked ?? []) {
      map[b.task_id] = { ...map[b.task_id], blockedKind: b.kind, blockedReason: b.fix_summary || b.summary_preview || null };
    }
    for (const res of results.data?.results ?? []) {
      const artifactLinks = res.artifact_links ?? [];
      map[res.task_id] = {
        ...map[res.task_id],
        resultQualityLabel: res.result_quality?.label ?? null,
        resultQualityTone: res.result_quality?.tone ?? null,
        deliverableCount: Math.max(res.deliverables?.length ?? 0, artifactLinks.length),
        resultArtifactLinks: artifactLinks,
      };
    }
    return map;
  }, [workers.data, reviews.data, blocked.data, results.data]);

  // Manueller Review-Fallback pro Task: rein abgeleitet aus Server-Daten
  // (Verdict-Zeitstempel des Review-Runs), kein Client-Zustand. Solange der
  // Verifier LÄUFT, bleibt das Gate exklusiv (shouldExposeManualReviewFallback
  // gibt dann immer false zurück).
  const manualReviewFallbackById = useMemo<Record<string, boolean>>(() => {
    const fallback: Record<string, boolean> = {};
    for (const task of allTasks) {
      const enriched = enrichmentById[task.id] ?? EMPTY_ENRICHED;
      if (task.status !== "review" || terminalVerifierGateKey(enriched) == null) continue;
      fallback[task.id] = shouldExposeManualReviewFallback(enriched, now);
    }
    return fallback;
  }, [allTasks, enrichmentById, now]);

  // One-Shot-Buchhaltung für ?task=-Deep-Links. `handledTaskParam` lebt als
  // State (im Render lesbar — Refs sind das nicht, react-hooks/refs);
  // `deepLinkScrolledRef` one-shottet die Scroll-/Fetch-Seite im Effekt.
  const [handledTaskParam, setHandledTaskParam] = useState<string | null>(null);
  const deepLinkScrolledRef = useRef<string | null>(null);
  const reloadBoard = board.reload;
  const fetchDetail = taskDetail.fetch;

  const setSelectedTask = useCallback((id: string) => {
    setSelectedId(id);
    // Selbst gesetzte ?task=-Werte sind KEINE Deep-Links: vorab als behandelt
    // markieren, sonst feuerte der Deep-Link-Effekt nach jedem Tap erneut und
    // scrollte die Karte in die Mitte (und kämpfte mit dem Rail-Scroll —
    // der "Seite springt beim Antippen"-Bug).
    setHandledTaskParam(id);
    deepLinkScrolledRef.current = id;
    setSearchParams((current) => {
      const next = new URLSearchParams(current);
      next.set("task", id);
      return next;
    }, { replace: true });
  }, [setSearchParams]);

  const selectTask = useCallback((id: string) => {
    setSelectedTask(id);
    if (!taskDetail.detailById[id]) void fetchDetail(id);
    // Unter xl liegt die Receipt-Leiste nicht mehr unten auf der Seite —
    // statt Auto-Scroll ans Seitenende öffnet das Detail als Bottom-Sheet.
    if (typeof window !== "undefined" && window.matchMedia("(max-width: 1279px)").matches) {
      setDetailSheetOpen(true);
    }
  }, [fetchDetail, setSelectedTask, taskDetail.detailById]);
  const clearDispatchChoice = useCallback(() => setDispatchChoice(null), []);
  const runTaskAction = useCallback((taskId: string, action: StageAction) => {
    // Terminal verifier fallback can race the verifier's own status PATCH; the
    // backend PATCH is idempotent toward the requested target status, so the
    // losing side observes an already-transitioned card on the next board poll.
    void runAction(taskId, action.target, action.key === "rework" ? { block_reason: "Operator-Nacharbeit aus dem Flow-Board" } : undefined);
    if (selectedId === taskId) void fetchDetail(taskId);
  }, [runAction, selectedId, fetchDetail]);
  const onAct = useCallback((task: BoardTask, action: StageAction) => {
    if (action.key !== "dispatch") {
      runTaskAction(task.id, action);
      return;
    }

    setSelectedTask(task.id);
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
  }, [runTaskAction, setSelectedTask, taskDetail.detailById, fetchDetail, allTasks]);
  // Release a gated plan: unblock the held subtasks, then refresh the board +
  // this root's detail so the held banner clears and the children move on.
  const onGateChanged = useCallback(async () => {
    await reloadBoard();
    if (selectedId) void fetchDetail(selectedId);
  }, [reloadBoard, selectedId, fetchDetail]);
  const onReleasePlan = useCallback((rootId: string, n: number, options?: FlowReleaseOptions) => {
    void flowRelease.release(rootId, options).then((res) => {
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
    setSelectedTask(taskId);
    void reloadBoard();
    void fetchDetail(taskId);
  }, [reloadBoard, fetchDetail, setSelectedTask]);

  const counts = useMemo(() => flowCounts(filteredTasks), [filteredTasks]);
  const fresh = freshness(board.lastUpdated, 8000, now);
  const hasAnyFiltered = filteredTasks.some((t) => t.status !== "archived");

  // Geliefert-Liste: fertige Ketten + fertige Einzeltasks, jüngste zuerst.
  const deliveredItems = useMemo<DeliveredItem[]>(() => {
    const items: DeliveredItem[] = [
      ...chainBoard.done.map((chain) => ({ kind: "chain" as const, at: chain.latestCompletedAt ?? 0, chain })),
      ...chainBoard.doneSingles.map((task) => ({ kind: "single" as const, at: task.completed_at ?? 0, task })),
    ];
    items.sort((a, b) => b.at - a.at);
    return items;
  }, [chainBoard]);

  // Eine Task-Karte mit kompletter Aktions-Mechanik — von Ketten-Mitgliedern
  // und Einzeltasks geteilt (FlowRunCard bleibt memoisiert; die Props hier
  // sind dieselben stabilen Referenzen wie vorher).
  const renderTaskCard = useCallback((task: BoardTask) => {
    const taskDispatchChoice = dispatchChoice?.taskId === task.id ? dispatchChoice : null;
    const isBusy = busyId === task.id || checkingDispatchId === task.id || (taskDispatchChoice != null && flowRelease.busyId === taskDispatchChoice.rootId);
    return (
      <FlowRunCard
        task={task}
        enriched={enrichmentById[task.id] ?? EMPTY_ENRICHED}
        selected={task.id === selectedId}
        busy={isBusy}
        error={errorById[task.id] || undefined}
        now={now}
        dispatchChoice={taskDispatchChoice}
        manualReviewFallback={manualReviewFallbackById[task.id]}
        onSelect={selectTask}
        onReleaseChain={onReleaseChain}
        onDispatchSingle={onDispatchSingle}
        onCancelDispatchChoice={clearDispatchChoice}
        onAct={onAct}
      />
    );
  }, [dispatchChoice, busyId, checkingDispatchId, flowRelease.busyId, enrichmentById, selectedId, errorById, now, manualReviewFallbackById, selectTask, onReleaseChain, onDispatchSingle, clearDispatchChoice, onAct]);

  const selectedTask = selectedId ? allTasks.find((t) => t.id === selectedId) : undefined;
  const selectedStatus = selectedTask?.status;
  const loadingFirst = board.loading && board.data == null;
  const hasAnyRun = allTasks.length > 0;
  const boardSourceErrors = board.data?.source_errors ?? [];

  // Deep-Link-Anwendung als Render-Phase-Anpassung (React-Doku "adjusting
  // state when props change") statt setState im Effekt (react-hooks/
  // set-state-in-effect). One-shot per param value: the data deps get a new
  // identity on every 8s board poll, and re-running the filter/expand block
  // on each tick yanked the page back to the card, re-expanded a manually
  // collapsed chain and reverted manual project-filter choices.
  if (taskParam && handledTaskParam !== taskParam && allTasks.length > 0) {
    const task = allTasks.find((item) => item.id === taskParam);
    if (task) {
      setHandledTaskParam(taskParam);
      if (selectedId !== taskParam) setSelectedId(taskParam);
      const targetProject = projectKey(task.tenant);
      if (projectFilter !== "all" && projectFilter !== targetProject) {
        setProjectFilter(targetProject);
      }
      const targetChain = [...allChainBoard.active, ...allChainBoard.done].find(
        (chain) => chain.rootId === taskParam || chain.members.some((member) => member.id === taskParam),
      );
      if (targetChain && expandedRoot !== targetChain.rootId) setExpandedRoot(targetChain.rootId);
      // Echte Deep-Links (z.B. aus dem Inbox-Tab) sollen das Detail auf
      // Handy/Tablet direkt zeigen — gleiche Mechanik wie ein Tap.
      if (typeof window !== "undefined" && window.matchMedia("(max-width: 1279px)").matches && !detailSheetOpen) {
        setDetailSheetOpen(true);
      }
    }
  }

  // Die impure Seite des Deep-Links (Detail-Fetch + Scroll) bleibt im Effekt;
  // `deepLinkScrolledRef` one-shottet sie pro Wert, damit neue Identitäten
  // der Daten-Deps (8s-Poll) NICHT erneut scrollen. Selbst gesetzte ?task=-
  // Werte sind im Tap-Handler vormarkiert und scrollen nie.
  useEffect(() => {
    if (!taskParam || handledTaskParam !== taskParam) return;
    if (deepLinkScrolledRef.current === taskParam) return;
    deepLinkScrolledRef.current = taskParam;
    if (!taskDetail.detailById[taskParam]) void fetchDetail(taskParam);
    scrollToFlowTask(taskParam);
  }, [taskParam, handledTaskParam, fetchDetail, taskDetail.detailById]);

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
      <Hero
        eyebrow={de.flow.eyebrow}
        count={loadingFirst ? "—" : counts.running}
        countHint={!hasAnyRun ? de.flow.heroHintCalm : counts.running > 0 ? de.flow.heroHint(counts.running) : de.flow.heroHintParked}
        // Auf dem Handy trägt schon Zahl + countHint die Aussage — Statement-
        // Zeile und Erklär-Subtitle sind dort nur Scrollweg (Operator-Wunsch).
        title={<span className="hidden sm:inline">{!hasAnyRun && !loadingFirst ? de.flow.heroLeadCalm : counts.running > 0 ? de.flow.heroLead(counts.running) : de.flow.heroLeadParked}</span>}
        subtitle={<span className="hidden sm:inline">{de.flow.subtitle}</span>}
        tone={counts.blocked > 0 ? "amber" : counts.running > 0 ? "cyan" : hasAnyRun ? "violet" : "emerald"}
        status={board.lastUpdated ? {
          label: fresh.stale ? de.flow.paused : de.flow.updated(fresh.label.replace("vor ", "")),
          tone: fresh.stale ? "amber" : "emerald",
          dot: fresh.stale ? "warn" : "live",
        } : undefined}
        action={
          <div className="flex flex-wrap items-center justify-end gap-2">
            <StaleBadge isStale={board.isStale} lastUpdated={board.lastUpdated} errorObj={board.errorObj} error={board.error} now={now} />
            <button type="button" onClick={() => void board.reload()} aria-label={de.flow.refresh} className="inline-flex h-9 w-9 items-center justify-center rounded-full border border-[var(--hc-border)] hc-soft transition hover:border-[var(--hc-border-strong)]"><RefreshCw className="h-4 w-4" /></button>
            <EpicCreate onCreated={() => void epicsReload()} />
            <FlowCapture onCreated={onCaptured} />
          </div>
        }
      >
        <div className={cn("grid gap-2", counts.blocked > 0 ? "grid-cols-3" : "grid-cols-2")}>
          <FleetPod label={de.flow.wip} value={loadingFirst ? "—" : counts.wip} />
          <FleetPod label={de.flow.review} dot="warn" value={loadingFirst ? "—" : counts.review} />
          {counts.blocked > 0 ? <FleetPod label={de.flow.rework} dot="error" value={loadingFirst ? "—" : counts.blocked} /> : null}
        </div>
      </Hero>

      {board.error ? <ToneCallout tone="red">{de.flow.loadError}<br />{board.error}</ToneCallout> : null}
      {boardSourceErrors.length ? (
        <ToneCallout tone="amber">
          {de.flow.sourceErrorTitle}
          {boardSourceErrors.map((err) => (
            <div key={`${err.artifact}-${err.source}-${err.stage}-${err.message}`} className="mt-1">
              <span className="hc-mono">{err.artifact}</span>: {err.message}
              <br />
              <span className="hc-dim">{de.flow.sourceErrorContext(err.source, err.stage, err.retry_count)}</span>
            </div>
          ))}
        </ToneCallout>
      ) : null}

      <RecoveryStrip />

      {/* Phase F (Programm 3): Fehler-Triage — failed/blocked 48h mit
          „Nochmal" / „Nochmal stärker" (model_override-Eskalation). Rendert
          nichts, wenn es nichts zu triagieren gibt. */}
      <TriageStrip />

      {/* Funnel-Freigaben — fertige Drafts aus dem Wunsch-Trichter, die auf
          den Operator-Klick warten (Freigeben = Build-Task als Ketten-Kind). */}
      <FunnelFreigaben />

      {/* Worker-Strip — die absorbierte Flotte: Live-Läufe mit Laufzeit-Budget,
          Runaway-Wache und vollen Aktionen (alle confirm-gated). Ruht die
          Flotte (geladen, kein Fehler, keine Läufe), entfällt das Panel —
          den Ruhe-Zustand trägt schon der Hero (Schaltzentrale statt Wand). */}
      {workerList.length === 0 && !workers.error && !workerActionError && workers.data != null ? null : (
      <FleetPanel eyebrow={de.flow.workersHeading} meta={<span className="hidden sm:inline">{de.flow.workersHint}</span>}>
        {workers.error ? <ToneCallout tone="red">{workers.error}</ToneCallout> : null}
        {workerActionError ? <div className="mb-3"><ToneCallout tone="red">{workerActionError}</ToneCallout></div> : null}
        {workerList.length && errorByRun[workerList[0].run_id] ? <div className="mb-3"><ToneCallout tone="amber">{de.worker.actionFailed}: {errorByRun[workerList[0].run_id]}</ToneCallout></div> : null}
        {workers.loading && workers.data == null ? (
          <div className="grid gap-3 lg:grid-cols-2"><SkeletonCard rows={4} /><SkeletonCard rows={4} /></div>
        ) : workerList.length === 0 ? (
          <FleetEmptyState ok title={de.flow.workersEmptyTitle} desc={de.flow.workersEmptyDesc} />
        ) : (
          <div className="grid gap-3 lg:grid-cols-2">
            {workerList.map((worker) => (
              <WorkerCard
                key={worker.run_id}
                worker={worker}
                health={workerHealth(worker, now)}
                density="airy"
                collapsible
                now={now}
                inspectLoading={loadingRun === worker.run_id}
                onInspect={inspect}
                onAction={onWorkerAction}
                actionBusy={busyRun === worker.run_id}
              />
            ))}
          </div>
        )}
      </FleetPanel>
      )}

      {loadingFirst ? (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3"><SkeletonCard rows={4} /><SkeletonCard rows={4} /><SkeletonCard rows={4} /></div>
      ) : (
        <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_340px]">
          <div className="min-w-0 space-y-5">
            {/* Projekt-Filter (tenant-Achse) */}
            {projects.length > 1 ? (
              // Mobil eine wischbare Zeile statt vieler Wrap-Zeilen (das
              // Chip-Feld wuchs auf ~8 Zeilen); ab sm wie gehabt umbrechend.
              <div className="-mx-1 flex items-center gap-1.5 overflow-x-auto px-1 pb-1 [scrollbar-width:none] sm:mx-0 sm:flex-wrap sm:overflow-x-visible sm:px-0 sm:pb-0">
                <span className="hc-eyebrow mr-1 shrink-0">{de.flow.projects}</span>
                {[{ key: "all", label: de.flow.projectAll, count: allTasks.filter((t) => t.status !== "archived").length }, ...projects].map((p) => (
                  <button
                    key={p.key}
                    type="button"
                    onClick={() => setProjectFilter(p.key)}
                    className={cn(
                      "inline-flex min-h-8 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full border px-2.5 text-xs transition",
                      projectFilter === p.key
                        ? "border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] text-[var(--hc-accent-text)]"
                        : "border-[var(--hc-border)] hc-soft hover:border-[var(--hc-border-strong)]",
                    )}
                  >
                    {p.label}<span className="hc-mono hc-type-label opacity-70">{p.count}</span>
                  </button>
                ))}
              </div>
            ) : null}

            {!hasAnyFiltered ? (
              <FleetEmptyState title={de.flow.emptyTitle} desc={de.flow.emptyDesc} />
            ) : (
              <>
                {/* Aktive Ketten — die primäre Einheit des Boards */}
                {chainBoard.active.length ? (
                  <section>
                    <div className="flex flex-wrap items-center gap-2">
                      <Eyebrow>{de.flow.chainsHeading}</Eyebrow>
                      <span className="hc-mono rounded-full border border-[var(--hc-border)] px-1.5 hc-type-label hc-soft">{chainBoard.active.length}</span>
                      <button
                        type="button"
                        aria-pressed={groupByEpic}
                        onClick={() => setGroupByEpic((v) => !v)}
                        className={cn(
                          "ml-auto inline-flex min-h-8 items-center rounded-full border px-2.5 text-xs transition",
                          groupByEpic
                            ? "border-indigo-400/40 bg-indigo-400/10 text-indigo-200"
                            : "border-[var(--hc-border)] hc-soft hover:border-[var(--hc-border-strong)]",
                        )}
                      >
                        {de.flow.epicGroupToggle}
                      </button>
                    </div>
                    {epicActions.error ? <div className="mt-2"><ToneCallout tone="red">{epicActions.error}</ToneCallout></div> : null}
                    {epicGroups ? (
                      <div className="mt-2 space-y-4">
                        {epicGroups.map((group) => (
                          <div key={group.epicId ?? "none"}>
                            <EpicGroupHeader
                              epicId={group.epicId}
                              epic={group.epicId ? epicsById.get(group.epicId) : undefined}
                              closeBusy={group.epicId != null && epicActions.busyKey === group.epicId}
                              onClose={onCloseEpic}
                            />
                            <div className="mt-2 space-y-2.5">
                              {group.chains.map((chain) => (
                                <ChainCard
                                  key={chain.rootId}
                                  chain={chain}
                                  epicTitle={chain.epicId ? epicsById.get(chain.epicId)?.title ?? null : null}
                                  onEpicClick={onEpicBadgeClick}
                                  openEpics={openEpics}
                                  epicBusy={epicActions.busyKey === chain.rootId}
                                  onAssignEpic={onAssignEpic}
                                  expanded={effectiveExpanded === chain.rootId}
                                  onToggle={() => setExpandedRoot(effectiveExpanded === chain.rootId ? "" : chain.rootId)}
                                  selectedId={selectedId}
                                  onSelect={selectTask}
                                  now={now}
                                  renderTask={renderTaskCard}
                                />
                              ))}
                            </div>
                          </div>
                        ))}
                      </div>
                    ) : (
                      <div className="mt-2 space-y-2.5">
                        {chainBoard.active.map((chain) => (
                          <ChainCard
                            key={chain.rootId}
                            chain={chain}
                            epicTitle={chain.epicId ? epicsById.get(chain.epicId)?.title ?? null : null}
                            onEpicClick={onEpicBadgeClick}
                            openEpics={openEpics}
                            epicBusy={epicActions.busyKey === chain.rootId}
                            onAssignEpic={onAssignEpic}
                            expanded={effectiveExpanded === chain.rootId}
                            onToggle={() => setExpandedRoot(effectiveExpanded === chain.rootId ? "" : chain.rootId)}
                            selectedId={selectedId}
                            onSelect={selectTask}
                            now={now}
                            renderTask={renderTaskCard}
                          />
                        ))}
                      </div>
                    )}
                  </section>
                ) : null}

                {/* Einzelne Aufgaben (keine Kette) */}
                {chainBoard.singles.length ? (
                  <section>
                    <div className="flex items-center gap-2">
                      <Eyebrow>{de.flow.singlesHeading}</Eyebrow>
                      <span className="hc-mono rounded-full border border-[var(--hc-border)] px-1.5 hc-type-label hc-soft">{chainBoard.singles.length}</span>
                    </div>
                    <div className="mt-2 grid gap-2 sm:grid-cols-2">
                      {chainBoard.singles.slice(0, MAX_CARDS).map((task) => <div key={task.id}>{renderTaskCard(task)}</div>)}
                    </div>
                    {chainBoard.singles.length > MAX_CARDS ? <p className="mt-1.5 px-1 hc-type-label hc-dim">+ {chainBoard.singles.length - MAX_CARDS} weitere</p> : null}
                  </section>
                ) : null}

                {/* Geliefert — Ketten + Einzeltasks, jüngste zuerst */}
                {deliveredItems.length ? (
                  <DeliveredList items={deliveredItems} selectedId={selectedId} onSelect={selectTask} now={now} enrichmentById={enrichmentById} />
                ) : null}
              </>
            )}
          </div>
          {/* Desktop (xl+): sticky Seitenleiste wie gehabt. Darunter ersetzt
              das Bottom-Sheet die früher unten gestapelte Leiste — die Seite
              wird kürzer und der Tap-Scroll ans Seitenende entfällt. */}
          <div className="hidden xl:block">
            <FlowReceiptRail
              taskId={selectedId}
              task={selectedTask}
              detail={selectedId ? taskDetail.detailById[selectedId] : undefined}
              enriched={selectedId ? enrichmentById[selectedId] : undefined}
              loading={taskDetail.loadingId === selectedId}
              error={selectedId ? taskDetail.errorById[selectedId] : undefined}
              now={now}
              boardTasks={allTasks}
              snapshotLabel={board.lastUpdated ? (fresh.stale ? de.flow.paused : fresh.label) : "unbekannt"}
              onRelease={onReleasePlan}
              releaseBusy={flowRelease.busyId === selectedId}
              releaseError={selectedId ? flowRelease.errorById[selectedId] : undefined}
              released={selectedId ? releasedById[selectedId] : undefined}
              onGateChanged={onGateChanged}
            />
          </div>
        </div>
      )}

      <PlanSpecHub onIngested={onCaptured} />

      {detailSheetOpen && selectedId ? (
        <FlowDetailSheet taskId={selectedId} onClose={() => setDetailSheetOpen(false)}>
          <FlowReceiptRail
            taskId={selectedId}
            task={selectedTask}
            detail={taskDetail.detailById[selectedId]}
            enriched={enrichmentById[selectedId]}
            loading={taskDetail.loadingId === selectedId}
            error={taskDetail.errorById[selectedId]}
            now={now}
            boardTasks={allTasks}
            snapshotLabel={board.lastUpdated ? (fresh.stale ? de.flow.paused : fresh.label) : "unbekannt"}
            onRelease={onReleasePlan}
            releaseBusy={flowRelease.busyId === selectedId}
            releaseError={flowRelease.errorById[selectedId]}
            released={releasedById[selectedId]}
            onGateChanged={onGateChanged}
          />
        </FlowDetailSheet>
      ) : null}
    </div>
  );
}
