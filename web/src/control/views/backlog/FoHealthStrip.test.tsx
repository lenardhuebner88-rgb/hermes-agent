// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { BacklogContractHealth, BacklogItem } from "../../lib/schemas";
import { FoHealthStrip } from "./FoHealthStrip";

afterEach(cleanup);

const zeroHealth: BacklogContractHealth = {
  source_count: 0,
  counted_sum: 0,
  unknown_statuses: [],
  invalid_risk_count: 0,
  invalid_owner_count: 0,
  unowned_count: 0,
  stale_count: 0,
  missing_acceptance_count: 0,
  missing_next_action_count: 0,
  invalid_area_count: 0,
};

function item(overrides: Partial<BacklogItem>): BacklogItem {
  return {
    id: "0063",
    title: "Defekt",
    status: "blocked",
    owner: "claude",
    risk: "high",
    area: "lists",
    updated: "2026-07-10",
    lane: null,
    result: null,
    stale: false,
    ...overrides,
  };
}

function tile(label: string): HTMLElement {
  return screen.getByText(label).parentElement?.parentElement as HTMLElement;
}

describe("FoHealthStrip conditional defect dots", () => {
  it("shows no alarm dot for zero-count defect counters", () => {
    render(<FoHealthStrip items={[]} contractHealth={zeroHealth} />);

    for (const label of ["Blocked", "Unowned", "Stale", "High Risk", "Contract Drift", "Missing Acceptance"]) {
      expect(tile(label).querySelector(".hc-led")).toBeNull();
    }
  });

  it("shows the semantic alarm dot when each defect count is positive", () => {
    render(
      <FoHealthStrip
        items={[item({})]}
        contractHealth={{
          ...zeroHealth,
          unknown_statuses: [{ status: "drift", count: 1, ids: ["0063"] }],
          unowned_count: 1,
          stale_count: 1,
          missing_acceptance_count: 1,
        }}
      />,
    );

    for (const label of ["Blocked", "Stale", "High Risk"]) {
      expect(tile(label).querySelector(".hc-led-error")).not.toBeNull();
    }
    for (const label of ["Unowned", "Contract Drift", "Missing Acceptance"]) {
      expect(tile(label).querySelector(".hc-led-warn")).not.toBeNull();
    }
  });
});
