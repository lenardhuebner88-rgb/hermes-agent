import { describe, expect, it } from "vitest";
import {
  buildReliabilityRiskModel,
  formatReliabilityWindow,
  riskToneForReliability,
} from "./fleetRisk";
import type { ReliabilityResponse } from "./schemas";

const baseReliability: ReliabilityResponse = {
  since_hours: 168,
  baseline_hours: 720,
  min_n: 5,
  now: 1_783_197_794,
  baseline: [],
  profiles: [
    { profile: "reviewer", runs: 115, tasks: 102, outcomes: { completed: 96 }, completed_rate: 0.8348, failed_rate: 0.0261, retries: 13, retry_rate: 0.113, judged: 0, approved: 0, rejected: 0, approve_rate: null, low_sample: false },
    { profile: "coder", runs: 110, tasks: 72, outcomes: { completed: 73, timed_out: 7 }, completed_rate: 0.6636, failed_rate: 0.0636, retries: 38, retry_rate: 0.3455, judged: 124, approved: 110, rejected: 14, approve_rate: 0.8871, low_sample: false },
    { profile: "research", runs: 7, tasks: 5, outcomes: { completed: 5, gave_up: 1, timed_out: 1 }, completed_rate: 0.7143, failed_rate: 0.2857, retries: 2, retry_rate: 0.2857, judged: 0, approved: 0, rejected: 0, approve_rate: null, low_sample: false },
    { profile: "unbekannt", runs: 70, tasks: 64, outcomes: { scheduled: 57 }, completed_rate: 0.0857, failed_rate: 0, retries: 6, retry_rate: 0.0857, judged: 0, approved: 0, rejected: 0, approve_rate: null, low_sample: false },
    { profile: "a", runs: 2, tasks: 1, outcomes: { completed: 1 }, completed_rate: 0.5, failed_rate: 0, retries: 1, retry_rate: 0.5, judged: 0, approved: 0, rejected: 0, approve_rate: null, low_sample: true },
    { profile: "admin", runs: 1, tasks: 1, outcomes: { completed: 1 }, completed_rate: 1, failed_rate: 0, retries: 0, retry_rate: 0, judged: 0, approved: 0, rejected: 0, approve_rate: null, low_sample: true },
    { profile: "default", runs: 1, tasks: 1, outcomes: { completed: 1 }, completed_rate: 1, failed_rate: 0, retries: 0, retry_rate: 0, judged: 0, approved: 0, rejected: 0, approve_rate: null, low_sample: true },
    { profile: "test-worker", runs: 1, tasks: 1, outcomes: { reclaimed: 1 }, completed_rate: 0, failed_rate: 0, retries: 0, retry_rate: 0, judged: 0, approved: 0, rejected: 0, approve_rate: null, low_sample: true },
  ],
};

describe("formatReliabilityWindow", () => {
  it("names the 168h window as letzte 7 Tage", () => {
    expect(formatReliabilityWindow(168)).toBe("letzte 7 Tage");
  });

  it("uses hours for short windows", () => {
    expect(formatReliabilityWindow(24)).toBe("letzte 24 Stunden");
  });
});

describe("buildReliabilityRiskModel", () => {
  it("keeps only operational active profiles and hides noise", () => {
    const model = buildReliabilityRiskModel({
      reliability: baseReliability,
      laneCatalogProfiles: [
        { name: "reviewer" },
        { name: "coder" },
        { name: "research" },
      ],
      activeWorkerProfiles: [],
    });

    expect(model.windowLabel).toBe("letzte 7 Tage");
    expect(model.rows.map((row) => row.profile)).toEqual(["research", "coder", "reviewer"]);
    expect(model.hiddenCount).toBe(5);
    expect(model.lowSampleHiddenCount).toBe(4);
    expect(model.noiseHiddenCount).toBe(1);
  });

  it("keeps an active worker even when the sample is still small", () => {
    const model = buildReliabilityRiskModel({
      reliability: {
        ...baseReliability,
        profiles: [
          ...baseReliability.profiles,
          { profile: "scout", runs: 2, tasks: 2, outcomes: { completed: 2 }, completed_rate: 1, failed_rate: 0, retries: 0, retry_rate: 0, judged: 0, approved: 0, rejected: 0, approve_rate: null, low_sample: true },
        ],
      },
      laneCatalogProfiles: [{ name: "scout" }],
      activeWorkerProfiles: ["scout"],
    });

    expect(model.rows.some((row) => row.profile === "scout")).toBe(true);
    expect(model.rows.find((row) => row.profile === "scout")?.sampleLabel).toBe("aktiv, wenig Daten");
  });

  it("opens the disclosure when notable risk exists", () => {
    const model = buildReliabilityRiskModel({
      reliability: baseReliability,
      laneCatalogProfiles: [{ name: "reviewer" }, { name: "coder" }, { name: "research" }],
      activeWorkerProfiles: [],
    });

    expect(model.notableCount).toBe(2);
    expect(model.defaultOpen).toBe(true);
    expect(model.summary).toBe("2 auffaellig · 3 Profile · letzte 7 Tage");
  });
});

describe("riskToneForReliability", () => {
  it("marks high failure or retry rates as amber/red", () => {
    expect(riskToneForReliability({ completed_rate: 0.92, failed_rate: 0.01, retry_rate: 0.05 })).toBe("emerald");
    expect(riskToneForReliability({ completed_rate: 0.72, failed_rate: 0.08, retry_rate: 0.3 })).toBe("amber");
    expect(riskToneForReliability({ completed_rate: 0.5, failed_rate: 0.3, retry_rate: 0.4 })).toBe("red");
  });
});


import { buildSystemPulseRiskModel } from "./fleetRisk";
import type { PressureStatusResponse, SystemHealthResponse } from "./types";

const healthySystem: SystemHealthResponse = {
  schema: "hermes-health-v1",
  checked_at: 1_783_197_794,
  overall: "healthy",
  subsystems: {
    gateway: { status: "healthy", detail: "ok", error: null, heartbeat_age_s: 9 },
    autoresearch: { status: "healthy", detail: "ok", error: null },
    kanban_db: { status: "healthy", detail: "ok", error: null },
    kanban_dispatcher: { status: "healthy", detail: "ok", error: null, heartbeat_age_s: 12 },
  },
};

const pressureWithBrowserTests: PressureStatusResponse = {
  schema: "hermes-pressure-v1",
  checked_at: 1_783_197_780,
  overall: "ok",
  cause: "Keine auffaellige Last",
  recommendation: { label: "Tests laufen", detail: "19 Browser-Testprozesse aktiv.", tone: "amber" },
  host: { cpu_count: 12, cpu_percent: 0, load_avg: [3.64, 3.36, 2.38], memory_percent: 52.3 },
  dashboard: { pid: 628936, rss_mb: 41.3, cpu_percent: 0, cpu_weight: 100, cpu_quota: "infinity", tasks_current: 4, num_threads: 1 },
  pressure_sources: [
    { kind: "browser_test", label: "browser test", count: 15, cpu_percent: 0, rss_mb: 1298.1, scope: "service", scope_kind: "service", throttled: false },
    { kind: "browser_test", label: "browser test", count: 4, cpu_percent: 0, rss_mb: 393.8, scope: "systemd scope", scope_kind: "scope", throttled: false },
  ],
  access: { tailnet: "inactive", detail: "no active tailnet peers", api_latency_ms: null },
  token_pressure: { class: "ok", pct: 14, updated_at: "2026-07-04T20:42:55.065561+00:00" },
  errors: [],
};

describe("buildSystemPulseRiskModel", () => {
  it("promotes Pressure recommendation without duplicating raw sources", () => {
    const model = buildSystemPulseRiskModel({ systemHealth: healthySystem, pressureStatus: pressureWithBrowserTests });

    expect(model.overallTone).toBe("amber");
    expect(model.headline).toBe("System ok · Tests laufen");
    expect(model.rows.map((row) => row.key)).toEqual(["gateway", "dispatcher", "pressure", "token", "host"]);
    expect(model.rows.find((row) => row.key === "pressure")?.value).toBe("Tests laufen");
    expect(model.rows.find((row) => row.key === "pressure")?.detail).toBe("19 Browser-Testprozesse aktiv.");
    expect(model.rows.find((row) => row.key === "token")?.value).toBe("ok · 14 %");
  });
});
