// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { FleetPipeline } from "./FleetPipeline";

afterEach(cleanup);

describe("FleetPipeline data identity", () => {
  it("gives all five labelled stages distinct shared data tokens", () => {
    const { container } = render(<FleetPipeline tasks={[]} />);
    const colors = Array.from(container.querySelectorAll<HTMLElement>(".hc-stage-rail"), (rail) =>
      rail.style.getPropertyValue("--hc-role"),
    );

    expect(colors).toEqual([
      "var(--color-data-6)",
      "var(--color-data-4)",
      "var(--color-data-3)",
      "var(--color-data-1)",
      "var(--color-data-2)",
    ]);
    expect(new Set(colors).size).toBe(5);
    for (const label of ["Capture", "Flow-Kette", "Execute", "Verify", "Ship"]) {
      expect(screen.getByText(label)).toBeTruthy();
    }
  });
});
