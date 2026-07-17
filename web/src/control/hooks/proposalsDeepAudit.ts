import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { fetchJSON } from "@/lib/api";
import { ProposalsResponseSchema, parseOrThrow } from "../lib/schemas";
import { isActionable } from "../lib/autoresearch";
import { proposalNeedsManualReview } from "../lib/autoresearchDecisionGuide";
import type { Proposal, ProposalsResponse } from "../lib/types";
import { usePolling } from "./internal";

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

type BatchSkipItem = { id?: string; ok?: boolean; reason?: string };

type BatchSkipResponse = { ok?: boolean; results?: BatchSkipItem[] };

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


export function testFoundryStatusPollIntervalMs(status: TestFoundryStatus | null): number | null {
  return status?.state === "running" ? 5000 : null;
}


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
    // chrome-badge cadence, 15s staleness accepted (perf plan 2026-07-17)
    15000,
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

  const skipBatch = useCallback(async (ids: string[]) => {
    const selectedIds = Array.from(new Set(ids)).filter(Boolean);
    if (selectedIds.length === 0) return;
    setBusy("skip-batch");
    state.updateData((current) => current ? {
      ...current,
      proposals: current.proposals.map((proposal) => selectedIds.includes(proposal.id) ? { ...proposal, status: "skipped", result: "übersprungen" } : proposal),
    } : current);
    try {
      const result = await fetchJSON<BatchSkipResponse>("/api/autoresearch/skip-batch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ids: selectedIds }),
      });
      const failed = (result.results ?? []).filter((item) => item.ok === false).length;
      log(`${selectedIds.length - failed}/${selectedIds.length} Vorschläge verworfen`, failed > 0 ? "amber" : "emerald");
      await state.reload();
    } catch (e) {
      log(`Gruppe verwerfen fehlgeschlagen: ${e instanceof Error ? e.message : String(e)}`, "red");
      await state.reload();
    } finally {
      setBusy(null);
    }
  }, [log, state]);

  return { ...state, proposals, openSkillProposals, activity, busy, batchConfirmById, generate, generateCodeWeaknesses, apply, skip, skipBatch, applyAll, confirmBatch };
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

