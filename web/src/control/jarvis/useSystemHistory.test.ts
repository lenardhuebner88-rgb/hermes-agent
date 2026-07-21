// @vitest-environment jsdom
/**
 * useSystemHistory — G4: sparkPathFromSeries (pure) + Hook-Verhalten
 * (Endpoint-Vorrang, 404 → Ringpuffer, Fehler-Isolation, Fenster-Cap).
 */
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { sparkPathFromSeries, useSystemHistory } from "./useSystemHistory";

// ── sparkPathFromSeries (pure) ────────────────────────────────────────

describe("sparkPathFromSeries", () => {
  it("leere Serie → leerer Pfad", () => {
    expect(sparkPathFromSeries([])).toBe("");
  });

  it("1 Punkt 50 % → horizontale Linie in der Mitte (y=12)", () => {
    expect(sparkPathFromSeries([50])).toBe("M0 12 L100 12");
  });

  it("1 Punkt 0 % → unterer Rand (y=24)", () => {
    expect(sparkPathFromSeries([0])).toBe("M0 24 L100 24");
  });

  it("1 Punkt 100 % → oberer Rand (y=0)", () => {
    expect(sparkPathFromSeries([100])).toBe("M0 0 L100 0");
  });

  it("Normalfall 3 Punkte: korrekte x/y-Koordinaten", () => {
    // [0, 50, 100] → x: 0/50/100, y: 24/12/0
    expect(sparkPathFromSeries([0, 50, 100])).toBe("M0 24 L50 12 L100 0");
  });

  it("Min/Max-Skalierung: Werte außerhalb 0–100 werden geclampt", () => {
    expect(sparkPathFromSeries([-10, 150])).toBe("M0 24 L100 0");
  });

  it("kein NaN bei Nicht-Zahlen (NaN, Infinity)", () => {
    const path = sparkPathFromSeries([NaN, Infinity, 50]);
    expect(path).not.toContain("NaN");
    expect(path).toMatch(/^M[\d.]+ [\d.]+ L/);
  });
});

// ── Hook-Mocks ────────────────────────────────────────────────────────

const getSystemStatsHistoryMock = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api", () => ({
  api: { getSystemStatsHistory: getSystemStatsHistoryMock },
}));

const statsState = vi.hoisted(() => ({
  data: null as { cpu_percent?: number; memory?: { percent: number } } | null,
  lastUpdated: null as number | null,
}));

vi.mock("./useSystemStats", () => ({
  useSystemStats: () => ({
    data: statsState.data,
    lastUpdated: statsState.lastUpdated,
    error: null,
    loading: false,
    reload: vi.fn(),
    updateData: vi.fn(),
  }),
}));

beforeEach(() => {
  getSystemStatsHistoryMock.mockReset();
  statsState.data = null;
  statsState.lastUpdated = null;
});

afterEach(() => cleanup());

// ── Endpoint-Vorrang ──────────────────────────────────────────────────

describe("useSystemHistory — Endpoint", () => {
  it("Real-Contract (samples mit cpu_percent/mem_percent) → Serien, source=endpoint", async () => {
    getSystemStatsHistoryMock.mockResolvedValue({
      interval_s: 60,
      window_s: 7200,
      samples: [
        { ts: 1000, cpu_percent: 10, mem_percent: 40 },
        { ts: 1060, cpu_percent: 20, mem_percent: 50 },
        { ts: 1120, cpu_percent: 30, mem_percent: 60 },
      ],
    });

    const { result } = renderHook(() => useSystemHistory());

    await waitFor(() => expect(result.current.source).toBe("endpoint"));
    expect(result.current.cpu).toEqual([10, 20, 30]);
    expect(result.current.mem).toEqual([40, 50, 60]);
    expect(result.current.live).toBe(true);
  });

  it("leere Endpoint-Samples → Ringpuffer-Fallback", async () => {
    getSystemStatsHistoryMock.mockResolvedValue({
      interval_s: 60,
      window_s: 7200,
      samples: [],
    });

    const { result } = renderHook(() => useSystemHistory());

    // Endpoint liefert keine Samples → Ringpuffer-Modus.
    await waitFor(() => expect(result.current.source).toBe("ring"));
  });
});

// ── Ringpuffer (404 / Netzfehler) ────────────────────────────────────

describe("useSystemHistory — Ringpuffer", () => {
  it("404 → Ringpuffer-Modus; Samples akkumulieren aus simuliertem 15s-Poll", async () => {
    getSystemStatsHistoryMock.mockRejectedValue(new Error("404: Not Found"));

    const { result, rerender } = renderHook(() => useSystemHistory());

    await waitFor(() => expect(result.current.source).toBe("ring"));
    expect(result.current.cpu).toEqual([]);
    expect(result.current.live).toBe(false);

    // Erster Poll.
    act(() => {
      statsState.data = { cpu_percent: 25, memory: { percent: 55 } };
      statsState.lastUpdated = 1000;
    });
    rerender();

    await waitFor(() => expect(result.current.cpu.length).toBe(1));
    expect(result.current.cpu).toEqual([25]);
    expect(result.current.mem).toEqual([55]);
    expect(result.current.cpuNow).toBe(25);
    expect(result.current.memNow).toBe(55);
    expect(result.current.live).toBe(true);

    // Zweiter Poll → Fenster wächst.
    act(() => {
      statsState.data = { cpu_percent: 30, memory: { percent: 60 } };
      statsState.lastUpdated = 1015;
    });
    rerender();

    await waitFor(() => expect(result.current.cpu.length).toBe(2));
    expect(result.current.cpu).toEqual([25, 30]);
    expect(result.current.mem).toEqual([55, 60]);
  });

  it("Fenster-Cap: maximal 40 Samples, älteste verworfen", async () => {
    getSystemStatsHistoryMock.mockRejectedValue(new Error("404"));

    const { result, rerender } = renderHook(() => useSystemHistory());

    for (let i = 0; i < 50; i++) {
      act(() => {
        statsState.data = { cpu_percent: i, memory: { percent: i } };
        statsState.lastUpdated = 1000 + i * 15;
      });
      rerender();
    }

    await waitFor(() => expect(result.current.cpu.length).toBe(40));
    // Älteste 10 verworfen: [10 … 49].
    expect(result.current.cpu[0]).toBe(10);
    expect(result.current.cpu[39]).toBe(49);
  });

  it("Netzfehler (TypeError) → Ringpuffer, keine Exception", async () => {
    getSystemStatsHistoryMock.mockRejectedValue(new TypeError("Failed to fetch"));

    const { result } = renderHook(() => useSystemHistory());

    await waitFor(() => expect(result.current.source).toBe("ring"));
    expect(result.current.live).toBe(false);
    expect(result.current.cpu).toEqual([]);
  });

  it("psutil-Felder fehlen (cpu_percent undefined) → kein Sample, kein Crash", async () => {
    getSystemStatsHistoryMock.mockRejectedValue(new Error("404"));

    const { result, rerender } = renderHook(() => useSystemHistory());

    act(() => {
      statsState.data = {};
      statsState.lastUpdated = 1000;
    });
    rerender();

    // Kein Sample angehängt (beide Felder undefined).
    expect(result.current.cpu).toEqual([]);
    expect(result.current.source).toBe("ring");
  });
});
