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

/**
 * Ruhige deutsche Label für geschlossene Pläne ohne Board-Endzustand —
 * die Dispositionen aus planSpecClosedDispositionLabel, übersetzt. Ein
 * geschlossener Plan ist ein Endzustand, nie „offen" und nie „blocked".
 */
const CLOSED_DISPOSITION_LABELS: Record<string, string> = {
  shipped: "ausgeliefert",
  "obsolete/not-needed": "obsolet",
  "kanban-archived": "archiviert",
  "kanban-completed": "erledigt",
};

export function planSpecKanbanLabel(item: PlanSpecRecord): string {
  if (item.kanban_state === "archived") return "archiviert";
  if (item.kanban_state === "completed" || item.kanban_state === "done") return "erledigt";
  if (item.kanban_state === "blocked") return "blocked";
  if (item.kanban_state === "running") return "läuft";
  if (item.kanban_state === "queued") return "geplant";
  if (planSpecIsClosed(item)) {
    return CLOSED_DISPOSITION_LABELS[planSpecClosedDispositionLabel(item)] ?? "geschlossen";
  }
  return item.valid ? "offen" : "blocked";
}

function normalizedClosedReason(item: PlanSpecRecord): string {
  return (item.closed_reason ?? item.status ?? "").toLowerCase();
}

/**
 * Status-Echo der Abschluss-Projektion ("closed status: done") — kein echter
 * Prüfbefund. Live-Befund 2026-08-04: praktisch jede abgeschlossene PlanSpec
 * trägt genau diesen Eintrag in ingest_findings/source_findings und zeigte
 * dadurch „1 Befunde", sodass echte Befunde im Rauschen untergingen.
 */
export function planSpecFindingIsStatusEcho(finding: string): boolean {
  return /^closed status:/i.test(finding.trim());
}

/** Sichtbare Befund-Anzahl: API-Zähler minus erkennbare Status-Echos. */
export function planSpecVisibleFindingCount(item: PlanSpecRecord): number {
  const total = item.finding_count ?? 0;
  if (total <= 0) return 0;
  const echoes = [...item.errors, ...item.ingest_findings].filter(planSpecFindingIsStatusEcho).length;
  return Math.max(0, total - echoes);
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
  // Abschluss-Echo der Hub-Projektion: „closed status: done|completed|archived"
  // ohne Board-Endzustand (kanban_state „not_ingested"). Fiel sonst auf den
  // generischen „geschlossen"-Fallback — direkt neben dem „Fertig" der Liste.
  const statusEcho = reason.match(/^closed status:\s*([a-z_-]+)/);
  if (statusEcho) {
    if (statusEcho[1] === "done" || statusEcho[1] === "completed") return "kanban-completed";
    if (statusEcho[1] === "archived") return "kanban-archived";
  }
  return item.closed_reason ?? item.status ?? "closed";
}
