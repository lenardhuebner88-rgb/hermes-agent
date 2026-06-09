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

  it("shows quiet operator explanations next to Flow subtask status pills", () => {
    expect(src).toMatch(/getFlowSubtaskStatusExplanation/);
    expect(src).toMatch(/c\.status === "blocked" \? c\.latest_summary : null/);
    expect(src).toMatch(/hc-dim/);
    expect(src).toMatch(/flex-wrap/);
  });

  it("renders a compact dependency-chain explanation from task-detail links", () => {
    expect(src).toMatch(/FlowChainInsight/);
    expect(src).toMatch(/detail\?\.links/);
    expect(src).toMatch(/Gehalten/);
    expect(src).toMatch(/Ready-Nachbar im Snapshot/);
    expect(src).not.toMatch(/Startbarer Snapshot-Kandidat/);
    expect(src).toMatch(/Läuft bereits/);
    expect(src).toMatch(/Direkte Verknüpfungen/);
    expect(src).toMatch(/Mögliche Vorgänger/);
    expect(src).toMatch(/Snapshot-Hinweis/);
    expect(src).toMatch(/todo ist uneindeutig/);
    expect(src).toMatch(/Snapshot-Alter/);
  });

  it("does not promote raw task-detail parents into certain blocking-cause copy", () => {
    expect(src).not.toMatch(/Wartet auf direkte Parents/);
    expect(src).not.toMatch(/Fan-in: .*Parents müssen abgeschlossen sein/);
    expect(src).not.toMatch(/Wartet auf direkte Parents:/);
    expect(src).not.toMatch(/Parent-Warten/);
    expect(src).not.toMatch(/label=\{`Parent /);
  });

  it("caveats chain-start copy as release-only rather than a force-run promise", () => {
    expect(src).toMatch(/keine Scheduler-Zusage/);
    expect(src).toMatch(/Queue\/Assignee/);
    expect(src).toMatch(/gibt gehaltene Subtasks frei/);
  });

  it("guards single dispatch for held Flow subtasks with a chain-first choice", () => {
    expect(src).toMatch(/getHeldFlowDispatchGuard/);
    expect(src).toMatch(/singleDispatch/);
    expect(src).toMatch(/onReleaseChain/);
    expect(src).toMatch(/onDispatchSingle/);
  });
});
