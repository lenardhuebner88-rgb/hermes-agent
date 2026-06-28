import type { TaskDetailResponse } from "./schemas";
import type { BoardTask } from "./types";

export interface HeldFlowDispatchGuard {
  rootId: string;
  heldSiblingIds: string[];
}

export interface HeldFlowRootGuard {
  rootId: string;
  heldChildIds: string[];
}

function payloadString(payload: Record<string, unknown> | null | undefined, key: string): string | null {
  const value = payload?.[key];
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

export function getFlowRootIdFromDetail(detail: TaskDetailResponse | null | undefined): string | null {
  const events = detail?.events ?? [];
  for (const event of events) {
    const rootId = payloadString(event.payload, "from_decompose_of");
    if (rootId) return rootId;
  }
  return null;
}

export function getFlowChildIdsFromRootDetail(detail: TaskDetailResponse | null | undefined): string[] {
  const decomposed = [...(detail?.events ?? [])].reverse().find((event) => event.kind === "decomposed");
  const rawIds = decomposed?.payload?.child_ids;
  return Array.isArray(rawIds) ? rawIds.filter((id): id is string => typeof id === "string" && !!id.trim()) : [];
}

export function getHeldFlowRootGuard(
  task: BoardTask,
  taskDetail: TaskDetailResponse | null | undefined,
  boardTasks: BoardTask[],
): HeldFlowRootGuard | null {
  if (task.status !== "scheduled") return null;
  const childIds = getFlowChildIdsFromRootDetail(taskDetail);
  if (!childIds.length) return null;

  const byId = new Map(boardTasks.map((boardTask) => [boardTask.id, boardTask]));
  const heldChildIds = childIds.filter((id) => byId.get(id)?.status === "scheduled");
  if (!heldChildIds.length) return null;
  return { rootId: task.id, heldChildIds };
}

export function getHeldFlowDispatchGuard(
  task: BoardTask,
  taskDetail: TaskDetailResponse | null | undefined,
  rootDetail: TaskDetailResponse | null | undefined,
  boardTasks: BoardTask[],
): HeldFlowDispatchGuard | null {
  if (task.status !== "scheduled") return null;
  const rootId = getFlowRootIdFromDetail(taskDetail);
  if (!rootId || rootId === task.id) return null;

  const childIds = getFlowChildIdsFromRootDetail(rootDetail);
  if (!childIds.includes(task.id)) return null;

  const byId = new Map(boardTasks.map((boardTask) => [boardTask.id, boardTask]));
  const heldSiblingIds = childIds.filter((id) => id !== task.id && byId.get(id)?.status === "scheduled");
  if (!heldSiblingIds.length) return null;
  return { rootId, heldSiblingIds };
}
