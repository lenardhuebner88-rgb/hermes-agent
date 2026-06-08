import { describe, it, expect } from "vitest";
import { buildPulse, dayKey, groupPulseByDay, proposalToEvent, summarizePulse, toEpochSec } from "./pulse";
import type { CronJob, KanbanResult, Proposal } from "./types";

const NOW = Math.floor(Date.parse("2026-06-06T12:00:00Z") / 1000);
const HOUR = 3600;

function result(over: Partial<KanbanResult> & { run_id: string; ended_at: number }): KanbanResult {
  const { run_role, run_role_label, run_role_source, ...rest } = over;
  return {
    task_id: `t-${over.run_id}`,
    task_title: `Task ${over.run_id}`,
    task_status: "done",
    task_assignee: "claude",
    profile: "coder",
    run_role: run_role ?? "implementation",
    run_role_label: run_role_label ?? "Implementation / coder run",
    run_role_source: run_role_source ?? "claimed_event",
    status: "done",
    outcome: "completed",
    started_at: over.ended_at - 60,
    duration_seconds: 60,
    summary: "did the thing",
    summary_preview: "did the thing",
    followups: [],
    artifacts: [],
    verification: [],
    result_quality: {
      state: "ungated",
      label: "Ungated",
      tone: "amber",
      description: "Completed without an independent verifier gate.",
    },
    ...rest,
  };
}

function proposal(over: Partial<Proposal> & { id: string }): Proposal {
  return {
    target: "skills/x.md",
    section: null,
    rationale_plain: "weil",
    diff_before_after: "",
    mode: "skill",
    status: "proposed",
    ...over,
  };
}

function cron(over: Partial<CronJob> & { id: string }): CronJob {
  return {
    name: `Job ${over.id}`,
    enabled: true,
    state: "scheduled",
    paused_at: null,
    paused_reason: null,
    schedule_display: "daily",
    repeat: null,
    next_run_at: null,
    last_run_at: null,
    last_status: "ok",
    last_error: null,
    last_delivery_error: null,
    deliver: "discord",
    skill: null,
    model: null,
    profile: "default",
    is_default_profile: true,
    has_script: false,
    has_prompt: true,
    latest_output: null,
    ...over,
  };
}

describe("toEpochSec", () => {
  it("passes through positive epoch seconds", () => {
    expect(toEpochSec(NOW)).toBe(NOW);
  });
  it("parses ISO strings to seconds", () => {
    expect(toEpochSec("2026-06-06T12:00:00Z")).toBe(NOW);
  });
  it("rejects null, zero and garbage", () => {
    expect(toEpochSec(null)).toBeNull();
    expect(toEpochSec(0)).toBeNull();
    expect(toEpochSec("not-a-date")).toBeNull();
  });
});

describe("proposalToEvent", () => {
  it("treats reverted_no_improvement as its own kind, before status", () => {
    const e = proposalToEvent(proposal({ id: "p1", status: "applied", last_outcome: "reverted_no_improvement", applied_at: NOW }));
    expect(e?.kind).toBe("reverted");
  });
  it("maps applied / skipped, ignores still-open proposals", () => {
    expect(proposalToEvent(proposal({ id: "a", status: "applied", applied_at: NOW }))?.kind).toBe("applied");
    expect(proposalToEvent(proposal({ id: "s", status: "skipped", applied_at: NOW }))?.kind).toBe("skipped");
    expect(proposalToEvent(proposal({ id: "o", status: "proposed" }))).toBeNull();
    expect(proposalToEvent(proposal({ id: "t", status: "testing" }))).toBeNull();
  });
  it("drops a decided proposal with no usable timestamp", () => {
    expect(proposalToEvent(proposal({ id: "x", status: "applied" }))).toBeNull();
  });
});

describe("buildPulse", () => {
  it("merges all three sources sorted newest first", () => {
    const events = buildPulse({
      results: [result({ run_id: "r1", ended_at: NOW - 2 * HOUR })],
      proposals: [proposal({ id: "p1", status: "applied", applied_at: NOW - 1 * HOUR })],
      crons: [cron({ id: "c1", last_run_at: NOW - 30 })],
      nowSec: NOW,
    });
    expect(events.map((e) => e.kind)).toEqual(["cron-ok", "applied", "run"]);
  });

  it("flags cron failures via status or delivery error", () => {
    const [a, b] = buildPulse({
      results: [], proposals: [],
      crons: [cron({ id: "ok", last_run_at: NOW - 10 }), cron({ id: "bad", last_run_at: NOW - 5, last_status: "error", last_error: "boom" })],
      nowSec: NOW,
    });
    expect(b.kind).toBe("cron-ok");
    expect(a.kind).toBe("cron-error");
    expect(a.detail).toBe("boom");
  });

  it("honours sinceSec and tolerates small future skew", () => {
    const events = buildPulse({
      results: [result({ run_id: "old", ended_at: NOW - 100 * HOUR }), result({ run_id: "skew", ended_at: NOW + 60 })],
      proposals: [], crons: [],
      sinceSec: NOW - 48 * HOUR,
      nowSec: NOW,
    });
    expect(events.map((e) => e.id)).toEqual(["run:skew"]);
  });
});

describe("summarizePulse", () => {
  it("counts per kind, cron-error also counts as a cron run", () => {
    const events = buildPulse({
      results: [result({ run_id: "r1", ended_at: NOW - HOUR })],
      proposals: [
        proposal({ id: "p1", status: "applied", applied_at: NOW - HOUR }),
        proposal({ id: "p2", status: "applied", last_outcome: "reverted_no_improvement", applied_at: NOW - HOUR }),
      ],
      crons: [cron({ id: "c1", last_run_at: NOW - 60, last_status: "error", last_error: "x" })],
      nowSec: NOW,
    });
    const s = summarizePulse(events);
    expect(s).toMatchObject({ runs: 1, applied: 1, reverted: 1, crons: 1, cronErrors: 1, total: 4 });
  });
});

describe("groupPulseByDay", () => {
  it("buckets by local day, newest day first, with daysAgo", () => {
    const events = buildPulse({
      results: [
        result({ run_id: "today", ended_at: NOW }),
        result({ run_id: "yesterday", ended_at: NOW - 24 * HOUR }),
      ],
      proposals: [], crons: [],
      nowSec: NOW,
    });
    const days = groupPulseByDay(events, NOW);
    expect(days.map((d) => d.daysAgo)).toEqual([0, 1]);
    expect(days[0].events).toHaveLength(1);
    expect(days[0].key).toBe(dayKey(NOW));
  });
});
