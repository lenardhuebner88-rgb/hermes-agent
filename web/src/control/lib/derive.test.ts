/**
 * Unit-Tests für die Ableitungslogik (vitest).
 * `npx vitest run` — diese Tests pinnen die Gesundheits-Schwellen & Formatierung,
 * damit ein Refactor sie nicht versehentlich verschiebt.
 */
import { describe, it, expect } from 'vitest';
import {
  workerHealth, buildOverview, fmtAge, fmtDur, fmtMB, freshness, fmtClock, fmtClockTime, STUCK_HEARTBEAT_S,
  elapsedSeconds, formatEffectiveCost, fmtRelativeTime, inspectEpochSeconds, validateChronology,
} from './derive';
import type { Worker, Proposal } from './types';

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
  it('benennt festhängende Worker deutsch', () => {
    const w = mkWorker({ last_heartbeat_at: NOW - (STUCK_HEARTBEAT_S + 1) });
    expect(workerHealth(w, NOW).label).toBe('Hängt');
  });
  it('stuck bei abgelaufenem claim_expires (trotz frischem Heartbeat)', () => {
    expect(workerHealth(mkWorker({ claim_expires: NOW - 1 }), NOW).key).toBe('stuck');
  });
  it('stuck bei millisekundenförmigem claim_expires statt plausibel gesund', () => {
    expect(workerHealth(mkWorker({ claim_expires: NOW * 1000 }), NOW).key).toBe('stuck');
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
  const proposals: Proposal[] = [
    { id: 'p1', target: 's', section: '', rationale_plain: '', diff_before_after: "", mode: 'skill', status: 'proposed' },
    { id: 'p2', target: 's', section: '', rationale_plain: '', diff_before_after: "", mode: 'code', status: 'applied' },
  ];

  it('zählt nur actionable Vorschläge und sammelt Worker-Warnungen', () => {
    const mixedProposals = [
      ...proposals,
      { id: 'p3', target: 's', section: '', rationale_plain: '', diff_before_after: "", mode: 'skill', status: 'proposed', last_outcome: 'reverted_no_improvement' },
    ] satisfies Proposal[];
    const o = buildOverview([mkWorker(), mkWorker({ run_status: 'blocked' })], [], mixedProposals, NOW);
    expect(o.openProposals).toBe(1);
    expect(o.ocActive).toBe(0);
    expect(o.ocHealthy).toBe(0);
    expect(o.warnings.length).toBe(1);
    expect(o.allHealthy).toBe(false);
  });

  it('allHealthy=true wenn nichts auffällt', () => {
    const o = buildOverview([mkWorker()], [], [], NOW);
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
  it.each([null, undefined, 0, -1, Number.NaN, Number.POSITIVE_INFINITY, NOW * 1000, 'bad'])(
    'rejects invalid epoch-seconds input %s', (value) => {
    expect(inspectEpochSeconds(value, NOW).valid).toBe(false);
    expect(fmtClock(value)).toBe('Zeit ungültig');
    expect(fmtRelativeTime(value, NOW)).toBe('Zeit ungültig');
    },
  );
  it('keeps old seconds valid and labels future seconds truthfully', () => {
    expect(fmtAge(NOW - 400 * 86400, NOW)).toBe('400d');
    expect(fmtAge(NOW + 86400, NOW)).toBe('in 1d');
    expect(fmtRelativeTime(NOW - 5, NOW)).toBe('vor 5s');
    expect(fmtRelativeTime(NOW + 86400, NOW)).toBe('in 1d');
    expect(elapsedSeconds(NOW + 86400, NOW)).toBeNull();
  });
  it('never makes invalid or negative durations look like zero seconds', () => {
    expect(fmtDur(-1)).toBe('Dauer ungültig');
    expect(fmtDur(Number.NaN)).toBe('Dauer ungültig');
    expect(fmtDur(Number.POSITIVE_INFINITY)).toBe('Dauer ungültig');
  });
  it('renders Europe/Berlin clocks correctly across both DST boundaries', () => {
    expect(fmtClock(Date.parse('2026-03-29T00:30:00Z') / 1000)).toContain('01:30');
    expect(fmtClock(Date.parse('2026-03-29T01:30:00Z') / 1000)).toContain('03:30');
    expect(fmtClock(Date.parse('2026-10-25T00:30:00Z') / 1000)).toContain('02:30');
    expect(fmtClock(Date.parse('2026-10-25T01:30:00Z') / 1000)).toContain('02:30');
  });
  it('rejects impossible task chronology instead of absolving it with Math.abs/clamping', () => {
    expect(validateChronology({ createdAt: NOW - 20, startedAt: NOW - 10, completedAt: NOW })).toEqual({ valid: true, reason: null });
    expect(validateChronology({ createdAt: NOW - 20, startedAt: NOW - 10, completedAt: NOW - 15 })).toEqual({
      valid: false,
      reason: 'Abschluss liegt vor Start',
    });
    expect(validateChronology({ createdAt: NOW - 10, startedAt: NOW - 20, completedAt: null })).toEqual({
      valid: false,
      reason: 'Start liegt vor Anlage',
    });
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

// ── Kosten-Anzeige (formatEffectiveCost) ──────────────────────────────────
describe('formatEffectiveCost', () => {
  it('real: cost_effective > 0 AND cost_usd > 0 → "$X.XX", estimated=false', () => {
    const r = formatEffectiveCost({ cost_usd: 0.50, cost_effective_usd: 0.50, tokens: 1000 });
    expect(r.text).toBe('$0.50');
    expect(r.estimated).toBe(false);
  });

  it('Abo-Schätzwert: cost_effective > 0 AND cost_usd === 0 → "$X.XX gesch.", estimated=true', () => {
    const r = formatEffectiveCost({ cost_usd: 0, cost_effective_usd: 1.23, tokens: 50000 });
    expect(r.text).toBe('$1.23 gesch.');
    expect(r.estimated).toBe(true);
  });

  it('kein Schätzwert aber Tokens vorhanden → "—", estimated=false', () => {
    const r = formatEffectiveCost({ cost_usd: 0, cost_effective_usd: 0, tokens: 5000 });
    expect(r.text).toBe('—');
    expect(r.estimated).toBe(false);
  });

  it('alles 0 → "—", estimated=false', () => {
    const r = formatEffectiveCost({ cost_usd: 0, cost_effective_usd: 0, tokens: 0 });
    expect(r.text).toBe('—');
    expect(r.estimated).toBe(false);
  });

  it('formatiert auf 2 Dezimalstellen', () => {
    const r = formatEffectiveCost({ cost_usd: 0, cost_effective_usd: 0.005, tokens: 100 });
    expect(r.text).toBe('$0.01 gesch.');
    expect(r.estimated).toBe(true);
  });
});

// ── Zeit-Achsen-Zustand (Phase B) ────────────────────────────────────────
import { workerTimeAxisState, timeAxisScaleMax } from './derive';

describe('workerTimeAxisState', () => {
  const BUDGET = 7200;

  it('im_plan wenn elapsed < p50', () => {
    const r = workerTimeAxisState(300, 600, 900, BUDGET, 10, true);
    expect(r.key).toBe('im_plan');
    expect(r.tone).toBe('emerald');
    expect(r.noEta).toBe(false);
  });

  it('laeuft wenn p50 ≤ elapsed < p90', () => {
    const r = workerTimeAxisState(700, 600, 900, BUDGET, 10, true);
    expect(r.key).toBe('laeuft');
    expect(r.tone).toBe('cyan');
  });

  it('langsamer wenn elapsed ≥ p90', () => {
    const r = workerTimeAxisState(950, 600, 900, BUDGET, 10, true);
    expect(r.key).toBe('langsamer');
    expect(r.tone).toBe('amber');
  });

  it('steht überschreibt im_plan wenn Heartbeat > STUCK_HEARTBEAT_S und hasHeartbeat', () => {
    const r = workerTimeAxisState(300, 600, 900, BUDGET, STUCK_HEARTBEAT_S + 1, true);
    expect(r.key).toBe('steht');
    expect(r.tone).toBe('red');
  });

  it('steht greift NICHT ohne Heartbeat (hasHeartbeat=false)', () => {
    // Ohne Heartbeat darf "steht" nicht feuern, auch bei großem heartbeatAge
    const r = workerTimeAxisState(300, 600, 900, BUDGET, STUCK_HEARTBEAT_S + 1, false);
    expect(r.key).toBe('im_plan');
  });

  it('ueber_budget wenn elapsed > budget', () => {
    const r = workerTimeAxisState(7300, 600, 900, 7200, 10, true);
    expect(r.key).toBe('ueber_budget');
    expect(r.tone).toBe('red');
  });

  it('steht schlägt ueber_budget (stuck hat Vorrang)', () => {
    const r = workerTimeAxisState(7300, 600, 900, 7200, STUCK_HEARTBEAT_S + 1, true);
    expect(r.key).toBe('steht');
  });

  it('no_eta wenn p50=null', () => {
    const r = workerTimeAxisState(300, null, null, BUDGET, 10, true);
    expect(r.key).toBe('no_eta');
    expect(r.noEta).toBe(true);
  });

  it('no_eta wenn p50=0', () => {
    const r = workerTimeAxisState(300, 0, 900, BUDGET, 10, true);
    expect(r.key).toBe('no_eta');
  });

  it('noEta=true auch wenn stuck (wegen fehlender ETA)', () => {
    const r = workerTimeAxisState(300, null, null, BUDGET, STUCK_HEARTBEAT_S + 1, true);
    expect(r.key).toBe('steht');
    expect(r.noEta).toBe(true);
  });
});

describe('timeAxisScaleMax', () => {
  it('mindestens elapsed*1.1 wenn kein p90 und kein Budget', () => {
    expect(timeAxisScaleMax(100, null, 0)).toBeCloseTo(110);
  });

  it('max(budget, p90*1.2, elapsed*1.1)', () => {
    // budget=7200, p90=900 → p90*1.2=1080, elapsed*1.1=330 → max=7200
    expect(timeAxisScaleMax(300, 900, 7200)).toBe(7200);
  });

  it('p90*1.2 wenn größer als budget', () => {
    expect(timeAxisScaleMax(300, 9000, 7200)).toBe(10800);
  });
});

// ── F2: Burn-Wächter ─────────────────────────────────────────────────────
import { workerBurnRate } from './derive';

describe('workerBurnRate', () => {
  it('noData wenn keine Tokens vorhanden', () => {
    const r = workerBurnRate(null, null, 600);
    expect(r.noData).toBe(true);
    expect(r.ratePerMin).toBeNull();
    expect(r.projectedTotal).toBeNull();
  });

  it('noData wenn elapsed=0', () => {
    const r = workerBurnRate(1000, 500, 0);
    expect(r.noData).toBe(false); // Tokens vorhanden
    expect(r.ratePerMin).toBeNull(); // aber kein elapsed
    expect(r.projectedTotal).toBeNull();
  });

  it('berechnet Rate aus In+Out über elapsed', () => {
    // 1500 Tokens in 300s (5 Min) = 300 Tok/min
    const r = workerBurnRate(1000, 500, 300);
    expect(r.noData).toBe(false);
    expect(r.ratePerMin).toBeCloseTo(300, 1);
    expect(r.projectedTotal).toBeNull(); // kein ETA
  });

  it('berechnet Hochrechnung wenn p50 bekannt', () => {
    // 300 Tok/min × 10 Min (600s p50) = 3000
    const r = workerBurnRate(1000, 500, 300, 600);
    expect(r.ratePerMin).toBeCloseTo(300, 1);
    expect(r.projectedTotal).toBeCloseTo(3000, 0);
  });

  it('nutzt Budget als Hochrechnungs-Horizont wenn kein p50', () => {
    // 300 Tok/min × 20 Min (1200s Budget) = 6000
    const r = workerBurnRate(1000, 500, 300, null, 1200);
    expect(r.projectedTotal).toBeCloseTo(6000, 0);
  });

  it('p50 hat Vorrang vor Budget für Hochrechnung', () => {
    // p50=600s (300 Tok/min × 10min=3000), Budget=1200 wäre 6000 — p50 gewinnt
    const r = workerBurnRate(1000, 500, 300, 600, 1200);
    expect(r.projectedTotal).toBeCloseTo(3000, 0);
  });

  it('funktioniert wenn nur outputTokens gesetzt (input=null)', () => {
    // 600 out in 60s = 600 Tok/min
    const r = workerBurnRate(null, 600, 60);
    expect(r.noData).toBe(false);
    expect(r.ratePerMin).toBeCloseTo(600, 1);
  });

  it('keine Hochrechnung wenn p50=0', () => {
    const r = workerBurnRate(1000, 500, 300, 0);
    expect(r.projectedTotal).toBeNull();
  });
});

// ── Runaway-Erkennung (Phase 2, Operator-Vertrag 2026-06-10) ───────────────
import { workerRunaway, RUNAWAY_RUNTIME_WARN_PCT } from './derive';

describe('workerRunaway', () => {
  it('none bei junger Laufzeit + frischem Heartbeat', () => {
    const w = mkWorker({ started_at: NOW - 600, max_runtime_seconds: 7200, last_heartbeat_at: NOW - 10 });
    expect(workerRunaway(w, NOW).level).toBe('none');
  });
  it('warn ab 80 % der max_runtime', () => {
    const w = mkWorker({ started_at: NOW - Math.ceil(7200 * RUNAWAY_RUNTIME_WARN_PCT), max_runtime_seconds: 7200, last_heartbeat_at: NOW - 10 });
    const r = workerRunaway(w, NOW);
    expect(r.level).toBe('warn');
    expect(r.reasons[0]).toMatch(/Laufzeit/);
  });
  it('critical ab 100 % der max_runtime', () => {
    const w = mkWorker({ started_at: NOW - 7200, max_runtime_seconds: 7200, last_heartbeat_at: NOW - 10 });
    expect(workerRunaway(w, NOW).level).toBe('critical');
  });
  it('Heartbeat-Stille: warn > 120 s, critical > 300 s — nur wenn Heartbeats existieren', () => {
    const base = { started_at: NOW - 600, max_runtime_seconds: 7200 };
    expect(workerRunaway(mkWorker({ ...base, last_heartbeat_at: NOW - 150 }), NOW).level).toBe('warn');
    expect(workerRunaway(mkWorker({ ...base, last_heartbeat_at: NOW - 301 }), NOW).level).toBe('critical');
    expect(workerRunaway(mkWorker({ ...base, last_heartbeat_at: 0 }), NOW).level).toBe('none');
  });
  it('kein Laufzeit-Limit (max_runtime_seconds=0) → keine Laufzeit-Regel', () => {
    const w = mkWorker({ started_at: NOW - 99999, max_runtime_seconds: 0, last_heartbeat_at: NOW - 10 });
    const r = workerRunaway(w, NOW);
    expect(r.level).toBe('none');
    expect(r.pct).toBe(0);
  });
});

// ── F4: Kapazitäts-Ableitung (Round C) ───────────────────────────────────────
import { deriveCapacity } from './derive';

describe('deriveCapacity', () => {
  it('kein Engpass wenn count < cap', () => {
    const r = deriveCapacity(2, 3, 5);
    expect(r.bottleneck).toBe(false);
    expect(r.count).toBe(2);
    expect(r.cap).toBe(3);
    expect(r.queueDepth).toBe(5);
  });

  it('Engpass wenn count >= cap UND queue > 0', () => {
    expect(deriveCapacity(3, 3, 2).bottleneck).toBe(true);
    expect(deriveCapacity(4, 3, 1).bottleneck).toBe(true);
  });

  it('kein Engpass wenn cap null (nicht konfiguriert)', () => {
    const r = deriveCapacity(3, null, 10);
    expect(r.bottleneck).toBe(false);
    expect(r.cap).toBeNull();
  });

  it('kein Engpass wenn queue leer, auch bei count >= cap', () => {
    expect(deriveCapacity(3, 3, 0).bottleneck).toBe(false);
  });

  it('count=0, cap=3, queue=0 → kein Engpass', () => {
    expect(deriveCapacity(0, 3, 0).bottleneck).toBe(false);
  });
});
