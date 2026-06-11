import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { CostBreakdownPanel, WertBilanzPanel } from "./StatistikView";
import type { RunsCostsResponse, RunsDailyPoint } from "../lib/schemas";

const bucket = (over: Partial<RunsCostsResponse["today"]> = {}): RunsCostsResponse["today"] => ({
  runs: 0,
  cost_usd: null,
  cost_usd_equivalent: null,
  input_tokens: null,
  output_tokens: null,
  ...over,
});

function fixture(profiles: RunsCostsResponse["profiles"]): RunsCostsResponse {
  return {
    days: 7,
    now: 1_700_000_000,
    today: bucket({ runs: 3, cost_usd: 0.25, cost_usd_equivalent: 1.5, input_tokens: 6000, output_tokens: 1100 }),
    window: bucket({ runs: 4, cost_usd: 0.3, input_tokens: 6400, output_tokens: 1180 }),
    profiles,
  };
}

describe("CostBreakdownPanel (F4)", () => {
  it("zeigt Kosten heute + Fenster, echte $ getrennt vom ≈ Äquivalent", () => {
    const html = renderToStaticMarkup(<CostBreakdownPanel data={fixture([])} />);
    expect(html).toContain("Kosten heute");
    expect(html).toContain("Kosten · 7 Tage");
    // heute: echte $ und das Äquivalent klar als ≈ daneben — nie addiert.
    expect(html).toContain("$ 0.25 · ≈ $ 1.50");
    // Fenster ohne Äquivalent: nur echte $.
    expect(html).toContain("$ 0.30");
    expect(html).toContain("Noch keine Kosten-Stamps");
  });

  it("rendert Top-Profile in Backend-Reihenfolge mit Runs, $, ≈ und Tokens", () => {
    const html = renderToStaticMarkup(
      <CostBreakdownPanel
        data={fixture([
          { profile: "premium", runs: 1, cost_usd: 0.0, cost_usd_equivalent: 1.5, input_tokens: 5000, output_tokens: 900 },
          { profile: "coder", runs: 2, cost_usd: 0.3, cost_usd_equivalent: null, input_tokens: 1400, output_tokens: 280 },
          { profile: "verifier", runs: 1, cost_usd: null, cost_usd_equivalent: null, input_tokens: null, output_tokens: null },
        ])}
      />,
    );
    // Reihenfolge bleibt die des Backends (Burn-sortiert); profileLabel
    // mappt coder→Coder, premium bleibt roh.
    expect(html.indexOf("premium")).toBeLessThan(html.indexOf("Coder"));
    expect(html).toContain("2 Runs");
    expect(html).toContain("≈ $ 1.50");
    // Verifier ohne Stamps: ehrliches — statt erfundener Nullen.
    const verifierRow = html.slice(html.indexOf("Verifier"));
    expect(verifierRow).toContain("—");
  });

  it("zeigt Skeleton solange keine Daten da sind", () => {
    const html = renderToStaticMarkup(<CostBreakdownPanel data={null} />);
    expect(html).not.toContain("Kosten heute");
  });
});

// T5: Wert-Bilanz-Kachel — Wochenbilanz nach Klasse (nutzer/haertung/meta).

function dailyPoint(over: Partial<RunsDailyPoint> = {}): RunsDailyPoint {
  return {
    date: "2026-06-10",
    done_roots: 0,
    done_roots_by_class: { nutzer: 0, haertung: 0, meta: 0 },
    done_tasks: 0,
    cost_usd: null,
    input_tokens: null,
    output_tokens: null,
    runs_completed: 0,
    runs_failed: 0,
    cycle_time_p50_seconds: null,
    ...over,
  };
}

describe("WertBilanzPanel (T5)", () => {
  it("summiert die Woche pro Klasse", () => {
    const html = renderToStaticMarkup(
      <WertBilanzPanel
        last7={[
          dailyPoint({ done_roots_by_class: { nutzer: 2, haertung: 1, meta: 4 } }),
          dailyPoint({ done_roots_by_class: { nutzer: 1, haertung: 0, meta: 3 } }),
        ]}
      />,
    );
    expect(html).toContain("Wert-Bilanz");
    expect(html).toContain("Nutzer-Feature");
    expect(html).toContain("Härtung");
    expect(html).toContain("Meta");
    // Summen 3 / 1 / 7 stehen als Pod-Werte im Markup.
    expect(html).toContain(">3<");
    expect(html).toContain(">1<");
    expect(html).toContain(">7<");
  });

  it("rendert Nullen bei leerer Woche", () => {
    const html = renderToStaticMarkup(<WertBilanzPanel last7={[]} />);
    expect(html).toContain("Wert-Bilanz");
    expect(html).toContain(">0<");
  });
});
