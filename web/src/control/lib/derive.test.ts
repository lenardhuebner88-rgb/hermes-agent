/**
 * Unit-Tests für die Ableitungslogik (vitest).
 * `npx vitest run` — diese Tests pinnen die Gesundheits-Schwellen & Formatierung,
 * damit ein Refactor sie nicht versehentlich verschiebt.
 */
import { describe, it, expect } from 'vitest';
import {
  workerHealth, buildOverview, buildOpenClawAlerts, fmtAge, fmtDur, fmtMB, freshness, fmtClock, fmtClockTime, STUCK_HEARTBEAT_S,
} from './derive';
import type { Worker, AgentLive, Proposal } from './types';

const NOW = 1_780_041_720;

function mkWorker(over: Partial<Worker> = {}): Worker {
  return {
    run_id: 'run_x', task_id: 'T-1', task_title: 't', task_status: 'running',
    task_assignee: 'hermes', profile: 'coder', worker_pid: 1,
    started_at: NOW - 3600, claim_lock: 'l', claim_expires: NOW + 600,
    last_heartbeat_at: NOW - 5, max_runtime_seconds: 7200,
    run_status: 'running', run_outcome: null,
    inspect: { cpu_percent: 10, rss: 1048576, num_threads: 1, num_fds: 1, status: 'running', alive: true },
    ...over,
  };
}

describe('workerHealth', () => {
  it('gesund bei frischem Heartbeat + gültigem Claim', () => {
    expect(workerHealth(mkWorker(), NOW).key).toBe('healthy');
  });
  it('offline bei timed_out ODER nicht-alive (höchste Priorität)', () => {
    expect(workerHealth(mkWorker({ run_status: 'timed_out' }), NOW).key).toBe('offline');
    expect(workerHealth(mkWorker({ inspect: { cpu_percent: 10, rss: 1048576, num_threads: 1, num_fds: 1, status: 'running', alive: false } }), NOW).key).toBe('offline');
  });
  it('blocked schlägt stuck (auch bei altem Heartbeat)', () => {
    const w = mkWorker({ run_status: 'blocked', last_heartbeat_at: NOW - 9999 });
    expect(workerHealth(w, NOW).key).toBe('blocked');
  });
  it('stuck bei Heartbeat > Schwelle', () => {
    const w = mkWorker({ last_heartbeat_at: NOW - (STUCK_HEARTBEAT_S + 1) });
    expect(workerHealth(w, NOW).key).toBe('stuck');
  });
  it('stuck bei abgelaufenem claim_expires (trotz frischem Heartbeat)', () => {
    expect(workerHealth(mkWorker({ claim_expires: NOW - 1 }), NOW).key).toBe('stuck');
  });
  it('Heartbeat genau auf der Schwelle ist NICHT stuck', () => {
    expect(workerHealth(mkWorker({ last_heartbeat_at: NOW - STUCK_HEARTBEAT_S }), NOW).key).toBe('healthy');
  });
  it('laufender Worker OHNE Heartbeat (last_heartbeat_at=0) ist healthy, nicht stuck', () => {
    // Regression: most workers never write a heartbeat (NULL -> coerced to 0).
    // A missing heartbeat must not read as "ancient" and flip a healthy worker
    // to "Stuck"; claim_expires is the liveness signal.
    expect(workerHealth(mkWorker({ last_heartbeat_at: 0 }), NOW).key).toBe('healthy');
  });
  it('OHNE Heartbeat aber abgelaufenem Claim ist stuck', () => {
    expect(workerHealth(mkWorker({ last_heartbeat_at: 0, claim_expires: NOW - 1 }), NOW).key).toBe('stuck');
  });
});

describe('buildOverview', () => {
  const agents: AgentLive[] = [
    { id: 'main', name: 'Main', emoji: '🦅', status: 'active', model: 'm', lastActive: NOW,
      tasks: { queued: [], active: [], review: [], recentDone: [] }, stuckSignal: false,
      activityPulse: 1, fleetHealth: { currentTask: '', heartbeat: NOW, throughput: '', currentTool: '', lastOutput: '' },
      roleLabel: '', roleSummary: '', escalationNote: null },
    { id: 'james', name: 'James', emoji: '🔬', status: 'active', model: 'm', lastActive: NOW,
      tasks: { queued: [], active: [], review: [], recentDone: [] }, stuckSignal: true,
      activityPulse: 0, fleetHealth: { currentTask: '', heartbeat: NOW, throughput: '', currentTool: '', lastOutput: '' },
      roleLabel: '', roleSummary: '', escalationNote: 'hängt' },
  ];
  const proposals: Proposal[] = [
    { id: 'p1', target: 's', section: '', rationale_plain: '', diff_before_after: "", mode: 'skill', status: 'proposed' },
    { id: 'p2', target: 's', section: '', rationale_plain: '', diff_before_after: "", mode: 'code', status: 'applied' },
  ];

  it('zählt nur actionable Vorschläge, aktive Agenten und sammelt Warnungen', () => {
    const mixedProposals = [
      ...proposals,
      { id: 'p3', target: 's', section: '', rationale_plain: '', diff_before_after: "", mode: 'skill', status: 'proposed', last_outcome: 'reverted_no_improvement' },
    ] satisfies Proposal[];
    const o = buildOverview([mkWorker(), mkWorker({ run_status: 'blocked' })], agents, mixedProposals, NOW);
    expect(o.openProposals).toBe(1);
    expect(o.ocActive).toBe(2);
    expect(o.ocHealthy).toBe(1);          // james ist stuck → nicht gesund
    expect(o.warnings.length).toBe(2);    // 1 blockierter Worker + 1 stuck Agent
    expect(o.allHealthy).toBe(false);
  });

  it('allHealthy=true wenn nichts auffällt', () => {
    const o = buildOverview([mkWorker()], [agents[0]], [], NOW);
    expect(o.allHealthy).toBe(true);
  });
});

describe('Formatierung', () => {
  it('fmtAge', () => {
    expect(fmtAge(NOW - 5, NOW)).toBe('5s');
    expect(fmtAge(NOW - 240, NOW)).toBe('4m');
    expect(fmtAge(NOW - 7200, NOW)).toBe('2h');
    expect(fmtAge(NOW - 345600, NOW)).toBe('4d');
  });
  it('fmtDur', () => {
    expect(fmtDur(52)).toBe('52s');
    expect(fmtDur(240)).toBe('4m');
    expect(fmtDur(8040)).toBe('2h 14m');
  });
  it('fmtMB', () => {
    expect(fmtMB(536870912)).toBe('512 MB');
  });
});

describe('Datenfrische (E1)', () => {
  it('noch nie aktualisiert → nicht stale, Label "noch nie"', () => {
    const f = freshness(null, 5000, NOW);
    expect(f).toEqual({ ageSec: null, stale: false, label: 'noch nie' });
  });
  it('frisch innerhalb 3x Intervall', () => {
    const f = freshness(NOW - 4, 5000, NOW);
    expect(f.stale).toBe(false);
    expect(f.label).toBe('vor 4s');
  });
  it('stale jenseits 3x Intervall (mind. 30s Boden)', () => {
    // 6s-Poll → Schwelle 18s, aber Boden 30s greift
    expect(freshness(NOW - 25, 6000, NOW).stale).toBe(false);
    expect(freshness(NOW - 31, 6000, NOW).stale).toBe(true);
  });
  it('langes Intervall: Schwelle = 3x Intervall', () => {
    // 20s-Poll → Schwelle 60s
    expect(freshness(NOW - 59, 20000, NOW).stale).toBe(false);
    expect(freshness(NOW - 61, 20000, NOW).stale).toBe(true);
  });
});

describe('fmtClockTime', () => {
  // TZ-unabhängig: nur das HH:MM-Format prüfen, nie einen festen Wert
  // (toLocaleTimeString hängt von der Laufzeit-Zeitzone ab).
  it('ISO-8601 mit Z → HH:MM-Format', () => {
    expect(fmtClockTime('2026-05-30T14:30:00Z')).toMatch(/^\d{2}:\d{2}$/);
  });
  it('epoch-Sekunden → HH:MM-Format', () => {
    expect(fmtClockTime(NOW)).toMatch(/^\d{2}:\d{2}$/);
  });
  it('leerer String → "–"', () => {
    expect(fmtClockTime('')).toBe('–');
  });
  it('unparsebarer Müll → "–"', () => {
    expect(fmtClockTime('not-a-real-date')).toBe('–');
  });
});

describe('fmtClock (Design-System-Format bleibt DD/MM/YYYY, HH:mm)', () => {
  // Regression: fmtClock NICHT mit fmtClockTime verwechseln — AutoresearchView
  // hängt am vollen Datum-Zeit-Format.
  it('epoch-Sekunden → "DD/MM/YYYY, HH:mm"', () => {
    expect(fmtClock(NOW)).toMatch(/^\d{2}\/\d{2}\/\d{4}, \d{2}:\d{2}$/);
  });
});

describe('buildOpenClawAlerts', () => {
  // buildOpenClawAlerts liest nur id/status/stuckSignal — minimal valide
  // AgentLive-Objekte genügen (Rest via Cast, nicht relevant für die Logik).
  function mkAgent(over: { id: string; status: AgentLive['status']; stuckSignal: boolean }): AgentLive {
    return over as unknown as AgentLive;
  }

  // (1) leeres Array → alle Counts 0
  it('leeres Array → alle Counts 0', () => {
    const result = buildOpenClawAlerts([]);
    expect(result.critical).toHaveLength(0);
    expect(result.criticalCount).toBe(0);
    expect(result.warning).toHaveLength(0);
    expect(result.warningCount).toBe(0);
  });

  // (2) ein Agent mit stuckSignal=true → in critical, criticalCount=1
  it('ein Agent mit stuckSignal=true → in critical, criticalCount=1', () => {
    const result = buildOpenClawAlerts([mkAgent({ id: 'a1', status: 'active', stuckSignal: true })]);
    expect(result.critical).toHaveLength(1);
    expect(result.criticalCount).toBe(1);
    expect(result.critical[0].id).toBe('a1');
    expect(result.warning).toHaveLength(0);
    expect(result.warningCount).toBe(0);
  });

  // (3) ein Agent status='offline' ohne stuckSignal → in warning, warningCount=1
  it("ein Agent status='offline' ohne stuckSignal → in warning, warningCount=1", () => {
    const result = buildOpenClawAlerts([mkAgent({ id: 'a2', status: 'offline', stuckSignal: false })]);
    expect(result.critical).toHaveLength(0);
    expect(result.criticalCount).toBe(0);
    expect(result.warning).toHaveLength(1);
    expect(result.warningCount).toBe(1);
    expect(result.warning[0].id).toBe('a2');
  });

  // (4) Agent mit BEIDEM (stuckSignal=true UND status='offline') → NUR critical, nicht doppelt
  it('Agent mit stuckSignal UND offline → zählt NUR als critical, nicht doppelt', () => {
    const result = buildOpenClawAlerts([mkAgent({ id: 'a3', status: 'offline', stuckSignal: true })]);
    expect(result.critical).toHaveLength(1);
    expect(result.criticalCount).toBe(1);
    expect(result.critical[0].id).toBe('a3');
    expect(result.warning).toHaveLength(0);
    expect(result.warningCount).toBe(0);
  });

  // (5) gemischte Liste → korrekte Aufteilung
  it('gemischte Liste → korrekte Aufteilung', () => {
    const result = buildOpenClawAlerts([
      mkAgent({ id: 'online-stuck',  status: 'active',  stuckSignal: true  }), // critical
      mkAgent({ id: 'offline-stuck', status: 'offline', stuckSignal: true  }), // nur critical
      mkAgent({ id: 'offline-clean', status: 'offline', stuckSignal: false }), // nur warning
      mkAgent({ id: 'online-clean',  status: 'active',  stuckSignal: false }), // irrelevant
    ]);
    expect(result.critical).toHaveLength(2);
    expect(result.criticalCount).toBe(2);
    expect(result.critical.map((a) => a.id)).toContain('online-stuck');
    expect(result.critical.map((a) => a.id)).toContain('offline-stuck');
    expect(result.warning).toHaveLength(1);
    expect(result.warningCount).toBe(1);
    expect(result.warning[0].id).toBe('offline-clean');
  });
});
