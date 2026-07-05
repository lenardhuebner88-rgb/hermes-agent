import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { PressureContent } from "./PressureContent";
import type { PressureStatusResponse } from "../../lib/types";

const busyPressure: PressureStatusResponse = {
  schema: "hermes-pressure-v1",
  checked_at: 1782070000,
  overall: "busy",
  cause: "Ungedrosselte Testprozesse in Session-Scope",
  recommendation: {
    label: "Tests laufen",
    detail: "2 Test-/Browser-Prozesse aktiv.",
    tone: "amber",
  },
  host: {
    cpu_percent: 72,
    load_avg: [7.2, 6.8, 5.1],
    cpu_count: 8,
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
    count: 1,
    cpu_percent: 190,
    rss_mb: 410,
    scope: "session scope",
    scope_kind: "session",
    throttled: false,
  }, {
    kind: "browser_test",
    label: "browser test",
    count: 1,
    cpu_percent: 90,
    rss_mb: 510,
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
};

describe("PressureContent", () => {
  it("renders the compact pressure strip, chips, and cause", () => {
    const html = renderToStaticMarkup(<PressureContent data={busyPressure} lastUpdated={1782070000} isStale={false} />);

    expect(html).toContain("Pressure");
    expect(html).toContain("Busy");
    expect(html).toContain("Last");
    expect(html).toContain("CPU");
    expect(html).toContain("RAM");
    expect(html).toContain("API");
    expect(html).toContain("Tailnet");
    expect(html).toContain("2 Tests");
    expect(html).toContain("280% Test-CPU");
    expect(html).toContain("8-Core-Schwelle");
    expect(html).not.toContain("12-Core-Schwelle");
    expect(html).toContain("Nächster Hebel");
    expect(html).toContain("Tests laufen");
    expect(html).toContain("Ungedrosselte Testprozesse");
    expect(html).toContain("pytest");
    expect(html).toContain("Browser-Tests: browser test");
    expect(html).not.toContain("/home/");
    expect(html).not.toContain("run_tests_parallel.py");
  });

  it("does not claim high load when no pressure role is visible", () => {
    const calmPressure: PressureStatusResponse = {
      ...busyPressure,
      overall: "ok",
      cause: "Keine auffaellige Last",
      recommendation: { label: "Kein Hebel", detail: "Keine auffaellige Last erkannt.", tone: "emerald" },
      pressure_sources: [],
      host: { ...busyPressure.host, cpu_percent: 12, load_avg: [0.3, 0.4, 0.5] },
    };

    const html = renderToStaticMarkup(<PressureContent data={calmPressure} lastUpdated={1782070000} isStale={false} />);

    expect(html).toContain("Keine auffaellige Quelle");
    expect(html).toContain("Keine Tests, Browser oder Agenten als Druckquelle erkannt.");
    expect(html).not.toContain("mit hoher Last erkannt");
  });
});
