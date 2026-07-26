// @vitest-environment jsdom
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { parseOrThrow, ScoreDigestSchema, type ScoreDigest } from "../../lib/schemas";
import { DigestCardBody } from "./DigestCard";

afterEach(cleanup);

/**
 * Fixture in der am 2026-07-26 gegen einen Snapshot der Live-kanban.db
 * gemessenen Form (`weeks=4`): `cost_usd` teils null, `by_model` mit dem
 * Schluessel "?" (Laeufe ohne Modellangabe), `top_findings` leer.
 */
const rawDigest = {
  generated_at: 1785075174,
  weeks_requested: 4,
  approval_rate: 0.8123,
  approved_rows: 1190,
  rows_total: 1465,
  wow_delta: -0.0908,
  weekly: [
    { year: 2026, week: 27, rows_total: 203, approved_rows: 182, approval_rate: 0.8966 },
    { year: 2026, week: 26, rows_total: 410, approved_rows: 340, approval_rate: 0.8293 },
    { year: 2026, week: 25, rows_total: 500, approved_rows: 400, approval_rate: 0.8 },
    { year: 2026, week: 24, rows_total: 352, approved_rows: 268, approval_rate: 0.7614 },
  ],
  by_profile: {
    verifier: { total: 870, approved: 732, approval_rate: 0.8414 },
    reviewer: { total: 430, approved: 350, approval_rate: 0.814 },
    critic: { total: 165, approved: 108, approval_rate: 0.6545 },
  },
  by_model: {
    "?": { total: 1057, approved: 880, approval_rate: 0.8325 },
    "gpt-5.6-terra": { total: 217, approved: 158, approval_rate: 0.7281 },
    "gpt-5.6-sol": { total: 133, approved: 55, approval_rate: 0.4135 },
    "claude-opus-4-8": { total: 40, approved: 33, approval_rate: 0.825 },
    k3: { total: 10, approved: 8, approval_rate: 0.8 },
    "gpt-5.6-luna": { total: 5, approved: 4, approval_rate: 0.8 },
    "grok-4.5": { total: 3, approved: 2, approval_rate: 0.6667 },
  },
  cost_duration_per_approved_run: [
    { run_id: 8235, task_id: "t_7e54de92", cost_usd: null, duration_seconds: 571.0 },
    { run_id: 8234, task_id: "t_1a2b3c4d", cost_usd: 12.5, duration_seconds: 125.0 },
    { run_id: 8233, task_id: "t_9f8e7d6c", cost_usd: 0.42, duration_seconds: 3720.0 },
  ],
  retry_hotspots: [
    { task_id: "t_30c55ccd", rejections: 12 },
    { task_id: "t_11a22b33", rejections: 7 },
    { task_id: "t_44c55d66", rejections: 4 },
  ],
  has_metric_scores: true,
  approved_run_metric_coverage: true,
  top_findings: [],
};

const makeDigest = (overrides: Record<string, unknown> = {}): ScoreDigest =>
  parseOrThrow(ScoreDigestSchema, { ...rawDigest, ...overrides }, "test-fixture");

describe("DigestCard (SC-S4)", () => {
  it("AC-1: zeigt Kopfzahlen, WoW-Delta, Trend, Profile, Modelle und Hotspots aus echten Digest-Daten", () => {
    render(<DigestCardBody digest={makeDigest()} />);

    // Kopfzahlen
    expect(screen.getByText("Approval-Rate")).toBeTruthy();
    expect(screen.getByText("81,2 %")).toBeTruthy();
    expect(screen.getByText("1.465")).toBeTruthy();
    expect(screen.getByText("1.190")).toBeTruthy();

    // WoW-Delta: Pfeil + explizites Vorzeichen, nicht nur Farbe
    expect(screen.getByText("▼ −9,1 pp zur Vorwoche")).toBeTruthy();

    // Wochentrend
    expect(screen.getByText("KW 27 · 2026")).toBeTruthy();
    expect(screen.getByText("182/203 freigegeben · 89,7 %")).toBeTruthy();

    // Profile
    expect(screen.getByText("verifier")).toBeTruthy();
    expect(screen.getByText("732/870 freigegeben · 84,1 %")).toBeTruthy();

    // Modelle
    expect(screen.getByText("gpt-5.6-terra")).toBeTruthy();
    expect(screen.getByText("158/217 freigegeben · 72,8 %")).toBeTruthy();

    // Retry-Hotspots
    expect(screen.getByText("t_30c55ccd")).toBeTruthy();
    expect(screen.getByText("12 Rejections")).toBeTruthy();
  });

  it("AC-2: leere top_findings erzeugen einen ruhigen Empty-State ohne Erfolgssignal", () => {
    render(<DigestCardBody digest={makeDigest({ top_findings: [] })} />);

    const empty = screen.getByTestId("digest-findings-empty");
    const text = within(empty);

    expect(text.getByText("Keine Top-Befunde vorhanden.")).toBeTruthy();
    // Datenluecke benannt (Situation → Bewertung → naechste Aktion)
    expect(text.getByText(/Score-Reihen/)).toBeTruthy();
    expect(text.getByText(/Datenlücke, kein guter Zustand/)).toBeTruthy();
    expect(text.getByText(/Nächste Aktion:/)).toBeTruthy();
    // Kein gruenes Erfolgssignal im Empty-State
    expect(empty.querySelector(".text-status-ok")).toBeNull();
    expect(empty.querySelector("[class*='status-ok']")).toBeNull();
  });

  it("AC-3: cost_usd null und Modellschluessel '?' kippen die Karte nicht und werden nicht verfaelscht", () => {
    const { container } = render(<DigestCardBody digest={makeDigest()} />);

    // "?" wird als "ohne Modellangabe" ausgewiesen, nie als Modellname
    expect(screen.getByText("ohne Modellangabe")).toBeTruthy();
    expect(screen.queryByText("?")).toBeNull();

    // cost_usd: null wird als "–" gezeigt, nicht als 0 / $0
    expect(screen.getByText(/Run 8235 · Dauer 9,5 min · Kosten –/)).toBeTruthy();
    expect(container.textContent).not.toContain("$0,00");
    // Belegte Kosten bleiben sichtbar
    expect(screen.getByText(/Kosten \$12,50/)).toBeTruthy();
  });
});
