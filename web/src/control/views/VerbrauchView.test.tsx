import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import type { UsageConsumptionResponse } from "../hooks/usageConsumption";

// Die Hook-Mock muss vor dem View-Import stehen (vitest hoisted vi.mock).
const hookState = {
  data: null as UsageConsumptionResponse | null,
  loading: false,
  error: null as string | null,
};

vi.mock("../hooks/usageConsumption", async (importOriginal) => {
  const original = await importOriginal<typeof import("../hooks/usageConsumption")>();
  return {
    ...original,
    useUsageConsumption: () => ({
      data: hookState.data,
      loading: hookState.loading,
      error: hookState.error,
      reload: () => Promise.resolve(),
    }),
  };
});

import { VerbrauchView } from "./VerbrauchView";

function payload(overrides: Partial<UsageConsumptionResponse> = {}): UsageConsumptionResponse {
  return {
    contract: "usage-consumption.v1",
    generated_at: "2026-08-04T12:00:00Z",
    window: { days: 30, cutoff_utc: "2026-07-05T12:00:00Z", timezone: "UTC" },
    confidence: { tokens: "measured", costs: "derived" },
    totals: {
      equivalent_usd: 18473.34,
      metered_usd: 0,
      subscription_saving_usd: 18473.34,
      metered_coverage: { value: 1, numerator: 10, denominator: 10 },
      tokens: 22_718_000_802,
      runs: 105398,
      cost_coverage: { value: 0.925, numerator: 97535, denominator: 105398 },
      token_coverage: { value: 0.949, numerator: 22718000802, denominator: 23931343141 },
    },
    cache_hit_rate: { value: 0.986, numerator: 23112254078, denominator: 23435288579 },
    component_shares: {
      billable_input: {
        tokens: 323_000_000,
        token_share: { value: 0.014, numerator: 323_000_000, denominator: 23_475_000_000 },
        cost_usd: 1200.5,
        cost_share: { value: 0.065, numerator: 1200.5, denominator: 18473.34 },
      },
      output: {
        tokens: 82_000_000,
        token_share: { value: 0.0035, numerator: 82_000_000, denominator: 23_475_000_000 },
        cost_usd: 1300.0,
        cost_share: { value: 0.07, numerator: 1300.0, denominator: 18473.34 },
      },
    },
    breakdown: {
      dimension: "origin",
      series: [
        {
          key: "claude_code",
          equivalent_usd: 12260.12,
          metered_usd: 0,
          subscription_saving_usd: 12260.12,
          tokens: 14_000_000_000,
          unpriced_tokens: 632_000_000,
          runs: 101557,
          cost_coverage: { value: 0.96, numerator: 97000, denominator: 101557 },
          token_coverage: { value: 0.95, numerator: 14_000_000_000, denominator: 14_700_000_000 },
        },
        {
          key: "qwen_cli",
          equivalent_usd: "not applicable",
          metered_usd: "not applicable",
          subscription_saving_usd: "not applicable",
          tokens: 0,
          unpriced_tokens: 281_000_000,
          runs: 1859,
          cost_coverage: { value: 0, numerator: 0, denominator: 1859 },
          token_coverage: { value: 0, numerator: 0, denominator: 281_000_000 },
        },
      ],
    },
    daily: [
      {
        day: "2026-08-03",
        equivalent_usd: 612.5,
        metered_usd: 0,
        tokens: 800_000_000,
        runs: 4200,
        token_coverage: { value: 0.7, numerator: 7, denominator: 10 },
      },
    ],
    daily_by_origin: {
      claude_code: [
        {
          day: "2026-08-03",
          equivalent_usd: 500.0,
          tokens: 700_000_000,
          token_coverage: { value: 0.99, numerator: 99, denominator: 100 },
        },
      ],
    },
    distributions: {
      tokens_per_run: { p50: 107580, p90: 301040, max: 9_294_555_292, count: 99906 },
      tokens_per_tool_call: { p50: 97661, p90: 282671, max: 940539, count: 93382 },
      calls_per_run: { p50: 1, p90: 1, max: 5491, count: 99906 },
    },
    top_runs: [
      {
        run_id: "codex_cli:anon:loop0",
        origin: "codex_cli",
        model: "gpt-5.5",
        provider: "openai",
        task_id: null,
        chain_id: null,
        session_id: null,
        day: "2026-07-05",
        tokens: 9_285_464_613,
        equivalent_usd: 6579.69,
      },
      {
        run_id: "run-mit-task",
        origin: "hermes_agent",
        model: "gpt-5.6-sol",
        provider: "openai-codex",
        task_id: "t_abcd1234",
        chain_id: null,
        session_id: null,
        day: "2026-08-02",
        tokens: 1_955_107,
        equivalent_usd: 7.83,
      },
    ],
    trend: {
      window_days: 30,
      equivalent_usd_per_day_full: 615.89,
      equivalent_usd_per_day_7d: 680.13,
      ratio_7d_vs_full: { value: 1.104, numerator: 680.13, denominator: 615.89 },
    },
    levers: [
      {
        id: "outlier-run-cap",
        title: "Teuersten Einzellauf prüfen/begrenzen",
        current_usd: 6579.69,
        share_of_total: { value: 0.356, numerator: 6579.69, denominator: 18473.34 },
        counterfactual_usd: 120.5,
        savings_usd: 6459.19,
        assumption: "Ausreißer über p99 ist Fehlverhalten",
        plausibility: { ratio_top_to_p99: 54.6 },
      },
    ],
    ...overrides,
  };
}

function render() {
  return renderToStaticMarkup(
    <MemoryRouter>
      <VerbrauchView />
    </MemoryRouter>,
  );
}

describe("VerbrauchView", () => {
  it("zeigt den Lade-Zustand als Skeleton, nicht als Pseudo-Leere", () => {
    hookState.data = null;
    hookState.loading = true;
    hookState.error = null;
    const html = render();
    expect(html).not.toContain("Keine Läufe");
    expect(html).not.toContain("nicht verfügbar");
  });

  it("zeigt den Fehler-Zustand mit Retry statt passivem Text", () => {
    hookState.data = null;
    hookState.loading = false;
    hookState.error = "boom";
    const html = render();
    expect(html).toContain("nicht verfügbar");
  });

  it("rechnet nicht gegen einen unbekannten Vertrag", () => {
    hookState.loading = false;
    hookState.error = null;
    hookState.data = payload({ contract: "usage-consumption.v0" });
    const html = render();
    expect(html).toContain("Vertrag unbekannt");
    expect(html).not.toContain("Hebel 1");
  });

  it("stellt die Hebel-Sektion als Held VOR den KPIs (D5)", () => {
    hookState.error = null;
    hookState.data = payload();
    const html = render();
    const leverPos = html.indexOf("verbrauch-levers");
    const kpiPos = html.indexOf("verbrauch-kpis");
    expect(leverPos).toBeGreaterThan(-1);
    expect(kpiPos).toBeGreaterThan(-1);
    expect(leverPos).toBeLessThan(kpiPos);
    expect(html).toContain("Teuersten Einzellauf");
    expect(html).toContain("Annahme und Plausibilität");
  });

  it("zeigt unbepreisbare Serien als n. a., nie als $0,00", () => {
    hookState.data = payload();
    const html = render();
    const qwenRow = html.split('verbrauch-breakdown-row-qwen_cli')[1] ?? "";
    expect(qwenRow).toContain("n. a.");
    expect(qwenRow).not.toContain("$0,00");
  });

  it("bietet Sprung zur Task bei verlinkbaren Läufen, sonst ehrliches Label", () => {
    hookState.data = payload();
    const html = render();
    expect(html).toContain("Task t_abcd1234");
    expect(html).toContain("kein Sprungziel");
  });

  it("leerer Hebel-Zustand sagt warum leer", () => {
    hookState.data = payload({ levers: [] });
    const html = render();
    expect(html).toContain("Kein Hebel mit belastbarer Gegenrechnung");
  });
});
