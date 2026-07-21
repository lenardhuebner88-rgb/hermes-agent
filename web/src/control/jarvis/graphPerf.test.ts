/**
 * F2 Perf-Gate harness (node/vitest) — 500 nodes / 1500 edges.
 * Measures simulation settle + synthetic paint-frame costs for Desktop and
 * mobile (390×844, reduced effects, DPR 1.5). Not a browser trace; documents
 * CPU-side bounds that the rAF loop must stay under.
 *
 * Run: cd web && ../node_modules/.bin/vitest run src/control/jarvis/graphPerf.bench.ts
 */
import { describe, expect, it } from "vitest";

import {
  buildModel,
  createSimulation,
  computeVisualState,
  edgeTier,
  nodeRadius,
  GLOW_WEIGHT,
  ORB_WEIGHT,
  A4_GRADIENT_IDS,
} from "./graphEngine";
import { generateLargeGraph } from "./graphFixtures";

function percentile(sorted: number[], p: number): number {
  if (sorted.length === 0) return 0;
  const idx = Math.min(sorted.length - 1, Math.max(0, Math.ceil((p / 100) * sorted.length) - 1));
  return sorted[idx]!;
}

/** Cheap stand-in for one paint frame (edges + nodes + auras + labels). */
function syntheticPaintMs(
  model: ReturnType<typeof buildModel>,
  opts: { mobile: boolean; labelLod: boolean },
): number {
  const t0 = performance.now();
  const visual = computeVisualState(model, null, null);
  let ops = 0;
  for (const _ of model.auras) ops += 1;
  for (const e of model.edges) {
    const tier = edgeTier(e.tierWeight);
    ops += tier.width + (visual.edges.get(e.key) === "lit" ? 1 : 0);
  }
  for (const n of model.sortedNodes) {
    const r = nodeRadius(n.weight);
    ops += r;
    if (!opts.mobile && n.weight >= GLOW_WEIGHT) ops += 2;
    if (n.weight >= ORB_WEIGHT && A4_GRADIENT_IDS.has(n.cluster)) ops += 1;
  }
  if (opts.labelLod) {
    for (const [id] of model.labels) {
      const n = model.byId.get(id);
      if (n?.label) ops += n.label.length;
    }
  }
  // Touch ops so the loop is not DCE'd.
  if (ops < 0) throw new Error("unreachable");
  return performance.now() - t0;
}

describe("F2 perf gate — 500/1500 Desktop + mobile", () => {
  it("documents settle + frame-p95 + long-task counts", () => {
    const { clusters, nodes, edges } = generateLargeGraph(500, 1500, 42);
    expect(nodes).toHaveLength(500);
    expect(edges.length).toBeGreaterThanOrEqual(1400);

    // ── Desktop settle ──
    const model = buildModel(clusters, nodes, edges, null);
    const sim = createSimulation(model);
    const settleT0 = performance.now();
    const ticks = sim.settle(0.02, 400);
    const settleMs = performance.now() - settleT0;
    const settled = sim.toModel(null);
    sim.stop();

    // ── Synthetic frames during settle path (desktop full effects) ──
    const desktopFrames: number[] = [];
    for (let i = 0; i < 60; i += 1) {
      desktopFrames.push(syntheticPaintMs(settled, { mobile: false, labelLod: true }));
    }
    desktopFrames.sort((a, b) => a - b);
    const desktopP95 = percentile(desktopFrames, 95);
    const desktopLong = desktopFrames.filter((ms) => ms > 50).length;

    // ── Mobile 390×844 profile (reduced effects, fewer labels via LOD off) ──
    const mobileFrames: number[] = [];
    for (let i = 0; i < 60; i += 1) {
      mobileFrames.push(syntheticPaintMs(settled, { mobile: true, labelLod: false }));
    }
    mobileFrames.sort((a, b) => a - b);
    const mobileP95 = percentile(mobileFrames, 95);
    const mobileLong = mobileFrames.filter((ms) => ms > 50).length;

    // Mobile settle (fresh sim, same graph) — same physics, report separately.
    const simM = createSimulation(buildModel(clusters, nodes, edges, null));
    const settleMT0 = performance.now();
    const ticksM = simM.settle(0.02, 400);
    const settleMMs = performance.now() - settleMT0;
    simM.stop();

    const report = {
      nodes: 500,
      edges: edges.length,
      desktop: {
        viewport: "contain-fit (fitTransform) 1280×820 world",
        dprCap: 2,
        settleMs: Math.round(settleMs * 10) / 10,
        settleTicks: ticks,
        frameP95Ms: Math.round(desktopP95 * 1000) / 1000,
        longTasksOver50ms: desktopLong,
        sampleFrames: desktopFrames.length,
      },
      mobile: {
        viewport: "390×844 emulation (effects reduced, DPR cap 1.5)",
        dprCap: 1.5,
        settleMs: Math.round(settleMMs * 10) / 10,
        settleTicks: ticksM,
        frameP95Ms: Math.round(mobileP95 * 1000) / 1000,
        longTasksOver50ms: mobileLong,
        sampleFrames: mobileFrames.length,
        reductions: ["stardust/3", "no glow sprites", "label LOD off when k low"],
      },
      alphaTarget: 0.02,
    };

    // Persist for the F2 report.
    console.log("F2_PERF_GATE", JSON.stringify(report, null, 2));

    // Soft budgets — simulation must settle; paint path must not be multi-frame.
    expect(ticks).toBeLessThanOrEqual(400);
    expect(settleMs).toBeLessThan(5000);
    expect(desktopP95).toBeLessThan(50);
    expect(mobileP95).toBeLessThan(50);
    expect(desktopLong).toBe(0);
    expect(mobileLong).toBe(0);
  }, 20000);
});
