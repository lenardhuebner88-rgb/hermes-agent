import { describe, expect, it } from "vitest";
import { getAutoresearchReadiness } from "./autoresearchReadiness";

// A baseline "ready to run a dry-run" input: route configured, nothing running,
// nothing busy, no open cards. Each test overrides the gate dimension under test.
const ready = {
  state: "idle" as const,
  routeStatus: "configured",
  heartbeatFresh: true,
  loopRunning: false,
  openCount: 0,
  highPriorityCount: 0,
  busy: false,
};

describe("getAutoresearchReadiness gate decisions", () => {
  it("gates with 'Status laden' when neither state nor route status is known yet", () => {
    const r = getAutoresearchReadiness({ ...ready, state: null, routeStatus: null });
    expect(r.label).toBe("Status laden");
    expect(r.tone).toBe("amber");
    expect(r.title).toContain("Noch nicht starten");
  });

  it("gates hardest (red) when the loop has crashed, ahead of every other check", () => {
    // crashed wins even though the route is fine and cards are open
    const r = getAutoresearchReadiness({ ...ready, state: "crashed", openCount: 5, highPriorityCount: 2 });
    expect(r.tone).toBe("red");
    expect(r.label).toBe("Fehler prüfen");
  });

  it("gates with 'Route prüfen' when the model route is not configured", () => {
    const r = getAutoresearchReadiness({ ...ready, routeStatus: "unavailable" });
    expect(r.label).toBe("Route prüfen");
    expect(r.tone).toBe("amber");
  });

  it("tells the operator to observe (not restart) while a loop is running", () => {
    const r = getAutoresearchReadiness({ ...ready, loopRunning: true });
    expect(r.label).toBe("Lauf aktiv");
    expect(r.tone).toBe("cyan");
  });

  it("asks the operator to wait while another cockpit action is busy", () => {
    const r = getAutoresearchReadiness({ ...ready, busy: true });
    expect(r.label).toBe("Aktion läuft");
    expect(r.tone).toBe("violet");
  });

  it("flags review-with-priority (amber) when high-priority cards are open", () => {
    const r = getAutoresearchReadiness({ ...ready, openCount: 4, highPriorityCount: 2 });
    expect(r.label).toBe("Review bereit");
    expect(r.tone).toBe("amber");
    expect(r.title).toContain("wichtigen");
  });

  it("flags review-ready (emerald) when open cards exist but none are high priority", () => {
    const r = getAutoresearchReadiness({ ...ready, openCount: 4, highPriorityCount: 0 });
    expect(r.label).toBe("Review bereit");
    expect(r.tone).toBe("emerald");
  });

  it("reports fully ready-for-dry-run (emerald) when route is ok, loop quiet, and nothing open", () => {
    const r = getAutoresearchReadiness(ready);
    expect(r.label).toBe("Betriebsbereit");
    expect(r.tone).toBe("emerald");
    expect(r.title).toContain("Probelauf");
  });

  it("orders the gates so crashed beats route-not-ok beats loop-running", () => {
    // route is unavailable AND loop running AND busy, but crashed dominates
    expect(
      getAutoresearchReadiness({ ...ready, state: "crashed", routeStatus: "unavailable", loopRunning: true, busy: true }).label,
    ).toBe("Fehler prüfen");
    // not crashed: route-not-ok dominates loop-running + busy
    expect(
      getAutoresearchReadiness({ ...ready, routeStatus: "unavailable", loopRunning: true, busy: true }).label,
    ).toBe("Route prüfen");
    // route ok: loop-running dominates busy
    expect(getAutoresearchReadiness({ ...ready, loopRunning: true, busy: true }).label).toBe("Lauf aktiv");
  });

  it("exposes readiness facts including route, loop, open count, and heartbeat", () => {
    const r = getAutoresearchReadiness({ ...ready, openCount: 3, highPriorityCount: 1 });
    const byLabel = Object.fromEntries(r.facts.map((f) => [f.label, f.value]));
    expect(byLabel.Route).toBe("bereit");
    expect(byLabel.Offen).toBe("3 offen");
    expect(byLabel["Hoch+"]).toBe("1");
    expect(byLabel.Heartbeat).toBe("frisch");
  });
});
