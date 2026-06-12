import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { CronView } from "./CronView";
import { jobTone } from "./CronView.helpers";
import type { CronJob } from "../lib/types";

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
  it("renders the title and a gateway-down banner when no data is loaded yet", () => {
    const html = renderToStaticMarkup(<CronView density="airy" />);
    expect(html).toContain("Crons");
    // No data yet → gateway.running defaults false → operator-critical banner shows.
    expect(html).toContain("Gateway läuft nicht");
  });
});
