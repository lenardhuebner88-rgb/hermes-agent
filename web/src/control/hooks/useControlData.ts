import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { z } from "zod";
import { fetchJSON } from "@/lib/api";
import {
  AgentsResponseSchema,
  BacklogResponseSchema,
  OrchestrationBacklogResponseSchema,
  AutoresearchRunsResponseSchema,
  AutoresearchStatusSchema,
  OpenClawDispatchedResponseSchema,
  ProposalsResponseSchema,
  RecentResultsResponseSchema,
  RunInspectSchema,
  SystemHealthResponseSchema,
  WorkersResponseSchema,
  parseOrThrow,
} from "../lib/schemas";
import type { BacklogResponse, OrchestrationBacklogResponse, OpenClawDispatchedResponse } from "../lib/schemas";
import { isActionable } from "../lib/autoresearch";
import type { AgentsResponse, AutoresearchRunsResponse, AutoresearchStatus, Proposal, ProposalsResponse, RecentResultsResponse, RunInspect, SystemHealthResponse, WorkersResponse } from "../lib/types";

export type OpenClawDispatchBody = {
  title: string;
  description?: string;
  agent: string;
  deliver_to?: string;
  operator_lock_acknowledged?: boolean;
  board?: string;
};

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

const OpenClawCronErrorSchema = z.object({
  id: z.coerce.string(),
  name: z.string().catch("cron"),
  lastError: z.string().catch(""),
  consecutiveErrors: z.coerce.number().catch(0),
  lastRunAt: z.coerce.number().catch(0),
});

const OpenClawCronErrorsResponseSchema = z.object({
  errors: z.array(OpenClawCronErrorSchema).catch([]),
  stale: z.string().optional(),
});

export type OpenClawCronError = z.infer<typeof OpenClawCronErrorSchema>;
export type OpenClawCronErrorsResponse = z.infer<typeof OpenClawCronErrorsResponseSchema>;

type LoadState<T> = {
  data: T | null;
  error: string | null;
  loading: boolean;
  /** Epoch seconds of the last SUCCESSFUL load (E1 freshness). null until first ok. */
  lastUpdated: number | null;
  reload: () => Promise<void>;
  updateData: React.Dispatch<React.SetStateAction<T | null>>;
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

function usePolling<T>(loader: () => Promise<T>, intervalMs: number): LoadState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [lastUpdated, setLastUpdated] = useState<number | null>(null);
  const loaderRef = useRef(loader);

  useEffect(() => {
    loaderRef.current = loader;
  }, [loader]);

  const reload = useCallback(async () => {
    try {
      const next = await loaderRef.current();
      setData(next);
      setError(null);
      setLastUpdated(Math.floor(Date.now() / 1000));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    let alive = true;
    const run = async () => {
      if (!alive || document.hidden) return;
      await reload();
    };
    void run();
    const timer = window.setInterval(run, intervalMs);
    const onVisible = () => { if (!document.hidden) void run(); };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      alive = false;
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [intervalMs, reload]);

  return { data, error, loading, lastUpdated, reload, updateData: setData };
}

export function useAutoresearchStatus() {
  return usePolling<AutoresearchStatus>(
    async () => parseOrThrow(AutoresearchStatusSchema, await fetchJSON<unknown>("/autoresearch/status"), "autoresearch/status"),
    5000,
  );
}

export function useAutoresearchRuns() {
  return usePolling<AutoresearchRunsResponse>(
    async () => parseOrThrow(AutoresearchRunsResponseSchema, await fetchJSON<unknown>("/autoresearch/runs"), "autoresearch/runs"),
    10000,
  );
}

export function useProposals() {
  const [activity, setActivity] = useState<Array<{ at: number; text: string; tone: "emerald" | "amber" | "violet" | "red" }>>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [batchConfirmById, setBatchConfirmById] = useState<BatchConfirmById>({});
  const state = usePolling<ProposalsResponse>(
    async () => parseOrThrow(ProposalsResponseSchema, await fetchJSON<unknown>("/autoresearch/proposals"), "autoresearch/proposals"),
    6000,
  );

  const proposals = useMemo(() => state.data?.proposals ?? [], [state.data]);
  const openSkillProposals = useMemo(() => proposals.filter((p) => isActionable(p) && p.mode === "skill"), [proposals]);

  const log = useCallback((text: string, tone: "emerald" | "amber" | "violet" | "red" = "violet") => {
    setActivity((items) => [{ at: Math.floor(Date.now() / 1000), text, tone }, ...items].slice(0, 8));
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
      await fetchJSON<unknown>("/autoresearch/generate", { method: "POST" });
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
      const result = await fetchJSON<{ created_count?: number; files_seen?: number; skipped_unchanged?: number; vetoed?: number; tokens?: number }>("/autoresearch/generate-code-weaknesses", {
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
      const result = await fetchJSON<{ ok?: boolean; status?: string; result?: string; detail?: string }>("/autoresearch/apply", {
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
      await fetchJSON<unknown>("/autoresearch/skip", {
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
    const seen = new Set<string>();
    for (const proposal of pending) {
      if (seen.has(proposal.id)) continue;
      seen.add(proposal.id);
      await apply(proposal);
    }
  }, [apply, openSkillProposals]);

  const confirmBatch = useCallback(async (ids: string[]) => {
    const selectedIds = Array.from(new Set(ids)).filter(Boolean);
    if (selectedIds.length === 0) return;
    setBusy("confirm-batch");
    setBatchConfirmById((current) => ({
      ...current,
      ...Object.fromEntries(selectedIds.map((id) => [id, { status: "pending" as const }])),
    }));
    try {
      const result = await fetchJSON<BatchConfirmResponse>("/autoresearch/confirm-batch", {
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

export function useHermesWorkers() {
  return usePolling<WorkersResponse>(
    async () => parseOrThrow(WorkersResponseSchema, await fetchJSON<unknown>("/api/plugins/kanban/workers/active"), "workers/active"),
    5000,
  );
}


export function useHermesRecentResults() {
  return usePolling<RecentResultsResponse>(
    async () => parseOrThrow(
      RecentResultsResponseSchema,
      await fetchJSON<unknown>("/api/plugins/kanban/runs/recent-results?limit=12&since_hours=48&outcome=completed"),
      "runs/recent-results",
    ),
    20000,
  );
}

export function useSystemHealth() {
  return usePolling<SystemHealthResponse>(
    async () => parseOrThrow(SystemHealthResponseSchema, await fetchJSON<unknown>("/api/health-status"), "health-status"),
    5000,
  );
}

// Read-only family-organizer backlog board. Polled slowly — the backlog changes
// rarely (a handful of git commits a day), so 30s keeps it fresh without churn.
export function useBacklog() {
  return usePolling<BacklogResponse>(
    async () => parseOrThrow(BacklogResponseSchema, await fetchJSON<unknown>("/api/family-organizer/backlog"), "family-organizer/backlog"),
    30000,
  );
}

// Read-only Orchestrator backlog board (~/orchestration/backlog working tree).
// Polled slowly — planning scratch changes a handful of times a day; 30s keeps it
// fresh without churn.
export function useOrchestrationBacklog() {
  return usePolling<OrchestrationBacklogResponse>(
    async () => parseOrThrow(OrchestrationBacklogResponseSchema, await fetchJSON<unknown>("/api/orchestration/backlog"), "orchestration/backlog"),
    30000,
  );
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


export function useOpenClawAgents() {
  return usePolling<AgentsResponse>(
    async () => parseOrThrow(AgentsResponseSchema, await fetchJSON<unknown>("/api/openclaw/agents"), "openclaw/agents"),
    5000,
  );
}

export function useOpenClawCronErrors() {
  return usePolling<OpenClawCronErrorsResponse>(
    async () => parseOrThrow(OpenClawCronErrorsResponseSchema, await fetchJSON<unknown>("/api/openclaw/cron-errors"), "openclaw/cron-errors"),
    5000,
  );
}

export function useOpenClawDispatched() {
  return usePolling<OpenClawDispatchedResponse>(
    async () => parseOrThrow(OpenClawDispatchedResponseSchema, await fetchJSON<unknown>("/api/openclaw/dispatched"), "openclaw/dispatched"),
    5000,
  );
}

/** POST a new openclaw:<agent> dispatch. Mirrors the autoresearch POST pattern
 *  (Content-Type JSON body). No secret material on this path — the dispatcher
 *  signs the MC envelope later. Returns the created task id. */
export async function dispatchOpenClawTask(body: OpenClawDispatchBody): Promise<{ ok?: boolean; taskId?: string; detail?: string }> {
  return fetchJSON<{ ok?: boolean; taskId?: string; detail?: string }>("/api/openclaw/dispatch", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}
