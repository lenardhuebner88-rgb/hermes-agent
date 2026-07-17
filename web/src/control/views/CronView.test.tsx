import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { CronJob } from "../lib/types";

const hooks = vi.hoisted(() => ({
  useCronObservability: vi.fn(),
  useCronOutput: vi.fn(),
}));

vi.mock("../hooks/cron", () => ({
  useCronObservability: hooks.useCronObservability,
  useCronOutput: hooks.useCronOutput,
}));

import { CronView } from "./CronView";
import { jobTone } from "./CronView.helpers";

const baseJob: CronJob = {
  id: "j1",
  name: "Morgenbrief",
  enabled: true,
  state: "scheduled",
  paused_at: null,
  paused_reason: null,
  schedule_display: "täglich 07:00",
  repeat: "daily",
  next_run_at: 1000,
  last_run_at: 900,
  last_status: "ok",
  last_error: null,
  last_delivery_error: null,
  deliver: "discord:123",
  skill: null,
  model: null,
  profile: "research",
  is_default_profile: false,
  has_script: false,
  has_prompt: true,
  latest_output: { filename: "x.md", mtime: 1, size_bytes: 3, run_count: 2 },
};

describe("jobTone", () => {
  it("is red when delivery failed (status:ok must not mask it)", () => {
    expect(jobTone({ ...baseJob, last_status: "ok", last_delivery_error: "Discord 500" }).tone).toBe("red");
  });
  it("is red on a run error", () => {
    expect(jobTone({ ...baseJob, last_error: "boom" }).tone).toBe("red");
  });
  it("is amber when paused or disabled", () => {
    expect(jobTone({ ...baseJob, state: "paused", paused_at: 5 }).tone).toBe("amber");
    expect(jobTone({ ...baseJob, enabled: false }).tone).toBe("amber");
  });
  it("is emerald when healthy", () => {
    expect(jobTone(baseJob).tone).toBe("emerald");
  });
});

describe("CronView", () => {
  beforeEach(() => {
    hooks.useCronObservability.mockReturnValue({
      data: null,
      error: null,
      errorObj: null,
      loading: true,
      lastUpdated: null,
      isStale: false,
      busyJob: null,
      actionError: null,
      reload: vi.fn(),
      updateData: vi.fn(),
      trigger: vi.fn(),
      pause: vi.fn(),
      resume: vi.fn(),
    });
    hooks.useCronOutput.mockReturnValue({
      outputById: {},
      errorById: {},
      loadingId: null,
      load: vi.fn(),
    });
  });

  it("renders the eyebrow and a gateway-down banner when no data is loaded yet", () => {
    const html = renderToStaticMarkup(<CronView density="airy" />);
    // Route name lives once in the shared Puls-Leiste (not re-rendered here) —
    // this view only carries the eyebrow + subtitle band.
    expect(html).toContain("Geplante Jobs");
    // No data yet → gateway.running defaults false → operator-critical banner shows.
    expect(html).toContain("Gateway läuft nicht");
  });

  it("renders an empty cron inventory with neutral tone and no status-ok class", () => {
    hooks.useCronObservability.mockReturnValue({
      data: { jobs: [], gateway: { running: true, pid: 42 } },
      error: null,
      errorObj: null,
      loading: false,
      lastUpdated: 1,
      isStale: false,
      busyJob: null,
      actionError: null,
      reload: vi.fn(),
      updateData: vi.fn(),
      trigger: vi.fn(),
      pause: vi.fn(),
      resume: vi.fn(),
    });

    const html = renderToStaticMarkup(<CronView density="airy" />);

    expect(html).toContain('class="hc-fleet-empty"');
    expect(html).toContain("Keine Cron-Jobs gefunden.");
    expect(html).toContain("Es gibt derzeit nichts auszuführen.");
    expect(html).not.toContain("hc-fleet-empty ok");
    expect(html).not.toContain("status-ok");
  });

  it("renders text action buttons, not the upstream aspect-square icon variant", () => {
    // Upstream Button size="xs" became icon-only (p-1 aspect-square grid-cols-1):
    // with a text label it renders as a huge square with letter-wrapped text.
    // Text actions (Jetzt auslösen / Pausieren / Fortsetzen) must use a text size.
    hooks.useCronObservability.mockReturnValue({
      data: { jobs: [baseJob], gateway: { running: true, pid: 42 } },
      error: null,
      errorObj: null,
      loading: false,
      lastUpdated: 1,
      isStale: false,
      busyJob: null,
      actionError: null,
      reload: vi.fn(),
      updateData: vi.fn(),
      trigger: vi.fn(),
      pause: vi.fn(),
      resume: vi.fn(),
    });

    const html = renderToStaticMarkup(<CronView density="airy" />);

    expect(html).toContain("Jetzt auslösen");
    expect(html).not.toContain("aspect-square");
  });
});
