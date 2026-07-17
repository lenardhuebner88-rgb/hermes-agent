import { useCallback, useEffect, useRef, useState } from "react";
import { fetchJSON } from "@/lib/api";
import {
  FlowReleaseResponseSchema,
  FlowGateResponseSchema,
  FlowSizingResponseSchema,
  FlowTimeoutSweepResponseSchema,
  ChainGraphResponseSchema,
  TaskDetailResponseSchema,
  parseOrThrow,
} from "../lib/schemas";
import type { TaskDetailResponse, FlowGateResponse } from "../lib/schemas";
import { nowSec } from "../lib/derive";
import { withBoardParam } from "../lib/multiBoard";
import type { ChainGraphResponse, FlowReleaseOptions, FlowReleaseResponse, FlowSizingResponse, FlowTimeoutSweepResponse } from "../lib/types";
import { extractDetail } from "./internal";

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


export function useChainGraph(rootId: string | null, board: string | null = null) {
  const [data, setData] = useState<ChainGraphResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [lastUpdated, setLastUpdated] = useState<number | null>(null);
  const aliveRef = useRef(true);
  const inFlightRef = useRef(false);
  const paramsRef = useRef({ rootId, board });
  useEffect(() => () => { aliveRef.current = false; }, []);
  useEffect(() => { paramsRef.current = { rootId, board }; }, [rootId, board]);
  const reload = useCallback(async (): Promise<ChainGraphResponse | null> => {
    if (!rootId) {
      if (aliveRef.current) setData(null);
      return null;
    }
    const startParams = { rootId, board };
    inFlightRef.current = true;
    if (aliveRef.current) setLoading(true);
    try {
      const parsed = parseOrThrow(
        ChainGraphResponseSchema,
        await fetchJSON<unknown>(withBoardParam(`/api/plugins/kanban/tasks/${encodeURIComponent(rootId)}/chain-graph`, board)),
        "chain-graph",
      );
      // Board-Switch-Race: Ergebnis verwerfen, wenn sich die Params geändert haben.
      if (aliveRef.current && paramsRef.current.rootId === startParams.rootId && paramsRef.current.board === startParams.board) {
        setData(parsed);
        setError("");
        setLastUpdated(nowSec());
      }
      return parsed;
    } catch (e) {
      const detail = extractDetail(e);
      if (aliveRef.current && paramsRef.current.rootId === startParams.rootId && paramsRef.current.board === startParams.board) setError(detail);
      return null;
    } finally {
      inFlightRef.current = false;
      if (aliveRef.current && paramsRef.current.rootId === startParams.rootId && paramsRef.current.board === startParams.board) setLoading(false);
    }
  }, [rootId, board]);
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
  return { data, loading, error, lastUpdated, isStale: Boolean(error && data), reload };
}


export interface ChainCancelResult {
  ok: boolean;
  detail?: string;
  root_id?: string;
  held: string[];
  terminated: string[];
  skipped: string[];
}


export interface ChainTaskCreatePayload {
  title: string;
  body?: string;
  assignee?: string;
  parents: string[];
  park?: boolean;
}


export interface ChainTaskCreateResult {
  ok: boolean;
  detail?: string;
  taskId?: string;
  taskStatus?: string;
}


export function useChainActions() {
  const [busy, setBusy] = useState<"cancel" | "add" | null>(null);
  const [error, setError] = useState("");
  const aliveRef = useRef(true);
  useEffect(() => () => { aliveRef.current = false; }, []);

  const cancelChain = useCallback(async (rootId: string): Promise<ChainCancelResult> => {
    if (aliveRef.current) {
      setBusy("cancel");
      setError("");
    }
    try {
      const res = await fetchJSON<ChainCancelResult>(
        `/api/plugins/kanban/tasks/${encodeURIComponent(rootId)}/cancel-chain`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ confirm: true }),
        },
      );
      return {
        ok: Boolean(res.ok),
        detail: res.detail,
        root_id: res.root_id,
        held: Array.isArray(res.held) ? res.held : [],
        terminated: Array.isArray(res.terminated) ? res.terminated : [],
        skipped: Array.isArray(res.skipped) ? res.skipped : [],
      };
    } catch (e) {
      const detail = extractDetail(e);
      if (aliveRef.current) setError(detail);
      return { ok: false, detail, held: [], terminated: [], skipped: [] };
    } finally {
      if (aliveRef.current) setBusy(null);
    }
  }, []);

  const addTask = useCallback(async (payload: ChainTaskCreatePayload): Promise<ChainTaskCreateResult> => {
    if (aliveRef.current) {
      setBusy("add");
      setError("");
    }
    try {
      const res = await fetchJSON<{ task?: { id?: string; status?: string } }>(
        "/api/plugins/kanban/tasks",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        },
      );
      return { ok: true, taskId: res.task?.id, taskStatus: res.task?.status };
    } catch (e) {
      const detail = extractDetail(e);
      if (aliveRef.current) setError(detail);
      return { ok: false, detail };
    } finally {
      if (aliveRef.current) setBusy(null);
    }
  }, []);

  return { busy, error, cancelChain, addTask };
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

