/**
 * JarvisGraphCanvas — Canvas-Renderer für den Estate-Graph (F2).
 *
 * Konsumiert graphEngine (F1): Simulation, visualState, Hit-Test, Viewport-Mathe.
 * rAF nur solange alpha > Schwelle oder Interaktion aktiv; staticLayout zeichnet
 * einen synchronen Frame aus Warm-Start-x/y (Test-Naht, kein rAF).
 *
 * A4-Optik: Fog/Stardust/Auren/Orb/Glow über einmalig vorrenderte Offscreen-
 * Sprites, pro Frame nur blitten. d3-zoom contain-fit (fitTransform) on mount/
 * resize; scaleExtent min = min(0.4, fitK) … 4×. d3-drag alphaTarget(0.3),
 * Tap=Fokus / Tap²=öffnen, A11y role=application + Pfeiltasten + aria-live.
 */
import { drag as d3Drag } from "d3-drag";
import { select, pointer } from "d3-selection";
import { zoom as d3Zoom, zoomIdentity, type ZoomBehavior, type ZoomTransform } from "d3-zoom";
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type CSSProperties,
} from "react";

import type { PaGraphNode, PaGraphResponse } from "@/lib/api";

import {
  A4_GRADIENT_IDS,
  BIG_LABEL_WEIGHT,
  buildModel,
  computeVisualState,
  createSimulation,
  EDGE_COLOR,
  edgeTier,
  fitTransform,
  GLOW_WEIGHT,
  hitTest,
  IDENTITY_TRANSFORM,
  nodeRadius,
  ORB_WEIGHT,
  screenToWorld,
  truncateLabel,
  type EngineNode,
  type GraphModel,
  type GraphSimulation,
  type ViewportTransform,
  WORLD_HEIGHT,
  WORLD_WIDTH,
  ZOOM_MAX,
  zoomExtentMin,
} from "./graphEngine";

/* ── Constants ── */

const ALPHA_THRESHOLD = 0.02;
const DRAG_ALPHA_TARGET = 0.3;
const LABEL_LOD_K = 0.7;
const MOBILE_MQ = "(max-width: 759px)";
const DPR_CAP_DESKTOP = 2;
const DPR_CAP_MOBILE = 1.5;
const DIM_ALPHA = 0.15;

/** A4 cluster palette — orb + nebula stops (ported from GraphDefs). */
const CLUSTER_PALETTE: Record<
  string,
  { color: string; orb: [string, string, string]; neb: string }
> = {
  canon: { color: "#38d8ff", orb: ["#bff3ff", "#38d8ff", "#1a647a"], neb: "#38d8ff" },
  projekte: { color: "#3ddc97", orb: ["#c1f4de", "#3ddc97", "#1d664c"], neb: "#3ddc97" },
  agenten: { color: "#ffb347", orb: ["#ffe7c4", "#ffb347", "#745428"], neb: "#ffb347" },
  skills: { color: "#5b8cff", orb: ["#cbdaff", "#5b8cff", "#2a427a"], neb: "#5b8cff" },
  memories: { color: "#b78cff", orb: ["#e8daff", "#b78cff", "#53427a"], neb: "#b78cff" },
  receipts: { color: "#ff7ab8", orb: ["#ffd4e8", "#ff7ab8", "#743a5a"], neb: "#ff7ab8" },
  archiv: { color: "#5a6f8f", orb: ["#cad1db", "#5a6f8f", "#2a3548"], neb: "#5a6f8f" },
};

/** Static stardust from the A4 SVG (subset kept dense enough for depth). */
const STARDUST: ReadonlyArray<readonly [number, number, number, number]> = [
  [1216, 452, 1.3, 0.12],
  [934, 299, 0.8, 0.29],
  [191, 241, 0.6, 0.12],
  [237, 43, 1.2, 0.14],
  [339, 290, 1.1, 0.19],
  [227, 676, 1.3, 0.24],
  [779, 412, 0.9, 0.14],
  [646, 467, 0.6, 0.11],
  [718, 52, 0.8, 0.26],
  [700, 60, 0.7, 0.1],
  [75, 601, 0.8, 0.29],
  [234, 350, 1.0, 0.22],
  [308, 560, 0.9, 0.27],
  [165, 619, 0.8, 0.16],
  [1081, 402, 1.2, 0.12],
  [1069, 300, 0.7, 0.12],
  [256, 377, 0.9, 0.14],
  [652, 213, 0.8, 0.2],
  [580, 397, 0.9, 0.25],
  [270, 316, 1.0, 0.23],
  [56, 26, 0.5, 0.26],
  [449, 210, 0.7, 0.15],
  [204, 24, 1.0, 0.13],
  [859, 193, 0.6, 0.1],
  [911, 111, 0.6, 0.3],
  [348, 232, 0.6, 0.16],
  [399, 648, 0.8, 0.29],
  [520, 139, 0.5, 0.17],
  [833, 290, 1.1, 0.11],
  [204, 52, 0.7, 0.08],
  [67, 339, 1.1, 0.08],
  [161, 206, 0.9, 0.15],
  [1209, 45, 1.0, 0.21],
  [747, 178, 0.7, 0.08],
  [935, 269, 0.6, 0.16],
  [190, 164, 1.3, 0.12],
  [923, 171, 0.5, 0.24],
  [824, 309, 1.2, 0.3],
  [924, 282, 1.3, 0.29],
  [164, 729, 0.9, 0.22],
  [578, 441, 1.1, 0.27],
  [688, 154, 1.0, 0.27],
  [755, 422, 0.8, 0.08],
  [1112, 694, 1.1, 0.14],
  [986, 264, 1.1, 0.12],
  [115, 598, 1.1, 0.1],
  [1026, 605, 1.0, 0.28],
  [255, 557, 0.7, 0.22],
  [1219, 363, 0.7, 0.2],
  [726, 263, 1.1, 0.27],
  [630, 319, 0.6, 0.17],
  [907, 495, 1.1, 0.2],
  [1221, 556, 0.5, 0.21],
  [1102, 372, 0.8, 0.09],
  [710, 553, 1.0, 0.22],
];

/* ── Sprite cache ── */

interface SpriteCache {
  orbs: Map<string, HTMLCanvasElement>;
  glows: Map<string, HTMLCanvasElement>;
  nebs: Map<string, HTMLCanvasElement>;
  fog: HTMLCanvasElement | null;
  stardust: HTMLCanvasElement | null;
  mobile: boolean;
}

function buildOrbSprite(clusterId: string, size: number): HTMLCanvasElement {
  const pal = CLUSTER_PALETTE[clusterId];
  const c = document.createElement("canvas");
  c.width = size;
  c.height = size;
  const ctx = c.getContext("2d");
  if (!ctx || !pal) return c;
  const g = ctx.createRadialGradient(size * 0.38, size * 0.3, 0, size * 0.5, size * 0.5, size * 0.5);
  g.addColorStop(0, pal.orb[0]);
  g.addColorStop(0.42, pal.orb[1]);
  g.addColorStop(1, pal.orb[2]);
  ctx.fillStyle = g;
  ctx.beginPath();
  ctx.arc(size / 2, size / 2, size / 2 - 0.5, 0, Math.PI * 2);
  ctx.fill();
  return c;
}

function buildGlowSprite(clusterId: string, size: number): HTMLCanvasElement {
  const pal = CLUSTER_PALETTE[clusterId];
  const c = document.createElement("canvas");
  c.width = size;
  c.height = size;
  const ctx = c.getContext("2d");
  if (!ctx || !pal) return c;
  // Soft blur via multi-pass translucent circles (no ctx.filter per frame).
  const cx = size / 2;
  const cy = size / 2;
  const r = size / 2 - 2;
  for (let i = 4; i >= 1; i -= 1) {
    ctx.beginPath();
    ctx.arc(cx, cy, r * (0.45 + i * 0.14), 0, Math.PI * 2);
    ctx.fillStyle = pal.color;
    ctx.globalAlpha = 0.05 * i;
    ctx.fill();
  }
  ctx.globalAlpha = 1;
  return c;
}

function buildNebSprite(clusterId: string, size: number): HTMLCanvasElement {
  const pal = CLUSTER_PALETTE[clusterId];
  const c = document.createElement("canvas");
  c.width = size;
  c.height = size;
  const ctx = c.getContext("2d");
  if (!ctx || !pal) return c;
  const g = ctx.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
  g.addColorStop(0, withAlpha(pal.neb, 0.13));
  g.addColorStop(0.55, withAlpha(pal.neb, 0.05));
  g.addColorStop(1, withAlpha(pal.neb, 0));
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, size, size);
  return c;
}

function buildFogSprite(w: number, h: number): HTMLCanvasElement {
  const c = document.createElement("canvas");
  c.width = w;
  c.height = h;
  const ctx = c.getContext("2d");
  if (!ctx) return c;
  const g = ctx.createRadialGradient(w * 0.5, h * 0.45, 0, w * 0.5, h * 0.45, Math.max(w, h) * 0.6);
  g.addColorStop(0, "rgba(56, 216, 255, 0.06)");
  g.addColorStop(1, "rgba(0, 0, 0, 0)");
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, w, h);
  return c;
}

function buildStardustSprite(mobile: boolean): HTMLCanvasElement {
  const c = document.createElement("canvas");
  c.width = WORLD_WIDTH;
  c.height = WORLD_HEIGHT;
  const ctx = c.getContext("2d");
  if (!ctx) return c;
  const stars = mobile ? STARDUST.filter((_, i) => i % 3 === 0) : STARDUST;
  for (const [x, y, r, op] of stars) {
    ctx.beginPath();
    ctx.arc(x, y, r, 0, Math.PI * 2);
    ctx.fillStyle = `rgba(207, 228, 255, ${op})`;
    ctx.fill();
  }
  return c;
}

function withAlpha(hex: string, a: number): string {
  const h = hex.replace("#", "");
  const n = parseInt(h.length === 3 ? h.split("").map((c) => c + c).join("") : h, 16);
  const r = (n >> 16) & 255;
  const g = (n >> 8) & 255;
  const b = n & 255;
  return `rgba(${r},${g},${b},${a})`;
}

function ensureSprites(mobile: boolean, prev: SpriteCache | null): SpriteCache {
  if (prev && prev.mobile === mobile && prev.fog && prev.stardust) return prev;
  const orbs = new Map<string, HTMLCanvasElement>();
  const glows = new Map<string, HTMLCanvasElement>();
  const nebs = new Map<string, HTMLCanvasElement>();
  for (const id of A4_GRADIENT_IDS) {
    orbs.set(id, buildOrbSprite(id, 64));
    glows.set(id, buildGlowSprite(id, 96));
    nebs.set(id, buildNebSprite(id, 256));
  }
  return {
    orbs,
    glows,
    nebs,
    fog: buildFogSprite(WORLD_WIDTH, WORLD_HEIGHT),
    stardust: buildStardustSprite(mobile),
    mobile,
  };
}

function vtFromZoom(t: ZoomTransform): ViewportTransform {
  return { x: t.x, y: t.y, k: t.k };
}

function clusterColor(model: GraphModel, clusterId: string): string {
  return model.clusterMeta.get(clusterId)?.color
    ?? CLUSTER_PALETTE[clusterId]?.color
    ?? EDGE_COLOR;
}

/* ── Paint ── */

interface PaintArgs {
  ctx: CanvasRenderingContext2D;
  cssW: number;
  cssH: number;
  dpr: number;
  model: GraphModel;
  transform: ViewportTransform;
  focusId: string | null;
  hoverId: string | null;
  sprites: SpriteCache;
  mobile: boolean;
  showGlow: boolean;
}

function paintFrame(args: PaintArgs): void {
  const { ctx, cssW, cssH, dpr, model, transform, focusId, hoverId, sprites, mobile, showGlow } = args;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, cssH);

  // Single transform path: d3-zoom {x,y,k} (initial = fitTransform). No cover-fit layer.
  ctx.save();
  ctx.translate(transform.x, transform.y);
  ctx.scale(transform.k, transform.k);

  // Fog + stardust (world-locked background)
  if (sprites.fog) ctx.drawImage(sprites.fog, 0, 0, WORLD_WIDTH, WORLD_HEIGHT);
  if (sprites.stardust) ctx.drawImage(sprites.stardust, 0, 0, WORLD_WIDTH, WORLD_HEIGHT);

  // Cluster auras
  for (const aura of model.auras) {
    const spr = sprites.nebs.get(aura.clusterId);
    if (!spr) continue;
    const r = aura.r;
    ctx.drawImage(spr, aura.cx - r, aura.cy - r, r * 2, r * 2);
  }
  // Hero aura
  if (model.heroId) {
    const hero = model.byId.get(model.heroId);
    if (hero && A4_GRADIENT_IDS.has(hero.cluster)) {
      const spr = sprites.nebs.get(hero.cluster);
      if (spr) ctx.drawImage(spr, hero.x - 98, hero.y - 98, 196, 196);
    }
  }

  const visual = computeVisualState(model, focusId, hoverId);
  const hasHighlight = focusId != null || hoverId != null;
  const focusNode = focusId ? model.byId.get(focusId) : undefined;
  const focusCol = focusNode ? clusterColor(model, focusNode.cluster) : EDGE_COLOR;

  // Edges
  for (const edge of model.edges) {
    const a = model.byId.get(edge.from);
    const b = model.byId.get(edge.to);
    if (!a || !b) continue;
    const vs = visual.edges.get(edge.key) ?? "normal";
    const tier = edgeTier(edge.tierWeight);
    const lit = vs === "lit";
    const dim = vs === "dim" && hasHighlight;
    ctx.beginPath();
    // Rebuild quadratic from current positions (sim may have moved nodes).
    const mx = (a.x + b.x) / 2 + 0.09 * (a.y - b.y);
    const my = (a.y + b.y) / 2 + 0.09 * (b.x - a.x);
    ctx.moveTo(a.x, a.y);
    ctx.quadraticCurveTo(mx, my, b.x, b.y);
    ctx.strokeStyle = lit ? focusCol : EDGE_COLOR;
    ctx.globalAlpha = lit ? 0.6 : dim ? tier.opacity * DIM_ALPHA : tier.opacity;
    ctx.lineWidth = lit ? 1.6 : tier.width;
    ctx.stroke();
    ctx.globalAlpha = 1;
  }

  // Nodes (sorted light→heavy so hubs on top)
  for (const node of model.sortedNodes) {
    const vs = visual.nodes.get(node.id) ?? "normal";
    const dim = vs === "dim" && hasHighlight;
    const r = nodeRadius(node.weight);
    const color = clusterColor(model, node.cluster);
    const alphaMul = dim ? DIM_ALPHA : 1;

    if (showGlow && node.weight >= GLOW_WEIGHT) {
      const glow = sprites.glows.get(node.cluster);
      const gr = r * 2.3;
      if (glow) {
        ctx.globalAlpha = 0.26 * alphaMul;
        ctx.drawImage(glow, node.x - gr, node.y - gr, gr * 2, gr * 2);
        ctx.globalAlpha = 1;
      }
    }

    if (vs === "focus") {
      ctx.beginPath();
      ctx.arc(node.x, node.y, r + 7, 0, Math.PI * 2);
      ctx.strokeStyle = color;
      ctx.globalAlpha = 0.85;
      ctx.lineWidth = 1.5;
      ctx.stroke();
      ctx.beginPath();
      ctx.arc(node.x, node.y, r + 13, 0, Math.PI * 2);
      ctx.globalAlpha = 0.22;
      ctx.lineWidth = 1;
      ctx.stroke();
      ctx.globalAlpha = 1;
    }

    const useOrb = node.weight >= ORB_WEIGHT && A4_GRADIENT_IDS.has(node.cluster);
    if (useOrb) {
      const orb = sprites.orbs.get(node.cluster);
      if (orb) {
        ctx.globalAlpha = alphaMul;
        ctx.drawImage(orb, node.x - r, node.y - r, r * 2, r * 2);
        ctx.globalAlpha = 1;
      }
    } else {
      ctx.beginPath();
      ctx.arc(node.x, node.y, r, 0, Math.PI * 2);
      ctx.fillStyle = color;
      ctx.globalAlpha = (0.55 + 0.35 * node.weight) * alphaMul;
      ctx.fill();
      ctx.globalAlpha = 1;
    }
  }

  // Labels (LOD: hide when zoomed out far)
  if (transform.k >= LABEL_LOD_K) {
    ctx.textBaseline = "middle";
    ctx.font = "600 10.5px IBM Plex Mono, ui-monospace, monospace";
    for (const node of model.sortedNodes) {
      const kind = model.labels.get(node.id);
      if (!kind || node.label == null) continue;
      const vs = visual.nodes.get(node.id) ?? "normal";
      if (vs === "dim" && hasHighlight) continue;
      const r = nodeRadius(node.weight);
      const left = node.x > 1050;
      const text = truncateLabel(node.label);
      const x = left ? node.x - r - 7 : node.x + r + 7;
      ctx.textAlign = left ? "end" : "start";
      const isBig = kind === "big" || node.weight >= BIG_LABEL_WEIGHT;
      ctx.font = isBig
        ? "600 13px IBM Plex Mono, ui-monospace, monospace"
        : "500 10.5px IBM Plex Mono, ui-monospace, monospace";
      // Halo
      ctx.lineWidth = isBig ? 2.6 : 2;
      ctx.strokeStyle = "rgba(4, 7, 15, 0.82)";
      ctx.strokeText(text, x, node.y + 1);
      ctx.fillStyle = isBig ? "#eef5ff" : "#b7cbe9";
      ctx.fillText(text, x, node.y + 1);
    }
  }

  if (focusNode?.label != null) {
    const r = nodeRadius(focusNode.weight);
    ctx.font = "500 8.5px IBM Plex Mono, ui-monospace, monospace";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.lineWidth = 1.8;
    ctx.strokeStyle = "rgba(4, 7, 15, 0.82)";
    ctx.strokeText("· FOKUS ·", focusNode.x, focusNode.y + r + 24);
    ctx.fillStyle = "#3ddc97";
    ctx.globalAlpha = 0.92;
    ctx.fillText("· FOKUS ·", focusNode.x, focusNode.y + r + 24);
    ctx.globalAlpha = 1;
  }

  ctx.restore();
  void mobile;
}

/* ── Props ── */

export interface JarvisGraphCanvasProps {
  graph: PaGraphResponse;
  /** Test seam: one sync frame from warm-start x/y — no sim/rAF. */
  staticLayout?: boolean;
  ariaLabel?: string;
  /** Polite status for aria-live (live N / Vorschau / STALE). */
  statusLive?: string;
  onNodeFocus?: (id: string | null, node: PaGraphNode | null) => void;
  onNodeOpen?: (node: PaGraphNode) => void;
  className?: string;
  style?: CSSProperties;
}

export function JarvisGraphCanvas({
  graph,
  staticLayout = false,
  ariaLabel = "Estate-Graph",
  statusLive,
  onNodeFocus,
  onNodeOpen,
  className,
  style,
}: JarvisGraphCanvasProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const simRef = useRef<GraphSimulation | null>(null);
  const modelRef = useRef<GraphModel | null>(null);
  const transformRef = useRef<ViewportTransform>({ ...IDENTITY_TRANSFORM });
  const zoomBehaviorRef = useRef<ZoomBehavior<HTMLCanvasElement, unknown> | null>(null);
  const rafRef = useRef<number | null>(null);
  const spritesRef = useRef<SpriteCache | null>(null);
  const focusIdRef = useRef<string | null>(null);
  const hoverIdRef = useRef<string | null>(null);
  const draggingRef = useRef(false);
  const interactionRef = useRef(false);
  const visibleRef = useRef(typeof document === "undefined" ? true : document.visibilityState !== "hidden");
  const sizeRef = useRef({ cssW: WORLD_WIDTH, cssH: WORLD_HEIGHT, dpr: 1 });
  const mobileRef = useRef(false);
  const onNodeFocusRef = useRef(onNodeFocus);
  const onNodeOpenRef = useRef(onNodeOpen);

  const [focusId, setFocusId] = useState<string | null>(null);
  const [hoverId, setHoverId] = useState<string | null>(null);
  const [cursor, setCursor] = useState<"grab" | "grabbing" | "pointer">("grab");
  const [focusAnnounce, setFocusAnnounce] = useState<string | null>(null);
  const [kbdIndex, setKbdIndex] = useState(-1);
  const liveAnnounce = focusAnnounce ?? statusLive ?? "";

  // Keep mutable refs in sync outside render body (eslint react-hooks/refs).
  useEffect(() => {
    onNodeFocusRef.current = onNodeFocus;
    onNodeOpenRef.current = onNodeOpen;
  }, [onNodeFocus, onNodeOpen]);
  useEffect(() => {
    focusIdRef.current = focusId;
    hoverIdRef.current = hoverId;
  }, [focusId, hoverId]);

  // Stable graph fingerprint for rebuilds
  const graphKey = useMemo(
    () =>
      `${graph.source}|${graph.generated_at}|${graph.nodes.length}|${graph.edges.length}|${graph.nodes[0]?.id ?? ""}|${graph.nodes[graph.nodes.length - 1]?.id ?? ""}`,
    [graph],
  );

  const labeledIds = useMemo(() => {
    const model = buildModel(graph.clusters, graph.nodes, graph.edges, focusId);
    return [...model.labels.keys()];
  }, [graph, focusId]);

  const cancelRaf = useCallback(() => {
    if (rafRef.current != null) {
      cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    }
  }, []);

  const paintNow = useCallback(() => {
    const canvas = canvasRef.current;
    const model = modelRef.current;
    if (!canvas || !model) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const mobile = mobileRef.current;
    spritesRef.current = ensureSprites(mobile, spritesRef.current);
    const { cssW, cssH, dpr } = sizeRef.current;
    paintFrame({
      ctx,
      cssW,
      cssH,
      dpr,
      model,
      transform: transformRef.current,
      focusId: focusIdRef.current,
      hoverId: hoverIdRef.current,
      sprites: spritesRef.current,
      mobile,
      showGlow: !mobile,
    });
  }, []);

  const scheduleLoop = useCallback(() => {
    if (staticLayout) return;
    if (!visibleRef.current) return;
    if (rafRef.current != null) return;

    const tick = () => {
      rafRef.current = null;
      const sim = simRef.current;
      if (!sim || !visibleRef.current) {
        paintNow();
        return;
      }
      // One physics step per frame while hot
      if (sim.alpha() > ALPHA_THRESHOLD || draggingRef.current || interactionRef.current) {
        if (sim.alpha() > ALPHA_THRESHOLD || draggingRef.current) {
          sim.tick(1);
          modelRef.current = sim.toModel(focusIdRef.current);
        }
        paintNow();
        const stillHot =
          sim.alpha() > ALPHA_THRESHOLD || draggingRef.current || interactionRef.current;
        if (stillHot && visibleRef.current) {
          rafRef.current = requestAnimationFrame(tick);
        }
      } else {
        paintNow();
      }
    };
    rafRef.current = requestAnimationFrame(tick);
  }, [paintNow, staticLayout]);

  const requestPaint = useCallback(() => {
    if (staticLayout) {
      paintNow();
      return;
    }
    const sim = simRef.current;
    const hot =
      (sim != null && sim.alpha() > ALPHA_THRESHOLD) ||
      draggingRef.current ||
      interactionRef.current;
    if (hot) scheduleLoop();
    else paintNow();
  }, [paintNow, scheduleLoop, staticLayout]);

  const applyFocus = useCallback(
    (id: string | null) => {
      setFocusId(id);
      focusIdRef.current = id;
      if (simRef.current && !staticLayout) {
        modelRef.current = simRef.current.toModel(id);
      } else {
        // staticLayout / no sim: rebuild labels for focus under threshold,
        // keep current warm-start positions from graph payload.
        modelRef.current = buildModel(graph.clusters, graph.nodes, graph.edges, id);
      }
      const node = id && modelRef.current ? modelRef.current.byId.get(id) ?? null : null;
      onNodeFocusRef.current?.(id, node);
      if (node?.label) {
        const cluster = modelRef.current?.clusterMeta.get(node.cluster)?.label ?? node.cluster;
        setFocusAnnounce(`${node.label}, ${cluster}`);
      } else {
        setFocusAnnounce(null);
      }
      requestPaint();
    },
    [graph, requestPaint, staticLayout],
  );

  const handleTapNode = useCallback(
    (node: EngineNode) => {
      if (focusIdRef.current === node.id) {
        onNodeOpenRef.current?.(node);
      } else {
        applyFocus(node.id);
      }
    },
    [applyFocus],
  );

  /** Apply contain-fit via d3-zoom when ready; always mirror into transformRef (staticLayout too). */
  const applyFitTransform = useCallback(
    (cssW: number, cssH: number) => {
      const fit = fitTransform(cssW, cssH);
      transformRef.current = fit;
      const canvas = canvasRef.current;
      const zoom = zoomBehaviorRef.current;
      if (canvas && zoom) {
        const minK = zoomExtentMin(cssW, cssH);
        zoom.scaleExtent([minK, ZOOM_MAX]);
        // zoomIdentity.translate(x,y).scale(k) → {k, x, y} matching fitTransform
        select(canvas).call(zoom.transform, zoomIdentity.translate(fit.x, fit.y).scale(fit.k));
      }
    },
    [],
  );

  // Size + DPR + mobile — re-fit world into viewport on mount and every resize
  useLayoutEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const measure = () => {
      const parent = canvas.parentElement;
      const cssW = Math.max(1, parent?.clientWidth || canvas.clientWidth || WORLD_WIDTH);
      const cssH = Math.max(1, parent?.clientHeight || canvas.clientHeight || WORLD_HEIGHT);
      const mobile =
        typeof window !== "undefined" && typeof window.matchMedia === "function"
          ? window.matchMedia(MOBILE_MQ).matches
          : cssW <= 759;
      mobileRef.current = mobile;
      const dprCap = mobile ? DPR_CAP_MOBILE : DPR_CAP_DESKTOP;
      const dpr = Math.min(typeof window !== "undefined" ? window.devicePixelRatio || 1 : 1, dprCap);
      sizeRef.current = { cssW, cssH, dpr };
      canvas.width = Math.floor(cssW * dpr);
      canvas.height = Math.floor(cssH * dpr);
      canvas.style.width = `${cssW}px`;
      canvas.style.height = `${cssH}px`;
      spritesRef.current = ensureSprites(mobile, spritesRef.current);
      applyFitTransform(cssW, cssH);
      requestPaint();
    };

    measure();
    const ro =
      typeof ResizeObserver !== "undefined"
        ? new ResizeObserver(() => measure())
        : null;
    if (ro && canvas.parentElement) ro.observe(canvas.parentElement);
    else if (ro) ro.observe(canvas);

    const mq =
      typeof window !== "undefined" && typeof window.matchMedia === "function"
        ? window.matchMedia(MOBILE_MQ)
        : null;
    const onMq = () => measure();
    mq?.addEventListener?.("change", onMq);

    return () => {
      ro?.disconnect();
      mq?.removeEventListener?.("change", onMq);
    };
  }, [applyFitTransform, requestPaint]);

  // Build model + simulation when graph changes
  useLayoutEffect(() => {
    const base = buildModel(graph.clusters, graph.nodes, graph.edges, focusIdRef.current);
    modelRef.current = base;

    // Tear down previous sim
    simRef.current?.stop();
    simRef.current = null;
    cancelRaf();

    if (staticLayout) {
      paintNow();
      return;
    }

    const sim = createSimulation(base);
    simRef.current = sim;
    // Kick alpha so the loop runs until settle
    sim.simulation.alpha(1);
    scheduleLoop();

    return () => {
      cancelRaf();
      sim.stop();
      if (simRef.current === sim) simRef.current = null;
    };
    // focusId intentionally not in deps — graph rebuild only
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [graphKey, staticLayout, paintNow, scheduleLoop, cancelRaf]);

  // visibilitychange pause
  useEffect(() => {
    if (staticLayout) return;
    const onVis = () => {
      const visible = document.visibilityState !== "hidden";
      visibleRef.current = visible;
      if (!visible) {
        cancelRaf();
        simRef.current?.simulation.stop();
      } else {
        const sim = simRef.current;
        if (sim && sim.alpha() > ALPHA_THRESHOLD) {
          sim.simulation.alphaTarget(0).restart();
          scheduleLoop();
        } else {
          paintNow();
        }
      }
    };
    document.addEventListener("visibilitychange", onVis);
    return () => document.removeEventListener("visibilitychange", onVis);
  }, [cancelRaf, paintNow, scheduleLoop, staticLayout]);

  // Zoom + drag + pointer
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const selection = select(canvas);

    const worldFromEvent = (event: Event): { x: number; y: number } => {
      const [sx, sy] = pointer(event as MouseEvent, canvas);
      // Screen coords are CSS pixels; invert the single d3-zoom transform.
      return screenToWorld(sx, sy, transformRef.current);
    };

    const hitFromEvent = (event: Event): EngineNode | undefined => {
      const model = modelRef.current;
      if (!model) return undefined;
      const w = worldFromEvent(event);
      return hitTest(model, w.x, w.y, 4);
    };

    // Zoom — extent min tracks fitK so mobile can fully contain the world
    const { cssW, cssH } = sizeRef.current;
    const zoom = d3Zoom<HTMLCanvasElement, unknown>()
      .scaleExtent([zoomExtentMin(cssW, cssH), ZOOM_MAX])
      .filter((event) => {
        if (event.type === "wheel") return true;
        if ("button" in event && (event as MouseEvent).button !== 0) return false;
        // Don't pan when starting on a node (let drag/tap handle it)
        if (event.type === "mousedown" || event.type === "touchstart") {
          return !hitFromEvent(event);
        }
        return !event.ctrlKey;
      })
      .on("start", () => {
        if (!draggingRef.current) setCursor("grabbing");
      })
      .on("zoom", (event) => {
        transformRef.current = vtFromZoom(event.transform);
        requestPaint();
      })
      .on("end", () => {
        if (!draggingRef.current) {
          setCursor(hoverIdRef.current ? "pointer" : "grab");
        }
      });

    zoomBehaviorRef.current = zoom;
    selection.call(zoom);
    // Seed contain-fit (same pure fitTransform as staticLayout / resize)
    const fit = fitTransform(cssW, cssH);
    selection.call(zoom.transform, zoomIdentity.translate(fit.x, fit.y).scale(fit.k));

    // Node drag (Obsidian feel: alphaTarget 0.3)
    const drag = d3Drag<HTMLCanvasElement, unknown>()
      .filter((event) => {
        if ("button" in event && (event as MouseEvent).button !== 0) return false;
        return !!hitFromEvent(event);
      })
      .subject((event) => hitFromEvent(event) ?? null)
      .on("start", (event) => {
        const subject = event.subject as EngineNode | null;
        if (!subject) return;
        draggingRef.current = true;
        interactionRef.current = true;
        setCursor("grabbing");
        const sim = simRef.current;
        if (sim && !staticLayout) {
          sim.simulation.alphaTarget(DRAG_ALPHA_TARGET).restart();
          scheduleLoop();
        }
        subject.fx = subject.x;
        subject.fy = subject.y;
      })
      .on("drag", (event) => {
        const subject = event.subject as EngineNode | null;
        if (!subject) return;
        const w = worldFromEvent(event.sourceEvent ?? event);
        subject.fx = w.x;
        subject.fy = w.y;
        subject.x = w.x;
        subject.y = w.y;
        if (simRef.current && !staticLayout) {
          modelRef.current = simRef.current.toModel(focusIdRef.current);
        }
        requestPaint();
      })
      .on("end", (event) => {
        const subject = event.subject as EngineNode | null;
        draggingRef.current = false;
        setCursor(hoverIdRef.current ? "pointer" : "grab");
        const sim = simRef.current;
        if (sim && !staticLayout) {
          sim.simulation.alphaTarget(0);
        }
        if (subject) {
          subject.fx = null;
          subject.fy = null;
        }
        // End interaction after a short cool-down so one more paint can settle
        interactionRef.current = false;
        scheduleLoop();
      });

    selection.call(drag);

    // Hover
    const onMove = (event: PointerEvent) => {
      if (draggingRef.current) return;
      const hit = hitFromEvent(event);
      const next = hit?.id ?? null;
      if (next !== hoverIdRef.current) {
        hoverIdRef.current = next;
        setHoverId(next);
        setCursor(next ? "pointer" : "grab");
        interactionRef.current = next != null;
        requestPaint();
        if (!next) {
          // allow loop to stop after hover-out
          interactionRef.current = false;
        }
      }
    };

    // Tap (click without drag)
    let downPos: { x: number; y: number; id: string | null } | null = null;
    const onDown = (event: PointerEvent) => {
      const hit = hitFromEvent(event);
      downPos = { x: event.clientX, y: event.clientY, id: hit?.id ?? null };
    };
    const onUp = (event: PointerEvent) => {
      if (!downPos) return;
      const dx = event.clientX - downPos.x;
      const dy = event.clientY - downPos.y;
      const moved = dx * dx + dy * dy > 16;
      if (moved) {
        downPos = null;
        return;
      }
      const hit = hitFromEvent(event);
      if (hit) {
        handleTapNode(hit);
      } else {
        applyFocus(null);
      }
      downPos = null;
    };

    canvas.addEventListener("pointermove", onMove);
    canvas.addEventListener("pointerdown", onDown);
    canvas.addEventListener("pointerup", onUp);

    return () => {
      selection.on(".zoom", null);
      selection.on(".drag", null);
      canvas.removeEventListener("pointermove", onMove);
      canvas.removeEventListener("pointerdown", onDown);
      canvas.removeEventListener("pointerup", onUp);
      zoomBehaviorRef.current = null;
    };
  }, [applyFocus, handleTapNode, requestPaint, scheduleLoop, staticLayout]);

  // Repaint when focus/hover changes (React state path)
  useEffect(() => {
    requestPaint();
  }, [focusId, hoverId, requestPaint]);

  // Unmount cleanup
  useEffect(() => {
    return () => {
      cancelRaf();
      simRef.current?.stop();
      simRef.current = null;
    };
  }, [cancelRaf]);

  const onKeyDown = (e: ReactKeyboardEvent<HTMLCanvasElement>) => {
    if (labeledIds.length === 0) return;
    if (e.key === "ArrowRight" || e.key === "ArrowDown") {
      e.preventDefault();
      const next = (kbdIndex + 1 + labeledIds.length) % labeledIds.length;
      setKbdIndex(next);
      applyFocus(labeledIds[next] ?? null);
    } else if (e.key === "ArrowLeft" || e.key === "ArrowUp") {
      e.preventDefault();
      const next = (kbdIndex - 1 + labeledIds.length) % labeledIds.length;
      setKbdIndex(next);
      applyFocus(labeledIds[next] ?? null);
    } else if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      if (focusIdRef.current) {
        const node = modelRef.current?.byId.get(focusIdRef.current);
        if (node) handleTapNode(node);
      } else if (labeledIds.length > 0) {
        setKbdIndex(0);
        applyFocus(labeledIds[0] ?? null);
      }
    } else if (e.key === "Escape") {
      e.preventDefault();
      applyFocus(null);
      setKbdIndex(-1);
    }
  };

  const cls = ["jv-brain", className].filter(Boolean).join(" ");

  return (
    <div ref={wrapRef} className="jv-brain-wrap" style={{ position: "absolute", inset: 0 }}>
      <canvas
        ref={canvasRef}
        className={cls}
        role="application"
        tabIndex={0}
        aria-label={ariaLabel}
        data-static-layout={staticLayout ? "1" : undefined}
        data-focus-id={focusId ?? undefined}
        style={{ ...style, cursor }}
        onKeyDown={onKeyDown}
      />
      <div className="sr-only" aria-live="polite" aria-atomic="true" data-testid="jv-graph-live">
        {liveAnnounce}
      </div>
    </div>
  );
}

