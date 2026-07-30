import { describe, expect, it } from "vitest";
import {
  CONTROL_ROUTES,
  activeFromPath,
  legacyControlRedirectTarget,
  moreRoutes,
  primaryRoutes,
  tabPath,
  type ControlRouteEntry,
  type ControlTab,
} from "./navigation";

const routes = CONTROL_ROUTES as readonly ControlRouteEntry[];

/**
 * Guards the ONE route table (UI/UX-Iteration 3): it replaced six hand-synced
 * structures (ControlPage lazy imports, activeFromPath, viewImporters, tabPath,
 * the ControlTab union, ControlShell tabs/moreTabs). These tests pin table
 * integrity and the derived consumers, so drift between nav, routing, and
 * active-tab detection cannot come back silently.
 */
describe("control route table", () => {
  it("has unique ids, paths, and segments", () => {
    const ids = CONTROL_ROUTES.map((route) => route.id);
    const paths = CONTROL_ROUTES.map((route) => route.path);
    const segments = CONTROL_ROUTES.filter((route) => route.segment).map((route) => route.segment);
    expect(new Set(ids).size).toBe(ids.length);
    expect(new Set(paths).size).toBe(paths.length);
    expect(new Set(segments).size).toBe(segments.length);
  });

  it("keeps the five primary tabs in the fixed daily-spine order", () => {
    expect(primaryRoutes.map((route) => route.id)).toEqual(["fleet", "inbox", "agentTerminals", "statistik", "bibliothek"]);
    // Jede Primary trägt ein Kurzlabel für die mobile Bottom-Bar.
    for (const route of primaryRoutes) expect(route.mobileLabel).toBeTruthy();
  });

  it("covers every non-primary tab exactly once in the 'Mehr' tier", () => {
    expect(moreRoutes.length).toBe(CONTROL_ROUTES.length - primaryRoutes.length);
    expect(moreRoutes.every((route) => route.nav === "more")).toBe(true);
  });

  it("derives tabPath from the table for every tab", () => {
    for (const route of CONTROL_ROUTES) {
      expect(tabPath[route.id as ControlTab]).toBe(route.path);
    }
    expect(Object.keys(tabPath).sort()).toEqual(CONTROL_ROUTES.map((route) => route.id).sort());
  });

  it("gives every lazy tab an importer + export name, and only inbox is eager", () => {
    for (const route of routes) {
      if (route.id === "inbox") {
        expect(route.load).toBeUndefined();
      } else {
        expect(typeof route.load).toBe("function");
        expect(route.exportName).toBeTruthy();
      }
    }
  });

  it("maps every live route path to its own tab", () => {
    for (const route of routes) {
      if (!route.segment) continue; // inbox = Start-Landing (Default)
      expect(activeFromPath(route.path)).toBe(route.id);
    }
  });

  it("maps legacy redirect-only paths to their surviving tab", () => {
    expect(activeFromPath("/control/flow")).toBe("fleet");
    expect(activeFromPath("/control/ketten")).toBe("fleet");
    expect(activeFromPath("/control/hermes")).toBe("fleet");
    expect(activeFromPath("/control/pulse")).toBe("system");
    expect(activeFromPath("/control/pressure")).toBe("system");
    expect(activeFromPath("/control/ops")).toBe("system");
    expect(activeFromPath("/control/overview")).toBe("bibliothek");
  });

  it("pins detail pages to their parent tab and falls back to inbox", () => {
    expect(activeFromPath("/control/runs/run_42")).toBe("workstreams");
    expect(activeFromPath("/control/issues")).toBe("statistik");
    expect(activeFromPath("/control/design-board/c_123")).toBe("designBoard");
    expect(activeFromPath("/control/projekte-klassisch")).toBe("projekte");
    expect(activeFromPath("/control")).toBe("inbox");
    expect(activeFromPath("/control/inbox")).toBe("inbox");
    expect(activeFromPath("/control/does-not-exist")).toBe("inbox");
  });
});

describe("legacy control redirects", () => {
  it("preserves drawer and filter query params when old fleet routes redirect", () => {
    expect(legacyControlRedirectTarget("/control/fleet", "?task=t_42&filter=blocked")).toBe("/control/fleet?task=t_42&filter=blocked");
    expect(legacyControlRedirectTarget("/control/system", "?filter=ops&task=t_99")).toBe("/control/system?filter=ops&task=t_99");
  });

  it("keeps clean targets clean when no query was present", () => {
    expect(legacyControlRedirectTarget("/control/bibliothek", "")).toBe("/control/bibliothek");
  });
});
