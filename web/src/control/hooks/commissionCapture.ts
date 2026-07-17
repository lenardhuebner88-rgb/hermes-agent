import { useCallback, useEffect, useRef, useState } from "react";
import { fetchJSON } from "@/lib/api";
import { captureRequest, flowCaptureRequest, usesFlowCaptureEndpoint, type CaptureMethod, type CaptureLevers } from "../lib/fleet";
import { extractDetail } from "./internal";

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


async function postFlowCapture(title: string, method: CaptureMethod, gate: boolean, levers?: CaptureLevers) {
  return fetchJSON<{ ok?: boolean; task_id?: string; reason?: string }>("/api/plugins/kanban/tasks/flow-capture", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(flowCaptureRequest(title, method, gate, levers)),
  });
}


async function postPlainCapture(title: string, method: CaptureMethod, levers?: CaptureLevers) {
  return fetchJSON<{ task?: { id?: string; status?: string } }>("/api/plugins/kanban/tasks", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(captureRequest(title, method, levers)),
  });
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
  const capture = useCallback(async (title: string, method: CaptureMethod, gate: boolean, levers?: CaptureLevers): Promise<CaptureResult> => {
    if (!title.trim()) {
      setState("error");
      setError("Titel fehlt");
      return { ok: false, detail: "Titel fehlt" };
    }
    setState("busy");
    setError("");
    try {
      if (usesFlowCaptureEndpoint(method, gate)) {
        const res = await postFlowCapture(title, method, gate, levers);
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
      const res = await postPlainCapture(title, method, levers);
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

