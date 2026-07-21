// @vitest-environment jsdom
/**
 * SystemVitals — G4: rendert echte Kurven aus Hook-Daten, Sammel-Zustand
 * bei <2 Samples, Prozent-Anzeige.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const historyState = vi.hoisted(() => ({
  cpu: [] as number[],
  mem: [] as number[],
  live: false,
  source: "ring" as "endpoint" | "ring",
  cpuNow: null as number | null,
  memNow: null as number | null,
}));

vi.mock("./useSystemHistory", async () => {
  const actual = await vi.importActual<typeof import("./useSystemHistory")>(
    "./useSystemHistory",
  );
  return {
    ...actual,
    useSystemHistory: () => ({ ...historyState }),
  };
});

import { SystemVitals } from "./SystemVitals";

beforeEach(() => {
  historyState.cpu = [];
  historyState.mem = [];
  historyState.live = false;
  historyState.source = "ring";
  historyState.cpuNow = null;
  historyState.memNow = null;
});

afterEach(() => cleanup());

describe("SystemVitals", () => {
  it("Sammel-Zustand (<2 Samples, Ringpuffer): zeigt Sammel-Hinweis", () => {
    historyState.cpu = [42];
    historyState.mem = [];
    historyState.source = "ring";

    render(<SystemVitals />);

    expect(screen.getByText("Vitals sammeln …")).toBeTruthy();
    expect(screen.queryByLabelText("System-Vitals")).toBeNull();
  });

  it("rendert Kurven und Prozentwerte aus Hook-Daten", () => {
    historyState.cpu = [10, 20, 30];
    historyState.mem = [40, 50, 60];
    historyState.live = true;
    historyState.source = "ring";
    historyState.cpuNow = 30;
    historyState.memNow = 60;

    const { container } = render(<SystemVitals />);

    expect(screen.getByLabelText("System-Vitals")).toBeTruthy();
    expect(screen.getByText("30%")).toBeTruthy();
    expect(screen.getByText("60%")).toBeTruthy();
    expect(screen.getByText("CPU")).toBeTruthy();
    expect(screen.getByText("RAM")).toBeTruthy();

    // Echte SVG-Pfade (keine Fake-Flatline: Pfad enthält mehrere L-Segmente).
    const paths = container.querySelectorAll(".jv-vital-spark path");
    expect(paths.length).toBe(2);
    expect(paths[0].getAttribute("d")).toContain("L");
    expect(paths[1].getAttribute("d")).toContain("L");
  });

  it("Endpoint-Quelle mit 1 Sample: zeigt Kurven (kein Sammel-Zustand)", () => {
    historyState.cpu = [55];
    historyState.mem = [66];
    historyState.live = true;
    historyState.source = "endpoint";
    historyState.cpuNow = 55;
    historyState.memNow = 66;

    render(<SystemVitals />);

    // source=endpoint → kein Sammel-Zustand trotz 1 Sample.
    expect(screen.queryByText("Vitals sammeln …")).toBeNull();
    expect(screen.getByText("55%")).toBeTruthy();
    expect(screen.getByText("66%")).toBeTruthy();
  });

  it("Prozent-Anzeige Strich bei fehlendem Punktwert", () => {
    historyState.cpu = [10, 20];
    historyState.mem = [30, 40];
    historyState.live = true;
    historyState.source = "ring";
    historyState.cpuNow = null;
    historyState.memNow = null;

    render(<SystemVitals />);

    const dashes = screen.getAllByText("–");
    expect(dashes.length).toBe(2);
  });
});
