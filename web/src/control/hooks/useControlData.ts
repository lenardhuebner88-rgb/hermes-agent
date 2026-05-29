import { useCallback, useEffect, useMemo, useState } from "react";
import { fetchJSON } from "@/lib/api";
import {
  AgentsResponseSchema,
  AutoresearchStatusSchema,
  ProposalsResponseSchema,
  RunInspectSchema,
  WorkersResponseSchema,
  parseOrThrow,
} from "../lib/schemas";
import type { AgentsResponse, AutoresearchStatus, Proposal, ProposalsResponse, RunInspect, WorkersResponse } from "../lib/types";

type LoadState<T> = {
  data: T | null;
  error: string | null;
  loading: boolean;
  reload: () => Promise<void>;
  updateData: React.Dispatch<React.SetStateAction<T | null>>;
};

function usePolling<T>(loader: () => Promise<T>, intervalMs: number): LoadState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const reload = useCallback(async () => {
    try {
      const next = await loader();
      setData(next);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [loader]);

  useEffect(() => {
    let alive = true;
    const run = async () => {
      if (!alive) return;
      await reload();
    };
    void run();
    const timer = window.setInterval(run, intervalMs);
    return () => { alive = false; window.clearInterval(timer); };
  }, [intervalMs, reload]);

  return { data, error, loading, reload, updateData: setData };
}

export function useAutoresearchStatus() {
  return usePolling<AutoresearchStatus>(
    async () => parseOrThrow(AutoresearchStatusSchema, await fetchJSON<unknown>("/autoresearch/status"), "autoresearch/status"),
    5000,
  );
}

export function useProposals() {
  const [activity, setActivity] = useState<Array<{ at: number; text: string; tone: "emerald" | "amber" | "violet" | "red" }>>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const state = usePolling<ProposalsResponse>(
    async () => parseOrThrow(ProposalsResponseSchema, await fetchJSON<unknown>("/autoresearch/proposals"), "autoresearch/proposals"),
    6000,
  );

  const proposals = useMemo(() => state.data?.proposals ?? [], [state.data]);
  const openSkillProposals = useMemo(() => proposals.filter((p) => p.status === "proposed" && p.mode === "skill"), [proposals]);

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

  const apply = useCallback(async (proposal: Proposal) => {
    if (proposal.mode === "code") return;
    setBusy(proposal.id);
    mutateProposal(proposal.id, { status: "applied", result: "übernommen" });
    try {
      const result = await fetchJSON<{ ok?: boolean; result?: string; gated?: string }>("/autoresearch/apply", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: proposal.id, confirm: true }),
      });
      log(`${proposal.title ?? proposal.target}: ${result.result ?? "übernommen"}`, "emerald");
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
    for (const proposal of openSkillProposals) await apply(proposal);
  }, [apply, openSkillProposals]);

  return { ...state, proposals, openSkillProposals, activity, busy, generate, apply, skip, applyAll };
}

export function useHermesWorkers() {
  return usePolling<WorkersResponse>(
    async () => parseOrThrow(WorkersResponseSchema, await fetchJSON<unknown>("/api/plugins/kanban/workers/active"), "workers/active"),
    5000,
  );
}

export function useRunInspect() {
  const [inspectByRun, setInspectByRun] = useState<Record<string, RunInspect>>({});
  const [loadingRun, setLoadingRun] = useState<string | null>(null);
  const inspect = useCallback(async (runId: string) => {
    setLoadingRun(runId);
    try {
      const raw = await fetchJSON<unknown>(`/api/plugins/kanban/runs/${encodeURIComponent(runId)}/inspect`);
      const data = parseOrThrow(RunInspectSchema, raw, "run/inspect");
      setInspectByRun((prev) => ({ ...prev, [runId]: data }));
    } finally {
      setLoadingRun(null);
    }
  }, []);
  return { inspectByRun, loadingRun, inspect };
}


export function useOpenClawAgents() {
  return usePolling<AgentsResponse>(
    async () => parseOrThrow(AgentsResponseSchema, await fetchJSON<unknown>("/api/openclaw/agents"), "openclaw/agents"),
    5000,
  );
}
