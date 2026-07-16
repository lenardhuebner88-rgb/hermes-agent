import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { SystemHealthStrip } from "./SystemHealthStrip";
import type { SystemHealthResponse } from "../lib/types";
import { de } from "../i18n/de";
import { OfflineStaleBanner } from "./OfflineStaleBanner";

const clock = vi.hoisted(() => ({ now: 100 }));

vi.mock("../lib/clock", () => ({
  useClientNowSeconds: () => clock.now,
}));

const baseHealth: SystemHealthResponse = {
  schema: "hermes-health-v1",
  checked_at: 100,
  overall: "healthy",
  subsystems: {
    gateway: { status: "healthy", detail: "ok", error: null, latency_ms: 12 },
    autoresearch: { status: "healthy", detail: "fresh", error: null, heartbeat_age_s: 2 },
    kanban_db: { status: "healthy", detail: "ok", error: null, latency_ms: 4 },
    kanban_dispatcher: { status: "healthy", detail: "ok", error: null, heartbeat_age_s: 5 },
  },
};

describe("SystemHealthStrip", () => {
  it("renders all subsystem labels with full data", () => {
    const html = renderToStaticMarkup(<SystemHealthStrip data={baseHealth} now={120} />);
    expect(html).toContain("Hermes-Gateway");
    expect(html).toContain("Autoresearch-Loop");
    expect(html).toContain("Kanban-DB");
    expect(html).toContain("Kanban-Dispatcher");
    // OpenClaw wurde 2026-06-01 abgeschaltet — nicht mehr in der Ampel.
    expect(html).not.toContain("OpenClaw-Proxy");
  });

  it("distinguishes fetch errors from paused or age-stale health refreshes with translated labels", () => {
    const errorHtml = renderToStaticMarkup(
      <OfflineStaleBanner health={{ data: baseHealth, error: "network down", lastUpdated: 99, pollIntervalMs: 10000 }} />,
    );
    const ageStaleHtml = renderToStaticMarkup(
      <OfflineStaleBanner health={{ data: baseHealth, error: null, lastUpdated: 0, pollIntervalMs: 10000 }} />,
    );

    expect(errorHtml).toContain(de.staleBanner.fetchError);
    expect(ageStaleHtml).toContain(de.staleBanner.pausedOrStale);
    expect(ageStaleHtml).not.toContain(de.staleBanner.fetchError);
  });

  it("shows amber tone for degraded subsystems", () => {
    const html = renderToStaticMarkup(
      <SystemHealthStrip
        data={{
          ...baseHealth,
          overall: "degraded",
          subsystems: { ...baseHealth.subsystems, autoresearch: { status: "degraded", detail: "langsam", error: null } },
        }}
      />,
    );
    expect(html).toContain("border-status-warn/25");
    expect(html).toContain("Beeinträchtigt");
  });

  it("shows red tone and visible error text for offline subsystems", () => {
    const html = renderToStaticMarkup(
      <SystemHealthStrip
        data={{
          ...baseHealth,
          overall: "offline",
          subsystems: { ...baseHealth.subsystems, gateway: { status: "offline", detail: "", error: "connection refused" } },
        }}
      />,
    );
    expect(html).toContain("border-status-alert/25");
    expect(html).toContain("connection refused");
  });

  it("renders gray unknown fallback without data", () => {
    const html = renderToStaticMarkup(<SystemHealthStrip data={null} />);
    expect(html).toContain("border-zinc-600/25");
    expect(html).toContain("Status unbekannt");
  });

  it("renders the metrics tile with aggregates and no badge under threshold", () => {
    const html = renderToStaticMarkup(
      <SystemHealthStrip
        data={baseHealth}
        metrics={{
          schema: "hermes-metrics-lite-v1",
          checked_at: 100,
          uptime_seconds: 60,
          groups: {
            "/api/x": { count: 100, error_count: 1, error_rate: 0.01, p50_ms: 5, p95_ms: 40 },
          },
        }}
      />,
    );
    expect(html).toContain("Selbstmetriken");
    expect(html).toContain("100"); // total requests
    expect(html).toContain("40ms"); // worst p95
    expect(html).not.toContain("Erhöhte Fehlerquote");
  });

  it("shows the error badge when the aggregate error rate exceeds the threshold", () => {
    const html = renderToStaticMarkup(
      <SystemHealthStrip
        data={baseHealth}
        metrics={{
          schema: "hermes-metrics-lite-v1",
          checked_at: 100,
          uptime_seconds: 60,
          groups: { "/api/x": { count: 100, error_count: 20, error_rate: 0.2, p50_ms: 5, p95_ms: 90 } },
        }}
      />,
    );
    expect(html).toContain("Erhöhte Fehlerquote");
    expect(html).toContain("text-red-300");
  });

  it("degrades the metrics tile without breaking subsystem lights when metrics is null", () => {
    const html = renderToStaticMarkup(<SystemHealthStrip data={baseHealth} metrics={null} />);
    expect(html).toContain("Hermes-Gateway"); // subsystem lights intact
    expect(html).toContain("Metriken konnten nicht geladen werden."); // metrics tile degraded
  });
});
