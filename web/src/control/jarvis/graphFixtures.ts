/**
 * Shared graph fixtures for tests + F2 perf harness.
 */
import type { PaGraphCluster, PaGraphEdge, PaGraphNode } from "@/lib/api";

import { CLUSTER_CENTERS, WORLD_HEIGHT, WORLD_WIDTH } from "./graphEngine";

export const FIXTURE_CLUSTERS: PaGraphCluster[] = [
  { id: "canon", label: "Canon", color: "#38d8ff" },
  { id: "projekte", label: "Projekte", color: "#3ddc97" },
  { id: "agenten", label: "Agenten", color: "#ffb347" },
  { id: "skills", label: "Skills", color: "#5b8cff" },
  { id: "memories", label: "Memories", color: "#b78cff" },
  { id: "receipts", label: "Receipts", color: "#ff7ab8" },
  { id: "archiv", label: "Archiv", color: "#5a6f8f" },
];

const CLUSTER_IDS = FIXTURE_CLUSTERS.map((c) => c.id);

/** Deterministic PRNG (mulberry32) — same seed → same graph. */
function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/**
 * 500-Knoten-Fixture: 7 Cluster, Gewichte 0.2–1.0, ~1500 Kanten,
 * x/y-Warm-Start um Cluster-Zentroide (pa_graph-Spiral-Analog).
 */
export function generateLargeGraph(
  nodeCount = 500,
  edgeCount = 1500,
  seed = 42,
): { clusters: PaGraphCluster[]; nodes: PaGraphNode[]; edges: PaGraphEdge[] } {
  const rand = mulberry32(seed);
  const nodes: PaGraphNode[] = [];

  for (let i = 0; i < nodeCount; i += 1) {
    const cluster = CLUSTER_IDS[i % CLUSTER_IDS.length]!;
    const [cx, cy] = CLUSTER_CENTERS[cluster] ?? [WORLD_WIDTH / 2, WORLD_HEIGHT / 2];
    const angle = (i / nodeCount) * Math.PI * 12 + rand() * 0.4;
    const radius = 20 + (i % 40) * 4 + rand() * 12;
    const weight = Math.round((0.2 + rand() * 0.8) * 100) / 100;
    const labeled = weight >= 0.4 || i % 7 === 0;
    nodes.push({
      id: `n:${i}`,
      label: labeled ? `Node ${i} ${cluster}` : null,
      cluster,
      kind: "doc",
      weight,
      x: Math.round((cx + Math.cos(angle) * radius) * 10) / 10,
      y: Math.round((cy + Math.sin(angle) * radius) * 10) / 10,
    });
  }

  const edges: PaGraphEdge[] = [];
  const seen = new Set<string>();
  let attempts = 0;
  while (edges.length < edgeCount && attempts < edgeCount * 8) {
    attempts += 1;
    const a = Math.floor(rand() * nodeCount);
    let b: number;
    if (rand() < 0.75) {
      const clusterIdx = a % CLUSTER_IDS.length;
      const candidates: number[] = [];
      for (let j = clusterIdx; j < nodeCount; j += CLUSTER_IDS.length) {
        if (j !== a) candidates.push(j);
      }
      b = candidates[Math.floor(rand() * candidates.length)] ?? (a + 1) % nodeCount;
    } else {
      b = Math.floor(rand() * nodeCount);
    }
    if (a === b) continue;
    const from = Math.min(a, b);
    const to = Math.max(a, b);
    const key = `${from}->${to}`;
    if (seen.has(key)) continue;
    seen.add(key);
    edges.push({ from: `n:${from}`, to: `n:${to}`, kind: "wikilink" });
  }

  return { clusters: FIXTURE_CLUSTERS, nodes, edges };
}
