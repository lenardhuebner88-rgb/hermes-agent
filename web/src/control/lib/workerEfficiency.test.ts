import { describe, expect, it } from "vitest";
import {
  buildWorkerEfficiencyRows,
  workerEfficiencyLevers,
} from "./workerEfficiency";
import type { ReliabilityProfile, WindowedRollupRoot } from "./schemas";

function reliability(over: Partial<ReliabilityProfile> = {}): ReliabilityProfile {
  return {
    profile: "coder",
    runs: 0,
    tasks: 0,
    outcomes: {},
    completed_rate: null,
    failed_rate: null,
    retries: 0,
    retry_rate: null,
    judged: 0,
    approved: 0,
    rejected: 0,
    approve_rate: null,
    low_sample: false,
    ...over,
  };
}

function root(over: Partial<WindowedRollupRoot> = {}): WindowedRollupRoot {
  return {
    id: "t_root",
    title: "Root",
    status: "done",
    assignee: "orchestrator",
    created_at: 10,
    started_at: 20,
    completed_at: 200,
    ended_at: 200,
    providers: ["anthropic"],
    cost_usd: 0,
    cost_usd_equivalent: 1,
    cost_effective_usd: 1,
    unknown_run_count: 0,
    billing_mode: "subscription_included",
    neuralwatt: null,
    runtime_seconds: 180,
    workers: [
      {
        profile: "coder",
        input_tokens: 9_000,
        output_tokens: 1_000,
        cost_usd: 0,
        actual_cost_usd: 0,
        run_count: 2,
        cost_usd_equivalent: 1.5,
        api_equivalent_usd: 1.5,
        cost_effective_usd: 1.5,
        billing_neuralwatt_kwh: 0,
        billing_neuralwatt_cost_usd: 0,
        provider: "anthropic",
        model: "claude",
        provider_model_source: "run_metadata",
        unknown_run_count: 0,
      },
    ],
    runners: [
      {
        id: 1,
        task_id: "t_worker",
        profile: "coder",
        provider: "anthropic",
        model: "claude",
        provider_model_source: "run_metadata",
        input_tokens: 9_000,
        output_tokens: 1_000,
        cost_usd: 0,
        cost_usd_equivalent: 1.5,
        cost_effective_usd: 1.5,
        billing_mode: "subscription_included",
        neuralwatt: null,
        started_at: 20,
        ended_at: 80,
        runtime_seconds: 60,
      },
    ],
    ...over,
  };
}

describe("buildWorkerEfficiencyRows", () => {
  it("derives worker efficiency from completed roots and attributed review verdicts", () => {
    // judged >= default min-n (5) so the review rate is asserted, not gated.
    const rows = buildWorkerEfficiencyRows(
      [root()],
      [reliability({ profile: "coder", judged: 8, approved: 6, rejected: 2 })],
    );

    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({
      profile: "coder",
      tasks: 1,
      runs: 2,
      tokens_total: 10_000,
      runtime_seconds: 60,
      cost_effective_usd: 1.5,
      token_per_task: 10_000,
      token_per_min: 10_000,
      cost_per_task: 1.5,
      reviewed_outputs: 8,
      review_request_changes: 2,
      review_return_rate: 0.25,
      data_quality: "complete",
    });
  });

  it("keeps missing runtime, cost and review as null instead of fake zeroes", () => {
    const rows = buildWorkerEfficiencyRows(
      [
        root({
          workers: [
            {
              profile: "coder",
              input_tokens: 1200,
              output_tokens: 300,
              cost_usd: 0,
              actual_cost_usd: 0,
              run_count: 1,
              cost_usd_equivalent: 0,
              api_equivalent_usd: 0,
              cost_effective_usd: 0,
              billing_neuralwatt_kwh: 0,
              billing_neuralwatt_cost_usd: 0,
              provider: null,
              model: null,
              provider_model_source: "unknown",
              unknown_run_count: 1,
            },
          ],
          runners: [],
        }),
      ],
      [],
    );

    expect(rows[0].token_per_task).toBe(1500);
    expect(rows[0].token_per_min).toBeNull();
    expect(rows[0].cost_per_task).toBeNull();
    expect(rows[0].review_return_rate).toBeNull();
    expect(rows[0].data_quality).toBe("partial");
  });

  it("suppresses the review-return rate below the min-n sample gate", () => {
    // A single judged output must not surface as "100 % zurück" (mirrors the
    // backend approve_rate min-n damping). reviewed_outputs stays visible.
    const rows = buildWorkerEfficiencyRows(
      [root()],
      [reliability({ profile: "coder", judged: 1, approved: 0, rejected: 1 })],
    );

    expect(rows[0].reviewed_outputs).toBe(1);
    expect(rows[0].review_request_changes).toBe(1);
    expect(rows[0].review_return_rate).toBeNull();
    expect(rows[0].data_quality).toBe("partial");
    expect(workerEfficiencyLevers(rows)).toEqual([]);
  });

  it("honours an explicit minReviewSample override", () => {
    const rows = buildWorkerEfficiencyRows(
      [root()],
      [reliability({ profile: "coder", judged: 3, approved: 2, rejected: 1 })],
      3,
    );

    expect(rows[0].review_return_rate).toBeCloseTo(1 / 3, 5);
  });

  it("drops phantom profiles and surfaces supported routing levers", () => {
    const rows = buildWorkerEfficiencyRows(
      [
        root({
          workers: [
            root().workers[0],
            {
              ...root().workers[0],
              profile: "premium",
              input_tokens: 4000,
              output_tokens: 1000,
              cost_usd_equivalent: 2,
              api_equivalent_usd: 2,
              cost_effective_usd: 2,
            },
            { ...root().workers[0], profile: "w", input_tokens: 999_999 },
          ],
          runners: [
            root().runners[0],
            { ...root().runners[0], id: 2, profile: "premium", runtime_seconds: 50 },
          ],
        }),
      ],
      [
        reliability({ profile: "coder", judged: 10, approved: 6, rejected: 4 }),
        reliability({ profile: "premium", judged: 10, approved: 9, rejected: 1 }),
      ],
    );

    expect(rows.map((row) => row.profile)).toEqual(["premium", "coder"]);
    expect(workerEfficiencyLevers(rows).map((lever) => lever.label)).toEqual([
      "Coder-Review-Schleifen senken",
      "Premium für riskante Tasks",
      "Coder Token/Task senken",
    ]);
  });
});
