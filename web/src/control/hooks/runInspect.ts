import { useCallback, useEffect, useRef, useState } from "react";
import { fetchJSON } from "@/lib/api";
import { RunInspectSchema, parseOrThrow } from "../lib/schemas";
import type { RunInspect } from "../lib/types";

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
