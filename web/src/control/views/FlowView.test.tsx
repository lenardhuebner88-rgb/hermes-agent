import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

// Guard: /control/flow must be LIVE — never the design-package demo data.
// (FLOW_LIVE_WIRING_ADDENDUM: flowMock must not be imported in the live view,
// and no design-package dummy run IDs may appear in the product path.)
const src = readFileSync(fileURLToPath(new URL("./FlowView.tsx", import.meta.url)), "utf8");

describe("FlowView is live-wired, not mock", () => {
  it("does not import flowMock (deleted from the product path)", () => {
    expect(src).not.toMatch(/flowMock/i);
  });

  it("contains none of the design-package demo run IDs", () => {
    for (const demo of ["REQ-142", "REQ-145", "REQ-150", "PLN-77", "PLN-81", "RUN-781", "RUN-786", "RUN-790", "CHK-58", "CHK-19", "SHIP-31", "SHIP-30"]) {
      expect(src, `demo id ${demo} leaked into FlowView`).not.toContain(demo);
    }
  });

  it("has no 'demo'/'mock' wording in the UI", () => {
    expect(src.toLowerCase()).not.toMatch(/demo-daten|mockdaten|demonstriert/);
  });

  it("reads from the live board + task-detail + worker hooks", () => {
    expect(src).toMatch(/useBoard/);
    expect(src).toMatch(/useTaskDetail/);
    expect(src).toMatch(/useHermesWorkers/);
    expect(src).toMatch(/groupByStage/);
  });
});
