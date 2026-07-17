import { useCallback, useEffect, useRef, useState } from "react";
import { fetchJSON } from "@/lib/api";
import { CronObservabilityResponseSchema, CronOutputSchema, parseOrThrow } from "../lib/schemas";
import type { CronObservabilityResponse, CronOutput } from "../lib/types";
import { usePolling } from "./internal";

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

