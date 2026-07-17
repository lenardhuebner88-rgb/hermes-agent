import { useCallback } from "react";
import { fetchJSON } from "@/lib/api";
import { TaskBodySchema, TaskDeliverablesResponseSchema, parseOrThrow } from "../lib/schemas";
import type { TaskBodyResponse, TaskDeliverablesResponse } from "../lib/schemas";
import { usePolling } from "./internal";

const EMPTY_TASK_BODY_RESPONSE: TaskBodyResponse = {
  task: null,
  runs: [],
  deliverables: [],
  links: { parents: [], children: [], parent_states: [], child_states: [] },
};


export function useTaskBodyOnDemand(taskId: string | null) {
  const key = taskId ? `task-body-on-demand/${taskId}` : "task-body-on-demand/__none__";
  const loader = useCallback(async (): Promise<TaskBodyResponse> => {
    if (!taskId) return EMPTY_TASK_BODY_RESPONSE;
    const raw = await fetchJSON<unknown>(`/api/plugins/kanban/tasks/${encodeURIComponent(taskId)}`);
    return parseOrThrow(TaskBodySchema, raw, `task-body/${taskId}`);
  }, [taskId]);
  const result = usePolling<TaskBodyResponse>(key, loader, taskId ? 8000 : 600_000);
  if (!taskId) return { ...result, data: EMPTY_TASK_BODY_RESPONSE };
  return result;
}


// useTaskDeliverablesOnDemand pollt alle 15s NUR wenn taskId != null (= Drawer offen).
// Endpoint: GET /api/plugins/kanban/tasks/{id}/deliverables — wird in NodeDetailDrawer
// für den Ergebnis-Tab genutzt. Degradiert sauber zu [] wenn Endpoint 404/empty.
export function useTaskDeliverablesOnDemand(taskId: string | null) {
  const key = taskId ? `task-deliverables-on-demand/${taskId}` : "task-deliverables-on-demand/__none__";
  const loader = useCallback(async (): Promise<TaskDeliverablesResponse> => {
    if (!taskId) return { task_id: "", deliverables: [] };
    const raw = await fetchJSON<unknown>(`/api/plugins/kanban/tasks/${encodeURIComponent(taskId)}/deliverables`);
    return parseOrThrow(TaskDeliverablesResponseSchema, raw, `task-deliverables/${taskId}`);
  }, [taskId]);
  const result = usePolling<TaskDeliverablesResponse>(key, loader, taskId ? 15_000 : 600_000);
  if (!taskId) return { ...result, data: { task_id: "", deliverables: [] } as TaskDeliverablesResponse };
  return result;
}
