import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { DictateStatusResponseSchema, type DictateHistoryDay } from "../lib/schemas";
import { DictateTrend } from "./DictateTrend";

// Shaped exactly per the fixed Stufe-11 contract (built against the contract,
// not a live server — the backend ships in parallel). Realistic values, not
// all-zero placeholders.
const HISTORY: DictateHistoryDay[] = [
  { date: "2026-07-10", dictations: 22, failures: 1, retries: 2, busy: 0, success_rate_percent: 95.5, latency_p50_ms: 780, latency_p95_ms: 1450 },
  { date: "2026-07-11", dictations: 18, failures: 3, retries: 1, busy: 1, success_rate_percent: 85.7, latency_p50_ms: 810, latency_p95_ms: 1600 },
  // All 3 attempts failed outright (0 completed dictations): per contract
  // success_rate_percent = null when dictations===0, and no latency samples
  // were recorded for pure failures — a realistic null day, not fabricated.
  { date: "2026-07-12", dictations: 0, failures: 3, retries: 0, busy: 0, success_rate_percent: null, latency_p50_ms: null, latency_p95_ms: null },
  { date: "2026-07-13", dictations: 30, failures: 0, retries: 0, busy: 0, success_rate_percent: 100, latency_p50_ms: 700, latency_p95_ms: 1200 },
  { date: "2026-07-14", dictations: 25, failures: 2, retries: 1, busy: 0, success_rate_percent: 92.6, latency_p50_ms: 750, latency_p95_ms: 1380 },
];

const TODAY: DictateHistoryDay = {
  date: "2026-07-15",
  dictations: 6,
  failures: 0,
  retries: 0,
  busy: 0,
  success_rate_percent: 100,
  latency_p50_ms: 690,
  latency_p95_ms: 950,
};

describe("DictateTrend", () => {
  it("rendert alle Tage mit korrekt formatierten Werten, today als „heute“ markiert", () => {
    const html = renderToStaticMarkup(<DictateTrend history={HISTORY} today={TODAY} />);
    for (const day of HISTORY) {
      expect(html).toContain(day.date);
    }
    expect(html).toContain("2026-07-15");
    expect(html).toContain("95.5%");
    expect(html).toContain("780 ms");
    expect(html).toContain("1450 ms");
    expect(html).toContain("100%");
    expect(html).toContain("heute");
    expect(html).toContain("6 Tage"); // 5 history + today
  });

  it("history leer + today null -> Leerzustand ohne Fehler-Look, Seite bleibt sonst unbeeinträchtigt", () => {
    const html = renderToStaticMarkup(<DictateTrend history={[]} today={null} />);
    expect(html).toContain("Noch keine Tagesdaten");
    expect(html).toContain("kommt mit dem ersten aktiven Diktat-Tag");
    expect(html).not.toContain("status-alert");
    expect(html).not.toContain("Fehler");
  });

  it("today mit Aktivität wird auch ohne history gezeigt (kein Leerzustand)", () => {
    const html = renderToStaticMarkup(<DictateTrend history={[]} today={TODAY} />);
    expect(html).not.toContain("Noch keine Tagesdaten");
    expect(html).toContain("heute");
    expect(html).toContain("100%");
  });

  it("ein frischer today ohne Aktivität (0 Diktate) triggert weiterhin den Leerzustand", () => {
    const freshDay: DictateHistoryDay = { ...TODAY, dictations: 0, failures: 0 };
    const html = renderToStaticMarkup(<DictateTrend history={[]} today={freshDay} />);
    expect(html).toContain("Noch keine Tagesdaten");
  });

  it("Response ohne history/today (alter Server) parst weiter und rendert wie bisher", () => {
    const legacyResponse = {
      schema: "hermes-dictate-status-v1",
      connected: true,
      last_contact_at: 1784000000,
      app_version: "1.2",
      engine: "on_device",
      language: "german",
      style: "auto",
      surface: "ime",
      microphone_permission: true,
      service_enabled: true,
      last_error: null,
      dictations: 12,
      failures: 1,
      retries: 0,
      busy: 0,
      success_rate_percent: 91.7,
      latency_ms: 800,
      latency_p50_ms: 800,
      latency_p95_ms: 1500,
      apk: null,
      // no `history`, no `today` — pre-Stufe-11 server shape.
    };
    const parsed = DictateStatusResponseSchema.parse(legacyResponse);
    expect(parsed.history).toBeUndefined();
    expect(parsed.today).toBeUndefined();

    // The page must still render fine — DictateTrend degrades to the empty state.
    const html = renderToStaticMarkup(<DictateTrend history={parsed.history} today={parsed.today} />);
    expect(html).toContain("Noch keine Tagesdaten");
    expect(html).not.toContain("NaN");
  });

  it("ein Tag mit success_rate_percent/Latenz null rendert „—“ statt NaN", () => {
    const html = renderToStaticMarkup(<DictateTrend history={HISTORY} today={null} />);
    expect(html).not.toContain("NaN");
    // The 2026-07-12 row's own metrics span carries three em-dash placeholders
    // (rate, p50, p95) since all three fields are null for that day.
    expect(html).toContain("— · p50 — · p95 —");
  });
});
