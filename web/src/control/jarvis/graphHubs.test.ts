/**
 * graphHubs — S6.4b: Ableitung der Top-Hubs/Filter-Zeilen aus Graph-Knoten.
 */
import { describe, expect, it } from "vitest";

import type { PaGraphResponse } from "@/lib/api";
import { deriveFilterRows, deriveTopHubs } from "./graphHubs";

function makeGraph(
  clusters: PaGraphResponse["clusters"],
  nodes: PaGraphResponse["nodes"],
): PaGraphResponse {
  return {
    schema: "pa-graph/v1",
    source: "test",
    layout: "test",
    generated_at: "2026-07-20T12:00:00+02:00",
    refresh: { interval_s: 30 },
    clusters,
    nodes,
    edges: [],
  };
}

const CLUSTERS: PaGraphResponse["clusters"] = [
  { id: "canon", label: "Canon", color: "#38d8ff" },
  { id: "skills", label: "Skills", color: "#5b8cff" },
  { id: "archiv", label: "Archiv", color: "#5a6f8f" },
];

function node(id: string, cluster: string): PaGraphResponse["nodes"][number] {
  return { id, label: id, cluster, kind: "doc", weight: 0.5, x: 0, y: 0 };
}

describe("deriveTopHubs", () => {
  it("zählt Knoten pro Cluster absteigend und bildet Farben auf Tones ab", () => {
    const graph = makeGraph(CLUSTERS, [
      node("c1", "canon"),
      node("c2", "canon"),
      node("c3", "canon"),
      node("s1", "skills"),
      node("a1", "archiv"),
      node("a2", "archiv"),
    ]);
    const hubs = deriveTopHubs(graph);
    expect(hubs).toEqual([
      { tone: "cyan", name: "Canon", count: "3" },
      { tone: "grau", name: "Archiv", count: "2" },
      { tone: "blau", name: "Skills", count: "1" },
    ]);
  });

  it("respektiert topN", () => {
    const graph = makeGraph(CLUSTERS, [
      node("c1", "canon"),
      node("s1", "skills"),
      node("a1", "archiv"),
    ]);
    expect(deriveTopHubs(graph, 2)).toHaveLength(2);
  });

  it("unbekannte Cluster-Farbe fällt auf grau", () => {
    const graph = makeGraph(
      [{ id: "x", label: "X", color: "#ffffff" }],
      [node("x1", "x")],
    );
    expect(deriveTopHubs(graph)[0].tone).toBe("grau");
  });

  it("unbekannte Cluster-Id zeigt die Id als Namen", () => {
    const graph = makeGraph([], [node("o1", "orphan")]);
    expect(deriveTopHubs(graph)[0].name).toBe("orphan");
  });

  it("leerer Graph → leere Liste", () => {
    expect(deriveTopHubs(makeGraph([], []))).toEqual([]);
  });
});

describe("deriveFilterRows", () => {
  it("alle Cluster mit Knotenzahl in Cluster-Reihenfolge", () => {
    const graph = makeGraph(CLUSTERS, [
      node("c1", "canon"),
      node("s1", "skills"),
      node("s2", "skills"),
    ]);
    const rows = deriveFilterRows(graph);
    expect(rows).toEqual([
      { tone: "cyan", name: "Canon", count: "1" },
      { tone: "blau", name: "Skills", count: "2" },
    ]);
  });

  it("Cluster ohne Knoten werden ausgelassen", () => {
    const graph = makeGraph(CLUSTERS, [node("c1", "canon")]);
    const rows = deriveFilterRows(graph);
    expect(rows).toHaveLength(1);
    expect(rows[0].name).toBe("Canon");
  });
});
