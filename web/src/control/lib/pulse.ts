/**
 * Puls — der "Receipt"-Strom des Dashboards: was die autonome Maschine
 * tatsächlich getan hat, während niemand hinschaute. Reine Ableitungslogik,
 * framework-neutral und testbar (kein verstecktes Date.now() — `nowSec` wird
 * injiziert). Sie führt drei bereits gepollte Quellen in EINEN umgekehrt
 * chronologischen Strom zusammen:
 *   - abgeschlossene Hermes-Worker         (KanbanResult.ended_at)
 *   - übernommene/zurückgerollte/übersprungene Vorschläge (Proposal.applied_at)
 *   - gefeuerte Cron-Jobs                  (CronJob.last_run_at)
 *
 * Bewusst rein lesend und i18n-frei: die View formatiert Labels, die Logik
 * liefert nur Fakten + Ton. So bleibt der "Was lief?"-Tab eine Sicht auf
 * vorhandene Daten, kein neuer Schreibpfad.
 */
import type { CronJob, KanbanResult, Proposal, ToneName } from "./types";

export type PulseKind =
  | "run"          // Worker fertig
  | "applied"      // Verbesserung übernommen
  | "reverted"     // automatisch zurückgerollt (Sicherheitsnetz)
  | "skipped"      // Vorschlag verworfen
  | "cron-ok"      // geplanter Job lief sauber
  | "cron-error";  // geplanter Job-/Zustell-Fehler

export interface PulseEvent {
  id: string;
  kind: PulseKind;
  /** epoch seconds */
  at: number;
  title: string;
  detail: string | null;
  tone: ToneName;
  /** Navigationsziel: Klick auf das Ereignis öffnet den Quell-Tab. */
  tab: string;
}

/**
 * Akzeptiert epoch-Sekunden (number) ODER ISO-8601 (string); null/ungültig → null.
 * Konsistent mit fmtClockTime: Zahlen sind Sekunden, keine Millis.
 */
export function toEpochSec(value: number | string | null | undefined): number | null {
  if (value == null) return null;
  if (typeof value === "number") return value > 0 ? Math.floor(value) : null;
  const ms = Date.parse(value);
  return Number.isFinite(ms) ? Math.floor(ms / 1000) : null;
}

export function runToEvent(r: KanbanResult): PulseEvent | null {
  const at = toEpochSec(r.ended_at);
  if (at == null) return null;
  const ok = r.outcome === "completed";
  return {
    id: `run:${r.run_id}`,
    kind: "run",
    at,
    title: r.task_title || r.task_id,
    detail: r.summary_preview || r.summary || null,
    tone: ok ? "emerald" : "amber",
    tab: "/control/hermes",
  };
}

export function proposalToEvent(p: Proposal): PulseEvent | null {
  const label = p.title ?? p.target;
  // Zurückgerollt ist ein Ergebnis für sich — vor dem reinen Status prüfen.
  if (p.last_outcome === "reverted_no_improvement") {
    const at = toEpochSec(p.applied_at ?? p.created_at);
    if (at == null) return null;
    return { id: `prop:${p.id}`, kind: "reverted", at, title: label, detail: p.category ?? null, tone: "zinc", tab: "/control/autoresearch" };
  }
  if (p.status === "applied") {
    const at = toEpochSec(p.applied_at ?? p.created_at);
    if (at == null) return null;
    return { id: `prop:${p.id}`, kind: "applied", at, title: label, detail: p.rationale_plain || p.category || null, tone: "emerald", tab: "/control/autoresearch" };
  }
  if (p.status === "skipped") {
    const at = toEpochSec(p.applied_at ?? p.created_at);
    if (at == null) return null;
    return { id: `prop:${p.id}`, kind: "skipped", at, title: label, detail: p.category ?? null, tone: "zinc", tab: "/control/autoresearch" };
  }
  // proposed / testing sind noch kein Receipt.
  return null;
}

export function cronToEvent(j: CronJob): PulseEvent | null {
  const at = toEpochSec(j.last_run_at);
  if (at == null) return null;
  const failed = Boolean(j.last_error) || Boolean(j.last_delivery_error) || (j.last_status != null && j.last_status !== "ok" && j.last_status !== "success");
  return {
    id: `cron:${j.id}:${at}`,
    kind: failed ? "cron-error" : "cron-ok",
    at,
    title: j.name || j.id,
    detail: failed
      ? (j.last_error ?? j.last_delivery_error ?? j.last_status ?? "Fehler")
      : (j.latest_output?.filename ?? null),
    tone: failed ? "red" : "sky",
    tab: "/control/crons",
  };
}

export interface PulseInput {
  results: KanbanResult[];
  proposals: Proposal[];
  crons: CronJob[];
  /** Untergrenze (epoch sec). Default 0 = alles, was die Quellen liefern. */
  sinceSec?: number;
  nowSec: number;
}

/** Führt alle Quellen zu einem nach Zeit absteigend sortierten Strom zusammen. */
export function buildPulse(input: PulseInput): PulseEvent[] {
  const events: PulseEvent[] = [];
  for (const r of input.results) { const e = runToEvent(r); if (e) events.push(e); }
  for (const p of input.proposals) { const e = proposalToEvent(p); if (e) events.push(e); }
  for (const j of input.crons) { const e = cronToEvent(j); if (e) events.push(e); }
  const cutoff = input.sinceSec ?? 0;
  // +120s Toleranz gegen leichten Uhren-Versatz zwischen Backend und Browser.
  return events
    .filter((e) => e.at >= cutoff && e.at <= input.nowSec + 120)
    .sort((a, b) => b.at - a.at);
}

export interface PulseSummary {
  runs: number;
  applied: number;
  reverted: number;
  skipped: number;
  crons: number;
  cronErrors: number;
  total: number;
}

export function summarizePulse(events: PulseEvent[]): PulseSummary {
  const s: PulseSummary = { runs: 0, applied: 0, reverted: 0, skipped: 0, crons: 0, cronErrors: 0, total: events.length };
  for (const e of events) {
    if (e.kind === "run") s.runs++;
    else if (e.kind === "applied") s.applied++;
    else if (e.kind === "reverted") s.reverted++;
    else if (e.kind === "skipped") s.skipped++;
    else if (e.kind === "cron-ok") s.crons++;
    else if (e.kind === "cron-error") { s.crons++; s.cronErrors++; }
  }
  return s;
}

/** Lokaler Tages-Schlüssel "YYYY-MM-DD" (für Tagesgruppen). */
export function dayKey(epochSec: number): string {
  const d = new Date(epochSec * 1000);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

function daysBetween(key: string, todayKey: string): number {
  const [y, m, d] = key.split("-").map(Number);
  const [ty, tm, td] = todayKey.split("-").map(Number);
  return Math.round((Date.UTC(ty, tm - 1, td) - Date.UTC(y, m - 1, d)) / 86400000);
}

export interface PulseDay {
  key: string;
  /** 0 = heute, 1 = gestern, … (für die View-Label-Wahl). */
  daysAgo: number;
  events: PulseEvent[];
}

/**
 * Gruppiert den (bereits absteigend sortierten) Strom nach lokalem Tag.
 * Die Gruppenreihenfolge folgt der Ereignis-Sortierung — neuester Tag zuerst.
 */
export function groupPulseByDay(events: PulseEvent[], nowSec: number): PulseDay[] {
  const todayKey = dayKey(nowSec);
  const order: string[] = [];
  const byDay = new Map<string, PulseEvent[]>();
  for (const e of events) {
    const k = dayKey(e.at);
    let bucket = byDay.get(k);
    if (!bucket) { bucket = []; byDay.set(k, bucket); order.push(k); }
    bucket.push(e);
  }
  return order.map((key) => ({ key, daysAgo: daysBetween(key, todayKey), events: byDay.get(key)! }));
}
