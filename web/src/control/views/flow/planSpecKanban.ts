import type { PlanSpecRecord } from "../../lib/types";
import type { ToneName } from "../../lib/types";

export function planSpecKanbanTone(state: PlanSpecRecord["kanban_state"]): ToneName {
  if (state === "completed" || state === "done") return "emerald";
  if (state === "blocked") return "red";
  if (state === "running") return "amber";
  if (state === "queued") return "violet";
  return "zinc";
}

export function planSpecKanbanLabel(item: PlanSpecRecord): string {
  if (item.kanban_state === "completed" || item.kanban_state === "done") return "erledigt";
  if (item.kanban_state === "blocked") return "blocked";
  if (item.kanban_state === "running") return "läuft";
  if (item.kanban_state === "queued") return "geplant";
  return item.valid ? "offen" : "blocked";
}
