// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { SignalStrip } from "./OrchestratorSections";

afterEach(cleanup);

function tile(label: string): HTMLElement {
  return screen.getByText(label).parentElement?.parentElement as HTMLElement;
}

describe("Orchestrator SignalStrip conditional defect dots", () => {
  it("shows no alarm dot for zero-count defect counters", () => {
    render(<SignalStrip signals={{ ready: 0, blocked: 0, unowned: 0, staleProof: 0, highRisk: 0, contractDrift: 0 }} />);

    for (const label of ["Blocked", "Unowned", "Stale Proof", "High Risk", "Contract Drift"]) {
      expect(tile(label).querySelector(".hc-led")).toBeNull();
    }
  });

  it("shows the semantic alarm dot when each defect count is positive", () => {
    render(<SignalStrip signals={{ ready: 0, blocked: 1, unowned: 1, staleProof: 1, highRisk: 1, contractDrift: 1 }} />);

    for (const label of ["Blocked", "High Risk", "Contract Drift"]) {
      expect(tile(label).querySelector(".hc-led-error")).not.toBeNull();
    }
    for (const label of ["Unowned", "Stale Proof"]) {
      expect(tile(label).querySelector(".hc-led-warn")).not.toBeNull();
    }
  });
});
