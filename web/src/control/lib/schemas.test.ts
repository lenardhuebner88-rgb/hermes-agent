import { describe, expect, it } from "vitest";
import { ProposalsResponseSchema, RecentResultsResponseSchema, WorkersResponseSchema, parseOrThrow } from "./schemas";

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
