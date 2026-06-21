import type { PlanSpecRecord } from "../../lib/types";
import type { ToneName } from "../../lib/types";

export function planSpecKanbanTone(state: PlanSpecRecord["kanban_state"]): ToneName {
  if (state === "archived") return "zinc";
  if (state === "completed" || state === "done") return "emerald";
  if (state === "blocked") return "red";
  if (state === "running") return "amber";
  if (state === "queued") return "violet";
  return "zinc";
}

export function planSpecKanbanLabel(item: PlanSpecRecord): string {
  if (item.kanban_state === "archived") return "archiviert";
  if (item.kanban_state === "completed" || item.kanban_state === "done") return "erledigt";
  if (item.kanban_state === "blocked") return "blocked";
  if (item.kanban_state === "running") return "läuft";
  if (item.kanban_state === "queued") return "geplant";
  return item.valid ? "offen" : "blocked";
}

function normalizedClosedReason(item: PlanSpecRecord): string {
  return (item.closed_reason ?? item.status ?? "").toLowerCase();
}

export function planSpecIsClosed(item: PlanSpecRecord): boolean {
  return !item.open || Boolean(item.closed_reason) || item.kanban_state === "completed" || item.kanban_state === "done" || item.kanban_state === "archived";
}

export function planSpecClosedDispositionLabel(item: PlanSpecRecord): string {
  if (!planSpecIsClosed(item)) return "open";
  const reason = normalizedClosedReason(item);
  const rootStatus = (item.kanban_root_status ?? "").toLowerCase();
  if (reason.includes("not needed") || reason.includes("not-needed") || reason.includes("obsolete")) return "obsolete/not-needed";
  if (reason.includes("shipped")) return "shipped";
  if (item.kanban_state === "archived" || rootStatus === "archived" || reason.includes("archived")) return "kanban-archived";
  if (item.kanban_state === "completed" || item.kanban_state === "done" || rootStatus === "completed" || rootStatus === "done") return "kanban-completed";
  return item.closed_reason ?? item.status ?? "closed";
}
