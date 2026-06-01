import { describe, expect, it } from "vitest";
import { AgentLiveSchema, ProposalsResponseSchema, RecentResultsResponseSchema, SystemHealthResponseSchema, WorkersResponseSchema, parseOrThrow } from "./schemas";

describe("WorkersResponseSchema", () => {
  it("coerces a numeric run_id so a real worker is not dropped", () => {
    // The backend sends run_id as an integer. Regression: z.string() rejected
    // it, the array .catch([]) emptied the list, and the UI showed 0 cards
    // while count > 0.
    const raw = {
      count: 1,
      checked_at: 1,
      workers: [{
        run_id: 380, task_id: "t_abc", task_title: "w", task_status: "running",
        task_assignee: "coder", profile: "coder", worker_pid: 123, started_at: 1,
        claim_lock: "l", claim_expires: 2, last_heartbeat_at: 1, max_runtime_seconds: 3600,
        run_status: "blocked", run_outcome: null, block_reason: null,
      }],
    };
    const parsed = parseOrThrow(WorkersResponseSchema, raw, "workers/active");
    expect(parsed.workers).toHaveLength(1);
    expect(parsed.workers[0].run_id).toBe("380");
    expect(parsed.workers[0].run_status).toBe("blocked");
  });
});

describe("ProposalsResponseSchema (A3 code gate)", () => {
  it("accepts a code proposal in the 'testing' state with its gate", () => {
    const raw = {
      count: 1, open_count: 0,
      proposals: [{
        id: "code-foo", target: "agent/foo.py", section: null,
        rationale_plain: "weil", diff_before_after: "- x = 1\n+ x = 2",
        mode: "code", status: "testing", result: "Test-Suite läuft …",
        gate: { phase: "running", started_at: "2026-05-29T00:00:00Z", returncode: null },
      }],
    };
    const parsed = parseOrThrow(ProposalsResponseSchema, raw, "proposals");
    expect(parsed.proposals[0].status).toBe("testing");
    expect(parsed.proposals[0].gate?.phase).toBe("running");
  });

  it("tolerates a missing gate and an unknown gate phase", () => {
    const raw = {
      count: 1, open_count: 1,
      proposals: [{
        id: "code-bar", target: "agent/bar.py", section: null,
        rationale_plain: "r", diff_before_after: "",
        mode: "code", status: "proposed",
        gate: { phase: "weird-future-phase" },
      }],
    };
    const parsed = parseOrThrow(ProposalsResponseSchema, raw, "proposals");
    expect(parsed.proposals[0].gate?.phase).toBe("running"); // .catch fallback
  });
});


describe("ProposalsResponseSchema (Track-C category/evidence)", () => {
  it("preserves category and verbatim evidence on a proposal", () => {
    const raw = {
      count: 1, open_count: 1,
      proposals: [{
        id: "p-cat", target: "agent/foo.py", section: null,
        rationale_plain: "weil", diff_before_after: "- a\n+ b",
        category: "Sicherheit", evidence: "Zeile 42: secret im Log",
        rank_score: 0.87,
        mode: "code", status: "proposed",
      }],
    };
    const parsed = parseOrThrow(ProposalsResponseSchema, raw, "proposals");
    expect(parsed.proposals[0].category).toBe("Sicherheit");
    expect(parsed.proposals[0].evidence).toBe("Zeile 42: secret im Log");
    expect(parsed.proposals[0].rank_score).toBe(0.87);
  });

  it("coerces a numeric-string rank_score and tolerates a missing category/evidence", () => {
    const raw = {
      count: 1, open_count: 1,
      proposals: [{
        id: "p-min", target: "skill", section: null,
        rationale_plain: "r", diff_before_after: "",
        rank_score: "0.5",
        mode: "skill", status: "proposed",
      }],
    };
    const parsed = parseOrThrow(ProposalsResponseSchema, raw, "proposals");
    expect(parsed.proposals[0].rank_score).toBe(0.5);
    expect(parsed.proposals[0].category ?? null).toBeNull();
    expect(parsed.proposals[0].evidence ?? null).toBeNull();
  });

  it("falls back to null when the backend sends a non-string category/evidence (catch contract)", () => {
    const raw = {
      count: 1, open_count: 1,
      proposals: [{
        id: "p-bad", target: "skill", section: null,
        rationale_plain: "r", diff_before_after: "",
        category: 123, evidence: { not: "a string" }, rank_score: "not-a-number",
        mode: "skill", status: "proposed",
      }],
    };
    const parsed = parseOrThrow(ProposalsResponseSchema, raw, "proposals");
    expect(parsed.proposals[0].category).toBeNull();
    expect(parsed.proposals[0].evidence).toBeNull();
    expect(parsed.proposals[0].rank_score).toBeNull();
  });
});

describe("ProposalsResponseSchema (Sprint A last_outcome counts)", () => {
  it("preserves last_outcome and the backend status split", () => {
    const raw = {
      count: 5, open_count: 1, reverted_count: 4, testing_count: 0, applied_count: 0, skipped_count: 0,
      proposals: [{
        id: "reverted", target: "skill", section: null, rationale_plain: "r", diff_before_after: "",
        mode: "skill", status: "proposed", last_outcome: "reverted_no_improvement",
      }],
    };
    const parsed = parseOrThrow(ProposalsResponseSchema, raw, "proposals");
    expect(parsed.reverted_count).toBe(4);
    expect(parsed.proposals[0].last_outcome).toBe("reverted_no_improvement");
  });
});


describe("RecentResultsResponseSchema", () => {
  it("coerces numeric run ids and counters without dropping real results", () => {
    const parsed = parseOrThrow(RecentResultsResponseSchema, {
      count: "1",
      checked_at: "100",
      results: [{
        run_id: 408,
        task_id: "t_1",
        task_title: "Visible result",
        task_assignee: "coder",
        profile: "coder",
        status: "done",
        outcome: "completed",
        started_at: "10",
        ended_at: "25",
        duration_seconds: "15",
        summary: "line one",
        summary_preview: "line one",
        followups: ["check artifact"],
        artifacts: ["/tmp/receipt.md"],
        verification: ["pytest"],
      }],
    }, "recent-results");
    expect(parsed.count).toBe(1);
    expect(parsed.results[0].run_id).toBe("408");
    expect(parsed.results[0].duration_seconds).toBe(15);
  });
});

describe("SystemHealthResponseSchema", () => {
  it("coerces checked_at and applies catch defaults for partial bad payloads", () => {
    const parsed = parseOrThrow(SystemHealthResponseSchema, {
      schema: 42,
      checked_at: "123",
      subsystems: {
        gateway: { status: "healthy", detail: "ok", error: null, latency_ms: "8" },
        autoresearch: { status: "degraded", detail: "late", error: null, heartbeat_age_s: "12" },
        kanban_db: null,
      },
    }, "health-status");
    expect(parsed.schema).toBe("hermes-health-v1");
    expect(parsed.checked_at).toBe(123);
    expect(parsed.overall).toBe("offline");
    expect(parsed.subsystems.gateway.latency_ms).toBe(8);
    expect(parsed.subsystems.autoresearch.heartbeat_age_s).toBe(12);
    expect(parsed.subsystems.kanban_db.status).toBe("offline");
  });
});

describe("AgentLiveSchema (dd drilldown)", () => {
  const baseAgent = {
    id: "spark",
    name: "Spark",
    emoji: "✨",
    status: "active",
    model: "m",
    lastActive: 1_780_041_720,
    tasks: { queued: [], active: [], review: [], recentDone: [] },
    stuckSignal: false,
    activityPulse: 1,
    fleetHealth: { currentTask: "", heartbeat: null, throughput: "0/h", currentTool: "-", lastOutput: "" },
    roleLabel: "Agent",
    roleSummary: "OpenClaw-Agent",
    escalationNote: null,
  };

  it("parst einen Agent MIT drilldown verlustfrei", () => {
    const raw = {
      ...baseAgent,
      drilldown: {
        decisions: [{ id: "d1", label: "Entscheidung A", detail: "weil X" }],
        artifacts: [{ label: "Report", value: "/tmp/r.md", source: "run 408" }],
        timeline: [{ id: "t1", at: "2026-05-30T14:30:00Z", kind: "tool", label: "lief", detail: "ok" }],
        highlights: ["Highlight eins", "Highlight zwei"],
        sources: ["https://example.com/1", "https://example.com/2"],
      },
    };
    const parsed = parseOrThrow(AgentLiveSchema, raw, "agent");
    expect(parsed.drilldown).toBeDefined();
    expect(parsed.drilldown!.decisions).toEqual([{ id: "d1", label: "Entscheidung A", detail: "weil X" }]);
    expect(parsed.drilldown!.artifacts).toEqual([{ label: "Report", value: "/tmp/r.md", source: "run 408" }]);
    expect(parsed.drilldown!.timeline).toEqual([{ id: "t1", at: "2026-05-30T14:30:00Z", kind: "tool", label: "lief", detail: "ok" }]);
    expect(parsed.drilldown!.highlights).toEqual(["Highlight eins", "Highlight zwei"]);
    expect(parsed.drilldown!.sources).toEqual(["https://example.com/1", "https://example.com/2"]);
  });

  it("parst einen Agent OHNE drilldown → drilldown undefined, kein Throw", () => {
    const parsed = parseOrThrow(AgentLiveSchema, baseAgent, "agent");
    expect(parsed.drilldown).toBeUndefined();
    expect(parsed.id).toBe("spark");
  });
});
