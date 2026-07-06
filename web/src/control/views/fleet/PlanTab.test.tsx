import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

const src = readFileSync(path.resolve(import.meta.dirname, "PlanTab.tsx"), "utf8");

describe("PlanTab profile approvals", () => {
  it("uses profile selection and sends assignee overrides instead of lane model ids", () => {
    expect(src).toContain("assigneeOverrides");
    expect(src).toContain("<ProfileSelect");
    expect(src).toContain("lanesCatalog?.profiles");
    expect(src).not.toContain("setLaneModels");
    expect(src).not.toContain("lane_models");
  });

  it("shows profile-specific accessible copy", () => {
    expect(src).toContain("Profil-Select (je Lane)");
    expect(src).toContain("Profil für Lane");
  });
});
