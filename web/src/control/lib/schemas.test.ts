import { describe, expect, it } from "vitest";
import { BacklogDetailSchema, BacklogResponseSchema, BlockedCompletionsResponseSchema, ChainCostsResponseSchema, CronObservabilityResponseSchema, DecisionQueueResponseSchema, FlowReleaseResponseSchema, MetricsLiteResponseSchema, OperatorInventoryResponseSchema, OrchestrationBacklogResponseSchema, PressureStatusResponseSchema, ProposalsResponseSchema, RecentResultsResponseSchema, RunsCostsResponseSchema, SystemHealthResponseSchema, TaskDetailResponseSchema, TodayDigestResponseSchema, WindowedRollupResponseSchema, WorkersResponseSchema, parseOrThrow } from "./schemas";

describe("FlowReleaseResponseSchema", () => {
  it("preserves the release contract ok flag and count", () => {
    const parsed = parseOrThrow(FlowReleaseResponseSchema, {
      ok: true,
      task_id: "t_root",
      released: 2,
      released_ids: ["t_a", "t_b"],
      release_level: "merge",
      assignee_overrides: { t_a: "reviewer" },
    }, "flow-release");

    expect(parsed.ok).toBe(true);
    expect(parsed.released).toBe(2);
    expect(parsed.released_ids).toEqual(["t_a", "t_b"]);
  });
});

describe("Cost schemas", () => {
  it("keeps actual USD, API-equivalent USD and Neuralwatt billing basis separate for runs costs", () => {
    const parsed = parseOrThrow(RunsCostsResponseSchema, {
      days: 7,
      now: 100,
      today: {
        runs: 2,
        cost_usd: 0.25,
        actual_cost_usd: 0.35,
        cost_usd_equivalent: 1.5,
        api_equivalent_usd: 1.5,
        billing_neuralwatt_kwh: 0.02,
        billing_neuralwatt_cost_usd: 0.1,
        input_tokens: 1600,
        output_tokens: 320,
      },
      window: { runs: 0 },
      profiles: [{
        profile: "neuralwatt",
        runs: 1,
        cost_usd: 0,
        actual_cost_usd: 0.1,
        cost_usd_equivalent: 0,
        api_equivalent_usd: 0,
        billing_neuralwatt_kwh: 0.02,
        billing_neuralwatt_cost_usd: 0.1,
        input_tokens: 600,
        output_tokens: 120,
      }],
    }, "runs-costs");

    expect(parsed.today.actual_cost_usd).toBeCloseTo(0.35);
    expect(parsed.today.api_equivalent_usd).toBeCloseTo(1.5);
    expect(parsed.today.billing_neuralwatt_kwh).toBeCloseTo(0.02);
    expect(parsed.today.billing_neuralwatt_cost_usd).toBeCloseTo(0.1);
    expect(parsed.profiles[0].actual_cost_usd).toBeCloseTo(0.1);
  });

  it("preserves Neuralwatt cost basis on chain cost lanes and totals", () => {
    const parsed = parseOrThrow(ChainCostsResponseSchema, {
      schema: "kanban-chain-costs-v1",
      root_id: "t_root",
      totals: {
        input_tokens: 100,
        output_tokens: 20,
        cost_usd: 0,
        actual_cost_usd: 0.15,
        cost_usd_equivalent: 0.9,
        api_equivalent_usd: 0.9,
        cost_effective_usd: 0.9,
        billing_neuralwatt_kwh: 0.04,
        billing_neuralwatt_cost_usd: 0.15,
        run_count: 1,
      },
      by_lane: [{
        profile: "neuralwatt",
        input_tokens: 100,
        output_tokens: 20,
        cost_usd: 0,
        actual_cost_usd: 0.15,
        cost_usd_equivalent: 0.9,
        api_equivalent_usd: 0.9,
        cost_effective_usd: 0.9,
        billing_neuralwatt_kwh: 0.04,
        billing_neuralwatt_cost_usd: 0.15,
        run_count: 1,
      }],
    }, "chain-costs");

    expect(parsed.totals.actual_cost_usd).toBeCloseTo(0.15);
    expect(parsed.totals.api_equivalent_usd).toBeCloseTo(0.9);
    expect(parsed.by_lane[0].billing_neuralwatt_kwh).toBeCloseTo(0.04);
    expect(parsed.by_lane[0].billing_neuralwatt_cost_usd).toBeCloseTo(0.15);
  });

  it("preserves windowed rollup detail fields for S3 tooltips", () => {
    const parsed = parseOrThrow(WindowedRollupResponseSchema, {
      schema: "kanban-windowed-rollup-v1",
      since_hours: 24,
      now: 1000,
      completed_roots: 1,
      roots: [{
        id: "t_root",
        title: "Mother",
        status: "done",
        assignee: "coder",
        created_at: 800,
        started_at: 900,
        completed_at: 960,
        ended_at: 960,
        providers: ["openrouter"],
        cost_usd: 0.12,
        cost_usd_equivalent: 0.75,
        cost_effective_usd: 0.87,
        billing_mode: "metered",
        neuralwatt: null,
        runtime_seconds: 60,
        workers: [],
        runners: [{
          id: 1,
          task_id: "t_root",
          profile: "coder",
          provider: "openrouter",
          model: "nous/hermes",
          input_tokens: 1,
          output_tokens: 2,
          cost_usd: 0.12,
          cost_usd_equivalent: 0.75,
          cost_effective_usd: 0.87,
          billing_mode: "metered",
          neuralwatt: null,
          started_at: 900,
          ended_at: 960,
          runtime_seconds: 60,
        }],
      }],
    }, "windowed-rollup");

    expect(parsed.roots[0].billing_mode).toBe("metered");
    expect(parsed.roots[0].runtime_seconds).toBe(60);
    expect(parsed.roots[0].runners[0].billing_mode).toBe("metered");
    expect(parsed.roots[0].runners[0].runtime_seconds).toBe(60);
  });
});

describe("BacklogResponseSchema (Family Organizer contract health)", () => {
  it("preserves unknown status/risk values and parses contract-health drift", () => {
    const parsed = parseOrThrow(BacklogResponseSchema, {
      schema: "fo-backlog-v1",
      checked_at: 1770000000,
      items: [{
        id: "0042",
        title: "Contract drift",
        status: "readyish",
        owner: "nobody",
        risk: "urgent",
        area: "lists",
        updated: "2026-06-01",
        stale: false,
        source_path: "backlog/items/0042-contract-drift.md",
      }],
      counts: { now: 0, next: 0, in_progress: 0, blocked: 0, later: 0, done: 0 },
      contract_health: {
        source_count: 1,
        counted_sum: 0,
        unknown_statuses: [{ status: "readyish", count: 1, ids: ["0042"] }],
        invalid_risk_count: 1,
        invalid_owner_count: 1,
        unowned_count: 0,
        stale_count: 0,
        missing_acceptance_count: 1,
        missing_next_action_count: 1,
      },
      source: { dir: "/home/piet/projects/family-organizer/backlog/items", ref: "git:origin/main", count: 1 },
      error: null,
    }, "family-organizer/backlog");

    expect(parsed.items[0].status).toBe("readyish");
    expect(parsed.items[0].risk).toBe("urgent");
    expect(parsed.items[0].source_path).toBe("backlog/items/0042-contract-drift.md");
    expect(parsed.contract_health?.unknown_statuses[0].ids).toEqual(["0042"]);
    expect(parsed.contract_health?.invalid_risk_count).toBe(1);
  });

  it("parses detail sections for the product-manager drawer", () => {
    const parsed = parseOrThrow(BacklogDetailSchema, {
      id: "0042",
      title: "Detail",
      status: "next",
      owner: "claude",
      risk: "medium",
      area: "lists",
      updated: "2026-06-01",
      stale: false,
      body: "body",
      decision: ["Warum jetzt"],
      acceptance_criteria: ["Kriterium"],
      proofs: ["Commit abc"],
      blockers: ["Keine"],
      next_action: "Starten",
      source_path: "backlog/items/0042-detail.md",
      source_ref: "git:origin/main",
      links: [{ label: "Runbook", href: "https://example.invalid/runbook" }],
    }, "family-organizer/backlog/detail");

    expect(parsed.decision).toEqual(["Warum jetzt"]);
    expect(parsed.acceptance_criteria).toEqual(["Kriterium"]);
    expect(parsed.next_action).toBe("Starten");
    expect(parsed.links[0].label).toBe("Runbook");
  });

  it("parses the v2 per-item facts (age_days/freshness/quality_issues/readiness)", () => {
    const parsed = parseOrThrow(BacklogResponseSchema, {
      schema: "fo-backlog-v2",
      checked_at: 1780000000,
      items: [{
        id: "0090",
        title: "A v2 item",
        status: "next",
        owner: "claude",
        risk: "high",
        area: "db",
        updated: "2026-06-01",
        stale: false,
        age_days: 4,
        freshness: "aging",
        quality_issues: [{ code: "missing_acceptance", severity: "risk" }],
        readiness: "needs_grooming",
      }],
      counts: { now: 0, next: 1, in_progress: 0, blocked: 0, later: 0, done: 0 },
      source: { dir: "/x", ref: "git:origin/main", count: 1 },
      error: null,
    }, "family-organizer/backlog");

    expect(parsed.schema).toBe("fo-backlog-v2");
    expect(parsed.items[0].age_days).toBe(4);
    expect(parsed.items[0].freshness).toBe("aging");
    expect(parsed.items[0].quality_issues).toEqual([{ code: "missing_acceptance", severity: "risk" }]);
    expect(parsed.items[0].readiness).toBe("needs_grooming");
  });

  it("stays back-compatible with a v1 payload (new fields absent → undefined)", () => {
    const parsed = parseOrThrow(BacklogResponseSchema, {
      schema: "fo-backlog-v1",
      checked_at: 1770000000,
      items: [{
        id: "0001",
        title: "A v1 item",
        status: "next",
        owner: "claude",
        risk: "low",
        area: "lists",
        updated: "2026-06-01",
        stale: false,
      }],
      counts: { now: 0, next: 1, in_progress: 0, blocked: 0, later: 0, done: 0 },
      source: { dir: "/x", ref: "git:origin/main", count: 1 },
      error: null,
    }, "family-organizer/backlog");

    expect(parsed.items[0].age_days).toBeUndefined();
    expect(parsed.items[0].freshness).toBeUndefined();
    expect(parsed.items[0].quality_issues).toBeUndefined();
    expect(parsed.items[0].readiness).toBeUndefined();
  });
});


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


describe("OperatorInventoryResponseSchema", () => {
  it("parses the read-only ops inventory without raw paths or command lines", () => {
    const parsed = parseOrThrow(OperatorInventoryResponseSchema, {
      schema: "hermes-operator-inventory-v1",
      checked_at: 1782070000,
      summary: {
        worktrees_total: 3,
        worktrees_locked: 1,
        worktrees_dirty: 1,
        worktrees_prunable: 0,
        worktrees_orphaned: 1,
        worktrees_status_unknown: 0,
        actors_total: 3,
        actors_canonical: 1,
      },
      next_lever: { action: "inspect_dirty_worktrees", label: "Dirty Worktrees", detail: "1 Worktree hat echte Git-Aenderungen.", tone: "amber", count: 1, target: "/control/ops?filter=dirty", mutation: "none" },
      levers: [
        { action: "inspect_dirty_worktrees", label: "Dirty Worktrees", detail: "1 Worktree hat echte Git-Aenderungen.", tone: "amber", count: 1, target: "/control/ops?filter=dirty", mutation: "none" },
      ],
      worktrees: [
        { id: "kanban:t_123", path_label: "kanban:t_123", branch: "kanban/t_123", head: "abc123", relation: "kanban", task_hint: "t_123", state: "dirty", locked: true, prunable: false, detached: false, dirty_count: 3, untracked_count: 1, status_checked: true, orphaned: true },
      ],
      actors: [
        { role: "kanban_worker", label: "Kanban Worker", count: 1, cpu_percent: null, rss_mb: null, oldest_age_seconds: 120, source: "canonical", confidence: "high", stale_count: 0, target: "/control/flow", controllable: false },
        { role: "codex", label: "Codex", count: 2, cpu_percent: 12.5, rss_mb: 512, oldest_age_seconds: 60, source: "process", confidence: "medium", stale_count: 0, target: "/control/ops", controllable: false },
      ],
      errors: [],
    }, "operator-inventory");

    expect(parsed.summary.worktrees_total).toBe(3);
    expect(parsed.next_lever.action).toBe("inspect_dirty_worktrees");
    expect(parsed.worktrees[0].path_label).toBe("kanban:t_123");
    expect(parsed.actors[0].source).toBe("canonical");
    expect(JSON.stringify(parsed)).not.toContain("/home/");
    expect(parsed.actors[0].cpu_percent).toBeNull();
    expect(parsed.actors[0].rss_mb).toBeNull();
    expect(JSON.stringify(parsed)).not.toContain(".worktrees/");
    expect(JSON.stringify(parsed).toLowerCase()).not.toContain("cmdline");
  });
});


describe("PressureStatusResponseSchema", () => {
  it("keeps the operator pressure contract small and redacted", () => {
    const parsed = parseOrThrow(PressureStatusResponseSchema, {
      schema: "hermes-pressure-v1",
      checked_at: 1782070000,
      overall: "busy",
      cause: "Ungedrosselte Testprozesse in Session-Scope",
      recommendation: {
        label: "Tests laufen",
        detail: "1 Testprozess aktiv.",
        tone: "amber",
      },
      host: {
        cpu_percent: 72,
        load_avg: [7.2, 6.8, 5.1],
        cpu_count: 12,
        memory_percent: 61,
      },
      dashboard: {
        pid: 1243,
        rss_mb: 788,
        cpu_percent: 8.5,
        cpu_weight: 300,
        cpu_quota: "infinity",
        tasks_current: 23,
      },
      pressure_sources: [{
        kind: "test",
        label: "pytest",
        count: 4,
        cpu_percent: 370,
        rss_mb: 410,
        scope: "session scope",
        scope_kind: "session",
        throttled: false,
      }],
      access: {
        tailnet: "direct",
        api_latency_ms: 190,
        detail: "tailnet direct",
      },
      token_pressure: {
        class: "unknown",
        pct: null,
      },
      errors: [],
    }, "pressure-status");

    expect(parsed.overall).toBe("busy");
    expect(parsed.recommendation.label).toBe("Tests laufen");
    expect(parsed.pressure_sources[0].label).toBe("pytest");
    expect(parsed.access.tailnet).toBe("direct");
  });
});

describe("TodayDigestResponseSchema", () => {
  it("parses human-readable digest items with deliverable pointers and gate state", () => {
    const parsed = parseOrThrow(TodayDigestResponseSchema, {
      schema: "kanban-today-digest-v1",
      checked_at: 1780000000,
      day_start: 1779990000,
      timezone: "local",
      count: 1,
      items: [{
        run_id: 42,
        task_id: "t_digest",
        task_title: "Useful slice",
        task_summary: "Delivered a useful RESULT.md",
        ended_at: 1780000000,
        profile: "coder",
        run_role: "implementation",
        run_role_label: "Implementation / coder run",
        verification_state: "approved",
        verifier_verdict: "APPROVED",
        verdict_label: "Verified: APPROVED",
        result_quality: {
          state: "verifier_approved",
          label: "Verifier-approved",
          tone: "emerald",
          description: "Independent verifier gate passed.",
        },
        gate_evidence: ["vitest -> 1 passed"],
        deliverable: { filename: "RESULT.md", relative_path: "RESULT.md", size: 123, mtime: 1780000000, content_type: "text/markdown", url: "/api/plugins/kanban/tasks/t_digest/deliverables/RESULT.md" },
        deliverable_excerpt: "Human readable output",
        residual_risk: null,
      }],
    }, "today-digest");

    expect(parsed.items[0].run_id).toBe("42");
    expect(parsed.items[0].deliverable?.relative_path).toBe("RESULT.md");
    expect(parsed.items[0].deliverable_excerpt).toContain("Human readable");
    expect(parsed.items[0].verdict_label).toBe("Verified: APPROVED");
    expect(parsed.items[0].result_quality.state).toBe("verifier_approved");
    expect(parsed.items[0].result_quality.label).toBe("Verifier-approved");
  });
});


describe("OrchestrationBacklogResponseSchema", () => {
  it("preserves unknown live statuses and exposes contract health", () => {
    const parsed = parseOrThrow(OrchestrationBacklogResponseSchema, {
      schema: "orchestration-backlog-v1",
      checked_at: 100,
      counts: { backlog: 0, todo: 0, doing: 0, review: 0, done: 0 },
      contract_health: {
        source_count: 3,
        counted_sum: 0,
        unknown_statuses: [
          { status: "decided", count: 1, ids: ["a"] },
          { status: "planning", count: 1, ids: ["b"] },
          { status: "obsolete", count: 1, ids: ["c"] },
        ],
        invalid_priority_count: 1,
        missing_dep_count: 2,
      },
      source: { dir: "/home/piet/orchestration/backlog", ref: "fs:working-tree", count: 3 },
      items: [
        { id: "a", title: "A", status: "decided", priority: "urgent", dependsOn: [], planGate: false, created: "2026-06-01" },
        { id: "b", title: "B", status: "planning", priority: "medium", dependsOn: ["ghost"], planGate: false, created: "2026-06-02" },
        { id: "c", title: "C", status: "obsolete", priority: "low", dependsOn: [], planGate: false, created: "2026-06-03" },
      ],
      error: null,
    }, "orchestration/backlog");

    expect(parsed.items.map((item) => item.status)).toEqual(["decided", "planning", "obsolete"]);
    expect(parsed.items[0].priority).toBe("urgent");
    expect(parsed.contract_health.source_count).toBe(3);
    expect(parsed.contract_health.counted_sum).toBe(0);
    expect(parsed.contract_health.unknown_statuses.map((status) => status.status)).toEqual(["decided", "planning", "obsolete"]);
    expect(parsed.contract_health.invalid_priority_count).toBe(1);
    expect(parsed.contract_health.missing_dep_count).toBe(2);
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


describe("ProposalsResponseSchema (Test-Foundry)", () => {
  it("preserves mutation-test proposals in the test lane", () => {
    const raw = {
      count: 1, open_count: 1,
      proposals: [{
        id: "test-foundry-1", target: "hermes_cli/kanban_db.py", section: null,
        rationale_plain: "A mutation survived the suite.", diff_before_after: "- x\n+ y",
        mode: "test", proposal_type: "mutation_test", status: "proposed",
      }],
    };
    const parsed = parseOrThrow(ProposalsResponseSchema, raw, "proposals");
    expect(parsed.proposals[0].mode).toBe("test");
    expect(parsed.proposals[0].proposal_type).toBe("mutation_test");
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


describe("BlockedCompletionsResponseSchema", () => {
  it("parses verifier rejection fields for the dedicated fix panel", () => {
    const parsed = parseOrThrow(BlockedCompletionsResponseSchema, {
      blocked: [{
        event_id: -501,
        run_id: 501,
        task_id: "t_rejected",
        task_title: "Rejected by verifier",
        task_status: "blocked",
        assignee: "coder",
        kind: "verifier_request_changes",
        created_at: "100",
        summary_preview: "REQUEST_CHANGES — pytest failed",
        phantom: [],
        reviewer_profile: "verifier",
        verifier_verdict: "REQUEST_CHANGES",
        failure_output: ["pytest -> FAILED"],
        fix_summary: "Fix add(a, b).",
      }],
      count: "1",
      checked_at: "120",
      since_hours: "48",
    }, "blocked-completions");

    expect(parsed.blocked[0].kind).toBe("verifier_request_changes");
    expect(parsed.blocked[0].run_id).toBe("501");
    expect(parsed.blocked[0].failure_output).toEqual(["pytest -> FAILED"]);
    expect(parsed.blocked[0].fix_summary).toBe("Fix add(a, b).");
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
        profile: "verifier",
        run_role: "verification",
        run_role_label: "Verifier / review run",
        run_role_source: "claimed_event",
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
        result_quality: {
          state: "unknown_legacy",
          label: "Unknown legacy",
          tone: "zinc",
          description: "Legacy run has no verifier metadata or profile lineage.",
        },
      }],
    }, "recent-results");
    expect(parsed.count).toBe(1);
    expect(parsed.results[0].run_id).toBe("408");
    expect(parsed.results[0].profile).toBe("verifier");
    expect(parsed.results[0].run_role).toBe("verification");
    expect(parsed.results[0].run_role_label).toBe("Verifier / review run");
    expect(parsed.results[0].duration_seconds).toBe(15);
    expect(parsed.results[0].result_quality.state).toBe("unknown_legacy");
    expect(parsed.results[0].result_quality.tone).toBe("zinc");
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
        kanban_dispatcher: { status: "healthy", detail: "ok", error: null, heartbeat_age_s: "4" },
      },
    }, "health-status");
    expect(parsed.schema).toBe("hermes-health-v1");
    expect(parsed.checked_at).toBe(123);
    expect(parsed.overall).toBe("offline");
    expect(parsed.subsystems.gateway.latency_ms).toBe(8);
    expect(parsed.subsystems.autoresearch.heartbeat_age_s).toBe(12);
    expect(parsed.subsystems.kanban_db.status).toBe("offline");
    expect(parsed.subsystems.kanban_dispatcher.heartbeat_age_s).toBe(4);
  });
});

describe("DecisionQueueResponseSchema", () => {
  it("accepts no-silent-stall decision kinds", () => {
    const parsed = parseOrThrow(DecisionQueueResponseSchema, {
      decisions: [
        {
          kind: "operator_escalation",
          task_id: "t1",
          title: "Escalated",
          reason: "needs human",
          age_seconds: "9",
          suggested_command: null,
          operator_escalation: {
            task: { id: "t1", title: "Escalated", status: "blocked", assignee: "coder" },
            why_now: "retry ladder exhausted",
            attempts_already_made: "2",
            evidence: { last_error: "boom" },
            recommended_human_action: "inspect and choose",
            blocked_action_boundary: ["DB schema/data mutation"],
          },
        },
        { kind: "integration_parked", task_id: "t2", title: "Parked", reason: "merge red", age_seconds: 10, suggested_command: "hermes kanban show t2" },
        { kind: "rate_limited_loop", task_id: "t3", title: "Quota", reason: "429", age_seconds: null, suggested_command: null },
      ],
      count: 3,
      checked_at: 123,
    }, "kanban/decision-queue");

    expect(parsed.decisions.map((d) => d.kind)).toEqual([
      "operator_escalation",
      "integration_parked",
      "rate_limited_loop",
    ]);
    expect(parsed.decisions[0].age_seconds).toBe(9);
    expect(parsed.decisions[0].operator_escalation?.attempts_already_made).toBe(2);
    expect(parsed.decisions[0].operator_escalation?.recommended_human_action).toBe("inspect and choose");
  });

  it("R1: accepts the deliverable_posted_not_completed repair kind verbatim", () => {
    const parsed = parseOrThrow(DecisionQueueResponseSchema, {
      decisions: [
        { kind: "deliverable_posted_not_completed", task_id: "t9", title: "Deliverable da", reason: "kanban_complete fehlt", age_seconds: 12, suggested_command: "hermes kanban show t9" },
      ],
      count: 1,
      checked_at: 123,
    }, "kanban/decision-queue");

    expect(parsed.decisions[0].kind).toBe("deliverable_posted_not_completed");
  });
});


describe("TaskDetailResponseSchema", () => {
  it("keeps dependency links from /tasks/:id so Flow can explain the selected chain", () => {
    const parsed = parseOrThrow(TaskDetailResponseSchema, {
      task: { id: "t_child", title: "Dependent", status: "todo", assignee: "coder", latest_summary: null },
      links: { parents: ["t_parent_a", "t_parent_b"], children: ["t_next"] },
      runs: [],
      events: [],
      deliverables: [],
    }, "kanban/task-detail");

    expect(parsed.links.parents).toEqual(["t_parent_a", "t_parent_b"]);
    expect(parsed.links.children).toEqual(["t_next"]);
  });

  it("defaults missing links to empty arrays for older task-detail payloads", () => {
    const parsed = parseOrThrow(TaskDetailResponseSchema, {
      task: { id: "t_old", title: "Old detail", status: "done", assignee: "coder" },
      runs: [],
      events: [],
      deliverables: [],
    }, "kanban/task-detail");

    expect(parsed.links.parents).toEqual([]);
    expect(parsed.links.children).toEqual([]);
  });
});


describe("CronObservabilityResponseSchema", () => {
  it("parses a full payload with gateway + per-job output meta", () => {
    const raw = {
      schema: "hermes-cron-obs-v1",
      checked_at: 100,
      gateway: { running: true, pids: [42, "99"] },
      jobs: [{
        id: "j1", name: "Morgenbrief", enabled: true, state: "scheduled",
        schedule_display: "07:00",
        // Real backend sends ISO-8601 strings here, not epoch numbers.
        next_run_at: "2026-06-03T07:30:00+02:00", last_run_at: "2026-06-02T07:30:23+02:00",
        repeat: { times: null, completed: 27 },
        last_status: "ok", last_delivery_error: null, deliver: "discord:1",
        profile: "research", has_prompt: true, has_script: false,
        latest_output: { filename: "x.md", mtime: 1, size_bytes: 3, run_count: 2 },
      }],
    };
    const parsed = parseOrThrow(CronObservabilityResponseSchema, raw, "cron/observability");
    expect(parsed.gateway.running).toBe(true);
    expect(parsed.gateway.pids).toEqual([42, 99]);
    expect(parsed.jobs).toHaveLength(1);
    expect(parsed.jobs[0].latest_output?.run_count).toBe(2);
    // ISO timestamps survive as strings (not coerced to NaN).
    expect(parsed.jobs[0].next_run_at).toBe("2026-06-03T07:30:00+02:00");
  });

  it("keeps the other jobs when one job is malformed (does not empty the list)", () => {
    const raw = {
      schema: "hermes-cron-obs-v1",
      checked_at: 100,
      gateway: { running: false, pids: [] },
      jobs: [
        { id: "good", name: "ok", enabled: true },
        { id: "bad", enabled: "not-a-boolean", latest_output: "garbage" },
      ],
    };
    const parsed = parseOrThrow(CronObservabilityResponseSchema, raw, "cron/observability");
    expect(parsed.jobs).toHaveLength(2);
    expect(parsed.jobs[0].id).toBe("good");
    // The malformed job degrades to defaults rather than throwing or vanishing.
    expect(parsed.jobs[1].id).toBe("bad");
    expect(parsed.jobs[1].enabled).toBe(false);
    expect(parsed.jobs[1].latest_output).toBeNull();
  });

  it("falls back to an empty list/offline gateway on a broken payload", () => {
    const parsed = parseOrThrow(CronObservabilityResponseSchema, { jobs: "nope", gateway: 5 }, "cron/observability");
    expect(parsed.jobs).toEqual([]);
    expect(parsed.gateway.running).toBe(false);
  });
});


describe("MetricsLiteResponseSchema", () => {
  it("parses a full payload and coerces numeric strings", () => {
    const parsed = parseOrThrow(MetricsLiteResponseSchema, {
      schema: "hermes-metrics-lite-v1", checked_at: "100", uptime_seconds: "60",
      groups: { "/api/x": { count: "10", error_count: 1, error_rate: 0.1, p50_ms: "5", p95_ms: 40 } },
    }, "metrics-lite");
    expect(parsed.groups["/api/x"].count).toBe(10);
    expect(parsed.groups["/api/x"].p50_ms).toBe(5);
    expect(parsed.uptime_seconds).toBe(60);
  });

  it("falls back to empty groups on a broken payload without throwing", () => {
    const parsed = parseOrThrow(MetricsLiteResponseSchema, { groups: "nope" }, "metrics-lite");
    expect(parsed.groups).toEqual({});
  });
});
