import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { fetchJSON } from "@/lib/api";
import type { PromptForgeCatalog } from "../views/schmiede/catalog";
import { subscribe, refresh, getSnapshot, type StoreSnapshot, type StructuredError } from "./pollingStore";
import {
  AccountUsageResponseSchema,
  BacklogDetailSchema,
  BacklogResponseSchema,
  OrchestrationDetailSchema,
  OrchestrationBacklogResponseSchema,
  AutoresearchRunsResponseSchema,
  AutoresearchStatusSchema,
  CronObservabilityResponseSchema,
  CronOutputSchema,
  MetricsLiteResponseSchema,
  ProposalsResponseSchema,
  RecentResultsResponseSchema,
  ReviewVerdictsResponseSchema,
  RunSummaryResponseSchema,
  ReliabilityResponseSchema,
  RunsDailyResponseSchema,
  RunsCostsResponseSchema,
  RunsIssuesResponseSchema,
  DecisionQueueResponseSchema,
  TodayDigestResponseSchema,
  BlockedCompletionsResponseSchema,
  BoardResponseSchema,
  ChainGraphResponseSchema,
  FlowGateResponseSchema,
  FlowReleaseResponseSchema,
  FlowSizingResponseSchema,
  FlowTimeoutSweepResponseSchema,
  PlanSpecsResponseSchema,
  EpicsResponseSchema,
  TaskDetailResponseSchema,
  RunInspectSchema,
  SystemHealthResponseSchema,
  WorkersResponseSchema,
  VaultProvenanceResponseSchema,
  parseOrThrow,
} from "../lib/schemas";
import type { BacklogDetail, BacklogResponse, OrchestrationDetail, OrchestrationBacklogResponse, RunSummaryResponse, ReliabilityResponse, RunsDailyResponse, RunsCostsResponse, RunsIssuesResponse, TaskDetailResponse, DecisionQueueResponse, EpicsResponse, PlanSpecsResponse, FlowGateResponse } from "../lib/schemas";
import { isActionable } from "../lib/autoresearch";
import { proposalNeedsManualReview } from "../lib/autoresearchDecisionGuide";
import { buildAgentOpsSnapshot, type AgentOpsSnapshot } from "../lib/agentOps";
import { buildDecisionInbox, inboxSummary, type InboxItem, type InboxSummary } from "../lib/decisionInbox";
import { nowSec } from "../lib/derive";
import type { AccountUsageResponse, AutoresearchRunsResponse, AutoresearchStatus, BlockedCompletionsResponse, BoardResponse, ChainGraphResponse, CronObservabilityResponse, CronOutput, FlowReleaseOptions, FlowReleaseResponse, FlowSizingResponse, FlowTimeoutSweepResponse, MetricsLiteResponse, Proposal, ProposalsResponse, RecentResultsResponse, ReviewVerdictsResponse, RunInspect, SystemHealthResponse, TaskStatus, TodayDigestResponse, ToneName, WorkersResponse, VaultProvenanceResponse } from "../lib/types";
import { captureRequest, flowCaptureRequest, usesFlowCaptureEndpoint, type CaptureMethod } from "../lib/fleet";

type BatchConfirmState = "pending" | "ok" | "fail";
type BatchConfirmById = Record<string, { status: BatchConfirmState; detail?: string }>;
type BatchConfirmItem = { id?: string; ok?: boolean; status?: string; result?: string; detail?: string; error?: string; reason?: string };
type BatchConfirmResponse = {
  ok?: boolean;
  detail?: string;
  results?: Record<string, BatchConfirmItem> | BatchConfirmItem[];
  confirmed?: string[];
  failed?: string[];
};
export interface DeepAuditFinding {
  fileline: string;
  severity: "critical" | "high" | "medium" | "low";
  category: string;
  title: string;
  problem: string;
  evidence: string;
  fix_hint: string;
}

export interface DeepAuditStatus {
  state: "idle" | "running";
  pid: number | null;
  request_id: string | null;
  subsystem: string | null;
  started_at: string | null;
  last_run: unknown | null;
}

export interface DeepAuditFindingsResponse {
  ok: boolean;
  subsystem: string | null;
  model: string | null;
  tokens: number;
  iterations: number;
  reason: string;
  findings: DeepAuditFinding[];
  proposals: string[];
  created_at?: string | null;
  request_id?: string | null;
  files: string[];
}

export interface TestFoundryStatus {
  schema?: string;
  state: "idle" | "running" | "error";
  pid: number | null;
  target: string | null;
  started_at: string | null;
  last_run: unknown | null;
}

export interface TestFoundryTargetsResponse {
  schema?: string;
  targets: string[];
}

export const HERMES_RECENT_RESULTS_URL = "/api/plugins/kanban/runs/recent-results?limit=50&since_hours=48&outcome=completed";
export const HERMES_REVIEW_VERDICTS_URL = "/api/plugins/kanban/tasks/review-verdicts?limit=50";

export function testFoundryStatusPollIntervalMs(status: TestFoundryStatus | null): number | null {
  return status?.state === "running" ? 5000 : null;
}

type LoadState<T> = {
  data: T | null;
  error: string | null;
  loading: boolean;
  /** Epoch seconds of the last SUCCESSFUL load (E1 freshness). null until first ok. */
  lastUpdated: number | null;
  reload: () => Promise<void>;
  updateData: React.Dispatch<React.SetStateAction<T | null>>;
  /** Additive (back-compat): structured error + stale-while-error flag. */
  errorObj?: StructuredError | null;
  isStale?: boolean;
};

function batchConfirmResultForIds(ids: string[], response: BatchConfirmResponse): BatchConfirmById {
  const next: BatchConfirmById = {};
  const resultById = new Map<string, BatchConfirmItem>();
  if (Array.isArray(response.results)) {
    for (const item of response.results) {
      if (item.id) resultById.set(item.id, item);
    }
  } else if (response.results && typeof response.results === "object") {
    for (const [id, item] of Object.entries(response.results)) resultById.set(id, { id, ...item });
  }
  const confirmed = new Set(response.confirmed ?? []);
  const failed = new Set(response.failed ?? []);

  for (const id of ids) {
    const item = resultById.get(id);
    const itemFailed = failed.has(id) || item?.ok === false || item?.status === "fail" || item?.status === "failed";
    const itemOk = confirmed.has(id) || item?.ok === true || item?.status === "ok" || item?.status === "confirmed" || item?.status === "applied";
    if (itemFailed) {
      next[id] = { status: "fail", detail: item?.detail ?? item?.reason ?? item?.error ?? item?.result ?? response.detail };
    } else if (itemOk || response.ok !== false) {
      next[id] = { status: "ok", detail: item?.detail ?? item?.result };
    } else {
      next[id] = { status: "fail", detail: item?.detail ?? item?.reason ?? item?.error ?? item?.result ?? response.detail };
    }
  }
  return next;
}

// Backed by the shared pollingStore: subscribers on the same `key` dedupe to one
// timer + one request, get 5xx backoff and stale-while-error for free. The
// public LoadState shape is UNCHANGED (errorObj/isStale are additive) so no view
// needs to change. updateData patches the local snapshot for optimistic edits;
// the next poll/reload overwrites it with server truth (same as before).
function usePolling<T>(key: string, loader: () => Promise<T>, intervalMs: number): LoadState<T> {
  const [snap, setSnap] = useState<StoreSnapshot<T>>(() => getSnapshot<T>(key) ?? {
    data: null, error: null, errorObj: null, loading: true, lastUpdated: null, isStale: false,
  });
  const loaderRef = useRef(loader);
  useEffect(() => {
    loaderRef.current = loader;
  }, [loader]);

  useEffect(() => {
    return subscribe<T>(key, () => loaderRef.current(), intervalMs, setSnap);
  }, [key, intervalMs]);

  const reload = useCallback(() => refresh(key), [key]);
  const updateData = useCallback<React.Dispatch<React.SetStateAction<T | null>>>((action) => {
    setSnap((s) => ({ ...s, data: typeof action === "function" ? (action as (prev: T | null) => T | null)(s.data) : action }));
  }, []);

  return {
    data: snap.data,
    error: snap.error,
    errorObj: snap.errorObj,
    loading: snap.loading,
    lastUpdated: snap.lastUpdated,
    isStale: snap.isStale,
    reload,
    updateData,
  };
}

export function useAutoresearchStatus() {
  return usePolling<AutoresearchStatus>(
    "autoresearch/status",
    async () => parseOrThrow(AutoresearchStatusSchema, await fetchJSON<unknown>("/api/autoresearch/status"), "autoresearch/status"),
    5000,
  );
}

export function useAutoresearchRuns() {
  return usePolling<AutoresearchRunsResponse>(
    "autoresearch/runs",
    async () => parseOrThrow(AutoresearchRunsResponseSchema, await fetchJSON<unknown>("/api/autoresearch/runs"), "autoresearch/runs"),
    10000,
  );
}

type ProposalActivityTone = "emerald" | "amber" | "violet" | "red";
type ProposalActivityEntry = { at: number; text: string; tone: ProposalActivityTone };

// C5: persist the proposals activity timeline so it survives a tab reload.
// sessionStorage is tab-scoped and cleared when the tab closes. Both read and
// write are guarded — a disabled/full storage or a malformed payload silently
// falls back to an empty log instead of throwing into the render path.
const PROPOSAL_ACTIVITY_STORAGE_KEY = "hermes-control:activity-timeline";
const PROPOSAL_ACTIVITY_CAP = 8;

function readProposalActivity(): ProposalActivityEntry[] {
  try {
    const raw = sessionStorage.getItem(PROPOSAL_ACTIVITY_STORAGE_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return (parsed as unknown[])
      .filter((e): e is ProposalActivityEntry => {
        if (!e || typeof e !== "object") return false;
        const rec = e as Record<string, unknown>;
        return typeof rec.at === "number" && typeof rec.text === "string" && typeof rec.tone === "string";
      })
      .slice(0, PROPOSAL_ACTIVITY_CAP);
  } catch {
    return [];
  }
}

function writeProposalActivity(items: ProposalActivityEntry[]): void {
  try {
    sessionStorage.setItem(PROPOSAL_ACTIVITY_STORAGE_KEY, JSON.stringify(items.slice(0, PROPOSAL_ACTIVITY_CAP)));
  } catch {
    // storage unavailable or over quota — keep the in-memory timeline only
  }
}

export function useProposals() {
  const [activity, setActivity] = useState<ProposalActivityEntry[]>(() => readProposalActivity());
  const [busy, setBusy] = useState<string | null>(null);
  const [batchConfirmById, setBatchConfirmById] = useState<BatchConfirmById>({});
  useEffect(() => {
    writeProposalActivity(activity);
  }, [activity]);
  const state = usePolling<ProposalsResponse>(
    "autoresearch/proposals",
    async () => parseOrThrow(ProposalsResponseSchema, await fetchJSON<unknown>("/api/autoresearch/proposals"), "autoresearch/proposals"),
    6000,
  );

  const proposals = useMemo(() => state.data?.proposals ?? [], [state.data]);
  const openSkillProposals = useMemo(() => proposals.filter((p) => isActionable(p) && p.mode === "skill"), [proposals]);

  const log = useCallback((text: string, tone: ProposalActivityTone = "violet") => {
    setActivity((items) => [{ at: Math.floor(Date.now() / 1000), text, tone }, ...items].slice(0, PROPOSAL_ACTIVITY_CAP));
  }, []);

  const mutateProposal = useCallback((id: string, patch: Partial<Proposal>) => {
    state.updateData((current) => current ? {
      ...current,
      proposals: current.proposals.map((p) => p.id === id ? { ...p, ...patch } : p),
    } : current);
  }, [state]);

  const generate = useCallback(async () => {
    setBusy("generate");
    try {
      await fetchJSON<unknown>("/api/autoresearch/generate", { method: "POST" });
      log("Neue Vorschläge angefragt", "violet");
      await state.reload();
    } catch (e) {
      log(`Generate fehlgeschlagen: ${e instanceof Error ? e.message : String(e)}`, "red");
    } finally {
      setBusy(null);
    }
  }, [log, state]);

  // f-autoresearch-tab-driver: MiniMax scans the (now hermes_cli/-wide) code allowlist
  // for grounded weaknesses; findings land in the queue as mode=code proposals and apply
  // through the full test-suite gate. Dry-run only — no writes here.
  const generateCodeWeaknesses = useCallback(async (variant: "incremental" | "full" | "deep" = "incremental") => {
    const busyKey = variant === "full" ? "generate-code-full" : variant === "deep" ? "generate-code-deep" : "generate-code";
    setBusy(busyKey);
    try {
      // Deep-Scan raises both caps (more files, more kept findings) — runs minutes,
      // tokens visible in the ROI panel. Incremental/Full keep the snappy defaults.
      const body = variant === "deep"
        ? { scope: "incremental", max_files: 40, limit: 8 }
        : { scope: variant };
      const result = await fetchJSON<{ created_count?: number; files_seen?: number; skipped_unchanged?: number; vetoed?: number; tokens?: number }>("/api/autoresearch/generate-code-weaknesses", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const created = result.created_count ?? 0;
      const scanned = result.files_seen ?? 0;
      const skipped = result.skipped_unchanged ?? 0;
      const vetoed = result.vetoed ?? 0;
      const mode = variant === "full" ? "Voll" : variant === "deep" ? "Deep" : "inkrementell";
      const skippedNote = variant !== "full" && skipped > 0 ? ` · ${skipped} unverändert` : "";
      const vetoedNote = vetoed > 0 ? ` · ${vetoed} verworfen` : "";
      log(`Code-Schwächen (${mode}): ${created} ${created === 1 ? "Fund" : "Funde"} · ${scanned} gescannt${skippedNote}${vetoedNote}`, created > 0 ? "emerald" : "violet");
      await state.reload();
    } catch (e) {
      log(`Code-Schwächen-Suche fehlgeschlagen: ${e instanceof Error ? e.message : String(e)}`, "red");
    } finally {
      setBusy(null);
    }
  }, [log, state]);

  const apply = useCallback(async (proposal: Proposal) => {
    const isCode = proposal.mode === "code";
    setBusy(proposal.id);
    // Code apply kicks off the full test-suite gate (async, status "testing");
    // skill apply resolves synchronously.
    mutateProposal(proposal.id, isCode
      ? { status: "testing", result: "Test-Suite läuft …" }
      : { status: "applied", result: "übernommen" });
    try {
      const result = await fetchJSON<{ ok?: boolean; status?: string; result?: string; detail?: string }>("/api/autoresearch/apply", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: proposal.id, confirm: true }),
      });
      const label = proposal.title ?? proposal.target;
      if (result.status === "testing") {
        log(`${label}: Test-Suite gestartet — Ergebnis folgt`, "violet");
      } else if (result.ok === false) {
        log(`${label}: ${result.detail ?? result.result ?? "nicht übernommen"}`, "amber");
      } else {
        log(`${label}: ${result.result ?? "übernommen"}`, "emerald");
      }
      await state.reload();
    } catch (e) {
      log(`Übernehmen fehlgeschlagen: ${e instanceof Error ? e.message : String(e)}`, "red");
      await state.reload();
    } finally {
      setBusy(null);
    }
  }, [log, mutateProposal, state]);

  const skip = useCallback(async (proposal: Proposal) => {
    setBusy(proposal.id);
    mutateProposal(proposal.id, { status: "skipped", result: "übersprungen" });
    try {
      await fetchJSON<unknown>("/api/autoresearch/skip", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: proposal.id }),
      });
      log(`${proposal.title ?? proposal.target}: übersprungen`, "amber");
      await state.reload();
    } catch (e) {
      log(`Überspringen fehlgeschlagen: ${e instanceof Error ? e.message : String(e)}`, "red");
      await state.reload();
    } finally {
      setBusy(null);
    }
  }, [log, mutateProposal, state]);

  const applyAll = useCallback(async () => {
    // Snapshot the actionable set up front and dedupe by id, so individual
    // applies + their reloads churning the list under us can't re-POST a
    // proposal that has already been submitted this run.
    const pending = openSkillProposals.filter(isActionable);
    const safe = pending.filter((proposal) => !proposalNeedsManualReview(proposal));
    const blocked = pending.length - safe.length;
    if (blocked > 0) {
      log(`${blocked} Skill-Vorschläge brauchen Einzelreview und wurden nicht gesammelt übernommen.`, "amber");
    }
    if (safe.length === 0) return;
    const seen = new Set<string>();
    for (const proposal of safe) {
      if (seen.has(proposal.id)) continue;
      seen.add(proposal.id);
      await apply(proposal);
    }
  }, [apply, log, openSkillProposals]);

  const confirmBatch = useCallback(async (ids: string[]) => {
    const selectedIds = Array.from(new Set(ids)).filter(Boolean);
    if (selectedIds.length === 0) return;
    setBusy("confirm-batch");
    setBatchConfirmById((current) => ({
      ...current,
      ...Object.fromEntries(selectedIds.map((id) => [id, { status: "pending" as const }])),
    }));
    try {
      const result = await fetchJSON<BatchConfirmResponse>("/api/autoresearch/confirm-batch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ids: selectedIds }),
      });
      const next = batchConfirmResultForIds(selectedIds, result);
      setBatchConfirmById((current) => ({ ...current, ...next }));
      const failed = Object.values(next).filter((entry) => entry.status === "fail").length;
      log(`${selectedIds.length - failed}/${selectedIds.length} Vorschläge bestätigt`, failed > 0 ? "amber" : "emerald");
      await state.reload();
    } catch (e) {
      const detail = e instanceof Error ? e.message : String(e);
      setBatchConfirmById((current) => ({
        ...current,
        ...Object.fromEntries(selectedIds.map((id) => [id, { status: "fail" as const, detail }])),
      }));
      log(`Batch-Bestätigung fehlgeschlagen: ${detail}`, "red");
      await state.reload();
    } finally {
      setBusy(null);
    }
  }, [log, state]);

  return { ...state, proposals, openSkillProposals, activity, busy, batchConfirmById, generate, generateCodeWeaknesses, apply, skip, applyAll, confirmBatch };
}

export function useDeepAudit() {
  const [status, setStatus] = useState<DeepAuditStatus | null>(null);
  const [findings, setFindings] = useState<DeepAuditFindingsResponse | null>(null);
  const [subsystems, setSubsystems] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const aliveRef = useRef(true);
  useEffect(() => () => { aliveRef.current = false; }, []);

  const loadStatus = useCallback(async () => {
    const data = await fetchJSON<DeepAuditStatus>("/api/autoresearch/deep-audit/status");
    if (aliveRef.current) setStatus(data);
    return data;
  }, []);

  const loadFindings = useCallback(async () => {
    const data = await fetchJSON<DeepAuditFindingsResponse>("/api/autoresearch/deep-audit/findings");
    if (aliveRef.current) setFindings(data);
    return data;
  }, []);

  const reload = useCallback(async () => {
    setError(null);
    try {
      await Promise.all([loadStatus(), loadFindings()]);
    } catch (e) {
      if (aliveRef.current) setError(e instanceof Error ? e.message : String(e));
    }
  }, [loadFindings, loadStatus]);

  useEffect(() => {
    const loadInitial = async () => {
      setLoading(true);
      setError(null);
      try {
        const [subsystemData] = await Promise.all([
          fetchJSON<{ subsystems?: string[] }>("/api/autoresearch/deep-audit/subsystems"),
          reload(),
        ]);
        if (aliveRef.current) setSubsystems(subsystemData.subsystems ?? []);
      } catch (e) {
        if (aliveRef.current) setError(e instanceof Error ? e.message : String(e));
      } finally {
        if (aliveRef.current) setLoading(false);
      }
    };
    void loadInitial();
  }, [reload]);

  useEffect(() => {
    if (status?.state !== "running") return;
    const timer = window.setInterval(() => {
      if (document.hidden) return;
      void reload();
    }, 5000);
    return () => window.clearInterval(timer);
  }, [reload, status?.state]);

  const trigger = useCallback(async (subsystem: string, focus: string, maxFiles = 12) => {
    setBusy(true);
    setError(null);
    try {
      const result = await fetchJSON<{ ok?: boolean; request_id?: string }>("/api/autoresearch/deep-audit/trigger", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ subsystem, focus, max_files: maxFiles }),
      });
      await reload();
      return result;
    } catch (e) {
      const detail = e instanceof Error ? e.message : String(e);
      setError(detail);
      throw e;
    } finally {
      setBusy(false);
    }
  }, [reload]);

  return { status, findings, subsystems, loading, busy, error, reload, trigger };
}

export function useTestFoundry() {
  const [status, setStatus] = useState<TestFoundryStatus | null>(null);
  const [targets, setTargets] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const aliveRef = useRef(true);
  useEffect(() => () => { aliveRef.current = false; }, []);

  const loadStatus = useCallback(async () => {
    const data = await fetchJSON<TestFoundryStatus>("/api/autoresearch/test-foundry/status");
    if (aliveRef.current) setStatus(data);
    return data;
  }, []);

  const reload = useCallback(async () => {
    setError(null);
    try {
      await loadStatus();
    } catch (e) {
      if (aliveRef.current) setError(e instanceof Error ? e.message : String(e));
    }
  }, [loadStatus]);

  useEffect(() => {
    const loadInitial = async () => {
      setLoading(true);
      setError(null);
      try {
        const [targetData] = await Promise.all([
          fetchJSON<TestFoundryTargetsResponse>("/api/autoresearch/test-foundry/targets"),
          reload(),
        ]);
        if (aliveRef.current) setTargets(targetData.targets ?? []);
      } catch (e) {
        if (aliveRef.current) setError(e instanceof Error ? e.message : String(e));
      } finally {
        if (aliveRef.current) setLoading(false);
      }
    };
    void loadInitial();
  }, [reload]);

  const pollIntervalMs = testFoundryStatusPollIntervalMs(status);
  useEffect(() => {
    if (pollIntervalMs === null) return;
    const timer = window.setInterval(() => {
      if (document.hidden) return;
      void reload();
    }, pollIntervalMs);
    return () => window.clearInterval(timer);
  }, [pollIntervalMs, reload]);

  const trigger = useCallback(async (target: string, apply: boolean) => {
    setBusy(true);
    setError(null);
    try {
      const result = await fetchJSON<{ ok?: boolean; pid?: number; target?: string }>("/api/autoresearch/test-foundry/trigger", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target, apply }),
      });
      await reload();
      return result;
    } catch (e) {
      const detail = e instanceof Error ? e.message : String(e);
      setError(detail);
      throw e;
    } finally {
      setBusy(false);
    }
  }, [reload]);

  return { status, targets, loading, busy, error, reload, trigger };
}

export function useHermesWorkers() {
  return usePolling<WorkersResponse>(
    "workers/active",
    async () => parseOrThrow(WorkersResponseSchema, await fetchJSON<unknown>("/api/plugins/kanban/workers/active"), "workers/active"),
    5000,
  );
}

export function useAccountUsage() {
  return usePolling<AccountUsageResponse>(
    "account-usage",
    async () => parseOrThrow(AccountUsageResponseSchema, await fetchJSON<unknown>("/api/account-usage"), "account-usage"),
    60000,
  );
}


// Loader auch für Nicht-Hook-Subscriber exportiert (CommandPalette abonniert
// Board/Crons/Epics on-demand, solange die Palette offen ist — sonst bleibt
// die globale Suche leer, bis die jeweilige View einmal besucht wurde).
// card_diagnostics=summary drops the per-card structured diagnostics list,
// card_body=none drops body+result (BoardTaskSchema strips both anyway —
// together they dominate the 8 s payload on real boards); the drawer
// fetches detail via /tasks/:id. The kanban plugin dashboard keeps the
// defaults (full). The server also sends an ETag, so an unchanged board
// revalidates as a 304 instead of re-transferring.
export const boardLoader = async () =>
  parseOrThrow(BoardResponseSchema, await fetchJSON<unknown>("/api/plugins/kanban/board?card_diagnostics=summary&card_body=none"), "kanban/board");

// Full kanban board grouped by status column — the Fleet pipeline (stage
// counts + actionable rows) reads this. 8s keeps the operator's stage view
// fresh without churning the DB; usePolling pauses it when the tab is hidden.
export function useBoard() {
  return usePolling<BoardResponse>("kanban/board", boardLoader, 8000);
}

export interface PlanSpecQueryOptions {
  limit?: number;
  valid?: boolean | null;
  search?: string;
}

function planSpecsUrl(options: PlanSpecQueryOptions = {}) {
  const params = new URLSearchParams({ scope: "open" });
  if (options.limit && options.limit > 0) params.set("limit", String(options.limit));
  if (options.valid != null) params.set("valid", String(options.valid));
  const query = options.search?.trim();
  if (query) params.set("q", query);
  return `/api/plugins/kanban/planspecs?${params.toString()}`;
}

export function usePlanSpecs(options: PlanSpecQueryOptions = {}) {
  const key = `kanban/planspecs:${options.limit ?? "all"}:${options.valid ?? "any"}:${options.search?.trim() ?? ""}`;
  return usePolling<PlanSpecsResponse>(
    key,
    async () => parseOrThrow(PlanSpecsResponseSchema, await fetchJSON<unknown>(planSpecsUrl(options)), "kanban/planspecs"),
    15000,
  );
}

// Derive which FO backlog items are already visible on the board (status IN
// ready/running/blocked/review/triage/scheduled) via idempotency_key matching.
// Returns a map { foItemId → FoBoardStatus } for fast O(1) lookup per row.
// Uses the shared useBoard() poll — no extra request.
export type FoBoardStatus = {
  /** Board task id (not the FO backlog id). */
  taskId: string;
  /** Raw kanban status string. */
  status: string;
  /** Human-readable label for the badge in the FO tab. */
  label: string;
};

const FO_BOARD_STATUSES = new Set(["ready", "running", "blocked", "review", "triage", "scheduled"]);
const FO_STATUS_LABEL: Record<string, string> = {
  running: "läuft",
  ready: "wartet",
  triage: "wartet",
  scheduled: "wartet",
  blocked: "blockiert",
  review: "in Review",
};

// Pure helper: extract the FO backlog item id from a kanban idempotency_key.
// Returns null for null/empty keys or keys without the "fo-backlog:" prefix.
// Exported for unit tests — no behaviour change to useFoBoardStatus.
export function extractFoIdFromIdempotencyKey(key: string | null | undefined): string | null {
  if (!key) return null;
  if (!key.startsWith("fo-backlog:")) return null;
  const foId = key.slice("fo-backlog:".length);
  return foId || null;
}

export function useFoBoardStatus(): Record<string, FoBoardStatus> {
  const board = useBoard();
  return useMemo(() => {
    const data = board.data;
    if (!data) return {};
    const result: Record<string, FoBoardStatus> = {};
    for (const col of data.columns) {
      if (!FO_BOARD_STATUSES.has(col.name)) continue;
      for (const task of col.tasks) {
        const foId = extractFoIdFromIdempotencyKey(task.idempotency_key);
        if (!foId) continue;
        result[foId] = {
          taskId: task.id,
          status: task.status,
          label: FO_STATUS_LABEL[task.status] ?? task.status,
        };
      }
    }
    return result;
  }, [board.data]);
}

// Epics (Vorhaben-Ebene): Rollup pro Epic für die Flow-Gruppierung und die
// Statistik-Kompaktübersicht. 15s — Epics ändern sich selten; ein Fehler hier
// darf das Board nie blanken (die Gruppierung degradiert auf rohe IDs).
export const epicsLoader = async () =>
  parseOrThrow(EpicsResponseSchema, await fetchJSON<unknown>("/api/plugins/kanban/epics"), "kanban/epics");

export function useEpics() {
  // M1: Epics ändern sich selten (Vorhaben-Ebene) — 60 s statt 15 s spart Polls.
  return usePolling<EpicsResponse>("kanban/epics", epicsLoader, 60000);
}

// Epic-Schreibpfade (Phase-1-API): anlegen, schließen, ganze Kette zuordnen.
// Die Ketten-Zuordnung patcht jedes Mitglied einzeln (PATCH epic_id) — Ketten
// sind klein, und der getestete Einzel-Task-Pfad bleibt die einzige Wahrheit.
// `busyKey` trägt die Epic-/Root-ID der laufenden Aktion (ein Operator,
// eine Aktion zur Zeit).
export function useEpicActions(onDone?: () => void | Promise<void>) {
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const aliveRef = useRef(true);
  useEffect(() => () => { aliveRef.current = false; }, []);

  const createEpic = useCallback(async (title: string, body?: string) => {
    setBusyKey("create");
    setError(null);
    try {
      const res = await fetchJSON<{ epic?: { id?: string } }>(
        "/api/plugins/kanban/epics",
        { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body?.trim() ? { title, body } : { title }) },
      );
      await onDone?.();
      return { ok: true as const, id: res.epic?.id ?? null };
    } catch (e) {
      const detail = extractDetail(e);
      if (aliveRef.current) setError(detail);
      return { ok: false as const, detail };
    } finally {
      if (aliveRef.current) setBusyKey(null);
    }
  }, [onDone]);

  const closeEpic = useCallback(async (epicId: string) => {
    setBusyKey(epicId);
    setError(null);
    try {
      await fetchJSON<{ epic?: unknown }>(
        `/api/plugins/kanban/epics/${encodeURIComponent(epicId)}/close`,
        { method: "POST" },
      );
      await onDone?.();
      return { ok: true as const };
    } catch (e) {
      const detail = extractDetail(e);
      if (aliveRef.current) setError(detail);
      return { ok: false as const, detail };
    } finally {
      if (aliveRef.current) setBusyKey(null);
    }
  }, [onDone]);

  const assignChain = useCallback(async (rootId: string, taskIds: string[], epicId: string | null) => {
    setBusyKey(rootId);
    setError(null);
    try {
      for (const id of taskIds) {
        await fetchJSON<{ task?: unknown }>(
          `/api/plugins/kanban/tasks/${encodeURIComponent(id)}`,
          { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ epic_id: epicId }) },
        );
      }
      await onDone?.();
      return { ok: true as const };
    } catch (e) {
      const detail = extractDetail(e);
      if (aliveRef.current) setError(detail);
      return { ok: false as const, detail };
    } finally {
      if (aliveRef.current) setBusyKey(null);
    }
  }, [onDone]);

  const clearError = useCallback(() => setError(null), []);
  return { busyKey, error, createEpic, closeEpic, assignChain, clearError };
}

// Roster size — how many profiles are installed (the "/ N" denominator on the
// Worker pod). Tolerant + slow (60s): the roster changes rarely and a failure
// here must never blank the Fleet, so it degrades to null (pod shows just the
// active count).
export function useRosterCount() {
  return usePolling<number | null>(
    "kanban/profiles-count",
    async () => {
      const data = await fetchJSON<{ profiles?: unknown[] }>("/api/plugins/kanban/profiles");
      return Array.isArray(data.profiles) ? data.profiles.length : null;
    },
    60000,
  );
}

// Operator stage transitions. Wraps PATCH /tasks/{id} (the same endpoint the
// kanban drawer uses) so a Fleet stage button has a REAL effect: Plan
// (triage→todo), Dispatch (todo→ready, auto-dispatches), Ship (review→done),
// Rework (review→blocked), Reopen (blocked→ready). The 409 "blocked by
// parent(s)" detail is surfaced verbatim so the guard is honest, not silent.
export interface TaskActionExtra {
  block_reason?: string;
  summary?: string;
}
export function useTaskAction(onDone?: () => void | Promise<void>) {
  const [busyId, setBusyId] = useState<string | null>(null);
  const [errorById, setErrorById] = useState<Record<string, string>>({});
  const aliveRef = useRef(true);
  useEffect(() => () => { aliveRef.current = false; }, []);
  const run = useCallback(async (taskId: string, status: TaskStatus, extra?: TaskActionExtra) => {
    setBusyId(taskId);
    setErrorById((prev) => ({ ...prev, [taskId]: "" }));
    try {
      const res = await fetchJSON<{ task?: unknown }>(
        `/api/plugins/kanban/tasks/${encodeURIComponent(taskId)}`,
        { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ status, ...extra }) },
      );
      await onDone?.();
      return { ok: true as const, res };
    } catch (e) {
      const detail = extractDetail(e);
      if (aliveRef.current) setErrorById((prev) => ({ ...prev, [taskId]: detail }));
      return { ok: false as const, detail };
    } finally {
      if (aliveRef.current) setBusyId(null);
    }
  }, [onDone]);
  const clearError = useCallback((taskId: string) => setErrorById((prev) => ({ ...prev, [taskId]: "" })), []);
  return { busyId, errorById, run, clearError };
}

// K3: Inline-Resolve für eine Verifier-Ablehnung (review_rejected) direkt am
// CommandHome — die EINE dominante Auflösung: Task entblocken (PATCH ready;
// der Server mappt das auf unblock_task) + ein Dispatcher-Tick, damit der
// Fix-Lauf sofort startet statt auf den nächsten Gateway-Tick zu warten.
// Der Coder-Retry sieht das Verifier-Feedback automatisch im worker_context.
// `doneIds` hält erfolgreich gestartete Tasks fest, bis der Decision-Queue-Poll
// die Zeile von selbst fallen lässt.
export function useFixRedispatch() {
  const [busyId, setBusyId] = useState<string | null>(null);
  const [doneIds, setDoneIds] = useState<Record<string, boolean>>({});
  const [errorById, setErrorById] = useState<Record<string, string>>({});
  const aliveRef = useRef(true);
  useEffect(() => () => { aliveRef.current = false; }, []);
  const run = useCallback(async (taskId: string) => {
    setBusyId(taskId);
    setErrorById((prev) => ({ ...prev, [taskId]: "" }));
    try {
      await fetchJSON<{ task?: unknown }>(
        `/api/plugins/kanban/tasks/${encodeURIComponent(taskId)}`,
        { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ status: "ready" }) },
      );
      // run_id ist für action=dispatch irrelevant (reiner Tick, kein Run-Bezug)
      // — 0 als Platzhalter, wie der Endpoint es erlaubt.
      await fetchJSON<{ ok?: boolean; detail?: string }>(
        "/api/plugins/kanban/workers/0/action",
        { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "dispatch", confirm: true, reason: "Fix-Lauf nach Verifier-Ablehnung (CommandHome)" }) },
      );
      if (aliveRef.current) setDoneIds((prev) => ({ ...prev, [taskId]: true }));
      return { ok: true as const };
    } catch (e) {
      const detail = extractDetail(e);
      if (aliveRef.current) setErrorById((prev) => ({ ...prev, [taskId]: detail }));
      return { ok: false as const, detail };
    } finally {
      if (aliveRef.current) setBusyId(null);
    }
  }, []);
  return { busyId, doneIds, errorById, run };
}

// fetchJSON throws `Error("409: {\"detail\":\"…\"}")` — pull out the human detail.
function extractDetail(e: unknown): string {
  const msg = e instanceof Error ? e.message : String(e);
  const m = msg.match(/^\d+:\s*(.*)$/s);
  const body = m ? m[1] : msg;
  try {
    const parsed = JSON.parse(body);
    if (parsed && typeof parsed.detail === "string") return parsed.detail;
  } catch {
    /* not JSON — use the raw text */
  }
  return body || msg;
}

// One-click "copy this Family-Organizer backlog item into the Fleet": creates a
// real Kanban task (POST /tasks) parked in triage (Capture stage) so it shows up
// in the Fleet pipeline where Plan/Execute/Verify/Ship drive it. Idempotent via
// idempotency_key=fo-backlog:<id> — re-clicking the same item returns the
// existing task instead of duplicating it. tenant=family-organizer namespaces them.
export type CommissionState = "busy" | "done" | "error";
export interface CommissionPayload {
  title: string;
  body: string;
  priority: number;
  assignee?: string;
}
export interface CommissionMeta {
  /** Namespaces the kanban task by origin: "family-organizer" | "orchestrator". */
  tenant: string;
  /** Stable dedup key, e.g. `fo-backlog:<id>` / `orch-backlog:<id>` — a second
   *  click returns the existing card instead of creating a duplicate. */
  idempotencyKey: string;
}
export interface CommissionResult {
  ok: boolean;
  taskId?: string;
  taskStatus?: string;
  detail?: string;
}
export function useCommissionToFleet() {
  const [stateById, setStateById] = useState<Record<string, CommissionState>>({});
  const [errorById, setErrorById] = useState<Record<string, string>>({});
  const [taskIdById, setTaskIdById] = useState<Record<string, string>>({});
  const aliveRef = useRef(true);
  useEffect(() => () => { aliveRef.current = false; }, []);
  const commission = useCallback(async (id: string, payload: CommissionPayload, meta: CommissionMeta): Promise<CommissionResult> => {
    setStateById((prev) => ({ ...prev, [id]: "busy" }));
    setErrorById((prev) => ({ ...prev, [id]: "" }));
    try {
      const res = await fetchJSON<{ task?: { id?: string; status?: string } }>("/api/plugins/kanban/tasks", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title: payload.title,
          body: payload.body,
          assignee: payload.assignee ?? "coder",
          tenant: meta.tenant,
          priority: payload.priority,
          triage: true,
          // Park it in the Fleet (status `scheduled`) so a one-click transfer
          // never auto-launches a worker — the operator clicks Dispatch in the
          // Fleet pipeline when they want it to run.
          park: true,
          idempotency_key: meta.idempotencyKey,
          notify_home: false,
        }),
      });
      if (aliveRef.current) {
        setStateById((prev) => ({ ...prev, [id]: "done" }));
        if (res.task?.id) setTaskIdById((prev) => ({ ...prev, [id]: res.task!.id! }));
      }
      return { ok: true, taskId: res.task?.id, taskStatus: res.task?.status };
    } catch (e) {
      const detail = extractDetail(e);
      if (aliveRef.current) {
        setStateById((prev) => ({ ...prev, [id]: "error" }));
        setErrorById((prev) => ({ ...prev, [id]: detail }));
      }
      return { ok: false, detail };
    }
  }, []);
  return { stateById, errorById, taskIdById, commission };
}

// Quick-capture a brand-new task into the Flow pipeline from the "+ Aufgabe"
// button (POST /tasks). `mode` is the operator's pick in the sheet: "park"
// (safe default — lands GEPARKT in scheduled/Plan, no auto-start) or
// "orchestrate" (lands in triage/Capture, the in-gateway orchestrator takes
// over). The payload shape lives in lib/fleet.captureRequest (pure + tested).
export type CaptureState = "idle" | "busy" | "done" | "error";
export interface CaptureResult {
  ok: boolean;
  taskId?: string;
  taskStatus?: string;
  detail?: string;
}
export function useCaptureTask(onCreated?: (taskId: string) => void) {
  const [state, setState] = useState<CaptureState>("idle");
  const [error, setError] = useState<string>("");
  const aliveRef = useRef(true);
  useEffect(() => () => { aliveRef.current = false; }, []);
  // `capture(title, method, gate)` routes to the right backend: park & lean+AUTO
  // hit the plain POST /tasks (Stufe-A); document* and lean+GATE hit the
  // backend-driven POST /tasks/flow-capture (which plans synchronously, so this
  // promise can take a little longer — the sheet shows a "planning" state). Both
  // resolve to the new task id; flow-capture also reports ok=false + reason when
  // the planner itself fails (the root is left safely parked).
  const capture = useCallback(async (title: string, method: CaptureMethod, gate: boolean): Promise<CaptureResult> => {
    if (!title.trim()) {
      setState("error");
      setError("Titel fehlt");
      return { ok: false, detail: "Titel fehlt" };
    }
    setState("busy");
    setError("");
    try {
      if (usesFlowCaptureEndpoint(method, gate)) {
        const res = await fetchJSON<{ ok?: boolean; task_id?: string; reason?: string }>("/api/plugins/kanban/tasks/flow-capture", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(flowCaptureRequest(title, method, gate)),
        });
        if (res.ok === false) {
          const detail = res.reason || "Planung fehlgeschlagen";
          if (aliveRef.current) { setState("error"); setError(detail); }
          // The root was still created (parked) — surface it so the operator can
          // act on the parked card.
          if (res.task_id) onCreated?.(res.task_id);
          return { ok: false, taskId: res.task_id, detail };
        }
        if (aliveRef.current) setState("done");
        if (res.task_id) onCreated?.(res.task_id);
        return { ok: true, taskId: res.task_id };
      }
      const res = await fetchJSON<{ task?: { id?: string; status?: string } }>("/api/plugins/kanban/tasks", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(captureRequest(title, method)),
      });
      if (aliveRef.current) setState("done");
      if (res.task?.id) onCreated?.(res.task.id);
      return { ok: true, taskId: res.task?.id, taskStatus: res.task?.status };
    } catch (e) {
      const detail = extractDetail(e);
      if (aliveRef.current) {
        setState("error");
        setError(detail);
      }
      return { ok: false, detail };
    }
  }, [onCreated]);
  const reset = useCallback(() => { setState("idle"); setError(""); }, []);
  return { state, error, capture, reset };
}

// Release ("Go ausführen") a gated Flow plan: POST /tasks/{root}/flow-release
// unblocks every subtask held in `scheduled` so the dispatcher picks them up.
export function useFlowRelease(onDone?: () => void | Promise<void>) {
  const [busyId, setBusyId] = useState<string | null>(null);
  const [errorById, setErrorById] = useState<Record<string, string>>({});
  const aliveRef = useRef(true);
  useEffect(() => () => { aliveRef.current = false; }, []);
  const release = useCallback(async (rootId: string, options?: FlowReleaseOptions): Promise<{ ok: boolean; released?: number; detail?: string }> => {
    setBusyId(rootId);
    setErrorById((prev) => ({ ...prev, [rootId]: "" }));
    try {
      const res: FlowReleaseResponse = parseOrThrow(
        FlowReleaseResponseSchema,
        await fetchJSON<unknown>(
          `/api/plugins/kanban/tasks/${encodeURIComponent(rootId)}/flow-release`,
          { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(options ?? {}) },
        ),
        "flow-release",
      );
      await onDone?.();
      return { ok: true, released: res.released ?? 0 };
    } catch (e) {
      const detail = extractDetail(e);
      if (aliveRef.current) setErrorById((prev) => ({ ...prev, [rootId]: detail }));
      return { ok: false, detail };
    } finally {
      if (aliveRef.current) setBusyId(null);
    }
  }, [onDone]);
  return { busyId, errorById, release };
}

export function useFlowGate(rootId: string | null, onDone?: () => void | Promise<void>) {
  const [data, setData] = useState<FlowGateResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const aliveRef = useRef(true);
  const inFlightRef = useRef(false);
  useEffect(() => () => { aliveRef.current = false; }, []);
  const reload = useCallback(async (): Promise<FlowGateResponse | null> => {
    if (!rootId) {
      if (aliveRef.current) setData(null);
      return null;
    }
    inFlightRef.current = true;
    if (aliveRef.current) setLoading(true);
    try {
      const parsed = parseOrThrow(
        FlowGateResponseSchema,
        await fetchJSON<unknown>(`/api/plugins/kanban/tasks/${encodeURIComponent(rootId)}/flow-gate`),
        "flow-gate",
      );
      if (aliveRef.current) {
        setData(parsed);
        setError("");
      }
      return parsed;
    } catch (e) {
      const detail = extractDetail(e);
      if (aliveRef.current) setError(detail);
      return null;
    } finally {
      inFlightRef.current = false;
      if (aliveRef.current) setLoading(false);
    }
  }, [rootId]);
  useEffect(() => {
    const initial = window.setTimeout(() => {
      void reload();
    }, 0);
    if (!rootId) return () => window.clearTimeout(initial);
    const interval = window.setInterval(() => {
      if (!document.hidden && !inFlightRef.current) void reload();
    }, 10000);
    return () => {
      window.clearTimeout(initial);
      window.clearInterval(interval);
    };
  }, [rootId, reload]);
  const sizing = useCallback(async (
    action: "merge" | "split",
    taskIds: string[],
    payload?: { title?: string; body?: string; assignee?: string | null },
  ): Promise<FlowSizingResponse | null> => {
    if (!rootId) return null;
    setBusy(true);
    setError("");
    try {
      const parsed = parseOrThrow(
        FlowSizingResponseSchema,
        await fetchJSON<unknown>(
          `/api/plugins/kanban/tasks/${encodeURIComponent(rootId)}/flow-gate/sizing`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ action, task_ids: taskIds, ...payload }),
          },
        ),
        "flow-gate/sizing",
      );
      if (aliveRef.current) setData(parsed.gate);
      await onDone?.();
      return parsed;
    } catch (e) {
      const detail = extractDetail(e);
      if (aliveRef.current) setError(detail);
      return null;
    } finally {
      if (aliveRef.current) setBusy(false);
    }
  }, [rootId, onDone]);
  const sweepTimeouts = useCallback(async (timeoutSeconds?: number): Promise<FlowTimeoutSweepResponse | null> => {
    setBusy(true);
    setError("");
    try {
      const parsed = parseOrThrow(
        FlowTimeoutSweepResponseSchema,
        await fetchJSON<unknown>(
          "/api/plugins/kanban/tasks/flow-gate/timeout-sweep",
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(timeoutSeconds ? { timeout_seconds: timeoutSeconds } : {}),
          },
        ),
        "flow-gate/timeout-sweep",
      );
      await Promise.all([onDone?.(), reload()]);
      return parsed;
    } catch (e) {
      const detail = extractDetail(e);
      if (aliveRef.current) setError(detail);
      return null;
    } finally {
      if (aliveRef.current) setBusy(false);
    }
  }, [onDone, reload]);
  return { data, loading, busy, error, reload, sizing, sweepTimeouts };
}

export function useChainGraph(rootId: string | null) {
  const [data, setData] = useState<ChainGraphResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const aliveRef = useRef(true);
  const inFlightRef = useRef(false);
  useEffect(() => () => { aliveRef.current = false; }, []);
  const reload = useCallback(async (): Promise<ChainGraphResponse | null> => {
    if (!rootId) {
      if (aliveRef.current) setData(null);
      return null;
    }
    inFlightRef.current = true;
    if (aliveRef.current) setLoading(true);
    try {
      const parsed = parseOrThrow(
        ChainGraphResponseSchema,
        await fetchJSON<unknown>(`/api/plugins/kanban/tasks/${encodeURIComponent(rootId)}/chain-graph`),
        "chain-graph",
      );
      if (aliveRef.current) {
        setData(parsed);
        setError("");
      }
      return parsed;
    } catch (e) {
      const detail = extractDetail(e);
      if (aliveRef.current) setError(detail);
      return null;
    } finally {
      inFlightRef.current = false;
      if (aliveRef.current) setLoading(false);
    }
  }, [rootId]);
  useEffect(() => {
    const initial = window.setTimeout(() => {
      void reload();
    }, 0);
    if (!rootId) return () => window.clearTimeout(initial);
    const interval = window.setInterval(() => {
      if (!document.hidden && !inFlightRef.current) void reload();
    }, 8000);
    return () => {
      window.clearTimeout(initial);
      window.clearInterval(interval);
    };
  }, [rootId, reload]);
  return { data, loading, error, reload };
}

// In-place dispatch of a parked FO task: PATCH /tasks/{id} with status="ready"
// moves a task from scheduled/triage → ready so the dispatcher picks it up.
// Keyed by Board task id (not the FO backlog id). Calls onDone() after success so
// the caller can e.g. refresh the board snapshot.
export type DispatchFoState = "idle" | "busy" | "done" | "error";

export function useDispatchFoTask(onDone?: () => void | Promise<void>) {
  const [stateById, setStateById] = useState<Record<string, DispatchFoState>>({});
  const [errorById, setErrorById] = useState<Record<string, string>>({});
  const aliveRef = useRef(true);
  useEffect(() => () => { aliveRef.current = false; }, []);
  const dispatch = useCallback(async (taskId: string): Promise<{ ok: boolean; detail?: string }> => {
    if (aliveRef.current) {
      setStateById((prev) => ({ ...prev, [taskId]: "busy" }));
      setErrorById((prev) => ({ ...prev, [taskId]: "" }));
    }
    try {
      await fetchJSON<unknown>(
        `/api/plugins/kanban/tasks/${encodeURIComponent(taskId)}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ status: "ready" }),
        },
      );
      if (aliveRef.current) setStateById((prev) => ({ ...prev, [taskId]: "done" }));
      await onDone?.();
      return { ok: true };
    } catch (e) {
      const detail = extractDetail(e);
      if (aliveRef.current) {
        setStateById((prev) => ({ ...prev, [taskId]: "error" }));
        setErrorById((prev) => ({ ...prev, [taskId]: detail }));
      }
      return { ok: false, detail };
    }
  }, [onDone]);
  const reset = useCallback((taskId: string) => {
    if (aliveRef.current) {
      setStateById((prev) => ({ ...prev, [taskId]: "idle" }));
      setErrorById((prev) => ({ ...prev, [taskId]: "" }));
    }
  }, []);
  return { stateById, errorById, dispatch, reset };
}

// On-demand task detail (runs + events + deliverables) for the Flow board's live
// receipt rail. Keyed by task id, fetched when a card is selected (like
// useRunInspect / useBacklogDetail) so the board poll stays lean.
export function useTaskDetail() {
  const [detailById, setDetailById] = useState<Record<string, TaskDetailResponse>>({});
  const [errorById, setErrorById] = useState<Record<string, string>>({});
  const [loadingId, setLoadingId] = useState<string | null>(null);
  const aliveRef = useRef(true);
  useEffect(() => () => { aliveRef.current = false; }, []);
  const fetch = useCallback(async (taskId: string): Promise<TaskDetailResponse | null> => {
    setLoadingId(taskId);
    try {
      const raw = await fetchJSON<unknown>(`/api/plugins/kanban/tasks/${encodeURIComponent(taskId)}`);
      const data = parseOrThrow(TaskDetailResponseSchema, raw, "tasks/detail");
      if (!aliveRef.current) return data;
      setDetailById((prev) => ({ ...prev, [taskId]: data }));
      setErrorById((prev) => ({ ...prev, [taskId]: "" }));
      return data;
    } catch (err) {
      if (aliveRef.current) setErrorById((prev) => ({ ...prev, [taskId]: err instanceof Error ? err.message : String(err) }));
      return null;
    } finally {
      if (aliveRef.current) setLoadingId(null);
    }
  }, []);
  return { detailById, errorById, loadingId, fetch };
}

export function useHermesRecentResults() {
  return usePolling<RecentResultsResponse>(
    "runs/recent-results",
    async () => parseOrThrow(
      RecentResultsResponseSchema,
      await fetchJSON<unknown>(HERMES_RECENT_RESULTS_URL),
      "runs/recent-results",
    ),
    20000,
  );
}

export function useHermesTodayDigest() {
  return usePolling<TodayDigestResponse>(
    "runs/today-digest",
    async () => parseOrThrow(
      TodayDigestResponseSchema,
      await fetchJSON<unknown>("/api/plugins/kanban/runs/today-digest?limit=12"),
      "runs/today-digest",
    ),
    20000,
  );
}

export function useHermesRunSummary() {
  return usePolling<RunSummaryResponse>(
    "runs/summary",
    async () => parseOrThrow(
      RunSummaryResponseSchema,
      await fetchJSON<unknown>("/api/plugins/kanban/runs/summary?since_hours=24"),
      "runs/summary",
    ),
    20000,
  );
}

// Phase 3 (Statistik): Verlässlichkeit pro Profil (7d + 30d-Baseline) und die
// Tages-Zeitreihe für die Charts. Beides langsam gepollt — Aggregate ändern
// sich im Minutentakt, nicht im Sekundentakt.
export function useHermesReliability() {
  return usePolling<ReliabilityResponse>(
    "runs/reliability",
    async () => parseOrThrow(
      ReliabilityResponseSchema,
      await fetchJSON<unknown>("/api/plugins/kanban/runs/reliability?since_hours=168&baseline_hours=720&min_n=5"),
      "runs/reliability",
    ),
    60000,
  );
}

export function useHermesRunsDaily() {
  return usePolling<RunsDailyResponse>(
    "runs/daily",
    async () => parseOrThrow(
      RunsDailyResponseSchema,
      await fetchJSON<unknown>("/api/plugins/kanban/runs/daily?days=30"),
      "runs/daily",
    ),
    60000,
  );
}

// F4 (Statistik): Kosten heute/7 Tage + Top-Profile — gleiche langsame
// Poll-Kadenz wie die übrigen Aggregate.
export function useHermesRunsCosts() {
  return usePolling<RunsCostsResponse>(
    "runs/costs",
    async () => parseOrThrow(
      RunsCostsResponseSchema,
      await fetchJSON<unknown>("/api/plugins/kanban/runs/costs?days=7"),
      "runs/costs",
    ),
    60000,
  );
}

// ST4 (Statistik-Broadsheet): wiederkehrende Fehler für die Fehler-Taxonomie —
// dieselbe Quelle wie das Issue-Board (F6), langsam gepollt (30-Tage-Aggregat).
export function useHermesRunsIssues() {
  return usePolling<RunsIssuesResponse>(
    "runs/issues",
    async () => parseOrThrow(
      RunsIssuesResponseSchema,
      await fetchJSON<unknown>("/api/plugins/kanban/runs/issues?days=30&limit=50"),
      "runs/issues",
    ),
    60000,
  );
}

export function useHermesReviewVerdicts() {
  return usePolling<ReviewVerdictsResponse>(
    "tasks/review-verdicts",
    async () => parseOrThrow(
      ReviewVerdictsResponseSchema,
      await fetchJSON<unknown>(HERMES_REVIEW_VERDICTS_URL),
      "tasks/review-verdicts",
    ),
    20000,
  );
}

export function useHermesBlockedCompletions() {
  return usePolling<BlockedCompletionsResponse>(
    "runs/blocked-completions",
    async () => parseOrThrow(
      BlockedCompletionsResponseSchema,
      await fetchJSON<unknown>("/api/plugins/kanban/runs/blocked-completions?since_hours=48"),
      "runs/blocked-completions",
    ),
    20000,
  );
}

export function useSystemHealth() {
  return usePolling<SystemHealthResponse>(
    "health-status",
    async () => parseOrThrow(SystemHealthResponseSchema, await fetchJSON<unknown>("/api/health-status"), "health-status"),
    5000,
  );
}

export function useVaultProvenance() {
  return usePolling<VaultProvenanceResponse>(
    "vault-provenance",
    async () => parseOrThrow(VaultProvenanceResponseSchema, await fetchJSON<unknown>("/api/vault/provenance"), "vault-provenance"),
    20000,
  );
}

// In-process self-metrics (per route-group latency/error rates). 5s like health.
export function useMetricsLite() {
  return usePolling<MetricsLiteResponse>(
    "metrics-lite",
    async () => parseOrThrow(MetricsLiteResponseSchema, await fetchJSON<unknown>("/api/metrics-lite"), "metrics-lite"),
    5000,
  );
}

// Read-only family-organizer backlog board. Polled slowly — the backlog changes
// rarely (a handful of git commits a day), so 30s keeps it fresh without churn.
export function useBacklog() {
  return usePolling<BacklogResponse>(
    "family-organizer/backlog",
    async () => parseOrThrow(BacklogResponseSchema, await fetchJSON<unknown>("/api/family-organizer/backlog"), "family-organizer/backlog"),
    30000,
  );
}

// Read-only Orchestrator backlog board (~/orchestration/backlog working tree).
// Polled slowly — planning scratch changes a handful of times a day; 30s keeps it
// fresh without churn.
export function useOrchestrationBacklog() {
  return usePolling<OrchestrationBacklogResponse>(
    "orchestration/backlog",
    async () => parseOrThrow(OrchestrationBacklogResponseSchema, await fetchJSON<unknown>("/api/orchestration/backlog"), "orchestration/backlog"),
    30000,
  );
}

export function useOrchestrationBacklogDetail() {
  const [detailById, setDetailById] = useState<Record<string, OrchestrationDetail>>({});
  const [errorById, setErrorById] = useState<Record<string, string>>({});
  const [loadingId, setLoadingId] = useState<string | null>(null);
  const aliveRef = useRef(true);
  useEffect(() => () => { aliveRef.current = false; }, []);
  const fetch = useCallback(async (id: string) => {
    setLoadingId(id);
    try {
      const raw = await fetchJSON<unknown>(`/api/orchestration/backlog/${encodeURIComponent(id)}`);
      const data = parseOrThrow(OrchestrationDetailSchema, raw, "orchestration/backlog/detail");
      if (!aliveRef.current) return;
      setDetailById((prev) => ({ ...prev, [id]: data }));
      setErrorById((prev) => ({ ...prev, [id]: "" }));
    } catch (err) {
      if (aliveRef.current) setErrorById((prev) => ({ ...prev, [id]: err instanceof Error ? err.message : String(err) }));
    } finally {
      if (aliveRef.current) setLoadingId(null);
    }
  }, []);
  return { detailById, errorById, loadingId, fetch };
}

export function useBacklogDetail() {
  const [detailById, setDetailById] = useState<Record<string, BacklogDetail>>({});
  const [errorById, setErrorById] = useState<Record<string, string>>({});
  const [loadingId, setLoadingId] = useState<string | null>(null);
  const aliveRef = useRef(true);
  useEffect(() => () => { aliveRef.current = false; }, []);
  const fetch = useCallback(async (id: string) => {
    setLoadingId(id);
    try {
      const raw = await fetchJSON<unknown>(`/api/family-organizer/backlog/${encodeURIComponent(id)}`);
      const data = parseOrThrow(BacklogDetailSchema, raw, "family-organizer/backlog/detail");
      if (!aliveRef.current) return;
      setDetailById((prev) => ({ ...prev, [id]: data }));
      setErrorById((prev) => ({ ...prev, [id]: "" }));
    } catch (err) {
      if (aliveRef.current) setErrorById((prev) => ({ ...prev, [id]: err instanceof Error ? err.message : String(err) }));
    } finally {
      if (aliveRef.current) setLoadingId(null);
    }
  }, []);
  return { detailById, errorById, loadingId, fetch };
}

// Read-only cron observability. Polled slowly (30s) — cron metadata changes
// only when a job fires; the document.hidden gate in usePolling pauses it when
// the tab is backgrounded. Control actions (trigger/pause/resume) reuse the
// existing POST endpoints and reload the bundle on success.
type CronControlJob = { id: string; profile: string };

export const cronObservabilityLoader = async () =>
  parseOrThrow(CronObservabilityResponseSchema, await fetchJSON<unknown>("/api/cron/observability"), "cron/observability");

export function useCronObservability() {
  const [busyJob, setBusyJob] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const state = usePolling<CronObservabilityResponse>("cron/observability", cronObservabilityLoader, 30000);

  const runControl = useCallback(async (action: "trigger" | "pause" | "resume", job: CronControlJob) => {
    setBusyJob(job.id);
    setActionError(null);
    try {
      await fetchJSON<unknown>(
        `/api/cron/jobs/${encodeURIComponent(job.id)}/${action}?profile=${encodeURIComponent(job.profile || "default")}`,
        { method: "POST" },
      );
      await state.reload();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyJob(null);
    }
  }, [state]);

  const trigger = useCallback((job: CronControlJob) => runControl("trigger", job), [runControl]);
  const pause = useCallback((job: CronControlJob) => runControl("pause", job), [runControl]);
  const resume = useCallback((job: CronControlJob) => runControl("resume", job), [runControl]);

  return { ...state, busyJob, actionError, trigger, pause, resume };
}

export function useCronOutput() {
  const [outputById, setOutputById] = useState<Record<string, CronOutput>>({});
  const [errorById, setErrorById] = useState<Record<string, string>>({});
  const [loadingId, setLoadingId] = useState<string | null>(null);
  const aliveRef = useRef(true);
  useEffect(() => () => { aliveRef.current = false; }, []);
  const load = useCallback(async (job: CronControlJob) => {
    setLoadingId(job.id);
    try {
      const raw = await fetchJSON<unknown>(
        `/api/cron/observability/output/${encodeURIComponent(job.id)}?profile=${encodeURIComponent(job.profile || "default")}`,
      );
      const data = parseOrThrow(CronOutputSchema, raw, "cron/output");
      if (!aliveRef.current) return;
      setOutputById((prev) => ({ ...prev, [job.id]: data }));
      setErrorById((prev) => ({ ...prev, [job.id]: "" }));
    } catch (err) {
      // Surface 404 / contract errors instead of silently clearing the spinner.
      if (aliveRef.current) setErrorById((prev) => ({ ...prev, [job.id]: err instanceof Error ? err.message : String(err) }));
    } finally {
      if (aliveRef.current) setLoadingId(null);
    }
  }, []);
  return { outputById, errorById, loadingId, load };
}

export function useRunInspect() {
  const [inspectByRun, setInspectByRun] = useState<Record<string, RunInspect>>({});
  const [errorByRun, setErrorByRun] = useState<Record<string, string>>({});
  const [loadingRun, setLoadingRun] = useState<string | null>(null);
  const aliveRef = useRef(true);
  useEffect(() => () => { aliveRef.current = false; }, []);
  const inspect = useCallback(async (runId: string) => {
    setLoadingRun(runId);
    try {
      const raw = await fetchJSON<unknown>(`/api/plugins/kanban/runs/${encodeURIComponent(runId)}/inspect`);
      const data = parseOrThrow(RunInspectSchema, raw, "run/inspect");
      if (!aliveRef.current) return;
      setInspectByRun((prev) => ({ ...prev, [runId]: data }));
      setErrorByRun((prev) => ({ ...prev, [runId]: "" }));
    } catch (err) {
      // 404 / contract error: surface it instead of leaving the button silently
      // doing nothing (the spinner just clears in finally).
      if (aliveRef.current) setErrorByRun((prev) => ({ ...prev, [runId]: err instanceof Error ? err.message : String(err) }));
    } finally {
      if (aliveRef.current) setLoadingRun(null);
    }
  }, []);
  return { inspectByRun, errorByRun, loadingRun, inspect };
}

// ── Decision Inbox — the single source for "Was braucht mich?" ─────────────
// Before f5 this exact pipeline (snapshot → buildDecisionInbox → inboxSummary)
// was rebuilt independently in OverviewView, InboxView AND the tab badge, so the
// count could drift between surfaces. One hook now owns it; every consumer reads
// the SAME list and the SAME total. Polls are deduped by pollingStore, so mounting
// this in several places costs no extra requests.
export interface DecisionInboxData {
  items: InboxItem[];
  summary: InboxSummary;
  snapshot: AgentOpsSnapshot;
  /** Worst tone present (drives the hero mood + count colour). */
  worstTone: ToneName;
  loading: boolean;
  /** Per-source load errors, labelled, ready to show as a callout. */
  sourceErrors: string[];
}

const INBOX_TONE_RANK: Record<ToneName, number> = {
  red: 5, rose: 5, amber: 4, cyan: 2, sky: 2, indigo: 2, violet: 2, emerald: 1, zinc: 0,
};

// N-E1/E2: the consolidated kanban decision queue (sticky_blocked, review_rejected,
// role_fit_held, budget_held, decompose_failed, stranded_by_stuck_parent).
// 404/error → the Inbox simply renders without a Kanban section (no crash).
export function useKanbanDecisionQueue() {
  return usePolling<DecisionQueueResponse>(
    "kanban/decision-queue",
    async () => parseOrThrow(
      DecisionQueueResponseSchema,
      await fetchJSON<unknown>("/api/plugins/kanban/decision-queue"),
      "kanban/decision-queue",
    ),
    15000,
  );
}

export function useDecisionInbox(): DecisionInboxData {
  const proposals = useProposals();
  const backlog = useBacklog();
  const workers = useHermesWorkers();
  const results = useHermesRecentResults();
  const health = useSystemHealth();
  const metrics = useMetricsLite();
  const orchestration = useOrchestrationBacklog();
  const kanbanDecisions = useKanbanDecisionQueue();
  const now = nowSec();

  const snapshot = useMemo(
    () =>
      buildAgentOpsSnapshot({
        workers: workers.data?.workers ?? [],
        results: results.data?.results ?? [],
        proposals: proposals.proposals,
        orchestrationItems: orchestration.data?.items ?? [],
        contractHealth: orchestration.data?.contract_health,
        systemHealth: health.data,
        metrics: metrics.data,
        nowSec: orchestration.data?.checked_at ?? now,
      }),
    // `now` is intentionally NOT a dependency: it is only a render-time fallback
    // for a missing payload `checked_at`. Including it forced this 8-source
    // aggregation to recompute on EVERY render — and ControlPage re-renders on
    // the 5s workers/health/metrics poll cadence even when nothing changed.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [workers.data, results.data, proposals.proposals, orchestration.data, health.data, metrics.data],
  );

  const items = useMemo(
    () =>
      buildDecisionInbox({
        proposals: proposals.proposals,
        foItems: backlog.data?.items ?? [],
        foNowSec: backlog.data?.checked_at ?? now,
        interventions: snapshot.interventions,
        kanbanDecisions: kanbanDecisions.data?.decisions ?? [],
      }),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `now` is a render-time fallback only (see snapshot memo above)
    [proposals.proposals, backlog.data, snapshot.interventions, kanbanDecisions.data],
  );

  const summary = useMemo(() => inboxSummary(items), [items]);
  const worstTone = useMemo(
    () => items.reduce<ToneName>((worst, it) => (INBOX_TONE_RANK[it.tone] > INBOX_TONE_RANK[worst] ? it.tone : worst), "emerald"),
    [items],
  );

  const sourceErrors = [
    proposals.error ? `Autoresearch: ${proposals.error}` : "",
    backlog.error ? `Family: ${backlog.error}` : "",
    orchestration.error ? `Orchestrator: ${orchestration.error}` : "",
  ].filter(Boolean);

  const loading = proposals.loading || backlog.loading || orchestration.loading;

  return { items, summary, snapshot, worstTone, loading, sourceErrors };
}

// ---------------------------------------------------------------------------
// Bibliothek-Badge (2026-06-11): Zahl neuer Lesesaal-Einträge seit dem letzten
// Besuch. Der Besuchs-Zeitstempel ist der localStorage-Schlüssel der
// BibliothekView (sie stempelt beim Mount); hier nur lesen. Leichtgewichtiger
// Listen-Poll ohne Bodies über den geteilten pollingStore.
// ---------------------------------------------------------------------------

const LIBRARY_LAST_VISIT_KEY = "hc-bibliothek-last-visit";

interface LibraryItemsLite {
  items?: { ts?: number; category?: string }[];
}

// Pure Zähllogik (testbar): "ungelesen" = neuer als der letzte Besuch UND
// kein wartung-Routine-Rauschen — das Badge soll "Neues, das dich
// interessiert" bedeuten.
export function countLibraryUnread(
  items: { ts?: number; category?: string }[],
  since: number,
): number {
  if (!since) return 0;
  return items.filter(
    (i) => (i.ts ?? 0) > since && i.category !== "wartung",
  ).length;
}

export function useLibraryUnread(): number {
  const state = usePolling<LibraryItemsLite>(
    "library/items-badge",
    () => fetchJSON<LibraryItemsLite>("/api/library/items?limit=60"),
    120000,
  );
  let since = 0;
  try {
    since = Number(window.localStorage.getItem(LIBRARY_LAST_VISIT_KEY) ?? 0) || 0;
  } catch {
    /* private mode */
  }
  // Erstbesuch (kein Stempel): nichts anbrüllen — der Tab ist Einladung genug.
  return countLibraryUnread(state.data?.items ?? [], since);
}

export interface PromptForgeCatalogState {
  data: PromptForgeCatalog | null;
  error: string | null;
  loading: boolean;
  lastUpdated: number | null;
}

/** One-shot load of the static Prompt-Schmiede catalog. No polling — the catalog
 *  is static content served read-only from GET /api/promptforge/catalog. */
export function usePromptForgeCatalog(): PromptForgeCatalogState {
  const [data, setData] = useState<PromptForgeCatalog | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [lastUpdated, setLastUpdated] = useState<number | null>(null);
  const aliveRef = useRef(true);

  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
    };
  }, []);

  useEffect(() => {
    void (async () => {
      try {
        const payload = await fetchJSON<PromptForgeCatalog>("/api/promptforge/catalog");
        if (!aliveRef.current) return;
        setData(payload);
        setError(null);
        setLastUpdated(Math.floor(Date.now() / 1000));
      } catch (err) {
        if (!aliveRef.current) return;
        setError(err instanceof Error ? err.message : "Katalog konnte nicht geladen werden");
      } finally {
        if (aliveRef.current) setLoading(false);
      }
    })();
  }, []);

  return { data, error, loading, lastUpdated };
}
