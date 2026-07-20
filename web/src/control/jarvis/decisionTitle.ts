/**
 * decisionTitle — S7.6: Entscheidungs-Titel für die Inbox (Decision-Cards).
 * Clientseitige Destillation nach dem Muster von `briefing_title()` aus
 * gateway/pa_watcher.py (S6.3) — dieselben Muster: Task-/Gate-Präfixe,
 * Status-Suffixe, Task-IDs und Beleg-Pfade fliegen raus; übrig bleibt die
 * Entscheidung statt des Wire-Rauschens. Dazu das PlanSpec-Slug-Präfix
 * („PlanSpec GATE-…-FIX:"), das die Inbox-Titel lang macht, das Briefing
 * aber nicht kennt.
 *
 * Der Server liefert (S7.6-Backend) `summary` pro Inbox-Item; fehlt das
 * Feld (älterer Server), ist dies der Fallback — `decisionHeadline` wählt.
 *
 * Abweichungen vom Python-Vorbild (bewusst):
 *  - Cap 80 statt 120 Zeichen (Karte hat weniger Platz als das Briefing).
 *  - Mehrzeilige Titel (z. B. planspec.ingest-Approvals) werden auf die
 *    erste Zeile destilliert — sie ist die Frage, der Rest steht hinter
 *    dem Expand der Karte.
 */
import { elapsedSeconds } from "../lib/derive";
import type { PaInboxItem } from "@/lib/api";

/** Maximale Länge des destillierten Titels (Backend-Briefing: 120). */
export const DECISION_TITLE_MAX = 80;

/** Fallback wie briefing_title() — leerer Titel trägt keine Entscheidung. */
const FALLBACK = "Ereignis";

// Muster 1:1 aus pa_watcher.py (_BRIEFING_TASK_PREFIX, um PlanSpec-Slug
// erweitert — \S+ frisst den SLUG bis zum Doppelpunkt).
const TASK_PREFIX_RE = /^(?:Task|Gate bei Task|PlanSpec)\s+\S+\s*[:：]\s*/i;
// _BRIEFING_KIND_SUFFIX: Status am Zeilenende („… — completed").
const KIND_SUFFIX_RE =
  /\s*[—–-]\s*(?:completed|blocked|gave_up|crashed|timed_out|operator_release_required|review_wait_attention|review_unavailable|worker_gate_blocked|release_gate_parked|rebase_conflict_returned|session_exit|new_receipt|blocked:\S+)\s*$/i;
// _BRIEFING_TASK_ID
const TASK_ID_RE = /\bt_[0-9a-f]{6,}\b/gi;
// _BRIEFING_PATH
const PATH_RE = /(?:\/home\/\S+|(?:[A-Za-z]:)?(?:\/[\w.-]+)+)/g;

function bounded(text: string, limit: number): string {
  return text.length <= limit ? text : `${text.slice(0, limit - 1).trimEnd()}…`;
}

/** Roh-Titel → Entscheidungs-Titel (≤80 Z., erste Zeile, ohne IDs/Pfade). */
export function decisionTitle(rawTitle: string | null | undefined): string {
  const firstLine = String(rawTitle ?? "").trim().split("\n", 1)[0] ?? "";
  let text = firstLine;
  text = text.replace(TASK_PREFIX_RE, "");
  text = text.replace(KIND_SUFFIX_RE, "");
  text = text.replace(TASK_ID_RE, "");
  text = text.replace(PATH_RE, "");
  text = text.replace(/\s{2,}/g, " ").trim();
  text = text.replace(/^[\s—–:;-]+|[\s—–:;-]+$/g, "");
  return bounded(text || FALLBACK, DECISION_TITLE_MAX);
}

/** S7.6: Zeile 1 der Decision-Card — Server-`summary` gewinnt, sonst
 *  clientseitige Destillation des Roh-Titels. */
export function decisionHeadline(item: PaInboxItem): string {
  const summary = item.summary?.trim();
  return summary || decisionTitle(item.title);
}

function compactSeconds(seconds: number): string {
  const d = Math.floor(seconds);
  if (d < 60) return `${d}s`;
  if (d < 3600) return `${Math.floor(d / 60)}m`;
  if (d < 86400) return `${Math.floor(d / 3600)}h`;
  return `${Math.floor(d / 86400)}d`;
}

/** S7.6: Alter-Badge „seit 3d"/„seit 5h"; null bei ungültigem oder
 *  zukünftigem ts — das Badge entfällt dann still (nie „Zeit ungültig"
 *  in der Entscheidungszeile). */
export function decisionAge(ts: number, now?: number): string | null {
  const elapsed = elapsedSeconds(ts, now);
  if (elapsed === null) return null;
  return `seit ${compactSeconds(elapsed)}`;
}

/** S7.6: 🔑-Badge — die Karte braucht eine Operator-Freigabe. */
export function needsOperatorKey(item: PaInboxItem): boolean {
  return (
    (item.type === "held_task" || item.type === "freigabe_gate") &&
    item.freigabe === "operator"
  );
}
