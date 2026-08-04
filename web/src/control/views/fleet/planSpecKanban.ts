import type { PlanSpecRecord } from "../../lib/types";
import type { SignalTone } from "../../components/leitstand";

export type PlanActionState = NonNullable<PlanSpecRecord["action_state"]> | "unknown";

/**
 * Der Anzeigezustand eines Plans — EINE Quelle für Listenzeile und Detail,
 * damit beide denselben Plan nie unterschiedlich einordnen (Befund D1,
 * 2026-08-04: Liste „Fertig" aus action_state, Detail „geschlossen" aus
 * Kanban-Feldern, auf demselben Schirm).
 *
 * `action_state` ist die Wahrheit — das Backend klassifiziert bereits
 * (classify_plan_record). Das Frontend leitet NICHTS mehr aus Kanban-Feldern
 * ab: der frühere Fallback kannte die Backend-Regeln „Live-Kette schlägt
 * geschlossene Quelle schlägt Wurzel-Stempel" nicht und malte geschlossene
 * Specs wieder als „Blockiert"/„Im Board". Fehlt `action_state`, ist das ein
 * Datenfehler und wird als „unknown" sichtbar — nicht heimlich geraten.
 */
export function planSpecActionState(item: PlanSpecRecord): PlanActionState {
  return item.action_state ?? "unknown";
}

/** Ton je Anzeigezustand — Liste und Detail-Chip teilen sich diese Tabelle. */
export const PLAN_ACTION_STATE_TONE: Record<PlanActionState, SignalTone> = {
  draft: "neutral",
  ready: "ok",
  held: "warn",
  handed_off: "neutral",
  running: "neutral",
  blocked: "alert",
  completed: "ok",
  archived: "neutral",
  unknown: "warn",
};

/**
 * Ausführungsachse der zugehörigen Kette — getrennt vom Plan-Zustand gezeigt
 * (Runde-1-Befund: Dokumentzustand und Lauf gehören nicht in ein Feld).
 * null = keine Kette übergeben bzw. kein lesbarer Laufzustand.
 */
export function planSpecChainStateLabel(item: PlanSpecRecord): string | null {
  if (!item.kanban_root_task_id) return null;
  if (item.kanban_state === "running") return "läuft";
  if (item.kanban_state === "blocked") return "blockiert";
  if (item.kanban_state === "queued") return item.kanban_root_status === "scheduled" ? "gehalten" : "geplant";
  if (item.kanban_state === "completed" || item.kanban_state === "done") return "fertig";
  if (item.kanban_state === "archived") return "archiviert";
  return null;
}

/**
 * Grundtexte, die nur den Endzustand wiederholen („Die PlanSpec ist
 * abgeschlossen."), tragen keine Information neben dem Chip — sie werden in
 * der Zeile unterdrückt, damit echte Gründe auffallen (Live: 399 Zeilen mit
 * derselben Floskel).
 */
export function planSpecActionReasonRestatesState(state: PlanActionState, reason: string | null | undefined): boolean {
  if (!reason) return false;
  if (state !== "completed" && state !== "archived") return false;
  return /abgeschlossen|geschlossen/i.test(reason);
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

/**
 * Chip-Label im Detail: das präzise Dispositionswort, solange das Backend
 * klassifiziert hat. Ohne `action_state` wird nichts abgeleitet — der
 * Datenfehler heißt dann wie in der Liste „ohne Zustand".
 */
export function planSpecStateChipLabel(item: PlanSpecRecord): string {
  return item.action_state ? planSpecKanbanLabel(item) : "ohne Zustand";
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
