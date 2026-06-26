/**
 * Reine Ableitungslogik — framework-neutral, testbar.
 * Hier liegt die EINZIGE Quelle der Wahrheit für Gesundheits-Schwellen,
 * Aggregation und Zeit-/Größen-Formatierung. (Im Prototyp: hermes-data.js)
 *
 * Wichtig: `now` wird injiziert (Default = Date.now()/1000), damit Tests
 * deterministisch sind und kein verstecktes Date.now() in der Logik steckt.
 */
import { isActionable } from './autoresearch';
import type {
  Worker, Proposal, WorkerHealth,
} from './types';

export const nowSec = () => Math.floor(Date.now() / 1000);

/**
 * Stuck-Schwelle: ein *tatsächlich getrackter* Heartbeat, der älter als das ist,
 * gilt als „stuck" (Sekunden). Bewusst nahe an der Backend-Reclaim-Schwelle
 * (`_STALE_HEARTBEAT_GAP_SECONDS = 3600`), damit die UI nicht „stuck" zeigt,
 * während der Dispatcher den Worker noch als lebendig führt. Greift NUR, wenn
 * der Run überhaupt Heartbeats schreibt — die meisten tun das nie (dann ist
 * `claim_expires` das maßgebliche Liveness-Signal, nicht das Heartbeat-Alter).
 */
export const STUCK_HEARTBEAT_S = 600;

/* ── Worker-Gesundheit ─────────────────────────────────────────────────── */
/**
 * Prüfreihenfolge ist bewusst: offline → blocked → stuck → healthy.
 * Genau diese Logik bildet die UI-Stati in allen drei Richtungen ab.
 */
export function workerHealth(w: Worker, now: number = nowSec()): WorkerHealth {
  // Most workers never write a heartbeat (last_heartbeat_at stays NULL, coerced
  // to 0 here), so a missing heartbeat must NOT read as "ancient" — that made
  // healthy running workers show "Stuck". A heartbeat only counts when present.
  const hasHeartbeat = w.last_heartbeat_at > 0;
  const heartbeatStale = hasHeartbeat && (now - w.last_heartbeat_at) > STUCK_HEARTBEAT_S;
  // Authoritative liveness signal (matches the dispatcher's TTL reclaim).
  const expired = w.claim_expires > 0 && w.claim_expires < now;

  if (w.run_status === 'timed_out' || w.run_status === 'crashed' || (w.inspect ? !w.inspect.alive : false)) {
    return { key: 'offline', tone: 'zinc', label: 'Offline', dot: 'offline' };
  }
  if (w.run_status === 'blocked') {
    return { key: 'blocked', tone: 'red', label: 'Blockiert', dot: 'error' };
  }
  if (expired || heartbeatStale) {
    return { key: 'stuck', tone: 'amber', label: 'Hängt', dot: 'warn' };
  }
  return { key: 'healthy', tone: 'cyan', label: 'Läuft', dot: 'live' };
}

/** Sortier-Rang: Probleme zuerst (stuck/blocked > offline > healthy). */
export function workerSortRank(w: Worker, now: number = nowSec()): number {
  const k = workerHealth(w, now).key;
  return ({ stuck: 3, blocked: 3, offline: 2, healthy: 0 } as const)[k] ?? 0;
}

export const workerRuntime = (w: Worker, now: number = nowSec()) => now - w.started_at;
export const workerRemaining = (w: Worker, now: number = nowSec()) =>
  w.max_runtime_seconds - workerRuntime(w, now);
export const workerHeartbeatAge = (w: Worker, now: number = nowSec()) =>
  now - w.last_heartbeat_at;

/* ── Runaway-Erkennung (Zeitbomben-Läufe) ─────────────────────────────────
 * Ein Lauf gilt als Runaway-Kandidat, BEVOR er Budget verbrennt:
 *   warn     ≥ 80 % der max_runtime_seconds ODER Heartbeat > 120 s alt
 *   critical ≥ 100 % der max_runtime_seconds ODER Heartbeat > 300 s alt
 * Heartbeat-Regeln greifen NUR, wenn der Run überhaupt Heartbeats schreibt
 * (gleiches Prinzip wie workerHealth). max_runtime_seconds ≤ 0 → keine
 * Laufzeit-Regel (kein Limit gesetzt). Operator-Vertrag 2026-06-10. */
export const RUNAWAY_RUNTIME_WARN_PCT = 0.8;
export const RUNAWAY_HEARTBEAT_WARN_S = 120;
export const RUNAWAY_HEARTBEAT_CRIT_S = 300;

export type RunawayLevel = 'none' | 'warn' | 'critical';

export interface RunawayState {
  level: RunawayLevel;
  /** Anteil der verbrauchten Laufzeit (0..>1); 0 wenn kein Limit gesetzt. */
  pct: number;
  /** Menschlich lesbare Gründe (leer bei level='none'). */
  reasons: string[];
}

export function workerRunaway(w: Worker, now: number = nowSec()): RunawayState {
  const runtime = workerRuntime(w, now);
  const max = w.max_runtime_seconds > 0 ? w.max_runtime_seconds : null;
  const pct = max ? runtime / max : 0;
  const hasHeartbeat = w.last_heartbeat_at > 0;
  const hbAge = hasHeartbeat ? now - w.last_heartbeat_at : null;

  const reasons: string[] = [];
  let level: RunawayLevel = 'none';
  if (max && runtime >= max) {
    level = 'critical';
    reasons.push(`Laufzeit ${fmtDur(runtime)} ≥ Limit ${fmtDur(max)}`);
  } else if (max && pct >= RUNAWAY_RUNTIME_WARN_PCT) {
    level = 'warn';
    reasons.push(`Laufzeit ${fmtDur(runtime)} von ${fmtDur(max)} (${Math.round(pct * 100)} %)`);
  }
  if (hbAge != null && hbAge > RUNAWAY_HEARTBEAT_CRIT_S) {
    level = 'critical';
    reasons.push(`Heartbeat seit ${fmtDur(hbAge)} still`);
  } else if (hbAge != null && hbAge > RUNAWAY_HEARTBEAT_WARN_S) {
    if (level === 'none') level = 'warn';
    reasons.push(`Heartbeat vor ${fmtDur(hbAge)}`);
  }
  return { level, pct, reasons };
}

/* ── Zeit-Achsen-Zustand (Phase B: ehrliche Zeit-Achse im Cockpit) ────────
 * Leitet ein Zustandswort + Ton aus elapsed / p50 / p90 / budget / heartbeat ab.
 * Stuck-Check schlägt alle anderen (ein hängender Worker ist immer rot, auch wenn
 * er eigentlich noch im Plan wäre). Fehlt p50/p90 (zu wenig Historie) → noEta.
 */

export type TimeAxisStateKey =
  | 'im_plan'         // elapsed < p50              → emerald
  | 'laeuft'          // p50 ≤ elapsed < p90         → cyan
  | 'langsamer'       // elapsed ≥ p90               → amber
  | 'steht'           // heartbeatAge > STUCK_HEARTBEAT_S → red (überschreibt)
  | 'ueber_budget'    // elapsed > budget            → red
  | 'no_eta';         // p50/p90 null → ehrlicher Fallback

export interface TimeAxisState {
  key: TimeAxisStateKey;
  tone: 'emerald' | 'cyan' | 'amber' | 'red' | 'zinc';
  label: string;
  /** true wenn p50/p90 nicht vorhanden (zu wenig Historie) */
  noEta: boolean;
}

export function workerTimeAxisState(
  elapsed: number,
  p50: number | null | undefined,
  p90: number | null | undefined,
  budget: number,
  heartbeatAge: number,
  hasHeartbeat: boolean,
): TimeAxisState {
  // Stuck überschreibt alles (wie workerHealth: nur wenn tatsächlich Heartbeats existieren)
  if (hasHeartbeat && heartbeatAge > STUCK_HEARTBEAT_S) {
    return { key: 'steht', tone: 'red', label: 'steht', noEta: p50 == null || p90 == null };
  }
  // Über Budget
  if (budget > 0 && elapsed > budget) {
    return { key: 'ueber_budget', tone: 'red', label: 'über Budget', noEta: p50 == null || p90 == null };
  }
  // Fehlende ETA → noEta-Fallback
  if (p50 == null || p90 == null || p50 <= 0) {
    return { key: 'no_eta', tone: 'zinc', label: 'kein Vergleichswert', noEta: true };
  }
  if (elapsed >= p90) {
    return { key: 'langsamer', tone: 'amber', label: 'langsamer als üblich', noEta: false };
  }
  if (elapsed >= p50) {
    return { key: 'laeuft', tone: 'cyan', label: 'läuft', noEta: false };
  }
  return { key: 'im_plan', tone: 'emerald', label: 'im Plan', noEta: false };
}

/** Berechnet die Skalierungs-Max der Zeit-Achse.
 * Immer mindestens budget oder p90*1.2 oder elapsed*1.1 — damit Marker nie
 * über den Rand hinausragen und der aktuelle Zeitpunkt sichtbar bleibt. */
export function timeAxisScaleMax(
  elapsed: number,
  p90: number | null | undefined,
  budget: number,
): number {
  const candidates: number[] = [elapsed * 1.1];
  if (p90 != null && p90 > 0) candidates.push(p90 * 1.2);
  if (budget > 0) candidates.push(budget);
  return Math.max(...candidates, 1);
}

/* ── Übersichts-Aggregation („Ist alles gesund?") ──────────────────────── */

export type Warning = { kind: 'hermes'; worker: Worker; health: WorkerHealth };

export interface Overview {
  hermesTotal: number;
  hermesHealthy: number;
  hermesRunning: number;
  ocTotal: number;
  ocHealthy: number;
  ocActive: number;
  openProposals: number;
  warnings: Warning[];
  allHealthy: boolean;
}

export function buildOverview(
  workers: Worker[],
  _agents: unknown[],
  proposals: Proposal[],
  now: number = nowSec(),
): Overview {
  const hProblem = workers.filter((w) =>
    ['stuck', 'blocked', 'offline'].includes(workerHealth(w, now).key));

  const warnings: Warning[] = [
    ...hProblem.map((w): Warning => ({ kind: 'hermes', worker: w, health: workerHealth(w, now) })),
  ];

  return {
    hermesTotal: workers.length,
    hermesHealthy: workers.filter((w) => workerHealth(w, now).key === 'healthy').length,
    hermesRunning: workers.filter((w) => w.run_status === 'running').length,
    ocTotal: 0,
    ocHealthy: 0,
    ocActive: 0,
    openProposals: proposals.filter(isActionable).length,
    warnings,
    allHealthy: warnings.length === 0,
  };
}

/* ── Formatierung ──────────────────────────────────────────────────────── */

/** Kurzes Alter aus epoch-Sekunden: "3s","4m","2h","4d". */
export function fmtAge(epochSec: number, now: number = nowSec()): string {
  const d = Math.max(0, now - epochSec);
  if (d < 60) return `${d}s`;
  if (d < 3600) return `${Math.floor(d / 60)}m`;
  if (d < 86400) return `${Math.floor(d / 3600)}h`;
  return `${Math.floor(d / 86400)}d`;
}

/** Dauer aus Sekunden: "2h 14m" / "4m 30s" / "52s". */
export function fmtDur(sec: number): string {
  sec = Math.max(0, Math.floor(sec));
  const hh = Math.floor(sec / 3600);
  const mm = Math.floor((sec % 3600) / 60);
  const ss = sec % 60;
  if (hh > 0) return `${hh}h ${String(mm).padStart(2, '0')}m`;
  if (mm > 0) return `${mm}m ${ss}s`;
  return `${sec}s`;
}

export const fmtMB = (bytes: number) => `${Math.round(bytes / 1048576)} MB`;

/** Token-Mengen kompakt: "1.2 M" / "57 k" / "985". */
export const fmtTokens = (v: number) =>
  v >= 1_000_000 ? `${(v / 1_000_000).toFixed(1)} M` : v >= 1_000 ? `${Math.round(v / 1_000)} k` : String(v);

/* ── Datenfrische (E1) ─────────────────────────────────────────────────── */
/**
 * Frische einer Polling-Quelle. `stale` heißt: die Quelle wurde länger als das
 * Dreifache ihres Poll-Intervalls (mind. 30 s) nicht erfolgreich aktualisiert.
 * Das ist erwartbar, wenn der Tab im Hintergrund liegt (usePolling pausiert bei
 * `document.hidden` — BY DESIGN), und genau deshalb eine *Warnung*, kein Fehler:
 * „pausiert/veraltet" ≠ „kaputt" ≠ „0". (Adressiert Sprint-A4: nie stilles 0.)
 */
export interface Freshness {
  ageSec: number | null;
  stale: boolean;
  /** kurzer Klartext: "vor 4s", "vor 2m", "noch nie" */
  label: string;
}

export function freshness(
  lastUpdated: number | null,
  intervalMs: number,
  now: number = nowSec(),
): Freshness {
  if (lastUpdated == null) {
    return { ageSec: null, stale: false, label: "noch nie" };
  }
  const ageSec = Math.max(0, now - lastUpdated);
  const threshold = Math.max(30, Math.floor((intervalMs / 1000) * 3));
  return { ageSec, stale: ageSec > threshold, label: `vor ${fmtAge(lastUpdated, now)}` };
}

/** DD/MM/YYYY, HH:mm (Design-System-Format). */
export function fmtClock(epochSec: number): string {
  const d = new Date(epochSec * 1000);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${p(d.getDate())}/${p(d.getMonth() + 1)}/${d.getFullYear()}, ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/* ── Kosten-Anzeige (geschätzte API-Äquivalente für Abo-Runs) ───────────────
 * Regel:
 *   cost_effective_usd > 0 UND cost_usd > 0  → "$X.XX"          (real)
 *   cost_effective_usd > 0 UND cost_usd === 0 → "$X.XX gesch."   (Abo-Schätzwert)
 *   cost_effective_usd === 0 UND tokens > 0   → "—"              (kein Schätzwert; Tokens bleiben extern sichtbar)
 *   alles 0                                   → "—"
 * Rückgabe: { text, estimated } — `estimated=true` signalisiert dem Aufrufer,
 * dass er einen Tooltip/Marker anzeigen soll.
 */
export interface EffectiveCostResult {
  /** Anzeigetext, z. B. "$1.23", "$0.45 gesch." oder "—". */
  text: string;
  /** true = nur geschätzter API-Gegenwert (läuft über Abo); false = real oder leer. */
  estimated: boolean;
}

export function formatEffectiveCost({
  cost_usd,
  cost_effective_usd,
  tokens,
}: {
  cost_usd: number;
  cost_effective_usd: number;
  tokens: number;
}): EffectiveCostResult {
  if (cost_effective_usd > 0) {
    if (cost_usd > 0) {
      // Real (API-billed)
      return { text: `$${cost_effective_usd.toFixed(2)}`, estimated: false };
    }
    // Rein über Abo — geschätzter Gegenwert
    return { text: `$${cost_effective_usd.toFixed(2)} gesch.`, estimated: true };
  }
  // Kein Schätzwert gestempelt (z.B. Codex/verifier-Lanes), Tokens ggf. vorhanden
  if (tokens > 0) {
    return { text: "—", estimated: false };
  }
  return { text: "—", estimated: false };
}

/* ── F2: Burn-Wächter ──────────────────────────────────────────────────────
 * Berechnet Burn-Rate und optionale Hochrechnung aus echten Token-Zahlen.
 * Alle Felder null-safe — wenn keine Tokens vorhanden, gibt die Funktion
 * einen expliziten noData-Marker zurück (niemals erfundene Werte).
 */

export interface BurnInfo {
  /** Tokens/Minute (Gesamt over elapsed). null wenn keine Tokens vorhanden. */
  ratePerMin: number | null;
  /** Hochrechnung = rate × p50-Minuten (wenn eta verfügbar). Sonst null. */
  projectedTotal: number | null;
  /** true wenn überhaupt keine Live-Tokens vorhanden */
  noData: boolean;
}

/**
 * Berechnet Burn-Rate + optionale Hochrechnung.
 * @param inputTokens  Gesamte In-Tokens seit Run-Start (oder null)
 * @param outputTokens Gesamte Out-Tokens seit Run-Start (oder null)
 * @param elapsedSec   Laufzeit in Sekunden (> 0 erwartet)
 * @param etaP50Sec    ETA p50 in Sekunden (optional — für Hochrechnung)
 * @param budgetSec    Budget in Sekunden (Fallback für Hochrechnung wenn kein p50)
 */
export function workerBurnRate(
  inputTokens: number | null | undefined,
  outputTokens: number | null | undefined,
  elapsedSec: number,
  etaP50Sec?: number | null,
  budgetSec?: number,
): BurnInfo {
  const hasData = inputTokens != null || outputTokens != null;
  if (!hasData || elapsedSec <= 0) {
    return { ratePerMin: null, projectedTotal: null, noData: !hasData };
  }
  const total = (inputTokens ?? 0) + (outputTokens ?? 0);
  const elapsedMin = elapsedSec / 60;
  const ratePerMin = total / elapsedMin;

  // Hochrechnung: rate × p50-Minuten (Vorrang) oder Budget-Minuten (Fallback)
  const horizonSec = (etaP50Sec != null && etaP50Sec > 0)
    ? etaP50Sec
    : (budgetSec != null && budgetSec > 0 ? budgetSec : null);
  const projectedTotal = horizonSec != null ? ratePerMin * (horizonSec / 60) : null;

  return { ratePerMin, projectedTotal, noData: false };
}

/** Nur Uhrzeit "HH:MM" aus ISO-8601-String ODER epoch-Sekunden; leer/ungültig → "–". */
// ── F4: Kapazitäts-Ableitung (Round C) ───────────────────────────────────────
// Pure function — testbar ohne DOM.
export interface CapacityState {
  /** Anzahl aktiver Worker. */
  count: number;
  /** Konfiguriertes Worker-Maximum — null = nicht konfiguriert. */
  cap: number | null;
  /** Anzahl wartender Tasks (status=ready OR todo). */
  queueDepth: number;
  /** true wenn count >= cap (und cap != null) UND queue > 0. */
  bottleneck: boolean;
}

export function deriveCapacity(
  count: number,
  cap: number | null,
  queueDepth: number,
): CapacityState {
  const bottleneck = cap != null && count >= cap && queueDepth > 0;
  return { count, cap, queueDepth, bottleneck };
}

export function fmtClockTime(at: string | number): string {
  if (at === "" || (typeof at === "number" && at <= 0)) return "–";
  const value = typeof at === 'number' ? at * 1000 : Date.parse(at);
  if (!Number.isFinite(value)) return "–";
  return new Date(value).toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });
}
