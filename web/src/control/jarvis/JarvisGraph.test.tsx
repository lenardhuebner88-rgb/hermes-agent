// @vitest-environment jsdom
/**
 * JarvisGraph — Wrapper-Tests (F2). Fallback-Hierarchie Mock/STALE/live,
 * Footer-Texte, aria-Label-Zustand. Canvas-Interaktion steckt in
 * JarvisGraphCanvas.test.tsx (staticLayout); Pure-Pins in graphEngine.test.ts.
 * Eliminiert die S7-SVG-Flake-Stelle strukturell.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, configure, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import type { PaGraphCluster, PaGraphEdge, PaGraphNode, PaGraphResponse } from "@/lib/api";
import { _resetPollingStore, refresh } from "../hooks/pollingStore";

configure({ asyncUtilTimeout: 5000 });

const getPaGraphMock = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: {
      ...actual.api,
      getPaGraph: getPaGraphMock,
    },
  };
});

// Canvas getContext stub so the wrapper mount does not throw in jsdom.
beforeEach(() => {
  const gradient = { addColorStop: vi.fn() };
  HTMLCanvasElement.prototype.getContext = vi.fn(() => ({
    setTransform: vi.fn(),
    clearRect: vi.fn(),
    save: vi.fn(),
    restore: vi.fn(),
    translate: vi.fn(),
    scale: vi.fn(),
    beginPath: vi.fn(),
    arc: vi.fn(),
    moveTo: vi.fn(),
    quadraticCurveTo: vi.fn(),
    fill: vi.fn(),
    stroke: vi.fn(),
    fillRect: vi.fn(),
    drawImage: vi.fn(),
    fillText: vi.fn(),
    strokeText: vi.fn(),
    measureText: () => ({ width: 40 }),
    createRadialGradient: () => gradient,
    createLinearGradient: () => gradient,
    canvas: document.createElement("canvas"),
  })) as unknown as typeof HTMLCanvasElement.prototype.getContext;
});

import {
  JarvisGraph,
  JarvisGraphStatsTag,
  JarvisGraphTag,
  openGraphRef,
} from "./JarvisGraph";
import { PA_GRAPH_KEY } from "./usePaGraph";

const CLUSTERS: PaGraphCluster[] = [
  { id: "canon", label: "Canon", color: "#38d8ff" },
  { id: "projekte", label: "Projekte", color: "#3ddc97" },
  { id: "agenten", label: "Agenten", color: "#ffb347" },
  { id: "skills", label: "Skills", color: "#5b8cff" },
  { id: "memories", label: "Memories", color: "#b78cff" },
  { id: "receipts", label: "Receipts", color: "#ff7ab8" },
  { id: "archiv", label: "Archiv", color: "#5a6f8f" },
];

function node(partial: Partial<PaGraphNode> & { id: string }): PaGraphNode {
  return {
    label: null,
    cluster: "canon",
    kind: "doc",
    weight: 0.3,
    x: 100,
    y: 100,
    ...partial,
  };
}

function liveGraph(
  nodes: PaGraphNode[],
  edges: PaGraphEdge[],
  extra?: { errors?: { source: string; error: string }[]; generated_at?: string },
): PaGraphResponse {
  return {
    schema: "pa-graph/v1",
    source: "live",
    layout: "precomputed-viewbox-1280x820",
    generated_at: extra?.generated_at ?? "2026-07-19T19:00:00+00:00",
    refresh: {
      interval_s: 30,
      cache_ttl_s: 60,
      on_error: "empty-live-data + frontend-mock-fallback",
    },
    clusters: CLUSTERS,
    nodes,
    edges,
    errors: extra?.errors ?? [],
  };
}

function defaultNodes(): PaGraphNode[] {
  return [
    node({
      id: "vault:00-canon/vision.md",
      label: "vision",
      cluster: "canon",
      weight: 0.9,
      x: 640,
      y: 400,
      href: "vault://00-Canon/vision.md",
      ref: "vault://00-Canon/vision.md",
    }),
    node({
      id: "project:hermes",
      label: "Hermes-Infra",
      cluster: "projekte",
      kind: "project",
      weight: 0.8,
      x: 520,
      y: 520,
      href: "/control/projekte-klassisch",
    }),
    node({
      id: "task:t_abc",
      label: "S2.7 Graph",
      cluster: "projekte",
      kind: "task",
      weight: 0.55,
      x: 448,
      y: 575,
      href: "/control/fleet?task=t_abc",
    }),
    node({
      id: "agent:jarvis",
      label: "Jarvis",
      cluster: "agenten",
      kind: "agent",
      weight: 0.75,
      x: 795,
      y: 300,
    }),
    node({
      id: "receipt:kimi/x.md",
      label: "Receipt X",
      cluster: "receipts",
      kind: "receipt",
      weight: 0.6,
      x: 980,
      y: 150,
      href: "/api/projects/receipts/Kimi/x.md",
    }),
    node({ id: "n2", label: "conventions", cluster: "canon", weight: 0.6, x: 640, y: 245 }),
    node({ id: "n3", cluster: "canon", weight: 0.3, x: 588, y: 182 }),
    node({ id: "proj:low", label: "Schwach", cluster: "projekte", kind: "project", weight: 0.4, x: 600, y: 655 }),
  ];
}

function defaultEdges(): PaGraphEdge[] {
  return [
    { from: "vault:00-canon/vision.md", to: "project:hermes", kind: "wikilink" },
    { from: "project:hermes", to: "task:t_abc", kind: "project-task" },
  ];
}

function renderGraph() {
  return render(
    <MemoryRouter initialEntries={["/control/projekte"]}>
      <JarvisGraph />
      <JarvisGraphTag />
      <JarvisGraphStatsTag />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  _resetPollingStore();
  getPaGraphMock.mockResolvedValue(liveGraph(defaultNodes(), defaultEdges()));
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  _resetPollingStore();
});

describe("JarvisGraph — Fallback-Hierarchie (Wrapper)", () => {
  it("ohne jede Live-Daten (Fetch-Fehler vor erstem Erfolg) → Mock + Vorschau-aria", async () => {
    getPaGraphMock.mockRejectedValue(new Error("network down"));
    const { container } = renderGraph();

    // Wait for poll to settle via footer tag (no SVG label wait).
    expect(await screen.findByText(/VORSCHAU/)).toBeTruthy();
    const canvas = container.querySelector("canvas.jv-brain");
    expect(canvas?.getAttribute("aria-label")).toBe("Estate-Graph (Vorschau, Mock-Daten)");
    expect(container.querySelector(".jv-gtag")?.textContent).toBe(
      "GRAPH · VORSCHAU — MOCK-DATEN · S2.7 FOLGT",
    );
    expect(container.querySelector(".jv-mocktag")?.textContent).toBe(" · Graph: Vorschau (Mock)");
  });

  it("HTTP 200 mit leeren nodes → ebenfalls Mock", async () => {
    getPaGraphMock.mockResolvedValue(
      liveGraph([], [], { errors: [{ source: "qmd", error: "index fehlt" }] }),
    );
    const { container } = renderGraph();

    expect(await screen.findByText(/VORSCHAU/)).toBeTruthy();
    expect(container.querySelector("canvas")?.getAttribute("aria-label")).toBe(
      "Estate-Graph (Vorschau, Mock-Daten)",
    );
  });

  it("keep-last-good: Live bleibt bei Fetch-Fehler, STALE-Hinweis dezent", async () => {
    const { container } = renderGraph();
    expect(await screen.findByText(/LIVE/)).toBeTruthy();
    expect(container.querySelector("canvas")?.getAttribute("aria-label")).toBe(
      "Estate-Graph (live, 8 Knoten)",
    );

    getPaGraphMock.mockRejectedValue(new Error("500: boom"));
    await act(async () => {
      await refresh(PA_GRAPH_KEY);
    });

    const aria = container.querySelector("canvas")?.getAttribute("aria-label") ?? "";
    expect(aria).toContain("Estate-Graph (live, 8 Knoten)");
    expect(aria).toContain("älterer Stand");
    expect(container.querySelector(".jv-gtag")?.textContent).toContain("STALE");
  });
});

describe("JarvisGraph — Live-Wrapper + Footer", () => {
  it("rendert Canvas mit live aria-label und Footer-Tags", async () => {
    const { container } = renderGraph();
    expect(await screen.findByText(/LIVE/)).toBeTruthy();

    const canvas = container.querySelector("canvas.jv-brain");
    expect(canvas).toBeTruthy();
    expect(canvas?.getAttribute("role")).toBe("application");
    expect(canvas?.getAttribute("aria-label")).toBe("Estate-Graph (live, 8 Knoten)");
    expect(container.querySelector(".jv-gtag")?.textContent).toMatch(
      /^GRAPH · LIVE — 8 KNOTEN · Stand \d{2}:\d{2}$/,
    );
    expect(container.querySelector(".jv-mocktag")?.textContent).toBe(" · Graph: live · 8 Knoten");
    // No leftover SVG graph tree
    expect(container.querySelector("svg.jv-brain")).toBeNull();
  });

  it("Teilquellen-Fehler landen dezent als Tooltip am Footer-Tag, kein Panel", async () => {
    getPaGraphMock.mockResolvedValue(
      liveGraph(defaultNodes(), defaultEdges(), {
        errors: [{ source: "kanban", error: "disk i/o error" }],
      }),
    );
    const { container } = renderGraph();

    expect(await screen.findByText(/LIVE/)).toBeTruthy();
    const gtag = container.querySelector(".jv-gtag");
    expect(gtag?.getAttribute("title")).toContain("Graph-Teilquellen derzeit fehlerhaft:");
    expect(gtag?.getAttribute("title")).toContain("kanban: disk i/o error");
    expect(container.querySelector('[role="alert"]')).toBeNull();
  });
});

describe("JarvisGraph — openGraphRef (pure, re-exported)", () => {
  it("openGraphRef: /control + /api navigierbar, vault://memory://undefined nicht", () => {
    const navigate = vi.fn();
    const assign = vi.fn();
    expect(openGraphRef("/control/fleet?task=t_1", { navigate, assign })).toBe(true);
    expect(navigate).toHaveBeenCalledWith("/control/fleet?task=t_1");
    expect(openGraphRef("/api/projects/receipts/Kimi/x.md", { navigate, assign })).toBe(true);
    expect(assign).toHaveBeenCalledWith("/api/projects/receipts/Kimi/x.md");
    expect(openGraphRef("vault://00-Canon/vision.md", { navigate, assign })).toBe(false);
    expect(openGraphRef("memory://memsearch/2026-07-19.md", { navigate, assign })).toBe(false);
    expect(openGraphRef(undefined, { navigate, assign })).toBe(false);
    expect(navigate).toHaveBeenCalledTimes(1);
    expect(assign).toHaveBeenCalledTimes(1);
  });
});
