/**
 * graphHubs — S6.4b: Top-Hubs und Filter-Zeilen aus den Graph-Knoten ableiten
 * (statt JARVIS_TOP_HUBS/JARVIS_FILTER_ROWS-Mock aus mockContent.ts).
 *
 * Die Ableitung zählt Knoten pro Cluster und bildet die Cluster-Farbe auf den
 * CSS-Tone-Namen der Jarvis-Shell ab. Top-Hubs: die N stärksten Cluster
 * absteigend; Filter: alle Cluster mit Knotenzahl (Reihenfolge der
 * Cluster-Liste des Graphen).
 */
import type { PaGraphResponse } from "@/lib/api";
import type { JarvisHubRow } from "./mockContent";

/** S6: Cluster-Farbe → CSS-Tone-Name der Jarvis-Shell (.jv-tone-*). */
const COLOR_TO_TONE: Record<string, JarvisHubRow["tone"]> = {
  "#38d8ff": "cyan",
  "#3ddc97": "gruen",
  "#ffb347": "amber",
  "#5b8cff": "blau",
  "#b78cff": "violett",
  "#ff7ab8": "pink",
  "#5a6f8f": "grau",
};

function clusterTone(color: string): JarvisHubRow["tone"] {
  return COLOR_TO_TONE[color] ?? "grau";
}

function countByCluster(graph: PaGraphResponse): Map<string, number> {
  const counts = new Map<string, number>();
  for (const node of graph.nodes) {
    counts.set(node.cluster, (counts.get(node.cluster) ?? 0) + 1);
  }
  return counts;
}

/** S6: Top-Hubs — Knoten pro Cluster zählen, absteigend, Top N (Default 6). */
export function deriveTopHubs(graph: PaGraphResponse, topN = 6): JarvisHubRow[] {
  const counts = countByCluster(graph);
  const clusterById = new Map(graph.clusters.map((c) => [c.id, c]));
  return [...counts.entries()]
    .sort((a, b) => b[1] - a[1])
    .slice(0, topN)
    .map(([clusterId, count]) => {
      const cluster = clusterById.get(clusterId);
      return {
        tone: cluster ? clusterTone(cluster.color) : "grau",
        name: cluster?.label ?? clusterId,
        count: String(count),
      };
    });
}

/** S6: Filter-Zeilen — alle Cluster mit Knotenzahl in Cluster-Reihenfolge. */
export function deriveFilterRows(graph: PaGraphResponse): JarvisHubRow[] {
  const counts = countByCluster(graph);
  return graph.clusters
    .filter((c) => (counts.get(c.id) ?? 0) > 0)
    .map((cluster) => ({
      tone: clusterTone(cluster.color),
      name: cluster.label,
      count: String(counts.get(cluster.id) ?? 0),
    }));
}
