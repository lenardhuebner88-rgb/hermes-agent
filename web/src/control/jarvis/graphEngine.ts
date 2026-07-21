/**
 * graphEngine — DOM-freie Jarvis-Graph-Engine (F1).
 *
 * Simulation (d3-force, synchron tickbar), Model-Bau (Auren/Labels/Hero),
 * visualState (normal|dim|lit|focus), Viewport-Mathe (d3-zoom-kompatibel
 * {x,y,k}) und Hit-Test. Kein React, kein DOM, kein rAF — die Canvas-
 * Komponente (F2) konsumiert diese API und steuert ticks per rAF.
 *
 * Pure-Functions/Konstanten hierher portiert aus JarvisGraph.tsx; der
 * SVG-Renderer re-exportiert sie unverändert, damit Bestandstests
 * byte-identisch grün bleiben.
 */
import {
  forceCollide,
  forceLink,
  forceManyBody,
  forceSimulation,
  type Simulation,
  type SimulationLinkDatum,
  type SimulationNodeDatum,
} from "d3-force";

import type { PaGraphCluster, PaGraphEdge, PaGraphNode } from "@/lib/api";

/**
 * Survives minification as a string literal so chunk verification
 * (`grep -l forceSimulation dist/assets/*.js`) can prove d3-force landed
 * only in the Jarvis lazy chunk (the import binding itself is renamed).
 */
export const FORCE_SIMULATION_CHUNK_MARKER = "forceSimulation";

/* ── World + cluster centroids (from pa_graph.py:_CLUSTER_CENTERS) ── */

export const WORLD_WIDTH = 1280;
export const WORLD_HEIGHT = 820;

/** Cluster-Zentroide — sanfte Pull-Targets der Simulation. */
export const CLUSTER_CENTERS: Readonly<Record<string, readonly [number, number]>> = {
  canon: [640, 310],
  projekte: [500, 590],
  agenten: [850, 285],
  skills: [440, 255],
  memories: [965, 450],
  receipts: [820, 595],
  archiv: [325, 450],
};

/* ── Renderer constants (A4 measures, ported from JarvisGraph) ── */

/** Ab diesem Gewicht Orb-Gradient statt simpler Farbfläche. */
export const ORB_WEIGHT = 0.45;
/** Ab diesem Gewicht zusätzlich der weiche Glow-Halo. */
export const GLOW_WEIGHT = 0.5;
/** Label-Schwelle + Cap: Top-Knoten je Cluster. */
export const LABEL_WEIGHT = 0.45;
export const LABELS_PER_CLUSTER = 5;
/** „big"-Label für die Schwergewichte (Mock: vision/Hermes-Infra/Jarvis). */
export const BIG_LABEL_WEIGHT = 0.7;

/** Live-Labels sind lang (Vault-Titel) — dezent kürzen, voller Text bleibt
 *  im Tooltip des Knotens. */
export const MAX_LABEL_CHARS = 30;

/** Cluster-IDs, für die die handgemischten A4-Gradients existieren. */
export const A4_GRADIENT_IDS = new Set([
  "canon",
  "projekte",
  "agenten",
  "skills",
  "memories",
  "receipts",
  "archiv",
]);

export const EDGE_COLOR = "#7fa8dc";

export function nodeRadius(weight: number): number {
  return Math.round((1.5 + 13.5 * weight) * 10) / 10;
}

export function truncateLabel(label: string): string {
  return label.length > MAX_LABEL_CHARS ? `${label.slice(0, MAX_LABEL_CHARS - 1)}…` : label;
}

/** Kanten-Staffelung nach dem leichteren Endpunkt (A4-Näherung). */
export function edgeTier(weight: number): { opacity: number; width: number } {
  if (weight >= 0.55) return { opacity: 0.34, width: 1.2 };
  if (weight >= 0.35) return { opacity: 0.24, width: 1.0 };
  return { opacity: 0.17, width: 0.8 };
}

/** A4-Kurven: Kontrollpunkt leicht senkrecht zur Kante versetzt. */
export function edgePath(x1: number, y1: number, x2: number, y2: number): string {
  const mx = (x1 + x2) / 2 + 0.09 * (y1 - y2);
  const my = (y1 + y2) / 2 + 0.09 * (x2 - x1);
  return `M${x1} ${y1} Q${mx.toFixed(1)} ${my.toFixed(1)} ${x2} ${y2}`;
}

/**
 * Navigation aus dem Graphen: nur echte Dashboard-/API-Pfade sind
 * navigierbar; vault://, memory:// & Co. sind semantische Anzeige-Refs.
 * Gibt true zurück, wenn navigiert wurde (für Tests und Tap-Logik).
 */
export function openGraphRef(
  href: string | undefined,
  nav: { navigate: (path: string) => void; assign: (url: string) => void },
): boolean {
  if (!href) return false;
  if (href.startsWith("/control/")) {
    nav.navigate(href);
    return true;
  }
  if (href.startsWith("/api/")) {
    nav.assign(href);
    return true;
  }
  return false;
}

/* ── Engine types ── */

export type VisualState = "normal" | "dim" | "lit" | "focus";

/** PaGraphNode + d3-force simulation fields. */
export interface EngineNode extends PaGraphNode, SimulationNodeDatum {
  x: number;
  y: number;
  vx?: number;
  vy?: number;
  fx?: number | null;
  fy?: number | null;
  index?: number;
}

export interface EngineEdge {
  key: string;
  from: string;
  to: string;
  kind: string;
  /** min(weightA, weightB) — Kanten-Staffelung / Link-Strength. */
  tierWeight: number;
  /** SVG path string from current endpoint positions. */
  d: string;
}

export interface GraphModel {
  byId: Map<string, EngineNode>;
  clusterMeta: Map<string, PaGraphCluster>;
  /** Aufsteigend nach Gewicht — Schwergewichte malen obenauf. */
  sortedNodes: EngineNode[];
  auras: { clusterId: string; cx: number; cy: number; r: number }[];
  heroId: string | null;
  edges: EngineEdge[];
  labels: Map<string, "big" | "normal">;
  /** Raw source edges (for rebuild after ticks). */
  sourceEdges: PaGraphEdge[];
}

/** d3-zoom-compatible transform. */
export interface ViewportTransform {
  x: number;
  y: number;
  k: number;
}

export interface VisualStateMap {
  nodes: Map<string, VisualState>;
  edges: Map<string, VisualState>;
}

export interface SimulationOpts {
  /** Charge strength (default -28). */
  charge?: number;
  /** Cluster-centroid pull strength (default 0.04 — gentle). */
  clusterStrength?: number;
  /** Link base strength multiplier (default 0.35). */
  linkStrength?: number;
  /** Collide padding beyond nodeRadius (default 3). */
  collidePadding?: number;
  /** Alpha decay (default 0.0228 ≈ d3 default). */
  alphaDecay?: number;
  /** Velocity decay (default 0.4). */
  velocityDecay?: number;
}

export interface GraphSimulation {
  simulation: Simulation<EngineNode, SimLink>;
  nodes: EngineNode[];
  /** Run n ticks synchronously (simulation is stopped). */
  tick: (n?: number) => void;
  /** Current alpha. */
  alpha: () => number;
  /** Tick until alpha < target or maxTicks; returns ticks used. */
  settle: (alphaTarget?: number, maxTicks?: number) => number;
  /** Snapshot positions back into a GraphModel (paths/auras refresh). */
  toModel: (focusId?: string | null) => GraphModel;
  stop: () => void;
  /**
   * Literal "forceSimulation" — kept on the live object so production
   * minification cannot drop the string (chunk-proof grep target).
   */
  marker: typeof FORCE_SIMULATION_CHUNK_MARKER;
}

interface SimLink extends SimulationLinkDatum<EngineNode> {
  source: string | EngineNode;
  target: string | EngineNode;
  tierWeight: number;
}

/* ── Model builder (ported buildModel + EngineNode positions) ── */

/**
 * Build the pure graph model from PA-graph payload + optional focus.
 * Positions come from node.x/y (warm-start / post-sim).
 */
export function buildModel(
  clusters: PaGraphCluster[],
  nodes: readonly PaGraphNode[],
  edges: readonly PaGraphEdge[],
  focusId: string | null = null,
): GraphModel {
  const clusterMeta = new Map(clusters.map((c) => [c.id, c]));
  const engineNodes: EngineNode[] = nodes.map((n) => ({
    ...n,
    x: n.x,
    y: n.y,
    vx: 0,
    vy: 0,
  }));
  const byId = new Map(engineNodes.map((n) => [n.id, n]));
  const sortedNodes = [...engineNodes].sort((a, b) => a.weight - b.weight);

  const auras: GraphModel["auras"] = [];
  const sums = new Map<string, { sx: number; sy: number; n: number }>();
  for (const n of engineNodes) {
    const s = sums.get(n.cluster) ?? { sx: 0, sy: 0, n: 0 };
    s.sx += n.x;
    s.sy += n.y;
    s.n += 1;
    sums.set(n.cluster, s);
  }
  for (const [clusterId, s] of sums) {
    if (!A4_GRADIENT_IDS.has(clusterId)) continue;
    const cx = s.sx / s.n;
    const cy = s.sy / s.n;
    let maxDist = 0;
    for (const n of engineNodes) {
      if (n.cluster !== clusterId) continue;
      maxDist = Math.max(maxDist, Math.hypot(n.x - cx, n.y - cy));
    }
    auras.push({ clusterId, cx, cy, r: Math.min(260, Math.max(100, maxDist + 70)) });
  }

  const heroId = sortedNodes.length > 0 ? sortedNodes[sortedNodes.length - 1].id : null;

  const edgeModels: EngineEdge[] = [];
  edges.forEach((e, i) => {
    const a = byId.get(e.from);
    const b = byId.get(e.to);
    if (!a || !b) return;
    edgeModels.push({
      key: `${e.from}->${e.to}#${i}`,
      from: e.from,
      to: e.to,
      kind: e.kind,
      tierWeight: Math.min(a.weight, b.weight),
      d: edgePath(a.x, a.y, b.x, b.y),
    });
  });

  const labels = new Map<string, "big" | "normal">();
  const labelCandidates = new Map<string, EngineNode[]>();
  for (const n of engineNodes) {
    if (n.label == null || n.weight < LABEL_WEIGHT) continue;
    const list = labelCandidates.get(n.cluster) ?? [];
    list.push(n);
    labelCandidates.set(n.cluster, list);
  }
  for (const list of labelCandidates.values()) {
    list.sort((a, b) => b.weight - a.weight);
    for (const n of list.slice(0, LABELS_PER_CLUSTER)) {
      labels.set(n.id, n.weight >= BIG_LABEL_WEIGHT ? "big" : "normal");
    }
  }
  // Der fokussierte Knoten ist immer gelabelt, auch unter der Schwelle.
  if (focusId) {
    const focusNode = byId.get(focusId);
    if (focusNode?.label != null && !labels.has(focusId)) labels.set(focusId, "normal");
  }

  return {
    byId,
    clusterMeta,
    sortedNodes,
    auras,
    heroId,
    edges: edgeModels,
    labels,
    sourceEdges: edges.map((e) => ({ ...e })),
  };
}

/* ── Simulation ── */

/**
 * Create a stopped d3-force simulation with warm-start from node.x/y.
 * Call tick(n) / settle() synchronously — no realtime loop here.
 */
export function createSimulation(
  model: GraphModel,
  opts: SimulationOpts = {},
): GraphSimulation {
  const charge = opts.charge ?? -28;
  const clusterStrength = opts.clusterStrength ?? 0.04;
  const linkStrength = opts.linkStrength ?? 0.35;
  const collidePadding = opts.collidePadding ?? 3;
  const alphaDecay = opts.alphaDecay ?? 0.0228;
  const velocityDecay = opts.velocityDecay ?? 0.4;

  // Fresh node copies so callers keep their model snapshot.
  const nodes: EngineNode[] = model.sortedNodes.map((n) => ({
    ...n,
    x: n.x,
    y: n.y,
    vx: 0,
    vy: 0,
    fx: undefined,
    fy: undefined,
  }));
  const byId = new Map(nodes.map((n) => [n.id, n]));

  const links: SimLink[] = [];
  for (const e of model.sourceEdges) {
    if (!byId.has(e.from) || !byId.has(e.to)) continue;
    const a = byId.get(e.from)!;
    const b = byId.get(e.to)!;
    links.push({
      source: e.from,
      target: e.to,
      tierWeight: Math.min(a.weight, b.weight),
    });
  }

  const simulation = forceSimulation<EngineNode, SimLink>(nodes)
    .force(
      "link",
      forceLink<EngineNode, SimLink>(links)
        .id((d) => d.id)
        .distance(40)
        .strength((l) => linkStrength * Math.max(0.15, l.tierWeight)),
    )
    .force("charge", forceManyBody<EngineNode>().strength(charge))
    .force(
      "collide",
      forceCollide<EngineNode>()
        .radius((d) => nodeRadius(d.weight) + collidePadding)
        .iterations(2),
    )
    .force("cluster", clusterCentroidForce(clusterStrength))
    .alphaDecay(alphaDecay)
    .velocityDecay(velocityDecay)
    .stop();

  const tick = (n = 1): void => {
    for (let i = 0; i < n; i += 1) simulation.tick();
  };

  const settle = (alphaTarget = 0.02, maxTicks = 300): number => {
    let used = 0;
    while (simulation.alpha() >= alphaTarget && used < maxTicks) {
      simulation.tick();
      used += 1;
    }
    return used;
  };

  const toModel = (focusId: string | null = null): GraphModel => {
    const clusters = [...model.clusterMeta.values()];
    const paNodes: PaGraphNode[] = nodes.map((n) => ({
      id: n.id,
      label: n.label,
      cluster: n.cluster,
      kind: n.kind,
      weight: n.weight,
      x: n.x ?? 0,
      y: n.y ?? 0,
      href: n.href,
      ref: n.ref,
    }));
    return buildModel(clusters, paNodes, model.sourceEdges, focusId);
  };

  return {
    simulation,
    nodes,
    tick,
    alpha: () => simulation.alpha(),
    settle,
    toModel,
    stop: () => {
      simulation.stop();
    },
    marker: FORCE_SIMULATION_CHUNK_MARKER,
  };
}

/**
 * Gentle force pulling each node toward its cluster centroid
 * (ported from pa_graph.py:_CLUSTER_CENTERS). Unknown clusters → world center.
 */
function clusterCentroidForce(strength: number) {
  let nodes: EngineNode[] = [];

  function force(alpha: number): void {
    const k = strength * alpha;
    for (const n of nodes) {
      const center = CLUSTER_CENTERS[n.cluster] ?? ([WORLD_WIDTH / 2, WORLD_HEIGHT / 2] as const);
      n.vx = (n.vx ?? 0) + (center[0] - (n.x ?? 0)) * k;
      n.vy = (n.vy ?? 0) + (center[1] - (n.y ?? 0)) * k;
    }
  }

  force.initialize = (initNodes: EngineNode[]): void => {
    nodes = initNodes;
  };

  return force;
}

/* ── visualState ── */

/**
 * Per-node / per-edge highlight state for focus/hover.
 * - no focusId/hoverId → all normal
 * - focus/hover node → focus; neighbors lit; non-neighbors dim
 * - edge lit only when BOTH endpoints are lit or focus
 */
export function computeVisualState(
  model: GraphModel,
  focusId: string | null | undefined,
  hoverId?: string | null,
): VisualStateMap {
  const activeId = focusId ?? hoverId ?? null;
  const nodeStates = new Map<string, VisualState>();
  const edgeStates = new Map<string, VisualState>();

  if (!activeId || !model.byId.has(activeId)) {
    for (const n of model.sortedNodes) nodeStates.set(n.id, "normal");
    for (const e of model.edges) edgeStates.set(e.key, "normal");
    return { nodes: nodeStates, edges: edgeStates };
  }

  const neighbors = new Set<string>();
  for (const e of model.edges) {
    if (e.from === activeId) neighbors.add(e.to);
    if (e.to === activeId) neighbors.add(e.from);
  }

  for (const n of model.sortedNodes) {
    if (n.id === activeId) nodeStates.set(n.id, "focus");
    else if (neighbors.has(n.id)) nodeStates.set(n.id, "lit");
    else nodeStates.set(n.id, "dim");
  }

  for (const e of model.edges) {
    const a = nodeStates.get(e.from) ?? "dim";
    const b = nodeStates.get(e.to) ?? "dim";
    const aOk = a === "lit" || a === "focus";
    const bOk = b === "lit" || b === "focus";
    edgeStates.set(e.key, aOk && bOk ? "lit" : "dim");
  }

  return { nodes: nodeStates, edges: edgeStates };
}

/* ── Viewport math (d3-zoom transform {x,y,k}) ── */

export function worldToScreen(
  wx: number,
  wy: number,
  t: ViewportTransform,
): { x: number; y: number } {
  return { x: wx * t.k + t.x, y: wy * t.k + t.y };
}

export function screenToWorld(
  sx: number,
  sy: number,
  t: ViewportTransform,
): { x: number; y: number } {
  const k = t.k === 0 ? 1 : t.k;
  return { x: (sx - t.x) / k, y: (sy - t.y) / k };
}

export const IDENTITY_TRANSFORM: ViewportTransform = { x: 0, y: 0, k: 1 };

/** Baseline pan/zoom floor (desktop). Mobile fit may go lower. */
export const ZOOM_MIN = 0.4;
export const ZOOM_MAX = 4;
/** Contain-margin so world does not hug the CSS edges. */
export const FIT_MARGIN = 0.95;

/**
 * Scale that fits WORLD_WIDTH×WORLD_HEIGHT into the CSS viewport with margin.
 * Pure — used by Canvas mount/resize and staticLayout; unit-tested.
 */
export function fitScale(viewW: number, viewH: number, margin = FIT_MARGIN): number {
  const w = Math.max(1, viewW);
  const h = Math.max(1, viewH);
  return Math.min(w / WORLD_WIDTH, h / WORLD_HEIGHT) * margin;
}

/**
 * d3-zoom-compatible transform that contain-fits the world into the viewport,
 * centered. At 390×844 → k ≈ 0.29 (< ZOOM_MIN); at 1440×900 → k ≈ 1.04.
 */
export function fitTransform(
  viewW: number,
  viewH: number,
  margin = FIT_MARGIN,
): ViewportTransform {
  const k = fitScale(viewW, viewH, margin);
  return {
    k,
    x: viewW / 2 - (WORLD_WIDTH * k) / 2,
    y: viewH / 2 - (WORLD_HEIGHT * k) / 2,
  };
}

/**
 * scaleExtent minimum: never block the initial fit on small viewports
 * (min(ZOOM_MIN, fitK) → ~0.29 at 390px wide).
 */
export function zoomExtentMin(viewW: number, viewH: number, margin = FIT_MARGIN): number {
  return Math.min(ZOOM_MIN, fitScale(viewW, viewH, margin));
}

/* ── Hit-Test ── */

/**
 * Nearest node whose center is within nodeRadius(weight) + padding of (wx, wy)
 * in world coordinates. undefined if none.
 */
export function hitTest(
  model: GraphModel,
  wx: number,
  wy: number,
  padding = 0,
): EngineNode | undefined {
  let best: EngineNode | undefined;
  let bestDist = Infinity;
  for (const n of model.sortedNodes) {
    const r = nodeRadius(n.weight) + padding;
    const d = Math.hypot(n.x - wx, n.y - wy);
    if (d <= r && d < bestDist) {
      best = n;
      bestDist = d;
    }
  }
  return best;
}
