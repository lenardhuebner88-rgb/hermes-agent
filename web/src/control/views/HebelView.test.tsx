import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { UsageFactsGroup, UsageFactsResponse } from "../hooks/usageFacts";

const mockState = vi.hoisted(() => ({
  data: null as UsageFactsResponse | null,
  loading: false,
  error: null as string | null,
}));

vi.mock("../hooks/usageFacts", async (importOriginal) => {
  const original = await importOriginal<typeof import("../hooks/usageFacts")>();
  return {
    ...original,
    useUsageFacts: () => ({
      data: mockState.data,
      loading: mockState.loading,
      error: mockState.error,
    }),
  };
});

import { HebelView } from "./HebelView";

const zeroTotals = () => ({
  context_input_tokens: 0,
  new_input_tokens: 0,
  cache_read_tokens: 0,
  cache_write_tokens: 0,
  output_tokens: 0,
  has_lower_bounds: false,
});

const totals = (
  context: number,
  cacheRead: number,
  newInput: number,
  output: number,
  hasLowerBounds = false,
) => ({
  context_input_tokens: context,
  new_input_tokens: newInput,
  cache_read_tokens: cacheRead,
  cache_write_tokens: 0,
  output_tokens: output,
  has_lower_bounds: hasLowerBounds,
});

const group = (
  origin: string,
  model: string | null,
  factRows: number,
  context: number,
  cacheRead: number,
  newInput: number,
  output: number,
  billing: UsageFactsGroup["billing"],
  contextStatus: "exact" | "lower_bound" = "exact",
) => ({
  key: {
    origin,
    profile: null,
    lane: `${origin}-lane`,
    model,
    model_label: model ?? "nicht_zuordenbar",
  },
  fact_rows: factRows,
  tokens: {
    input_semantics: origin === "claude_code" || origin === "hermes_aux" ? "cache_exclusive" : "cache_inclusive",
    context_input: { tokens: context, status: contextStatus },
    new_input: { tokens: newInput, status: "exact" as const },
    cache_read_tokens: cacheRead,
    cache_write_tokens: 0,
    output_tokens: output,
  },
  billing,
});

const baseData = (): UsageFactsResponse => ({
  contract_version: "usage-facts.v1",
  generated_at: "2026-07-27T15:36:00+00:00",
  normalization: {
    version: "origin-input.v1",
    origins: {
      claude_code: { input_semantics: "cache_exclusive" },
      codex_cli: { input_semantics: "cache_inclusive" },
      grok_cli: { input_semantics: "cache_inclusive" },
      hermes_aux: { input_semantics: "cache_exclusive" },
    },
  },
  summary: {
    group_count: 4,
    fact_rows: 598,
    // Die naive Roh-Summe — sie darf im Tab nirgends auftauchen (Faktor ~2,09).
    raw_input_tokens: 1234,
    tokens: totals(2500, 2279, 221, 75, true),
    billing: {
      metered: {
        fact_rows: 0,
        tokens: zeroTotals(),
        metered_usd: {
          known_amount_usd: "0.000000",
          status: "complete",
          priced_breakdowns: 0,
          unpriced_breakdowns: 0,
          lower_bound_breakdowns: 0,
        },
      },
      quota: {
        fact_rows: 1,
        tokens: totals(900, 810, 90, 20),
        marginal_usd: "0",
        list_equivalent_usd: {
          known_amount_usd: "12.340000",
          status: "partial",
          priced_breakdowns: 9,
          unpriced_breakdowns: 135,
          lower_bound_breakdowns: 0,
        },
      },
      unclassified: {
        fact_rows: 597,
        tokens: totals(1600, 1469, 131, 55),
        reason: "billing_mode_unknown",
      },
    },
  },
  groups: [
    group("claude_code", "test-model-a", 2, 1000, 994, 6, 40, {
      unclassified: { fact_rows: 2, tokens: totals(1000, 994, 6, 40), reason: "billing_mode_unknown" },
    }),
    group("codex_cli", "test-model-b", 1, 900, 810, 90, 20, {
      quota: {
        fact_rows: 1,
        tokens: totals(900, 810, 90, 20),
        marginal_usd: "0",
        list_equivalent_usd: {
          known_amount_usd: "12.340000",
          status: "partial",
          priced_breakdowns: 9,
          unpriced_breakdowns: 135,
          lower_bound_breakdowns: 0,
        },
      },
    }),
    group("hermes_aux", "test-model-a", 12, 100, 0, 100, 5, {
      unclassified: { fact_rows: 12, tokens: totals(100, 0, 100, 5), reason: "billing_mode_unknown" },
    }, "lower_bound"),
    group("grok_cli", null, 583, 500, 475, 25, 10, {
      unclassified: { fact_rows: 583, tokens: totals(500, 475, 25, 10), reason: "billing_mode_unknown" },
    }),
  ],
  unattributed: {
    label: "nicht_zuordenbar",
    reason: "model_missing",
    fact_rows: 583,
    tokens: totals(500, 475, 25, 10),
    by_origin: [{ origin: "grok_cli", fact_rows: 583, tokens: totals(500, 475, 25, 10) }],
  },
  kanban: {
    available: true,
    total_runs: 8284,
    provider_classification: { nie_gelaufen: 2592, unbekannt: 3750, rekonstruiert: 638, gestempelt: 1304 },
    usage_coverage: {
      token_bearing_runs: 30,
      provider_only_fact_runs: 500,
      runs_without_fact: 7754,
      state: "thin",
    },
  },
});

describe("HebelView", () => {
  beforeEach(() => {
    mockState.data = baseData();
    mockState.loading = false;
    mockState.error = null;
  });

  it("zeigt den normalisierten Kontext-Input, niemals die naive Roh-Summe", () => {
    const markup = renderToStaticMarkup(<HebelView />);
    // 2.500 = normalisierte Summe; Lower-Bound-Flag → „≥“ + Beschriftung.
    expect(markup).toContain("≥ 2.500");
    expect(markup).toContain("Untergrenze");
    // 1.234 = raw_input_tokens — würde die Wahrheit halbieren.
    expect(markup).not.toContain("1.234");
  });

  it("macht den Cache-Read-Anteil zur Hauptzahl, nicht den Roh-Input", () => {
    const markup = renderToStaticMarkup(<HebelView />);
    expect(markup).toContain("Cache-Read-Anteil");
    expect(markup).toContain("91,2 %");
    expect(markup).toContain("2.279 von 2.500 Token");
    // Welt-Zeile: Claude-Code-Anteil 99,4 %.
    expect(markup).toContain("99,4 % Cache-Read");
    expect(markup).toContain("Cache exklusiv");
    expect(markup).toContain("Cache inklusiv");
  });

  it("zeigt den Kompressions-Overhead aus den Aux-Gruppen als dünne Datenlage", () => {
    const markup = renderToStaticMarkup(<HebelView />);
    expect(markup).toContain("Kompression (Aux)");
    expect(markup).toContain("4,0 %");
    expect(markup).toContain("12 Faktenzeilen");
    expect(markup).toContain("dünne Datenlage");
  });

  it("hält Dollar und Quota getrennt und beschriftet das Listenpreis-Äquivalent ehrlich", () => {
    const markup = renderToStaticMarkup(<HebelView />);
    expect(markup).toContain("Kein metered Verbrauch erfasst");
    expect(markup).toContain("Grenzkosten $0");
    expect(markup).toContain("≥ $12,34");
    expect(markup).toContain("Listenpreis-Äquivalent — kein Spend");
    expect(markup).toContain("9 bepreist · 135 unbepreist");
  });

  it("zeigt unclassified als eigene sichtbare Größe statt als Fußnote", () => {
    const markup = renderToStaticMarkup(<HebelView />);
    expect(markup).toContain("Nicht klassifiziert");
    expect(markup).toContain("1.600");
    expect(markup).toContain("billing_mode_unknown");
  });

  it("zeigt die dünne Kanban-Abdeckung als dünne Datenlage, nicht als Nullverbrauch", () => {
    const markup = renderToStaticMarkup(<HebelView />);
    expect(markup).toContain("hebel-kanban-thin");
    expect(markup).toContain("keine Null-Kosten-Aussage");
    expect(markup).toContain("30");
    expect(markup).toContain("7.754");
    expect(markup).toContain("nie_gelaufen 2.592");
  });

  it("zeigt nicht zuordenbare Token als eigenen Topf mit Origin-Breakdown", () => {
    const markup = renderToStaticMarkup(<HebelView />);
    expect(markup).toContain("hebel-unattributed");
    expect(markup).toContain("nicht_zuordenbar");
    expect(markup).toContain("500 Kontext-Input ohne Modellangabe");
    expect(markup).toContain("model_missing");
  });

  it("faltet Modelle über Gruppen und lässt modell-lose Gruppen im unattributed-Topf", () => {
    const markup = renderToStaticMarkup(<HebelView />);
    // test-model-a: claude (1.000) + aux (100) = 1.100; grok (null) fehlt hier.
    expect(markup).toContain("test-model-a");
    expect(markup).toContain("1.100 Kontext");
    expect(markup).toContain("test-model-b");
  });

  it("unterscheidet ein leeres Board von dünner Hook-Abdeckung", () => {
    mockState.data = {
      ...baseData(),
      kanban: {
        available: true,
        total_runs: 0,
        provider_classification: {},
        usage_coverage: { token_bearing_runs: 0, provider_only_fact_runs: 0, runs_without_fact: 0, state: "thin" },
      },
    };
    const markup = renderToStaticMarkup(<HebelView />);
    expect(markup).toContain("Keine Board-Runs im Scope");
    expect(markup).not.toContain("hebel-kanban-thin");
  });

  it("lehnt einen unbekannten Vertrag ab statt dagegen zu rechnen", () => {
    mockState.data = { ...baseData(), contract_version: "usage-facts.v0" };
    const markup = renderToStaticMarkup(<HebelView />);
    expect(markup).toContain("hebel-contract-mismatch");
    expect(markup).toContain("Vertrag unbekannt");
    expect(markup).toContain("usage-facts.v0");
    expect(markup).not.toContain("Cache-Read-Anteil");
  });

  it("unterscheidet Ladend und Fehler vom Datenstand", () => {
    mockState.data = null;
    mockState.loading = true;
    expect(renderToStaticMarkup(<HebelView />)).toContain("Hebel werden geladen");
    mockState.loading = false;
    mockState.error = "boom";
    expect(renderToStaticMarkup(<HebelView />)).toContain("Hebel sind derzeit nicht verfügbar");
  });
});
