import type { TaskStatus } from "./types";

const FLOW_SUBTASK_STATUS_EXPLANATIONS: Partial<Record<TaskStatus, string>> = {
  scheduled: "wartet auf Kette starten",
  ready: "startklar im Snapshot; Start hängt von Queue/Assignee und Worker-Kapazität ab",
  todo: "wartet; Ursache im Snapshot nicht eindeutig",
  running: "Worker läuft",
  done: "abgeschlossen",
  review: "wartet auf Verifier-Abnahme",
  triage: "wartet auf Planung",
  archived: "archiviert",
};

export function getFlowSubtaskStatusExplanation(status: TaskStatus, blockedReason?: string | null): string {
  if (status === "blocked") {
    const reason = blockedReason?.trim();
    return reason ? `Blockiert: ${reason}` : "blockiert; wartet auf Klärung";
  }
  return FLOW_SUBTASK_STATUS_EXPLANATIONS[status] ?? status;
}
