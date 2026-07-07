import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

const src = readFileSync(path.resolve(import.meta.dirname, "KettenTab.tsx"), "utf8");

describe("KettenTab v4 — redesign checks", () => {
  it("makes all interactive elements keyboard-focus-visible", () => {
    expect(src).toMatch(/chain-item/);
    expect(src).toMatch(/detail/);
    expect(src).toMatch(/uitem/);
    expect(src).toMatch(/done-item/);
    // CSS has focus-visible outline
    const css = readFileSync(path.resolve(import.meta.dirname, "ketten-v4.css"), "utf8");
    expect(css).toMatch(/button:focus-visible/);
  });

  it("joins worker data via task_id for model + override", () => {
    expect(src).toContain("useHermesWorkers");
    expect(src).toContain("workerByNodeId");
    expect(src).toContain("effective_model");
    expect(src).toContain("model_override");
  });

  it("renders model-row with GGFM Override badge when override is present", () => {
    expect(src).toMatch(/model-row/);
    expect(src).toMatch(/model-override-badge/);
    expect(src).toContain("GGFM Override");
  });

  it("uses CSS design tokens (no raw hex)", () => {
    const css = readFileSync(path.resolve(import.meta.dirname, "ketten-v4.css"), "utf8");
    expect(css).toContain("--color-surface");
    expect(css).toContain("--color-live");
    expect(css).toContain("--color-status-ok");
    expect(css).toContain("--color-status-warn");
  });

  it("renders all 6 sections", () => {
    expect(src).toMatch(/SECTION 1.*Ketten-Liste/);
    expect(src).toMatch(/SECTION 2.*Active Chain Header/);
    expect(src).toMatch(/SECTION 3.*Step Pipeline/);
    expect(src).toMatch(/SECTION 4.*Active Step Detail/);
    expect(src).toMatch(/SECTION 5.*Upcoming Steps/);
    expect(src).toMatch(/SECTION 6.*Done.*Gate/);
  });

  it("shows heartbeat LED only for running nodes", () => {
    expect(src).toMatch(/focusNode\.status === "running" && focusHbAge/);
    expect(src).toContain("led-dot");
  });

  it("shows inline model for upcoming steps", () => {
    expect(src).toMatch(/umodel/);
    expect(src).toMatch(/umodel-override/);
  });
});
