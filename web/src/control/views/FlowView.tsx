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
import { createPortal } from "react-dom";
import { AlertTriangle, ArrowRight, Check, ChevronDown, ChevronRight, Copy, FileText, HeartPulse, Loader2, Lock, MessageSquarePlus, Play, RefreshCw, Send, ShieldCheck, X } from "lucide-react";
import { Link, useSearchParams } from "react-router-dom";
import { cn } from "@/lib/utils";
import { fetchJSON, openAuthedApiFile } from "@/lib/api";
import { de } from "../i18n/de";
import { TONE_HEX, profileLabel, taskStatusLabel } from "../lib/tones";
import { deriveCapacity, fmtAge, fmtDur, fmtTokens, formatEffectiveCost, freshness, workerHealth, workerSortRank } from "../lib/derive";
import {
  FLEET_STAGES,
  STAGE_META,
  buildChains,
  chainReviewTier,
  chainActiveReviewStage,
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
import { countActionableFailures, countOpenFunnelDrafts, summarizeFlowAttention } from "../lib/flowAttention";
import { getHeldFlowDispatchGuard, getHeldFlowRootGuard, type HeldFlowDispatchGuard } from "../lib/flowDispatchGuard";
import {
  useBoard,
  useDispositionItems,
  useEpicActions,
  useEpics,
  useFlowGate,
  useFlowRelease,
  useFlowTriageFailures,
  useFunnelDrafts,
  useKanbanDecisionQueue,
  useHermesBlockedCompletions,
  useHermesRecentResults,
  useHermesReviewVerdicts,
  usePlanSpecs,
  usePlanSpecDetail,
  useSystemHealth,
  useHermesWorkers,
  useRunInspect,
  useTaskAction,
  useTaskDetail,
} from "../hooks/useControlData";
import { PlanSpecDetailDrawer } from "./flow/PlanSpecDetailDrawer";
import { planSpecClosedDispositionLabel, planSpecIsClosed, planSpecKanbanLabel, planSpecKanbanTone } from "./flow/planSpecKanban";
import type { ActiveReviewStage, BoardTask, FlowGateReleaseLevel, FlowReleaseOptions, PlanSpecCloseResponse, PlanSpecIngestResponse, PlanSpecPromptResponse, PlanSpecRecord, ReviewTier, TaskArtifactLink, TaskDeliverable, TaskStatus, ToneName } from "../lib/types";
import { isIsolatedWorkspace } from "../lib/types";
import type { Epic, KanbanDecision, TaskDetailResponse } from "../lib/schemas";
import { StaleBadge, StatusPill, ToneCallout } from "../components/atoms";
import { TriageStrip } from "../components/TriageStrip";
import { FunnelFreigaben } from "../components/FunnelFreigaben";
import { DispositionLifecycle } from "../components/DispositionLifecycle";
import { Hero } from "../components/Hero";
import { Eyebrow, SkeletonCard } from "../components/primitives";
import { FleetPod, FleetEmptyState, FleetPanel, RoleChip } from "../components/fleet/atoms";
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


function recoveryDecisionMeta(kind: string): { label: string; tone: ToneName; dot: "live" | "ready" | "warn" | "error" | "idle" } {
  switch (kind) {
    case "review_rejected":
      return { label: "Review abgelehnt", tone: "red", dot: "error" };
    case "budget_held":
      return { label: "Budget gehalten", tone: "amber", dot: "warn" };
    case "role_fit_held":
      return { label: "Rolle unklar", tone: "amber", dot: "warn" };
    case "operator_escalation":
      return { label: "Operator nötig", tone: "amber", dot: "warn" };
    case "integration_parked":
      return { label: "Integration geparkt", tone: "violet", dot: "idle" };
    case "rate_limited_loop":
      return { label: "Rate-Limit Loop", tone: "red", dot: "error" };
    case "release_gate_parked":
      return { label: "Kette bereit", tone: "violet", dot: "idle" };
    case "tree_root_woke":
      return { label: "Root wach", tone: "emerald", dot: "ready" };
    case "decompose_failed":
      return { label: "Decompose fehlgeschlagen", tone: "red", dot: "error" };
    case "stranded_by_stuck_parent":
      return { label: "Parent blockiert", tone: "amber", dot: "warn" };
    case "deliverable_posted_not_completed":
      return { label: "Repair nötig", tone: "amber", dot: "warn" };
    default:
      return { label: kind, tone: "zinc", dot: "idle" };
  }
}

const RECOVERY_HIDDEN_KINDS = new Set(["informational", "noop"]);

export function RecoveryDecisionCard({ row }: { row: KanbanDecision }) {
  const meta = recoveryDecisionMeta(row.kind);
  const nextAction = row.operator_escalation?.recommended_human_action || row.suggested_command || "";
  const nextActionIsCommand = Boolean(!row.operator_escalation?.recommended_human_action && row.suggested_command);
  return (
    <li className="rounded-[8px] border border-[var(--hc-border)] bg-[var(--hc-panel)] px-[11px] py-1.5">
      <div className="flex items-center justify-between gap-2">
        <span className="min-w-0 break-words text-xs font-semibold text-white">{row.title}</span>
        <StatusPill tone={meta.tone} label={meta.label} dot={meta.dot} />
      </div>
      <p className="mt-1 break-words text-xs hc-soft">{row.reason}</p>
      {nextAction ? (
        <section className="mt-2 rounded-md border border-amber-400/35 bg-amber-100/55 px-2 py-1.5">
          <p className="text-[9px] font-semibold uppercase tracking-[.14em] text-amber-700">Nächste Aktion</p>
          <p className={cn("mt-1 break-words text-xs text-[var(--hc-text)]", nextActionIsCommand && "break-all hc-mono text-[10px] text-cyan-800")}>
            {nextAction}
          </p>
        </section>
      ) : null}
      {row.suggested_command && !nextActionIsCommand ? (
        <p className="mt-1 break-all hc-mono text-[10px] text-cyan-100">{row.suggested_command}</p>
      ) : null}
      {row.operator_escalation?.blocked_action_boundary?.length ? (
        <p className="mt-1 break-words hc-mono text-[10px] hc-dim">Grenze: {row.operator_escalation.blocked_action_boundary.join(", ")}</p>
      ) : null}
    </li>
  );
}

function RecoveryStrip() {
  const health = useSystemHealth();
  const decisions = useKanbanDecisionQueue();
  const dispatcher = health.data?.subsystems.kanban_dispatcher;
  const recoveryRows = (decisions.data?.decisions ?? []).filter((d) => !RECOVERY_HIDDEN_KINDS.has(d.kind));
  const tone = dispatcher?.status === "healthy"
    ? "emerald"
    : dispatcher?.status === "degraded"
      ? "amber"
      : "red";

  if (!dispatcher && recoveryRows.length === 0 && !health.error && !decisions.error) {
    return null;
  }

  return (
    <div className="rounded-[12px] border border-[var(--hc-border)] bg-[var(--hc-panel-card)] px-4 py-[11px] shadow-[inset_0_1px_0_#fff]">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <span className="hc-eyebrow">Recovery</span>
          {dispatcher ? (
            <StatusPill
              tone={tone}
              label={`Dispatcher · ${dispatcher.detail || dispatcher.status || "unbekannt"}`}
              dot={dispatcher.status === "healthy" ? "live" : dispatcher.status === "degraded" ? "warn" : "error"}
            />
          ) : null}
          {health.error ? <span className="text-xs text-amber-200">{health.error}</span> : null}
          {decisions.error ? <span className="text-xs text-amber-200">{decisions.error}</span> : null}
        </div>
        <span className="hc-mono text-[11px] text-[var(--hc-text-dim)]">
          {recoveryAgeLabel(dispatcher?.heartbeat_age_s)} · {recoveryRows.length} offen
        </span>
      </div>
      {recoveryRows.length ? (
        <ul className="mt-3 grid max-h-72 gap-2 overflow-y-auto pr-1 lg:grid-cols-3">
          {recoveryRows.map((row) => <RecoveryDecisionCard key={`${row.kind}:${row.task_id}`} row={row} />)}
        </ul>
      ) : (
        <p className="mt-3 text-sm hc-dim">Keine Recovery-Parks.</p>
      )}
    </div>
  );
}

// F4 — Kapazitäts-/Engpass-Banner: schlanke Leiste, kein eigener Poll.
// Nutzt die schon vorhandenen workers- und board-Hooks.
interface CapacityBannerProps {
  count: number;
  cap: number | null;
  queueDepth: number;
}

function CapacityBanner({ count, cap, queueDepth }: CapacityBannerProps) {
  // Zeige die Leiste nur wenn mindestens ein Worker läuft oder Tasks warten.
  if (count === 0 && queueDepth === 0) return null;
  const state = deriveCapacity(count, cap, queueDepth);
  return (
    <div
      data-testid="capacity-banner"
      className={cn(
        "flex flex-wrap items-center gap-x-3 gap-y-1 rounded-[10px] border px-3 py-2 text-xs",
        state.bottleneck
          ? "border-amber-400/40 bg-amber-400/10 text-amber-200"
          : "border-[var(--hc-border)] bg-[var(--hc-panel)] text-[var(--hc-text-soft)]",
      )}
    >
      <span className="hc-mono font-medium">
        {cap != null
          ? de.flow.capacityWorkers(count, cap)
          : de.flow.capacityWorkersNoCap(count)}
      </span>
      {queueDepth > 0 ? (
        <span className="hc-mono opacity-80">
          {de.flow.capacityQueue(queueDepth)}
        </span>
      ) : null}
      {state.bottleneck ? (
        <span className="ml-auto font-semibold text-amber-300">
          {de.flow.capacityBottleneck(queueDepth)}
        </span>
      ) : null}
    </div>
  );
}

function planSpecKanbanProgress(item: PlanSpecRecord): string | null {
  if (!item.kanban_root_task_id || item.kanban_child_total <= 0) return null;
  const bits = [`${item.kanban_child_done}/${item.kanban_child_total} done`];
  if (item.kanban_child_running > 0) bits.push(`${item.kanban_child_running} läuft`);
  if (item.kanban_child_blocked > 0) bits.push(`${item.kanban_child_blocked} blocked`);
  return bits.join(" · ");
}


function PlanSpecHub({ onIngested }: { onIngested: (rootTaskId: string) => void }) {
  const [planspecSearch, setPlanspecSearch] = useState("");
  const [openOnly, setOpenOnly] = useState(false);
  const plans = usePlanSpecs({ scope: openOnly ? "open" : "all", limit: 8, search: planspecSearch });
  const [plansOpen, setPlansOpen] = useState(false);
  const [busyPath, setBusyPath] = useState<string | null>(null);
  const [errorByPath, setErrorByPath] = useState<Record<string, string>>({});
  const [promptByPath, setPromptByPath] = useState<Record<string, string>>({});
  const [detailItem, setDetailItem] = useState<PlanSpecRecord | null>(null);
  const detail = usePlanSpecDetail(detailItem?.path ?? null);
  const items = plans.data?.planspecs ?? [];
  const openCount = items.filter((item) => item.open).length;
  const hasFilters = Boolean(planspecSearch.trim()) || openOnly;

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

  const markNotNeeded = useCallback(async (item: PlanSpecRecord) => {
    setBusyPath(`${item.path}:close`);
    setRowError(item.path, null);
    try {
      await fetchJSON<PlanSpecCloseResponse>("/api/plugins/kanban/planspecs/not-needed", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: item.path, author: "dashboard" }),
      });
      setPromptByPath((current) => {
        const next = { ...current };
        delete next[item.path];
        return next;
      });
      await plans.reload();
    } catch (e) {
      setRowError(item.path, e instanceof Error ? e.message : String(e));
    } finally {
      setBusyPath(null);
    }
  }, [plans, setRowError]);

  if (!plans.loading && !plans.error && items.length === 0 && !hasFilters) return null;

  return (
    <div className="rounded-[14px] border border-[var(--hc-border)] bg-[var(--hc-panel-card)] px-[18px] py-4 shadow-[var(--hc-elev-1)]">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="hc-eyebrow">Planspec-Hub</span>
          <span className="hc-mono rounded-full border border-[var(--hc-border)] bg-[rgba(26,29,40,.05)] px-2 py-0.5 hc-type-label hc-soft">{openCount}/{items.length} offen · Vault</span>
        </div>
        <button
          type="button"
          aria-expanded={plansOpen}
          onClick={() => setPlansOpen((value) => !value)}
          className="inline-flex min-h-8 items-center gap-1.5 rounded-full border border-[var(--hc-border)] px-2.5 text-xs hc-soft transition hover:border-[var(--hc-border-strong)]"
        >
          {plansOpen ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
          <span className="min-w-0 break-words text-sm font-semibold text-white">PlanSpecs</span>
          <span className="shrink-0 rounded-full border border-[var(--hc-border)] px-2 py-0.5 hc-mono hc-type-label hc-soft">{items.length}</span>
        </button>
      </div>
      <div className="mt-3 grid gap-2 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center">
        <label className="min-w-0" htmlFor="PlanSpecSearch">
          <span className="sr-only">PlanSpec-Suche</span>
          <input
            id="PlanSpecSearch"
            type="search"
            value={planspecSearch}
            onChange={(event) => setPlanspecSearch(event.target.value)}
            placeholder="PlanSpecs suchen…"
            className="min-h-11 w-full rounded-md border border-[var(--hc-border)] bg-[var(--hc-panel)] px-3 text-sm outline-none placeholder:text-[var(--hc-text-dim)] focus:border-[var(--hc-accent-border)]"
          />
        </label>
        <label className="inline-flex min-h-11 items-center gap-2 rounded-md border border-[var(--hc-border)] px-3 text-sm hc-soft">
          <input
            type="checkbox"
            checked={openOnly}
            onChange={(event) => setOpenOnly(event.target.checked)}
            className="h-4 w-4 accent-[var(--hc-accent-text)]"
          />
          Nur offene
        </label>
      </div>
      {plans.error ? <ToneCallout tone="amber">{plans.error}</ToneCallout> : null}
      {plansOpen && plans.loading && plans.data == null ? <SkeletonCard rows={3} /> : null}
      {plansOpen ? <div className="mt-3 grid gap-2">
        {items.map((item) => {
          const ingestBusy = busyPath === `${item.path}:ingest`;
          const promptBusy = busyPath === `${item.path}:prompt`;
          const closeBusy = busyPath === `${item.path}:close`;
          const rowError = errorByPath[item.path];
          const prompt = promptByPath[item.path];
          const closed = planSpecIsClosed(item);
          const closedDisposition = planSpecClosedDispositionLabel(item);
          const kanbanLabel = planSpecKanbanLabel(item);
          const kanbanProgress = planSpecKanbanProgress(item);
          const kanbanTone = planSpecKanbanTone(item.kanban_state);
          const ingestBlocked = item.ingest_would_block;
          const ingestBlockerReason = ingestBlocked && item.ingest_findings.length > 0 ? item.ingest_findings[0] : null;
          return (
            <div key={item.path} className="min-w-0 rounded-[12px] border border-[var(--hc-border)] bg-[var(--hc-panel)] p-3 sm:p-[14px]">
              <div className="grid min-w-0 grid-cols-[auto_minmax(0,1fr)] items-start gap-2 sm:grid-cols-[auto_minmax(0,1fr)_auto]">
                <FileText className="mt-0.5 h-4 w-4 shrink-0 text-[var(--hc-accent-text)]" />
                <div className="min-w-0">
                  <p title={item.topic} className="line-clamp-3 break-words text-sm font-semibold leading-snug text-white sm:line-clamp-2">{item.topic}</p>
                  <p title={item.path} className="mt-1 line-clamp-1 break-all hc-mono hc-type-label hc-dim">{item.path}</p>
                </div>
                <div className="col-span-2 sm:col-span-1 sm:col-start-3">
                  <StatusPill tone={kanbanTone} label={kanbanLabel} dot={item.kanban_state === "running" ? "live" : item.kanban_state === "completed" ? "live" : item.valid ? "live" : "warn"} />
                </div>
              </div>
              <div className="mt-2 flex flex-wrap items-center gap-1.5 overflow-hidden">
                <span className="max-w-full truncate rounded-[7px] border border-[var(--hc-border)] bg-[var(--hc-panel)] px-2 py-1 hc-mono text-[10px] text-[var(--hc-text-soft)]">{item.freigabe || "ohne Freigabe"}</span>
                <span className="max-w-full truncate rounded-[7px] border border-[var(--hc-border)] bg-[var(--hc-panel)] px-2 py-1 hc-mono text-[10px] text-[var(--hc-text-soft)]">{item.live_test_depth || "smoke"}</span>
                <span className="max-w-full truncate rounded-[7px] border border-[var(--hc-border)] bg-[var(--hc-panel)] px-2 py-1 hc-mono text-[10px] text-[var(--hc-text-soft)]">{item.subtask_count} Subtasks</span>
                {item.kanban_root_task_id ? <Link to={`/control/ketten?root=${encodeURIComponent(item.kanban_root_task_id)}`} className="max-w-full truncate rounded-[7px] border border-cyan-400/30 bg-cyan-400/10 px-2 py-1 hc-mono text-[10px] text-cyan-100 hover:brightness-110">Root {item.kanban_root_task_id}</Link> : null}
                {kanbanProgress ? <span className="max-w-full truncate rounded-[7px] border border-[var(--hc-border)] bg-[var(--hc-panel)] px-2 py-1 hc-mono text-[10px] text-[var(--hc-text-soft)]">{kanbanProgress}</span> : null}
                <span className="max-w-full truncate rounded-[7px] border border-[var(--hc-border)] bg-[var(--hc-panel)] px-2 py-1 hc-mono text-[10px] text-[var(--hc-text-soft)]">{closedDisposition}</span>
                <span className="max-w-full truncate rounded-[7px] border border-[var(--hc-border)] bg-[var(--hc-panel)] px-2 py-1 hc-mono text-[10px] text-[var(--hc-text-dim)]">{item.agent}</span>
              </div>
              {item.errors.length ? <p className="mt-2 break-words text-[0.75rem] text-amber-200">{item.errors.join(" · ")}</p> : null}
              {rowError ? <p className="mt-2 break-words text-[0.75rem] text-red-300">{rowError}</p> : null}
              <div className="mt-3 grid grid-cols-2 gap-2 sm:flex sm:flex-wrap sm:items-center">
                <button
                  type="button"
                  disabled={closed || !item.valid || ingestBlocked || ingestBusy || promptBusy || closeBusy}
                  onClick={() => void ingest(item)}
                  className="inline-flex min-h-10 items-center justify-center gap-1.5 rounded-full border border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] px-3 text-sm text-[var(--hc-accent-text)] transition hover:brightness-110 disabled:opacity-40 sm:min-h-9 sm:justify-start"
                  aria-label={`PlanSpec ${item.topic} in Kanban umsetzen`}
                >
                  {ingestBusy ? <RefreshCw className="h-3.5 w-3.5 animate-spin" /> : <ArrowRight className="h-3.5 w-3.5" />}
                  Kanban
                </button>
                {ingestBlocked && ingestBlockerReason && !closed ? <p className="col-span-2 break-words text-[0.7rem] text-amber-300/80 sm:col-span-1">⚠ {ingestBlockerReason}</p> : null}
                <button
                  type="button"
                  disabled={!item.valid || ingestBusy || promptBusy || closeBusy}
                  onClick={() => { setDetailItem(item); }}
                  className="inline-flex min-h-10 items-center justify-center gap-1.5 rounded-full border border-[var(--hc-border-strong)] px-3 text-sm hc-soft transition hover:bg-white/5 disabled:opacity-40 sm:min-h-9 sm:justify-start"
                  aria-label={`Details für PlanSpec ${item.topic} öffnen`}
                >
                  <FileText className="h-3.5 w-3.5" />
                  Details
                </button>
                <button
                  type="button"
                  disabled={closed || !item.valid || ingestBusy || promptBusy || closeBusy}
                  onClick={() => void buildPrompt(item)}
                  className="inline-flex min-h-10 items-center justify-center gap-1.5 rounded-full border border-[var(--hc-border-strong)] px-3 text-sm hc-soft transition hover:bg-white/5 disabled:opacity-40 sm:min-h-9 sm:justify-start"
                  aria-label={`Sprint-Prompt für PlanSpec ${item.topic} kopieren`}
                >
                  {promptBusy ? <RefreshCw className="h-3.5 w-3.5 animate-spin" /> : <Copy className="h-3.5 w-3.5" />}
                  Sprint-Prompt
                </button>
                <button
                  type="button"
                  disabled={closed || ingestBusy || promptBusy || closeBusy}
                  onClick={() => void markNotNeeded(item)}
                  className="inline-flex min-h-10 items-center justify-center gap-1.5 rounded-full border border-red-300/30 px-3 text-sm text-red-200 transition hover:bg-red-500/10 disabled:opacity-40 sm:min-h-9 sm:justify-start"
                  aria-label={`PlanSpec ${item.topic} als nicht benötigt markieren`}
                >
                  {closeBusy ? <RefreshCw className="h-3.5 w-3.5 animate-spin" /> : <X className="h-3.5 w-3.5" />}
                  Wird nicht benötigt
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
      {detailItem ? (
        <PlanSpecDetailDrawer
          item={detailItem}
          detail={detail.data}
          loading={detail.loading}
          error={detail.error}
          onClose={() => { setDetailItem(null); }}
        />
      ) : null}
    </div>
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
  releaseCount?: number;
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
          {/* Round D: Dispatch-Prüf-Spinner — solange busy+dispatch-Aktion vorhanden
              ist kein dispatchChoice gesetzt, d.h. der Check läuft noch. */}
          {busy && actions.some((a) => a.key === "dispatch") ? (
            <span className="inline-flex items-center gap-1.5 hc-type-label hc-dim">
              <span className="h-3 w-3 animate-spin rounded-full border border-[var(--hc-border-strong)] border-t-[var(--hc-accent-text)]" aria-hidden />
              {de.flow.dispatchChecking}
            </span>
          ) : null}
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

/** Per-run cost read-out on the bottom hairline of a board card — "$0.42 · 31k tok"
 *  ($ in emerald; a subscription $-equivalent is flagged via title). Renders only
 *  when the task carries cost data (i.e. it actually ran — otherwise no footer).
 *  Reuses formatEffectiveCost so the $/token rule matches the chain-graph NodeCost. */
function FlowCostFooter({ task }: { task: BoardTask }) {
  if (task.cost_effective_usd == null) return null;
  const totalTokens = (task.input_tokens ?? 0) + (task.output_tokens ?? 0);
  const { text: costText, estimated } = formatEffectiveCost({
    cost_usd: task.cost_usd ?? 0,
    cost_effective_usd: task.cost_effective_usd,
    tokens: totalTokens,
  });
  return (
    <div className="mt-2 flex items-center justify-between gap-2 border-t border-[var(--hc-border)] pt-2">
      <span className="hc-mono text-[10px] font-semibold uppercase tracking-wider text-[var(--hc-text-dim)]">Kosten / Run</span>
      <span className="hc-mono shrink-0 text-[10px] tabular-nums text-[var(--hc-text-dim)]">
        <span className="font-semibold text-[var(--hc-emerald)]" title={estimated ? "geschätzter Abo-Gegenwert (kein metered $)" : undefined}>{costText}</span>
        {totalTokens > 0 ? ` · ${fmtTokens(totalTokens)} tok` : null}
      </span>
    </div>
  );
}

export const FlowRunCard = memo(function FlowRunCard({ task, enriched, selected, busy, error, now, dispatchChoice, manualReviewFallback, onSelect, onReleaseChain, onDispatchSingle, onCancelDispatchChoice, onAct }: FlowRunCardProps) {
  const role = roleChip(enriched.workerProfile ?? task.assignee, task.status === "review" ? "verification" : null);
  const isBlocked = task.status === "blocked";
  const isRunning = task.status === "running";
  const isReview = task.status === "review";
  const isDone = task.status === "done";
  const verifierGate = reviewGateStatus(enriched);
  const resultArtifact = preferredResultArtifactLink(enriched.resultArtifactLinks);
  const ageSec = task.age?.created_age_seconds ?? null;
  const hasProgress = task.progress != null && task.progress.total > 0;
  const pct = hasProgress ? Math.round((task.progress!.done / task.progress!.total) * 100) : 0;
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
      className={cn(
        "min-w-0 max-w-full cursor-pointer overflow-hidden rounded-[14px] border p-3 transition shadow-[var(--hc-elev-1)]",
        selected
          ? "border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)]"
          : isRunning
            ? "border-cyan-400/50 bg-[var(--hc-panel-card)]"
            : "border-[var(--hc-border)] bg-[var(--hc-panel-card)] hover:border-[var(--hc-border-strong)]",
        isBlocked && "border-red-500/40",
      )}
    >
      {/* Header: role-chip (left) + status-pill (right) */}
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <RoleChip role={role} />
        <span className="ml-auto shrink-0">
          <StatusPill
            tone={isBlocked ? "red" : isDone ? "emerald" : isReview ? "amber" : isRunning ? "cyan" : "zinc"}
            label={taskStatusLabel[task.status] ?? task.status}
            dot={isRunning ? "live" : isBlocked ? "error" : isDone ? "ready" : isReview ? "warn" : "idle"}
          />
        </span>
      </div>
      {/* Task-ID + Titel */}
      <span className="hc-mono mt-1.5 block min-w-0 max-w-full truncate hc-type-label hc-dim">{task.id}</span>
      <p className="mt-0.5 line-clamp-2 text-[15px] font-semibold leading-snug text-white">{task.title}</p>
      {/* Meta-Zeile: Alter · Branch */}
      <p className="mt-1 flex min-w-0 items-center truncate hc-mono hc-type-label hc-dim">
        <span className="truncate">
          {ageSec != null ? `vor ${fmtAge(now - ageSec, now)}` : ""}{task.branch_name ? ` · ${task.branch_name}` : ""}
        </span>
        {enriched.workerHeartbeat ? (
          <span className="ml-1 inline-flex shrink-0 items-center gap-1 text-[var(--hc-emerald)]">
            ·<HeartPulse className="h-3 w-3 motion-safe:animate-pulse" aria-hidden />
            {fmtAge(enriched.workerHeartbeat, now)}
          </span>
        ) : null}
      </p>
      {/* Fortschritts-Bar */}
      {hasProgress ? (
        <div className="mt-2 flex items-center gap-2">
          <div className="hc-stage-rail min-w-0 flex-1">
            <i style={{ width: `${pct}%` }} />
          </div>
          <span className="hc-mono shrink-0 text-[10px] text-[var(--hc-text-dim)]">{pct}%</span>
        </div>
      ) : null}
      {/* Telemetrie-Chips */}
      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        {task.priority >= 2 ? (
          <span className="rounded-[7px] border border-[var(--hc-border)] bg-[var(--hc-panel)] px-2 py-1 hc-mono text-[10px] text-[var(--hc-text-soft)]">Hoch</span>
        ) : null}
        {(task.auto_retry_count ?? 0) > 0 ? (
          <span className="rounded-[7px] border border-amber-400/30 bg-amber-400/10 px-2 py-1 hc-mono text-[10px] text-amber-200">Retry {Math.min(task.auto_retry_count ?? 0, 2)}/2</span>
        ) : null}
        {hasProgress ? (
          <span className="rounded-[7px] border border-[var(--hc-border)] bg-[var(--hc-panel)] px-2 py-1 hc-mono text-[10px] text-[var(--hc-text-soft)]">{task.progress!.done}/{task.progress!.total} {de.flow.plan.subtasksHeading}</span>
        ) : null}
        {enriched.verdict ? (
          <span className="rounded-[7px] border border-cyan-400/30 bg-cyan-400/10 px-2 py-1 hc-mono text-[10px] text-cyan-200">{enriched.verdict}</span>
        ) : null}
        {isIsolatedWorkspace(task) ? (
          <span title={task.workspace_path ?? undefined} className="rounded-[7px] border border-violet-400/30 bg-violet-400/10 px-2 py-1 hc-mono text-[10px] text-violet-200">⧉ Worktree</span>
        ) : null}
        {isDone && enriched.resultQualityLabel ? (
          <span className="rounded-[7px] border border-emerald-400/30 bg-emerald-400/10 px-2 py-1 hc-mono text-[10px] text-emerald-200">{enriched.resultQualityLabel}</span>
        ) : null}
        {enriched.deliverableCount ? (
          <span className="rounded-[7px] border border-emerald-400/30 bg-emerald-400/10 px-2 py-1 hc-mono text-[10px] text-emerald-200">{enriched.deliverableCount} Deliverable{enriched.deliverableCount === 1 ? "" : "s"}</span>
        ) : null}
      </div>
      {task.latest_summary ? <p className="mt-2 line-clamp-2 text-xs hc-soft">{task.latest_summary}</p> : null}
      {/* Blocked-Reason-Bar */}
      {isBlocked && enriched.blockedReason ? (
        <p className="mt-2 flex items-start gap-1.5 rounded-md border border-red-500/30 bg-red-500/10 px-2 py-1 hc-type-label text-red-200"><Lock className="mt-0.5 h-3 w-3 shrink-0" />{enriched.blockedReason}</p>
      ) : null}
      {isReview ? (
        <p className="mt-2 flex items-center gap-1.5 hc-type-label hc-dim"><ShieldCheck className="h-3 w-3 text-cyan-300" />Verifier-Gate — {verifierGate ?? "wartet auf Verifier; Nacharbeit schickt zurück."}</p>
      ) : null}
      {resultArtifact ? (
        <div className="mt-2 flex items-center justify-between gap-2 rounded-md border border-emerald-400/20 bg-emerald-500/[.06] px-2 py-1.5">
          <span className="min-w-0 truncate hc-type-label text-emerald-100">Spec-Draft / RESULT · {resultArtifact.relative_path}</span>
          <DeliverableOpenButton url={resultArtifact.url} label="RESULT öffnen" />
        </div>
      ) : null}
      <FlowCostFooter task={task} />
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
const REVIEW_TIER_LABEL: Record<ReviewTier, string> = { standard: "Standard", review: "Review", critical: "Kritisch" };
// Slice b: label for the LIVE review stage pill (the verifier→reviewer→critic
// step currently running), distinct from the configured-tier pill above.
const ACTIVE_STAGE_LABEL: Record<ActiveReviewStage, string> = { verifier: "Verifier", reviewer: "Reviewer", critic: "Critic" };
// Phase C: the operator's chain-wide lane choices at start (canonical code lanes;
// premium = the Claude Opus lane, coder = the default code lane).
const FLOW_LANE_OPTIONS: ReadonlyArray<string> = ["coder", "premium"];

function FlowPlanPanel({ rootId, detail, boardTasks, now, onRelease, releaseBusy, releaseError, released, onGateChanged }: {
  rootId: string; detail?: TaskDetailResponse; boardTasks: BoardTask[];
  now: number; onRelease: (rootId: string, n: number, options?: FlowReleaseOptions) => void; releaseBusy: boolean; releaseError?: string; released?: number; onGateChanged?: () => void | Promise<void>;
}) {
  const [confirming, setConfirming] = useState(false);
  const [releaseLevel, setReleaseLevel] = useState<FlowGateReleaseLevel>("merge");
  const [assigneeOverrides, setAssigneeOverrides] = useState<Record<string, string>>({});
  const [selectedSizing, setSelectedSizing] = useState<string[]>([]);
  const [splitTitle, setSplitTitle] = useState("");
  // Phase C operator levers (chain-wide, applied at start via flow-release).
  const [reviewTier, setReviewTier] = useState<ReviewTier | "">("");
  const [injectScout, setInjectScout] = useState(false);
  const [laneAll, setLaneAll] = useState("");
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
  const laneOptions = FLOW_LANE_OPTIONS.filter((p) => !profiles.length || profiles.includes(p));
  // Chain-wide lane spreads onto every child first; a per-subtask override
  // (overridePayload) is applied AFTER so the more specific choice wins.
  const laneOverrides: Record<string, string | null> = laneAll
    ? Object.fromEntries(childIds.map((id) => [id, laneAll]))
    : {};
  const releaseOptions: FlowReleaseOptions = {
    release_level: releaseLevel,
    assignee_overrides: { ...laneOverrides, ...overridePayload },
    ...(reviewTier ? { review_tier: reviewTier } : {}),
    ...(injectScout ? { inject_scout: true } : {}),
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
        <div className="mt-2.5 rounded-[11px] border border-[var(--hc-border)] bg-[var(--hc-panel-2)] p-2 shadow-[var(--hc-elev-1)]">
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
            <p className="mt-1.5 flex items-start gap-1.5 hc-type-label text-[var(--hc-amber)]">
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
                {level === "merge" ? de.flow.plan.releaseLevelMerge : de.flow.plan.releaseLevelLive}
              </button>
            ))}
          </div>
          <p className="mt-1 hc-type-label hc-dim">
            {releaseLevel === "live" ? de.flow.plan.hintReleaseLevelLive : de.flow.plan.hintReleaseLevelMerge}
          </p>
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
                  aria-label={selected ? "Aus Auswahl entfernen" : "Zur Auswahl hinzufügen"}
                  aria-pressed={selected}
                >
                  {selected ? "x" : "+"}
                </button>
                <span className="hc-mono min-w-0 max-w-full truncate hc-type-label hc-dim">{c.id}</span>
                <span className="min-w-0 flex-1 basis-36 truncate hc-type-label text-[var(--hc-text)]">{c.title}</span>
                <div className="ml-auto flex max-w-full flex-wrap items-center justify-end gap-1.5">
                  {risk ? <StatusPill tone={riskTone} label={`Risiko ${risk.tone}`} /> : null}
                  <StatusPill tone={statusTone(c.status)} label={taskStatusLabel[c.status] ?? c.status} />
                  {profiles.length ? (
                    <select
                      value={assigneeOverrides[c.id] ?? gateChild?.assignee ?? c.assignee ?? ""}
                      onChange={(e) => setAssigneeOverrides((prev) => ({ ...prev, [c.id]: e.target.value }))}
                      className="min-h-8 max-w-40 rounded-md border border-[var(--hc-border)] bg-[var(--hc-panel)] px-2 hc-type-label text-[var(--hc-text)]"
                    >
                      <option value="">Nicht zugewiesen</option>
                      {profiles.map((profile) => <option key={profile} value={profile}>{profileLabel[profile] ?? profile}</option>)}
                    </select>
                  ) : null}
                  {/* truncate + title: a long status sentence (blocked/running/
                      "Repair nötig") otherwise has no width cap and pushes the
                      subtask row past the 340px detail column / off-screen. */}
                  <span className={cn("max-w-full truncate hc-type-label hc-dim sm:max-w-[13rem] sm:text-right", c.status === "blocked" && "text-red-200")} title={statusExplanation}>
                    {statusExplanation}
                  </span>
                </div>
              </li>
            );
          })}
        </ul>
      ) : null}

      {/* Gate-Levers: sichtbar sobald gate.data geladen (auch wenn heldCount=0),
          damit der Operator Lane/Tier/Scout nach dem Dispatch noch einsehen kann.
          Dispatch-Button bleibt deaktiviert wenn kein gehaltenener Subtask vorhanden. */}
      {gate.data != null || heldCount > 0 ? (
        <div className="mt-2.5">
          {heldCount > 0 ? (
            <>
              <div className="mb-1 flex min-w-0 flex-wrap items-center gap-2">
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
                  className="min-h-9 min-w-0 flex-1 rounded-md border border-[var(--hc-border)] bg-[var(--hc-panel)] px-2 text-xs text-[var(--hc-text)] placeholder:text-[var(--hc-text-dim)]"
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
              <p className="mb-2 hc-type-label hc-dim">{de.flow.plan.hintSizing}</p>
            </>
          ) : null}
          <div className="mb-2 flex flex-wrap items-center gap-x-3 gap-y-2">
            {laneOptions.length ? (
              <label className="inline-flex items-center gap-1.5 hc-type-label hc-soft">
                Lane
                <select
                  value={laneAll}
                  onChange={(e) => setLaneAll(e.target.value)}
                  disabled={heldCount === 0}
                  className="min-h-8 rounded-md border border-[var(--hc-border)] bg-[var(--hc-panel)] px-2 hc-type-label text-[var(--hc-text)] disabled:opacity-50"
                >
                  <option value="">unverändert</option>
                  {laneOptions.map((p) => <option key={p} value={p}>{profileLabel[p] ?? p}</option>)}
                </select>
              </label>
            ) : null}
            <div className="inline-flex items-center gap-1">
              <span className="hc-type-label hc-soft">Review</span>
              {(["standard", "review", "critical"] as ReviewTier[]).map((tier) => (
                <button
                  key={tier}
                  type="button"
                  disabled={heldCount === 0}
                  aria-pressed={reviewTier === tier}
                  onClick={() => setReviewTier((prev) => (prev === tier ? "" : tier))}
                  className={cn(
                    "inline-flex min-h-8 items-center rounded-full border px-2.5 hc-type-label transition disabled:opacity-50",
                    reviewTier === tier
                      ? "border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] text-[var(--hc-accent-text)]"
                      : "border-[var(--hc-border)] hc-soft hover:border-[var(--hc-border-strong)]",
                  )}
                >
                  {REVIEW_TIER_LABEL[tier]}
                </button>
              ))}
            </div>
            <label className="inline-flex items-center gap-1.5 hc-type-label hc-soft">
              <input
                type="checkbox"
                checked={injectScout}
                disabled={heldCount === 0}
                onChange={(e) => setInjectScout(e.target.checked)}
                className="h-3.5 w-3.5 accent-[var(--hc-accent)] disabled:opacity-50"
              />
              Scout-Vorlauf
            </label>
          </div>
          <p className="mb-1 hc-type-label hc-dim">{de.flow.plan.hintReviewTier}</p>
          <p className="mb-1 hc-type-label hc-dim">{de.flow.plan.hintScout}</p>
          {heldCount > 0 ? (
            confirming ? (
              <div className="flex flex-wrap items-center gap-2">
                <span className="hc-type-label hc-soft">{de.flow.plan.releaseConfirm(heldCount)}</span>
                <button type="button" disabled={releaseBusy} onClick={() => { onRelease(rootId, heldCount, releaseOptions); setConfirming(false); }} className="inline-flex min-h-9 items-center rounded-full border border-emerald-400/40 bg-emerald-400/10 px-3 text-xs text-emerald-100 disabled:opacity-40">{releaseBusy ? de.flow.plan.releaseBusy : `${de.flow.plan.releaseConfirmButton} · ${releaseLevel}`}</button>
                <button type="button" onClick={() => setConfirming(false)} className="inline-flex min-h-9 items-center rounded-full border border-[var(--hc-border-strong)] px-3 text-xs hc-soft">Abbrechen</button>
              </div>
            ) : (
              <button type="button" disabled={releaseBusy} onClick={() => setConfirming(true)} className="inline-flex min-h-9 items-center gap-1.5 rounded-full border border-emerald-400/40 bg-emerald-400/10 px-3 text-xs font-medium text-emerald-100 transition hover:brightness-110 disabled:opacity-40">
                <Play className="h-3.5 w-3.5" />{de.flow.plan.release} · {de.flow.plan.subtasksOf(heldCount)}
              </button>
            )
          ) : null}
          {heldCount > 0 ? (
            <>
              <p className="mt-1.5 flex items-center gap-1.5 hc-type-label hc-dim"><Lock className="h-3 w-3" />{de.flow.plan.heldGate}</p>
              <p className="mt-1 hc-type-label hc-dim">Kette starten gibt gehaltene Subtasks frei; Queue/Assignee und Dependencies entscheiden den tatsächlichen Start.</p>
            </>
          ) : null}
        </div>
      ) : null}

      {released ? <p className="mt-1.5 hc-type-label text-[var(--hc-emerald)]">{de.flow.plan.released(released)}</p> : null}
      {gate.error ? <p className="mt-1.5 flex items-start gap-1 hc-type-label text-[var(--hc-red)]"><AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />{gate.error}</p> : null}
      {releaseError ? <p className="mt-1.5 flex items-start gap-1 hc-type-label text-[var(--hc-red)]"><AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />{releaseError}</p> : null}
    </div>
  );
}

function FlowChainInsight({ task, detail, boardTasks, snapshotLabel }: { task?: BoardTask; detail?: TaskDetailResponse; boardTasks: BoardTask[]; snapshotLabel: string }) {
  // useState muss vor dem frühen return stehen (React-Hooks-Regel: bedingte Hooks verboten).
  const [chainInsightOpen, setChainInsightOpen] = useState(false);
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
        <span className="rounded-full border border-[var(--hc-border)] px-2 py-0.5 hc-type-label hc-dim">Snapshot-Alter: {snapshotLabel}</span>
      </div>
      {/* Zwei handlungsorientierte Sektionen */}
      <div className="mt-2 grid gap-2">
        <section aria-label="Was blockiert?" className="rounded-md border border-[var(--hc-border)] bg-[var(--hc-panel)] px-2.5 py-2">
          <p className="hc-eyebrow text-red-200">Was blockiert?</p>
          <p className="mt-1 break-words text-[0.75rem] hc-soft">
            {heldTasks.length
              ? `Gehalten: ${heldTasks.map(taskLine).join(" · ")}`
              : possibleActivePredecessors.length
                ? `Mögliche Vorgänger aktiv: ${possibleActivePredecessors.map(taskLine).join(" · ")}`
                : "Keine Blockierung im Snapshot erkennbar."}
          </p>
          {hasAmbiguousTodo ? <p className="mt-1 break-words hc-type-label text-amber-200">Hinweis: todo ist uneindeutig; es kann Dependency-Warten, manuelles Backlog oder Dispatcher-Queue sein.</p> : null}
        </section>
        <section aria-label="Was kommt als nächstes?" className="rounded-md border border-[var(--hc-border)] bg-[var(--hc-panel)] px-2.5 py-2">
          <p className="hc-eyebrow text-emerald-100">Was kommt als nächstes?</p>
          <p className="mt-1 break-words text-[0.75rem] hc-soft">
            {runningTasks.length
              ? `Läuft bereits: ${runningTasks.map(taskLine).join(" · ")}`
              : readyTasks.length
                ? `Ready-Nachbar im Snapshot: ${readyTasks.map(taskLine).join(" · ")}`
                : waitingDependents.length
                  ? `Mögliche Nachfolger warten: ${waitingDependents.map(taskLine).join(" · ")}`
                  : "Kein ready-Nachbar im Snapshot; keine Scheduler-Zusage."}
          </p>
        </section>
      </div>
      {/* Detail-Toggle: technische Daten für Debugging */}
      <button
        type="button"
        aria-expanded={chainInsightOpen}
        onClick={() => setChainInsightOpen((v) => !v)}
        className="mt-2 inline-flex items-center gap-1 hc-type-label hc-dim hover:text-[var(--hc-text-soft)]"
      >
        {chainInsightOpen ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
        Details
      </button>
      {chainInsightOpen ? (
        <div className="mt-2 grid gap-1.5">
          <p className="hc-type-label hc-dim">{parallelNote}</p>
          <div className="rounded-md border border-[var(--hc-border)] bg-[var(--hc-panel)] px-2.5 py-2">
            <p className="hc-eyebrow text-amber-100">Mögliche Vorgänger</p>
            <p className="mt-1 break-words text-[0.75rem] hc-soft">{predecessorHintLine}</p>
          </div>
          <div className="flex flex-wrap gap-1.5">
            {parentTasks.map((p) => <StatusPill key={`p-${p.id}`} tone={statusTone(p.status)} label={`Möglicher Vorgänger ${taskStatusLabel[p.status] ?? p.status}`} />)}
            {childTasks.map((c) => <StatusPill key={`c-${c.id}`} tone={statusTone(c.status)} label={`Möglicher Nachfolger ${taskStatusLabel[c.status] ?? c.status}`} />)}
          </div>
          {unknownIds.length ? <p className="break-words hc-type-label hc-dim">Nicht im Board-Snapshot: {unknownIds.join(", ")}</p> : null}
          {parentIds.length ? <p className="hc-type-label hc-dim">Snapshot-Hinweis: rohe Detail-Links zeigen Nähe, aber keinen sicheren Blockierungsgrund.</p> : null}
        </div>
      ) : null}
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

// Operator-Direktive an einen Task: schreibt einen Kommentar via
// POST /tasks/{id}/comments (Backend seit 2026-06-19). Bisher nur per CLI
// erreichbar — diese Fläche bringt die Kurskorrektur an einen laufenden/geblockten
// Worker ans Handy. Nach dem Posten refrescht onPosted (onGateChanged) Board +
// Detail, der Kommentar erscheint im Live-Flow. Strings lokal (kein Edit an der
// shared i18n/de.ts paralleler Sessions).
const COMMENT_STRINGS = {
  add: "Direktive / Kommentar",
  title: "Direktive an den Worker",
  placeholder: "z.B. Fokussiere AC-2 zuerst und ignoriere den Lint-Nit — landet als Kommentar am Task.",
  send: "Senden",
  sending: "sendet…",
  sent: "gesendet",
  cancel: "Abbrechen",
  hint: "claude-cli-Worker lesen neue Kommentare beim nächsten Kontext-Render, Hermes-Worker sofort.",
};

function TaskCommentComposer({ taskId, onPosted }: { taskId: string; onPosted?: () => void | Promise<void> }) {
  const [open, setOpen] = useState(false);
  const [body, setBody] = useState("");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submit = async () => {
    const text = body.trim();
    if (!text || busy) return;
    setBusy(true);
    setError(null);
    try {
      await fetchJSON(`/api/plugins/kanban/tasks/${encodeURIComponent(taskId)}/comments`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ body: text }),
      });
      setBody("");
      setDone(true);
      window.setTimeout(() => setDone(false), 2000);
      await onPosted?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="mt-3 inline-flex min-h-9 items-center gap-1.5 rounded-lg border border-[var(--hc-border)] px-3 text-sm hc-soft transition hover:border-[var(--hc-border-strong)]"
      >
        <MessageSquarePlus className="h-4 w-4" />{COMMENT_STRINGS.add}
      </button>
    );
  }
  return (
    <div className="mt-3 rounded-lg border border-[var(--hc-border)] p-2.5">
      <p className="hc-eyebrow">{COMMENT_STRINGS.title}</p>
      <textarea
        value={body}
        onChange={(e) => { setBody(e.target.value); if (error) setError(null); }}
        onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) void submit(); }}
        rows={3}
        placeholder={COMMENT_STRINGS.placeholder}
        className="mt-2 w-full resize-y rounded-md border border-[var(--hc-border)] bg-[var(--hc-panel)] px-2.5 py-2 text-sm text-[var(--hc-text)] outline-none placeholder:text-[var(--hc-text-dim)] focus:border-[var(--hc-accent-border)]"
      />
      {error ? <p className="mt-1.5 flex items-start gap-1.5 text-[0.75rem] text-red-300"><AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />{error}</p> : null}
      <div className="mt-2 flex items-center justify-end gap-2">
        <button type="button" onClick={() => { setOpen(false); setBody(""); setError(null); }} className="inline-flex min-h-9 items-center rounded-md border border-white/10 px-3 text-sm hc-soft">{COMMENT_STRINGS.cancel}</button>
        <button
          type="button"
          disabled={busy || !body.trim()}
          onClick={() => void submit()}
          className="inline-flex min-h-9 items-center gap-1.5 rounded-md border border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] px-3 text-sm font-medium text-[var(--hc-accent-text)] transition hover:brightness-110 disabled:opacity-40"
        >
          {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : done ? <Check className="h-4 w-4" /> : <Send className="h-4 w-4" />}
          {busy ? COMMENT_STRINGS.sending : done ? COMMENT_STRINGS.sent : COMMENT_STRINGS.send}
        </button>
      </div>
      <p className="mt-1.5 text-[0.72rem] hc-dim">{COMMENT_STRINGS.hint}</p>
    </div>
  );
}

export function FlowReceiptRail({ taskId, task, detail, enriched = EMPTY_ENRICHED, loading, error, now, boardTasks, snapshotLabel, onRelease, releaseBusy, releaseError, released, onGateChanged }: {
  taskId: string | null; task?: BoardTask; detail?: TaskDetailResponse; enriched?: Enriched; loading: boolean; error?: string; now: number;
  boardTasks: BoardTask[]; snapshotLabel: string; onRelease: (rootId: string, n: number, options?: FlowReleaseOptions) => void; releaseBusy: boolean; releaseError?: string; released?: number; onGateChanged?: () => void | Promise<void>;
}) {
  if (!taskId) {
    return (
      <aside className="hc-surface-card min-w-0 overflow-hidden h-fit p-4 xl:sticky xl:top-4">
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
        {/* Root-Titel sind oft ganze PlanSpec-Sätze — auf 3 Zeilen clampen statt
            17-Zeilen-Wand (Sicht-Audit 2026-06-19 G); Volltext via title-Attribut. */}
        <p title={detail?.task?.title ?? task?.title ?? ""} className="mt-1 line-clamp-3 text-sm font-semibold leading-snug text-[var(--hc-text)]">{detail?.task?.title ?? task?.title ?? ""}</p>
        {task ? (
          <div className="mt-2 flex flex-wrap items-center gap-1.5">
            <RoleChip role={roleChip(task.assignee, task.status === "review" ? "verification" : null)} />
            <StatusPill tone={task.status === "done" ? "emerald" : task.status === "blocked" ? "red" : "zinc"} label={taskStatusLabel[task.status] ?? task.status} />
          </div>
        ) : null}
      </div>

      <TaskCommentComposer taskId={taskId} onPosted={onGateChanged} />

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

      <section aria-label="Ketten-Kontext">
        <FlowChainInsight task={task} detail={detail} boardTasks={boardTasks} snapshotLabel={snapshotLabel} />
      </section>

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
                <div key={run.id} className="min-w-0 max-w-full overflow-hidden rounded-[14px] border border-[var(--hc-border)] bg-[var(--hc-panel-card)] p-2.5 shadow-[var(--hc-elev-1)]">
                  <div className="flex min-w-0 items-center gap-2">
                    <RoleChip role={role} />
                    <span className={cn("ml-auto inline-flex shrink-0 items-center gap-1 hc-type-label", ok ? "text-[var(--hc-emerald)]" : run.error ? "text-[var(--hc-red)]" : "hc-dim")}>
                      <span className={cn("h-1.5 w-1.5 rounded-full", ok ? "bg-[var(--hc-emerald)]" : run.error ? "bg-[var(--hc-red)]" : "bg-[var(--hc-text-dim)]")} />{run.outcome ?? run.status}
                    </span>
                  </div>
                  <p className="mt-1 truncate hc-mono hc-type-label hc-dim">Run {run.id} · {run.run_role_label ?? profileLabel[run.profile ?? ""] ?? run.profile ?? "—"}{run.ended_at ? ` · vor ${fmtAge(run.ended_at, now)}` : run.started_at ? ` · seit ${fmtAge(run.started_at, now)}` : ""}</p>
                  {run.summary ? <p className="mt-1 line-clamp-3 text-[0.78rem] hc-soft">{run.summary}</p> : null}
                  {run.error ? <p className="mt-1 line-clamp-2 hc-type-label text-[var(--hc-red)]">{run.error}</p> : null}
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
              <li key={d.relative_path} className="flex items-center justify-between gap-2 rounded-[7px] border border-[var(--hc-border)] bg-[var(--hc-panel-card)] px-2.5 py-1.5 shadow-[var(--hc-elev-1)]">
                <span className="min-w-0 truncate text-[0.78rem] hc-soft">{d.relative_path}</span>
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
              <li key={`${d.source}-${artifactKey(d)}`} className="flex items-center justify-between gap-2 rounded-[7px] border border-[var(--hc-border)] bg-[var(--hc-panel-card)] px-2.5 py-1.5 shadow-[var(--hc-elev-1)]">
                <span className="min-w-0 truncate text-[0.78rem] hc-soft" title={d.path}>{d.relative_path || d.filename || d.path}</span>
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
                <span className={cn("mt-1 h-2 w-2 shrink-0 rounded-full", i === 0 ? "bg-[var(--hc-emerald)]" : "bg-[var(--hc-border-strong)]", i === 0 && "motion-safe:animate-pulse")} />
                <div className="min-w-0">
                  <p className="text-[0.82rem] text-[var(--hc-text)]">{eventLabel(ev.kind)}</p>
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
  const reviewTier = chainReviewTier(chain.members);
  const activeStage = chainActiveReviewStage(chain.members);
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
          {reviewTier ? <StatusPill tone="indigo" label={`Review: ${REVIEW_TIER_LABEL[reviewTier]}`} /> : null}
          {activeStage ? <StatusPill tone="violet" label={`Stufe: ${ACTIVE_STAGE_LABEL[activeStage]}`} dot="live" /> : null}
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
function FlowDetailSheet({ taskId, taskTitle, onClose, children }: { taskId: string; taskTitle?: string; onClose: () => void; children: React.ReactNode }) {
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
  return createPortal(
    // Portal an document.body: inline säße das Sheet im Stacking-Context der View
    // (RouteTransition/.hc-hero) — sein z-50 zählte nur dort und der body-Level
    // Capture-FAB (z-40) malte darüber (Screenshot-Audit 2026-06-19).
    <div data-control className="contents">
    <div className="fixed inset-0 z-50 xl:hidden">
      <button type="button" aria-label={de.flow.detailClose} onClick={onClose} className="absolute inset-0 bg-black/60" />
      <div
        role="dialog"
        aria-modal="true"
        aria-label={de.flow.selectedChain}
        className="absolute inset-x-0 bottom-0 flex max-h-[85dvh] flex-col rounded-t-2xl border border-b-0 border-[var(--hc-border)] bg-[var(--hc-panel)] shadow-2xl"
      >
        <div className="flex items-center justify-between gap-2 border-b border-[var(--hc-border)] px-4 py-2.5">
          <div className="min-w-0 flex-1">
            {taskTitle ? (
              <p className="line-clamp-1 text-sm font-semibold leading-snug text-white">{taskTitle}</p>
            ) : null}
            <span className="hc-mono hc-type-label hc-dim truncate">{taskId}</span>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label={de.flow.detailClose}
            className="grid h-9 w-9 shrink-0 place-items-center rounded-lg border border-[var(--hc-border)] hc-soft transition hover:border-[var(--hc-border-strong)]"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto overflow-x-hidden overscroll-contain p-3 pb-[calc(0.75rem+env(safe-area-inset-bottom,0px))]">{children}</div>
      </div>
    </div>
    </div>,
    document.body,
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
  const onWorkerAction = useCallback(async (
    runId: string,
    action: WorkerActionKey,
    extra?: { model_override?: string; assignee?: string },
  ) => {
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
        // extra (model_override, assignee) wird optional in den Body gemerged.
        const body: Record<string, unknown> = { action, confirm: true, ...extra };
        const res = await fetchJSON<{ ok?: boolean; detail?: string }>(
          `/api/plugins/kanban/workers/${encodeURIComponent(runId)}/action`,
          { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) },
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
      const rootGuard = getHeldFlowRootGuard(task, detail, allTasks);
      if (rootGuard) {
        setDispatchChoice({
          taskId: task.id,
          rootId: rootGuard.rootId,
          heldSiblingIds: rootGuard.heldChildIds,
          releaseCount: rootGuard.heldChildIds.length,
        });
      } else {
        const rootId = detail?.events
          .map((event) => event.payload?.from_decompose_of)
          .find((value): value is string => typeof value === "string" && !!value.trim()) ?? null;
        const rootDetail = rootId ? (taskDetail.detailById[rootId] ?? await fetchDetail(rootId)) : null;
        const guard = getHeldFlowDispatchGuard(task, detail, rootDetail, allTasks);
        if (guard) {
          setDispatchChoice({ taskId: task.id, ...guard, releaseCount: guard.heldSiblingIds.length + 1 });
        } else {
          runTaskAction(task.id, action);
        }
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
    onReleasePlan(dispatchChoice.rootId, dispatchChoice.releaseCount ?? dispatchChoice.heldSiblingIds.length + 1);
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

  // C1: Aufmerksamkeits-Aggregation — zählt offene Entscheidungs-Queues
  // für die Hero-Aufmerksamkeitszeile. Triage/Funnel werden jetzt separat
  // gepollt (10s, read-only) damit die Zeile echte Zahlen zeigt.
  // Ehrlichkeits-Regel: solange triage/funnel noch nicht geladen sind (data===null)
  // bleibt ihr Count auf 0 — die Zeile zeigt dann keine affirmative Entwarnung
  // (attentionLoading=true → Zeile wird unterdrückt, s. Render unten).
  const decisionQueue = useKanbanDecisionQueue();
  const dispositionItems = useDispositionItems();
  const triageFailures = useFlowTriageFailures();
  const funnelDrafts = useFunnelDrafts();
  const attentionLoading = triageFailures.data === null || funnelDrafts.data === null;
  const attentionSummary = useMemo(() => summarizeFlowAttention({
    recoveryCount: (decisionQueue.data?.decisions ?? []).filter((d) => !RECOVERY_HIDDEN_KINDS.has(d.kind)).length,
    triageCount: triageFailures.data !== null ? countActionableFailures(triageFailures.data) : 0,
    funnelCount: funnelDrafts.data !== null ? countOpenFunnelDrafts(funnelDrafts.data) : 0,
    dispositionCount: (dispositionItems.data?.items ?? []).length,
    blockedCount: counts.blocked,
  }), [decisionQueue.data, dispositionItems.data, triageFailures.data, funnelDrafts.data, counts.blocked]);
  const hasAnyFiltered = filteredTasks.some((t) => t.status !== "archived");

  // F4: Queue-Tiefe = ready+todo Tasks über alle (ungefilterten) Tasks.
  const queueDepth = useMemo(
    () => allTasks.filter((t) => t.status === "ready" || t.status === "todo").length,
    [allTasks],
  );

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

      {/* C1: Aufmerksamkeitszeile — zeigt welche Queues nicht leer sind.
           Ehrlichkeit: die grüne Entwarnung erscheint NUR wenn ALLE Daten
           geladen sind (attentionLoading=false). Solange triage/funnel noch
           nicht geantwortet haben, wird die Zeile weggelassen statt fälschlich
           "Nichts wartet auf dich." zu zeigen. Nicht-leere Queues werden immer
           sofort angezeigt (nur quiet-State ist gated). */}
      {!loadingFirst && (!attentionSummary.quiet || !attentionLoading) ? (
        <div
          data-testid="flow-attention-band"
          className={cn(
            "flex flex-wrap items-center gap-x-2 gap-y-1 rounded-[10px] border px-3 py-2 text-xs",
            attentionSummary.quiet
              ? "border-emerald-500/20 bg-emerald-500/[.06] text-emerald-200"
              : "border-amber-500/20 bg-amber-500/[.06] text-amber-100",
          )}
        >
          {attentionSummary.quiet ? (
            <span>{attentionSummary.line}</span>
          ) : (
            <>
              {attentionSummary.segments.map((seg) => (
                <a
                  key={seg.anchorId}
                  href={`#${seg.anchorId}`}
                  onClick={(e) => {
                    e.preventDefault();
                    document.getElementById(seg.anchorId)?.scrollIntoView({ behavior: "smooth", block: "start" });
                  }}
                  className="inline-flex items-center gap-1 underline-offset-2 hover:underline"
                >
                  <span className="font-semibold tabular-nums">{seg.count}</span>
                  <span>{seg.label}</span>
                </a>
              ))}
            </>
          )}
        </div>
      ) : null}

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

      <div id="flow-section-recovery"><RecoveryStrip /></div>

      {/* F4: Kapazitäts-/Engpass-Banner — schlanke Leiste, nur wenn Worker
          aktiv oder Tasks warten; amber wenn Engpass (alle belegt + Queue > 0). */}
      {workers.data != null ? (
        <CapacityBanner
          count={workers.data.count}
          cap={workers.data.cap}
          queueDepth={queueDepth}
        />
      ) : null}

      {/* C2: PlanSpecHub nach oben — PlanSpec-Ingest ist der Einstiegspunkt
          neuer Arbeit; gehört vor die aktiven Ketten, nicht ans Ende. */}
      <PlanSpecHub onIngested={onCaptured} />

      {/* Phase F (Programm 3): Fehler-Triage — failed/blocked 48h mit
          „Nochmal" / „Nochmal stärker" (model_override-Eskalation). Rendert
          nichts, wenn es nichts zu triagieren gibt. */}
      <div id="flow-section-triage"><TriageStrip /></div>

      {/* Funnel-Freigaben — fertige Drafts aus dem Wunsch-Trichter, die auf
          den Operator-Klick warten (Freigeben = Build-Task als Ketten-Kind). */}
      <div id="flow-section-funnel"><FunnelFreigaben /></div>

      {/* Disposition-Items — offene Follow-ups & Risiken aus abgeschlossenen
          Tasks, die auf eine Operator-Entscheidung warten (Phase 3b). */}
      <div id="flow-section-disposition"><DispositionLifecycle /></div>

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
        <div data-testid="flow-skeleton" className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3"><SkeletonCard rows={4} /><SkeletonCard rows={4} /><SkeletonCard rows={4} /></div>
      ) : (
        <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_340px]">
          <div className="min-w-0 space-y-5">
            {/* Projekt-Filter (tenant-Achse) */}
            {projects.length > 1 ? (
              // Mobil eine wischbare Zeile statt vieler Wrap-Zeilen (das
              // Chip-Feld wuchs auf ~8 Zeilen); ab sm wie gehabt umbrechend.
              <div className="-mx-1 flex items-center gap-1.5 overflow-x-auto px-1 pb-1 [scrollbar-width:none] [mask-image:linear-gradient(to_right,#000_88%,transparent)] sm:mx-0 sm:flex-wrap sm:overflow-x-visible sm:px-0 sm:pb-0 sm:[mask-image:none]">
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
                  <section id="flow-section-blocked">
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
                    {chainBoard.singles.length > MAX_CARDS ? <p className="mt-1.5 px-1 hc-type-label hc-dim">Nur {MAX_CARDS} von {chainBoard.singles.length} angezeigt</p> : null}
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

      {detailSheetOpen && selectedId ? (
        <FlowDetailSheet taskId={selectedId} taskTitle={selectedTask?.title} onClose={() => setDetailSheetOpen(false)}>
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
