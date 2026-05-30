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
  Worker, AgentLive, Proposal, WorkerHealth, ToneName,
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
    return { key: 'stuck', tone: 'amber', label: 'Stuck', dot: 'warn' };
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

/* ── Agenten-Ableitungen ───────────────────────────────────────────────── */

export const agentStatusTone: Record<AgentLive['status'], ToneName> = {
  active: 'cyan', monitoring: 'amber', ready: 'sky', idle: 'zinc', offline: 'zinc',
};
export const agentStatusLabel: Record<AgentLive['status'], string> = {
  active: 'Aktiv', monitoring: 'Beobachtet', ready: 'Bereit', idle: 'Inaktiv', offline: 'Offline',
};

export function agentIsProblem(a: AgentLive): boolean {
  return a.stuckSignal || a.status === 'offline';
}
export function agentSortRank(a: AgentLive): number {
  if (a.stuckSignal) return 3;
  if (a.status === 'offline') return 2;
  return 0;
}
export function buildOpenClawAlerts(agents: AgentLive[]): {
  critical: AgentLive[];
  warning: AgentLive[];
  criticalCount: number;
  warningCount: number;
} {
  const critical = agents.filter((a) => a.stuckSignal === true);
  const warning = agents.filter((a) => a.status === 'offline' && a.stuckSignal !== true);
  return { critical, warning, criticalCount: critical.length, warningCount: warning.length };
}
/** Effektiver Ton: stuckSignal überschreibt den Status optisch (amber). */
export function agentTone(a: AgentLive): ToneName {
  return a.stuckSignal ? 'amber' : agentStatusTone[a.status];
}
export function agentLabel(a: AgentLive): string {
  return a.stuckSignal ? 'Stuck' : agentStatusLabel[a.status];
}

/* ── Übersichts-Aggregation („Ist alles gesund?") ──────────────────────── */

export type Warning =
  | { kind: 'hermes'; worker: Worker; health: WorkerHealth }
  | { kind: 'openclaw'; agent: AgentLive };

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
  agents: AgentLive[],
  proposals: Proposal[],
  now: number = nowSec(),
): Overview {
  const hProblem = workers.filter((w) =>
    ['stuck', 'blocked', 'offline'].includes(workerHealth(w, now).key));
  const ocProblem = agents.filter(agentIsProblem);

  const warnings: Warning[] = [
    ...hProblem.map((w): Warning => ({ kind: 'hermes', worker: w, health: workerHealth(w, now) })),
    ...ocProblem.map((a): Warning => ({ kind: 'openclaw', agent: a })),
  ];

  return {
    hermesTotal: workers.length,
    hermesHealthy: workers.filter((w) => workerHealth(w, now).key === 'healthy').length,
    hermesRunning: workers.filter((w) => w.run_status === 'running').length,
    ocTotal: agents.length,
    ocHealthy: agents.filter((a) =>
      ['active', 'monitoring', 'ready'].includes(a.status) && !a.stuckSignal).length,
    ocActive: agents.filter((a) => a.status === 'active').length,
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

/** Dauer aus Sekunden: "2h 14m" / "4m" / "52s". */
export function fmtDur(sec: number): string {
  sec = Math.max(0, Math.floor(sec));
  const hh = Math.floor(sec / 3600);
  const mm = Math.floor((sec % 3600) / 60);
  if (hh > 0) return `${hh}h ${String(mm).padStart(2, '0')}m`;
  if (mm > 0) return `${mm}m`;
  return `${sec}s`;
}

export const fmtMB = (bytes: number) => `${Math.round(bytes / 1048576)} MB`;

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

/** Nur Uhrzeit "HH:MM" aus ISO-8601-String ODER epoch-Sekunden; leer/ungültig → "–". */
export function fmtClockTime(at: string | number): string {
  if (at === "") return "–";
  const value = typeof at === 'number' ? at * 1000 : Date.parse(at);
  if (!Number.isFinite(value)) return "–";
  return new Date(value).toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });
}
