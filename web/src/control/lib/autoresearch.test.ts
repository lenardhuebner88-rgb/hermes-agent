import { describe, expect, it } from "vitest";
import { clampLoopIterations, describeLoopStatus } from "./autoresearch";
import type { AutoresearchStatus } from "./types";

const base: AutoresearchStatus = {
  state: "idle", pid: null, request_id: null, iteration: 0, max: 0,
  last_step: null, last_eval: null, route_status: "configured",
  heartbeat_age_s: null, heartbeat_fresh: false, last_receipt: null, last_run: null, note: null,
};

describe("autoresearch loop display", () => {
  it("clamps loop iterations to the bounded runner range", () => {
    expect(clampLoopIterations(0)).toBe(1);
    expect(clampLoopIterations(3)).toBe(3);
    expect(clampLoopIterations(99)).toBe(5);
  });

  it("does not show 0/0 when idle", () => {
    const view = describeLoopStatus(base);
    expect(view.iterationLabel).toBe("kein Lauf aktiv");
    expect(view.progressPercent).toBe(0);
  });

  it("shows live progress and heartbeat freshness while running", () => {
    const view = describeLoopStatus({ ...base, state: "running", iteration: 2, max: 5, heartbeat_age_s: 7, heartbeat_fresh: true, last_step: "eval" });
    expect(view.iterationLabel).toBe("2 / 5");
    expect(view.progressPercent).toBe(40);
    expect(view.heartbeatLabel).toBe("7s frisch");
    expect(view.stepLabel).toBe("eval");
  });

  it("marks unconfirmed model routes as amber", () => {
    const view = describeLoopStatus({ ...base, route_status: "unavailable" });
    expect(view.routeTone).toBe("amber");
    expect(view.routeHint).toBe("Modell-Route nicht bestätigt");
  });
});
