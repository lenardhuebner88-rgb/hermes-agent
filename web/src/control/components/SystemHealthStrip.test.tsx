import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { SystemHealthStrip } from "./SystemHealthStrip";
import type { SystemHealthResponse } from "../lib/types";

const baseHealth: SystemHealthResponse = {
  schema: "hermes-health-v1",
  checked_at: 100,
  overall: "healthy",
  subsystems: {
    gateway: { status: "healthy", detail: "ok", error: null, latency_ms: 12 },
    openclaw: { status: "healthy", detail: "ok", error: null, latency_ms: 18 },
    autoresearch: { status: "healthy", detail: "fresh", error: null, heartbeat_age_s: 2 },
    kanban_db: { status: "healthy", detail: "ok", error: null, latency_ms: 4 },
  },
};

describe("SystemHealthStrip", () => {
  it("renders all four subsystem labels with full data", () => {
    const html = renderToStaticMarkup(<SystemHealthStrip data={baseHealth} now={120} />);
    expect(html).toContain("Hermes-Gateway");
    expect(html).toContain("OpenClaw-Proxy");
    expect(html).toContain("Autoresearch-Loop");
    expect(html).toContain("Kanban-DB");
  });

  it("shows amber tone for degraded subsystems", () => {
    const html = renderToStaticMarkup(
      <SystemHealthStrip
        data={{
          ...baseHealth,
          overall: "degraded",
          subsystems: { ...baseHealth.subsystems, openclaw: { status: "degraded", detail: "langsam", error: null } },
        }}
      />,
    );
    expect(html).toContain("border-amber-500/25");
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
    expect(html).toContain("border-red-500/25");
    expect(html).toContain("connection refused");
  });

  it("renders gray unknown fallback without data", () => {
    const html = renderToStaticMarkup(<SystemHealthStrip data={null} />);
    expect(html).toContain("border-zinc-600/25");
    expect(html).toContain("Status unbekannt");
  });
});
