/**
 * Unit-Tests für die Ableitungslogik (vitest).
 * `npx vitest run` — diese Tests pinnen die Gesundheits-Schwellen & Formatierung,
 * damit ein Refactor sie nicht versehentlich verschiebt.
 */
import { describe, it, expect } from 'vitest';
import {
  workerHealth, buildOverview, fmtAge, fmtDur, fmtMB, STUCK_HEARTBEAT_S,
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
