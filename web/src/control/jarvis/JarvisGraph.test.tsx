// @vitest-environment jsdom
/**
 * JarvisGraph — Kontrakt-Invariante zwischen dem `#pa-graph-mock`-Datensatz
 * (graphMock.ts, 1:1 aus dem A4-Mockup) und der statisch gerenderten SVG:
 * jeder gelabelte Mock-Knoten erscheint als Graph-Label, jede Cluster-Farbe
 * des Datensatzes steckt in der SVG. So bleibt die „statische Vorschau"
 * nachweislich DIE Vorschau des Datensatzes, den S2.7 live füllt.
 */
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render } from "@testing-library/react";

import { JarvisGraph } from "./JarvisGraph";
import { PA_GRAPH_MOCK } from "./graphMock";

afterEach(cleanup);

describe("JarvisGraph (Mock-Datenkontrakt #pa-graph-mock)", () => {
  it("rendert jedes gelabelte Mock-Knoten-Label als SVG-Text", () => {
    const { container } = render(<JarvisGraph />);
    const texts = Array.from(container.querySelectorAll("text")).map((el) => el.textContent);
    const labeledNodes = PA_GRAPH_MOCK.nodes.filter((node) => node.label !== null);
    expect(labeledNodes.length).toBeGreaterThan(10);
    for (const node of labeledNodes) {
      expect(texts, `Label fehlt im Graph: ${node.label}`).toContain(node.label);
    }
  });

  it("nutzt jede Cluster-Farbe des Datensatzes in der SVG", () => {
    const { container } = render(<JarvisGraph />);
    for (const cluster of PA_GRAPH_MOCK.clusters) {
      const hits = container.querySelectorAll(
        `[fill="${cluster.color}"], [stop-color="${cluster.color}"]`,
      );
      expect(hits.length, `Cluster-Farbe fehlt im Graph: ${cluster.id}`).toBeGreaterThan(0);
    }
  });

  it("trägt Fokus-Zustand und Vorschau-Kennzeichnung des Mockups", () => {
    const { container } = render(<JarvisGraph />);
    const texts = Array.from(container.querySelectorAll("text")).map((el) => el.textContent);
    expect(texts).toContain("· FOKUS ·");
    const svg = container.querySelector("svg");
    expect(svg?.getAttribute("aria-label")).toBe("Estate-Graph (Vorschau, Mock-Daten)");
  });
});
