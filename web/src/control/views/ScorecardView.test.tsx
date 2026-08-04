import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ScorecardResponseSchema, type ScorecardResponse } from "../lib/schemas";

const mockState = vi.hoisted(() => ({
  data: null as ScorecardResponse | null,
  loading: false,
  error: null as string | null,
  reload: vi.fn(),
}));

vi.mock("../hooks/scorecard", () => ({
  useScorecard: () => ({
    loading: mockState.loading,
    error: mockState.error,
    data: mockState.data,
    reload: mockState.reload,
  }),
}));

import { ScorecardView } from "./ScorecardView";

const baseData = (): ScorecardResponse => ({
  contract_version: "hermes-scorecard.v2",
  overall: { runs: 12, approved: 9, approval_rate: .75 },
  verdicts: { approved: 9, rejected: 3 },
  profiles: [{ name: "coder", runs: 10, approved: 8, approval_rate: .8 }],
  models: [{ name: "gpt-test", runs: 10, approved: 8, approval_rate: .8 }],
  weeks: [{ year: 2026, week: 30, runs: 10, approved: 8, approval_rate: .8 }],
  materialized_scores: {},
  quality: {
    window_days: 7,
    overall: { runs: 282, approved: 192, approval_rate: 192 / 282 },
    verdicts: { approved: 192, rejected: 90 },
    outcomes: {
      completed: 397,
      blocked: 171,
      iteration_budget_exhausted: 24,
      spawn_failed: 18,
    },
    profiles: [{ name: "coder", runs: 139, approved: 103, approval_rate: 103 / 139 }],
    models: [
      { name: "terra", runs: 139, approved: 103, approval_rate: 103 / 139 },
      { name: "k3", runs: 33, approved: 30, approval_rate: 30 / 33 },
    ],
    daily_verdicts: [
      { date: "2026-07-27", approved: 15, rejected: 4 },
      { date: "2026-07-28", approved: 56, rejected: 8 },
    ],
    review_iterations: {
      average: 1.25,
      count: 75,
      distribution: { "0": 38, "1": 22, "2": 15 },
    },
    coverage: {
      runs: 987,
      review_verdicts: 282,
      run_outcomes: 755,
      review_iterations: 75,
    },
    source: "kanban.metric_scores",
    captured_at: 1_785_300_000,
  },
  observability: {
    available: false,
    state: "absent",
    reason: "credentials_missing",
    captured_at: null,
    cache: { ttl_seconds: 45, age_seconds: null },
  },
  checked_at: 1_785_300_000,
});

describe("ScorecardView", () => {
  beforeEach(() => {
    mockState.data = baseData();
    mockState.loading = false;
    mockState.error = null;
    mockState.reload.mockClear();
  });

  it("unterscheidet die drei Zustände: Skeleton beim Erst-Load, ErrorNote mit Retry bei Fehler", () => {
    mockState.data = null;
    mockState.loading = true;
    const loadingMarkup = renderToStaticMarkup(<ScorecardView />);
    expect(loadingMarkup).toContain('aria-busy="true"');
    // A11y: SkeletonCard meldet den Ladezustand per role="status" + sr-only-
    // Text an Screenreader — sichtbar bleibt nur der Shimmer (Leer ≠ Ladend).
    expect(loadingMarkup).toContain('role="status"');
    expect(loadingMarkup).toContain('sr-only');
    expect(loadingMarkup).not.toContain("Worker Scorecard");

    mockState.loading = false;
    mockState.error = "500: boom";
    const errorMarkup = renderToStaticMarkup(<ScorecardView />);
    expect(errorMarkup).toContain('role="alert"');
    expect(errorMarkup).toContain("Scorecard ist derzeit nicht verfügbar.");
    expect(errorMarkup).toContain("Erneut laden");
    expect(errorMarkup).not.toContain('aria-busy="true"');
  });

  it("preserves materialized distributions in the response contract", () => {
    const scores = ScorecardResponseSchema.parse({
      ...baseData(),
      materialized_scores: {
        run_cost_usd: { value: 1.5, min: 0.5, max: 2.5, sum: 3, count: 2 },
        run_outcome_kind: { value: { completed: 2 }, count: 2 },
      },
    }).materialized_scores;

    expect(scores.run_cost_usd).toMatchObject({ value: 1.5, min: 0.5, max: 2.5, sum: 3, count: 2 });
    expect(scores.run_outcome_kind).toMatchObject({ value: { completed: 2 }, count: 2 });
  });

  it("rejects a malformed quality run count instead of reporting zero reviews", () => {
    const data = baseData();
    data.quality!.overall = { runs: "unknown" as unknown as number, approved: 9, approval_rate: .75 };

    expect(() => ScorecardResponseSchema.parse(data)).toThrow(/runs/);
  });

  it("zeigt Qualitätsentscheidungen mit sichtbaren Nennern", () => {
    const markup = renderToStaticMarkup(<ScorecardView />);
    expect(markup).toContain("Worker Scorecard");
    expect(markup).toContain("68,1 %");
    expect(markup).toContain("192 / 282 entschiedene Reviews");
    expect(markup).toContain("282 Verdicts / 987 Runs");
    expect(markup).toContain("Verdicts je Tag");
  });

  it("benennt partielle Langfuse-Coverage als partial statt absent", () => {
    const data = baseData();
    data.observability = {
      available: true,
      state: "partial",
      reason: "window_truncated",
      captured_at: "2026-07-30T12:00:00Z",
      cache: { ttl_seconds: 45, age_seconds: 7 },
    };
    mockState.data = data;

    const markup = renderToStaticMarkup(<ScorecardView />);
    expect(markup).toContain("Langfuse teilweise · Cache 7 s");
    expect(markup).not.toContain("Langfuse absent");
  });

  it("zeigt vollständige Outcomes und Review-Iterationen", () => {
    const markup = renderToStaticMarkup(<ScorecardView />);
    expect(markup).toContain("Iterationsbudget erschöpft");
    expect(markup).toContain("Spawn fehlgeschlagen");
    expect(markup).toContain("Ø 1,25 Iterationen");
  });

  it("markiert genau die dünnen Modellstichproben", () => {
    const markup = renderToStaticMarkup(<ScorecardView />);
    expect(markup).toContain("k3");
    expect(markup).toContain("n 33");
    expect(markup).toContain("n 139");
    // Auf den MARKER prüfen, nicht auf das Wort: die Panel-Fußzeile rendert
    // unbedingt "dünne Nenner bleiben sichtbar" und enthält "dünn" als
    // Teilzeichenkette. Ein toContain("dünn") blieb deshalb auch dann grün,
    // wenn man `thin` und den Marker komplett entfernte — der Test sicherte
    // nichts ab. Diese Fassung liest die markierten Namen aus dem Markup:
    // k3 hat n 33 (dünn), terra hat n 139 (nicht dünn).
    const marked = [...markup.matchAll(/>([^<>]+)<span[^>]*>· dünn<\/span>/g)].map((m) => m[1]);
    expect(marked).toEqual(["k3"]);
  });

  it("zeigt ohne Verdicts einen neutralen Nullzustand", () => {
    const data = baseData();
    data.quality!.verdicts = { approved: 0, rejected: 0 };
    data.quality!.overall = { runs: 0, approved: 0, approval_rate: null };
    mockState.data = data;

    const markup = renderToStaticMarkup(<ScorecardView />);

    expect(markup).toContain('aria-label="Keine Verdict-Daten"');
    expect(markup).toContain("Keine Verdict-Daten");
    expect(markup).not.toContain('aria-label="0.0 Prozent freigegeben"');
  });

  it("mischt keine Kosten-, Token- oder Laufzeitmetriken in die Scorecard", () => {
    const markup = renderToStaticMarkup(<ScorecardView />);
    expect(markup).not.toContain("Context Tokens");
    expect(markup).not.toContain("Run Duration");
    expect(markup).not.toContain("Kosten");
    expect(markup).not.toContain("Event- &amp; Usage-Scores");
  });

  it("erfindet ohne quality-Block keine Coverage und kein Zeitfenster", () => {
    // Deploy-Versatz: neues Frontend, Dashboard-Dienst noch auf der alten
    // Route — die Payload trägt dann kein `quality`. Der Rückfall darf daraus
    // KEINE Vollständigkeit ableiten. Zuvor setzte er Zähler = Nenner, was
    // exakt 100 % samt grünem Haken ergab, und beschriftete eine
    // All-Time-Aggregation als "7 TAGE".
    const data = baseData();
    delete (data as { quality?: unknown }).quality;
    mockState.data = data;

    const markup = renderToStaticMarkup(<ScorecardView />);

    expect(markup).toContain("GESAMTZEITRAUM");
    expect(markup).not.toContain("7 TAGE");
    expect(markup).not.toContain("100,0 %");
    // Nenner sichtbar und leer statt Zähler = Nenner.
    expect(markup).toContain("0 Verdicts / 0 Runs");
    // Der grüne Haken der CoverageRail darf hier nicht erscheinen; die Kacheln
    // tragen das Warndreieck. (`text-status-ok` allein taugt nicht als Probe —
    // das trägt auch die "approved"-Zahl der Entscheidungsleiste.)
    expect(markup).not.toContain("lucide-circle-check");
  });
});
