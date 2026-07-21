/**
 * graphEngine — flake-free unit tests (sync ticks only, no rAF/waitFor).
 * 500-Knoten-Generator, Settle-Budget, Hit-Test, visualState, Port-Parität.
 */
import { describe, expect, it } from "vitest";

import type { PaGraphCluster, PaGraphEdge, PaGraphNode } from "@/lib/api";

import {
  BIG_LABEL_WEIGHT,
  buildModel,
  CLUSTER_CENTERS,
  computeVisualState,
  createSimulation,
  edgePath,
  edgeTier,
  hitTest,
  FIT_MARGIN,
  fitScale,
  fitTransform,
  IDENTITY_TRANSFORM,
  LABEL_WEIGHT,
  LABELS_PER_CLUSTER,
  MAX_LABEL_CHARS,
  nodeRadius,
  openGraphRef,
  screenToWorld,
  truncateLabel,
  WORLD_HEIGHT,
  WORLD_WIDTH,
  worldToScreen,
  ZOOM_MAX,
  ZOOM_MIN,
  zoomExtentMin,
} from "./graphEngine";
import { FIXTURE_CLUSTERS, generateLargeGraph } from "./graphFixtures";

// Re-export for any external consumers of the F1 test helper path.
export { generateLargeGraph };

const CLUSTERS = FIXTURE_CLUSTERS;

function smallFixture(): { clusters: PaGraphCluster[]; nodes: PaGraphNode[]; edges: PaGraphEdge[] } {
  const nodes: PaGraphNode[] = [
    { id: "a", label: "Alpha", cluster: "canon", kind: "doc", weight: 0.9, x: 640, y: 310 },
    { id: "b", label: "Beta", cluster: "canon", kind: "doc", weight: 0.6, x: 660, y: 330 },
    { id: "c", label: "Gamma", cluster: "projekte", kind: "project", weight: 0.8, x: 500, y: 590 },
    { id: "d", label: null, cluster: "projekte", kind: "task", weight: 0.3, x: 520, y: 610 },
    { id: "e", label: "Low", cluster: "agenten", kind: "agent", weight: 0.35, x: 850, y: 285 },
  ];
  const edges: PaGraphEdge[] = [
    { from: "a", to: "b", kind: "wikilink" },
    { from: "a", to: "c", kind: "wikilink" },
    { from: "c", to: "d", kind: "project-task" },
  ];
  return { clusters: CLUSTERS, nodes, edges };
}

describe("graphEngine — port parity", () => {
  it("nodeRadius matches A4 linear map (0.2→4.2, 1.0→15.0)", () => {
    expect(nodeRadius(0.2)).toBe(4.2);
    expect(nodeRadius(1.0)).toBe(15);
    expect(nodeRadius(0.5)).toBe(8.3);
  });

  it("edgeTier matches historical thresholds", () => {
    expect(edgeTier(0.55)).toEqual({ opacity: 0.34, width: 1.2 });
    expect(edgeTier(0.35)).toEqual({ opacity: 0.24, width: 1.0 });
    expect(edgeTier(0.2)).toEqual({ opacity: 0.17, width: 0.8 });
  });

  it("edgePath pins exact A4 quadratic string", () => {
    // Same values JarvisGraph.test pins via live fixture (vision→hermes).
    expect(edgePath(640, 400, 520, 520)).toBe("M640 400 Q569.2 449.2 520 520");
    expect(edgePath(100, 100, 200, 200)).toBe("M100 100 Q141.0 159.0 200 200");
  });

  it("truncateLabel caps at MAX_LABEL_CHARS with ellipsis", () => {
    expect(MAX_LABEL_CHARS).toBe(30);
    expect(truncateLabel("short")).toBe("short");
    const long = "a".repeat(40);
    expect(truncateLabel(long)).toBe(`${"a".repeat(29)}…`);
    expect(truncateLabel(long).length).toBe(30);
  });

  it("openGraphRef navigates /control and /api only", () => {
    const navigated: string[] = [];
    const assigned: string[] = [];
    const nav = {
      navigate: (p: string) => {
        navigated.push(p);
      },
      assign: (u: string) => {
        assigned.push(u);
      },
    };
    expect(openGraphRef("/control/fleet", nav)).toBe(true);
    expect(navigated).toEqual(["/control/fleet"]);
    expect(openGraphRef("/api/x", nav)).toBe(true);
    expect(assigned).toEqual(["/api/x"]);
    expect(openGraphRef("vault://x", nav)).toBe(false);
    expect(openGraphRef(undefined, nav)).toBe(false);
  });
});

describe("graphEngine — label model", () => {
  it("caps 5 labels per cluster, threshold 0.45, big ≥ 0.7, focus forces label", () => {
    const nodes: PaGraphNode[] = [];
    // 8 labeled canon nodes above threshold — only top 5 should label
    for (let i = 0; i < 8; i += 1) {
      nodes.push({
        id: `c${i}`,
        label: `C${i}`,
        cluster: "canon",
        kind: "doc",
        weight: 0.5 + i * 0.05, // 0.50 … 0.85
        x: 640 + i,
        y: 310,
      });
    }
    // below threshold with label
    nodes.push({
      id: "low",
      label: "Low",
      cluster: "canon",
      kind: "doc",
      weight: 0.3,
      x: 600,
      y: 300,
    });
    // another cluster hub for big
    nodes.push({
      id: "hub",
      label: "Hub",
      cluster: "projekte",
      kind: "project",
      weight: 0.95,
      x: 500,
      y: 590,
    });

    const model = buildModel(CLUSTERS, nodes, [], null);
    const canonLabeled = [...model.labels.keys()].filter((id) => id.startsWith("c"));
    expect(canonLabeled).toHaveLength(LABELS_PER_CLUSTER);
    // top 5 by weight: c7..c3
    expect(canonLabeled.sort()).toEqual(["c3", "c4", "c5", "c6", "c7"].sort());
    expect(model.labels.get("c7")).toBe("big"); // 0.85 ≥ 0.7
    expect(model.labels.has("low")).toBe(false);
    expect(model.labels.get("hub")).toBe("big");

    // focus forces label under threshold
    const focused = buildModel(CLUSTERS, nodes, [], "low");
    expect(focused.labels.get("low")).toBe("normal");
    expect(LABEL_WEIGHT).toBe(0.45);
    expect(BIG_LABEL_WEIGHT).toBe(0.7);
  });
});

describe("graphEngine — visualState", () => {
  it("without focus everything is normal", () => {
    const { clusters, nodes, edges } = smallFixture();
    const model = buildModel(clusters, nodes, edges, null);
    const vs = computeVisualState(model, null);
    for (const n of model.sortedNodes) expect(vs.nodes.get(n.id)).toBe("normal");
    for (const e of model.edges) expect(vs.edges.get(e.key)).toBe("normal");
  });

  it("focus: self=focus, neighbors=lit, others=dim; edge lit only if both ends lit/focus", () => {
    const { clusters, nodes, edges } = smallFixture();
    const model = buildModel(clusters, nodes, edges, "a");
    const vs = computeVisualState(model, "a");

    expect(vs.nodes.get("a")).toBe("focus");
    expect(vs.nodes.get("b")).toBe("lit"); // linked to a
    expect(vs.nodes.get("c")).toBe("lit"); // linked to a
    expect(vs.nodes.get("d")).toBe("dim"); // not neighbor of a
    expect(vs.nodes.get("e")).toBe("dim");

    // a-b: both focus/lit → lit
    const ab = model.edges.find((e) => e.from === "a" && e.to === "b")!;
    expect(vs.edges.get(ab.key)).toBe("lit");
    // a-c: both focus/lit → lit
    const ac = model.edges.find((e) => e.from === "a" && e.to === "c")!;
    expect(vs.edges.get(ac.key)).toBe("lit");
    // c-d: c lit, d dim → dim (NOT both)
    const cd = model.edges.find((e) => e.from === "c" && e.to === "d")!;
    expect(vs.edges.get(cd.key)).toBe("dim");
  });

  it("hoverId alone activates when focusId is null", () => {
    const { clusters, nodes, edges } = smallFixture();
    const model = buildModel(clusters, nodes, edges, null);
    const vs = computeVisualState(model, null, "c");
    expect(vs.nodes.get("c")).toBe("focus");
    expect(vs.nodes.get("a")).toBe("lit");
    expect(vs.nodes.get("d")).toBe("lit");
    expect(vs.nodes.get("b")).toBe("dim");
  });
});

describe("graphEngine — hitTest", () => {
  it("hits exact node center, respects padding boundary, empty → undefined", () => {
    const { clusters, nodes, edges } = smallFixture();
    const model = buildModel(clusters, nodes, edges, null);
    const a = model.byId.get("a")!;
    const r = nodeRadius(a.weight);

    expect(hitTest(model, a.x, a.y)?.id).toBe("a");
    // just outside radius without padding
    expect(hitTest(model, a.x + r + 0.5, a.y)).toBeUndefined();
    // with padding catches it
    expect(hitTest(model, a.x + r + 0.5, a.y, 1)?.id).toBe("a");
    // empty far field
    expect(hitTest(model, 10, 10)).toBeUndefined();
  });
});

describe("graphEngine — viewport math", () => {
  it("worldToScreen / screenToWorld invert with d3-zoom transform", () => {
    const t = { x: 100, y: 50, k: 2 };
    const s = worldToScreen(640, 410, t);
    expect(s).toEqual({ x: 640 * 2 + 100, y: 410 * 2 + 50 });
    const w = screenToWorld(s.x, s.y, t);
    expect(w.x).toBeCloseTo(640);
    expect(w.y).toBeCloseTo(410);

    const id = worldToScreen(10, 20, IDENTITY_TRANSFORM);
    expect(id).toEqual({ x: 10, y: 20 });
  });

  it("fitTransform 390×844: k < ZOOM_MIN, world centered, full world in viewport", () => {
    const viewW = 390;
    const viewH = 844;
    const t = fitTransform(viewW, viewH);
    const expectedK = Math.min(viewW / WORLD_WIDTH, viewH / WORLD_HEIGHT) * FIT_MARGIN;
    expect(t.k).toBeCloseTo(expectedK, 6);
    expect(t.k).toBeLessThan(ZOOM_MIN);
    expect(t.k).toBeCloseTo(0.289453125, 5); // 390/1280 * 0.95

    // Center of world maps to center of view
    const mid = worldToScreen(WORLD_WIDTH / 2, WORLD_HEIGHT / 2, t);
    expect(mid.x).toBeCloseTo(viewW / 2, 5);
    expect(mid.y).toBeCloseTo(viewH / 2, 5);

    // World corners stay inside the CSS box (contain + margin)
    const tl = worldToScreen(0, 0, t);
    const br = worldToScreen(WORLD_WIDTH, WORLD_HEIGHT, t);
    expect(tl.x).toBeGreaterThanOrEqual(0);
    expect(tl.y).toBeGreaterThanOrEqual(0);
    expect(br.x).toBeLessThanOrEqual(viewW);
    expect(br.y).toBeLessThanOrEqual(viewH);

    // scaleExtent min may go below 0.4 so fit is reachable
    expect(zoomExtentMin(viewW, viewH)).toBeCloseTo(t.k, 6);
    expect(zoomExtentMin(viewW, viewH)).toBeLessThan(ZOOM_MIN);
  });

  it("fitTransform 1440×900: k in [ZOOM_MIN, ZOOM_MAX], centered", () => {
    const viewW = 1440;
    const viewH = 900;
    const t = fitTransform(viewW, viewH);
    expect(t.k).toBeGreaterThanOrEqual(ZOOM_MIN);
    expect(t.k).toBeLessThanOrEqual(ZOOM_MAX);
    const expectedK = Math.min(viewW / WORLD_WIDTH, viewH / WORLD_HEIGHT) * FIT_MARGIN;
    expect(t.k).toBeCloseTo(expectedK, 6);

    const mid = worldToScreen(WORLD_WIDTH / 2, WORLD_HEIGHT / 2, t);
    expect(mid.x).toBeCloseTo(viewW / 2, 5);
    expect(mid.y).toBeCloseTo(viewH / 2, 5);

    // Desktop: floor stays at ZOOM_MIN (fitK > 0.4)
    expect(zoomExtentMin(viewW, viewH)).toBe(ZOOM_MIN);
  });

  it("fitTransform resize changes k (mobile → desktop)", () => {
    const mobile = fitTransform(390, 844);
    const desktop = fitTransform(1440, 900);
    expect(desktop.k).toBeGreaterThan(mobile.k);
    expect(fitScale(800, 600)).not.toBe(fitScale(390, 844));
  });
});

describe("graphEngine — 500-node simulation settle", () => {
  it("settles within tick budget, no NaN, nodes stay near cluster centroids", () => {
    const { clusters, nodes, edges } = generateLargeGraph(500, 1500, 42);
    expect(nodes).toHaveLength(500);
    expect(edges.length).toBeGreaterThanOrEqual(1400);

    const model = buildModel(clusters, nodes, edges, null);
    const sim = createSimulation(model);
    const ticks = sim.settle(0.02, 300);

    expect(ticks).toBeLessThan(300);
    expect(sim.alpha()).toBeLessThan(0.02);

    // no NaN in positions/velocities
    for (const n of sim.nodes) {
      expect(Number.isFinite(n.x)).toBe(true);
      expect(Number.isFinite(n.y)).toBe(true);
      expect(Number.isFinite(n.vx ?? 0)).toBe(true);
      expect(Number.isFinite(n.vy ?? 0)).toBe(true);
      expect(Number.isNaN(n.x)).toBe(false);
      expect(Number.isNaN(n.y)).toBe(false);
    }

    // Mean distance to cluster centroid stays bounded (warm-start + soft pull).
    // World is 1280×820; centroids are spread — allow generous but sensible radius.
    const byCluster = new Map<string, EngineNodeLike[]>();
    for (const n of sim.nodes) {
      const list = byCluster.get(n.cluster) ?? [];
      list.push(n);
      byCluster.set(n.cluster, list);
    }
    for (const [cluster, list] of byCluster) {
      const center = CLUSTER_CENTERS[cluster];
      expect(center).toBeDefined();
      const [cx, cy] = center!;
      let sum = 0;
      for (const n of list) sum += Math.hypot((n.x ?? 0) - cx, (n.y ?? 0) - cy);
      const mean = sum / list.length;
      // Mean near centroid: far under half-diagonal (~760). Soft budget 350.
      expect(mean).toBeLessThan(350);
    }

    // toModel refreshes edge paths from settled positions
    const settled = sim.toModel(null);
    expect(settled.sortedNodes).toHaveLength(500);
    expect(settled.edges.length).toBe(edges.length);
    for (const e of settled.edges) {
      expect(e.d.startsWith("M")).toBe(true);
      expect(e.d.includes("Q")).toBe(true);
    }

    sim.stop();
  }, 20000);
});

interface EngineNodeLike {
  cluster: string;
  x?: number;
  y?: number;
}

describe("graphEngine — buildModel auras/hero", () => {
  it("computes auras only for A4 clusters and picks heaviest hero", () => {
    const { clusters, nodes, edges } = smallFixture();
    const model = buildModel(clusters, nodes, edges, null);
    expect(model.heroId).toBe("a"); // weight 0.9
    expect(model.auras.some((a) => a.clusterId === "canon")).toBe(true);
    for (const aura of model.auras) {
      expect(aura.r).toBeGreaterThanOrEqual(100);
      expect(aura.r).toBeLessThanOrEqual(260);
    }
  });
});
