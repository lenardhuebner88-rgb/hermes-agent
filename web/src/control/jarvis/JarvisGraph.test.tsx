// @vitest-environment jsdom
/**
 * JarvisGraph — datengetriebener Estate-Graph (S2.7, GET /api/pa/graph).
 * Belegt die Fallback-Hierarchie (Mock nur ohne jede Live-Daten oder bei
 * leeren nodes; keep-last-good + STALE bei Fetch-Fehler), den Live-Render
 * aus einer Fixture (Orbs/Kanten/Labels/Auren/Cluster-Farben), die
 * href-Navigation (/control + /api navigierbar, vault://memory:// nicht),
 * die Label-Schwelle samt Cap und die zustandsabhängigen Footer-Texte.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useEffect } from "react";
import { act, cleanup, configure, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";

import type { PaGraphCluster, PaGraphEdge, PaGraphNode, PaGraphResponse } from "@/lib/api";
import { _resetPollingStore, refresh } from "../hooks/pollingStore";

configure({ asyncUtilTimeout: 5000 });
// S7: unter geteilter Host-Last bounce-t der Footer-Tag-waitFor vereinzelt
// über den Default-testTimeout (S6.6-Muster: scoped, keine Pauschale).
vi.setConfig({ testTimeout: 15_000 });

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

import {
  edgePath,
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
    refresh: { interval_s: 30, cache_ttl_s: 60, on_error: "empty-live-data + frontend-mock-fallback" },
    clusters: CLUSTERS,
    nodes,
    edges,
    errors: extra?.errors ?? [],
  };
}

/** Deckt alle Renderer-Pfade ab: Orb-Schwelle, Glow, drei Kanten-Tiers,
 *  vault://- vs. /control- vs. /api-hrefs, Schwelle/unter Schwelle. */
function defaultNodes(): PaGraphNode[] {
  return [
    node({ id: "vault:00-canon/vision.md", label: "vision", cluster: "canon", weight: 0.9, x: 640, y: 400, href: "vault://00-Canon/vision.md", ref: "vault://00-Canon/vision.md" }),
    node({ id: "vault:00-canon/conventions-gates.md", label: "conventions", cluster: "canon", weight: 0.6, x: 640, y: 245, href: "vault://00-Canon/conventions-gates.md" }),
    node({ id: "vault:00-canon/planspec.md", cluster: "canon", weight: 0.3, x: 588, y: 182 }),
    node({ id: "project:hermes", label: "Hermes-Infra", cluster: "projekte", kind: "project", weight: 0.8, x: 520, y: 520, href: "/control/projekte-klassisch" }),
    node({ id: "task:t_abc", label: "S2.7 Graph", cluster: "projekte", kind: "task", weight: 0.55, x: 448, y: 575, href: "/control/fleet?task=t_abc" }),
    node({ id: "proj:low", label: "Schwach", cluster: "projekte", kind: "project", weight: 0.4, x: 600, y: 655 }),
    node({ id: "agent:jarvis", label: "Jarvis", cluster: "agenten", kind: "agent", weight: 0.75, x: 795, y: 300 }),
    node({ id: "receipt:kimi/x.md", label: "Receipt X", cluster: "receipts", kind: "receipt", weight: 0.6, x: 980, y: 150, href: "/api/projects/receipts/Kimi/x.md" }),
  ];
}

function defaultEdges(): PaGraphEdge[] {
  return [
    { from: "vault:00-canon/vision.md", to: "project:hermes", kind: "wikilink" },
    { from: "vault:00-canon/vision.md", to: "vault:00-canon/conventions-gates.md", kind: "wikilink" },
    { from: "vault:00-canon/conventions-gates.md", to: "vault:00-canon/planspec.md", kind: "wikilink" },
    { from: "project:hermes", to: "task:t_abc", kind: "project-task" },
    { from: "task:t_abc", to: "proj:low", kind: "task-link" },
  ];
}

let seenPath = "";
function LocationProbe() {
  const loc = useLocation();
  useEffect(() => {
    seenPath = loc.pathname + loc.search;
  }, [loc]);
  return null;
}

function renderGraph() {
  return render(
    <MemoryRouter initialEntries={["/control/projekte"]}>
      <LocationProbe />
      <JarvisGraph />
      <JarvisGraphTag />
      <JarvisGraphStatsTag />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  _resetPollingStore();
  seenPath = "";
  getPaGraphMock.mockResolvedValue(liveGraph(defaultNodes(), defaultEdges()));
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  _resetPollingStore();
});

describe("JarvisGraph — Fallback-Hierarchie", () => {
  it("ohne jede Live-Daten (Fetch-Fehler vor erstem Erfolg) → A4-Mock mit Vorschau-Labels", async () => {
    getPaGraphMock.mockRejectedValue(new Error("network down"));
    const { container } = renderGraph();

    // Mock-Labels oberhalb der Schwelle erscheinen, darunter nicht.
    expect(await screen.findByText("vision")).toBeTruthy();
    expect(await screen.findByText("Hermes-Infra")).toBeTruthy();
    expect(await screen.findByText("Diktat")).toBeTruthy(); // 0.47 ≥ Schwelle
    expect(screen.queryByText("Grok")).toBeNull(); // 0.4 < Schwelle

    const svg = container.querySelector("svg");
    expect(svg?.getAttribute("aria-label")).toBe("Estate-Graph (Vorschau, Mock-Daten)");
    expect(container.querySelector(".jv-gtag")?.textContent).toBe(
      "GRAPH · VORSCHAU — MOCK-DATEN · S2.7 FOLGT",
    );
    expect(container.querySelector(".jv-mocktag")?.textContent).toBe(" · Graph: Vorschau (Mock)");
  });

  it("HTTP 200 mit leeren nodes (Backend-Gesamtausfall) → ebenfalls A4-Mock", async () => {
    getPaGraphMock.mockResolvedValue(
      liveGraph([], [], { errors: [{ source: "qmd", error: "index fehlt" }] }),
    );
    const { container } = renderGraph();

    expect(await screen.findByText("Hermes-Infra")).toBeTruthy();
    expect(container.querySelector("svg")?.getAttribute("aria-label")).toBe(
      "Estate-Graph (Vorschau, Mock-Daten)",
    );
  });

  it("keep-last-good: Live-Daten bleiben bei Fetch-Fehler stehen, STALE-Hinweis dezent", async () => {
    const { container } = renderGraph();
    expect(await screen.findByText("vision")).toBeTruthy();

    getPaGraphMock.mockRejectedValue(new Error("500: boom"));
    await act(async () => {
      await refresh(PA_GRAPH_KEY);
    });

    // Daten stehen weiter, Zustand kippt auf stale.
    expect(screen.getByText("vision")).toBeTruthy();
    expect(screen.getByText("Hermes-Infra")).toBeTruthy();
    const aria = container.querySelector("svg")?.getAttribute("aria-label") ?? "";
    expect(aria).toContain("Estate-Graph (live, 8 Knoten)");
    expect(aria).toContain("älterer Stand");
    expect(container.querySelector(".jv-gtag")?.textContent).toContain("STALE");
  });
});

describe("JarvisGraph — Live-Render aus Fixture", () => {
  it("rendert Knoten als Orbs, Kanten als A4-Kurven, Labels und Auren aus den Daten", async () => {
    const { container } = renderGraph();

    // Labels: Schwelle 0.45 — „Schwach" (0.4) fehlt, Rest da.
    for (const label of ["vision", "conventions", "Hermes-Infra", "S2.7 Graph", "Jarvis", "Receipt X"]) {
      expect(await screen.findByText(label), `Label fehlt: ${label}`).toBeTruthy();
    }
    expect(screen.queryByText("Schwach")).toBeNull();
    expect(screen.getByText("vision").getAttribute("class")).toContain("big"); // ≥ 0.7

    // Orbs ab 0.45 mit Cluster-Gradient; darunter einfache Cluster-Farbe.
    expect(
      container.querySelector('[data-node-id="vault:00-canon/vision.md"] circle[fill="url(#jv-orb-canon)"]'),
    ).toBeTruthy();
    expect(
      container.querySelector('[data-node-id="project:hermes"] circle[fill="url(#jv-orb-projekte)"]'),
    ).toBeTruthy();
    expect(
      container.querySelector('[data-node-id="agent:jarvis"] circle[fill="url(#jv-orb-agenten)"]'),
    ).toBeTruthy();
    const plain = container.querySelector('[data-node-id="proj:low"] circle[fill="#3ddc97"]');
    expect(plain).toBeTruthy();
    expect(plain?.getAttribute("opacity")).toBe("0.69");

    // Auren nur für belegte Cluster (skills/memories/archiv haben keine Knoten).
    expect(container.querySelectorAll('[fill="url(#jv-neb-canon)"]').length).toBeGreaterThan(0);
    expect(container.querySelectorAll('[fill="url(#jv-neb-projekte)"]').length).toBeGreaterThan(0);
    expect(container.querySelectorAll('[fill="url(#jv-neb-skills)"]')).toHaveLength(0);

    // Kanten: 5 Kurven, d aus der A4-Quadratic-Formel, Tier aus Endpunkt-Gewicht.
    expect(container.querySelectorAll("path")).toHaveLength(5);
    const hub = container.querySelector('[data-edge="vault:00-canon/vision.md->project:hermes"]');
    expect(hub?.getAttribute("d")).toBe(edgePath(640, 400, 520, 520));
    expect(hub?.getAttribute("stroke-opacity")).toBe("0.34");
    expect(hub?.getAttribute("stroke-width")).toBe("1.2");
    const mid = container.querySelector('[data-edge="task:t_abc->proj:low"]');
    expect(mid?.getAttribute("stroke-opacity")).toBe("0.24");
    const low = container.querySelector('[data-edge="vault:00-canon/conventions-gates.md->vault:00-canon/planspec.md"]');
    expect(low?.getAttribute("stroke-opacity")).toBe("0.17");
    expect(low?.getAttribute("stroke-width")).toBe("0.8");

    // Zustands-Labels live.
    expect(container.querySelector("svg")?.getAttribute("aria-label")).toBe(
      "Estate-Graph (live, 8 Knoten)",
    );
    expect(container.querySelector(".jv-gtag")?.textContent).toMatch(
      /^GRAPH · LIVE — 8 KNOTEN · Stand \d{2}:\d{2}$/,
    );
    expect(container.querySelector(".jv-mocktag")?.textContent).toBe(" · Graph: live · 8 Knoten");
  });

  it("Teilquellen-Fehler (errors[]) landen dezent als Tooltip am Footer-Tag, kein Panel", async () => {
    getPaGraphMock.mockResolvedValue(
      liveGraph(defaultNodes(), defaultEdges(), {
        errors: [{ source: "kanban", error: "disk i/o error" }],
      }),
    );
    const { container } = renderGraph();

    expect(await screen.findByText("vision")).toBeTruthy();
    const gtag = container.querySelector(".jv-gtag");
    expect(gtag?.getAttribute("title")).toContain("Graph-Teilquellen derzeit fehlerhaft:");
    expect(gtag?.getAttribute("title")).toContain("kanban: disk i/o error");
    expect(container.querySelector('[role="alert"]')).toBeNull();
  });
});

describe("JarvisGraph — Fokus und href-Navigation", () => {
  it("Tap fokussiert (Ring + FOKUS-Tag + Nachbar-Kanten in Clusterfarbe), Canvas-Tap löst", async () => {
    const { container } = renderGraph();
    await screen.findByText("vision");

    const task = container.querySelector('[data-node-id="task:t_abc"]');
    expect(task).toBeTruthy();
    fireEvent.click(task!);

    expect(container.querySelector('[data-focus-ring="task:t_abc"]')).toBeTruthy();
    expect(await screen.findByText("· FOKUS ·")).toBeTruthy();
    const focusEdge = container.querySelector('[data-edge="project:hermes->task:t_abc"]');
    expect(focusEdge?.getAttribute("stroke")).toBe("#3ddc97");
    expect(focusEdge?.getAttribute("stroke-opacity")).toBe("0.6");
    expect(focusEdge?.getAttribute("stroke-width")).toBe("1.6");
    expect(seenPath).toBe("/control/projekte"); // Tap ≠ Navigation

    fireEvent.click(container.querySelector("svg")!);
    expect(container.querySelector('[data-focus-ring="task:t_abc"]')).toBeNull();
  });

  it("erneuter Tap auf fokussierten /control-Knoten navigiert per SPA-Router", async () => {
    const { container } = renderGraph();
    await screen.findByText("S2.7 Graph");

    const task = container.querySelector('[data-node-id="task:t_abc"]')!;
    fireEvent.click(task); // Fokus
    expect(seenPath).toBe("/control/projekte");
    fireEvent.click(task); // Öffnen
    expect(seenPath).toBe("/control/fleet?task=t_abc");
  });

  it("vault://-Knoten sind reine Anzeige: Fokus ja, Navigation nein", async () => {
    const { container } = renderGraph();
    await screen.findByText("vision");

    const vision = container.querySelector('[data-node-id="vault:00-canon/vision.md"]')!;
    fireEvent.click(vision);
    expect(container.querySelector('[data-focus-ring="vault:00-canon/vision.md"]')).toBeTruthy();
    fireEvent.click(vision); // erneuter Tap — keine Navigation
    expect(seenPath).toBe("/control/projekte");
    expect(vision.querySelector("title")?.textContent).toContain("vault://00-Canon/vision.md");
  });

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

describe("JarvisGraph — Label-Schwelle", () => {
  it("max 5 Labels je Cluster bei weight ≥ 0.45; Fokus labelt auch unter der Schwelle", async () => {
    const weights = [0.9, 0.8, 0.7, 0.65, 0.6, 0.55, 0.5];
    const nodes = weights.map((w, i) =>
      node({ id: `n${i}`, label: `L${i + 1}`, cluster: "canon", weight: w, x: 200 + i * 90, y: 300 }),
    );
    nodes.push(node({ id: "weak", label: "ZuSchwach", cluster: "memories", kind: "memory", weight: 0.44, x: 900, y: 500 }));
    nodes.push(node({ id: "long", label: "Ein sehr langer Vault-Titel der gekuerzt wird", cluster: "skills", kind: "skill", weight: 0.95, x: 400, y: 150 }));
    getPaGraphMock.mockResolvedValue(liveGraph(nodes, []));
    const { container } = renderGraph();

    // Top 5 des Clusters gelabelt, Rang 6+7 gekappt, 0.44 unter der Schwelle.
    for (const label of ["L1", "L2", "L3", "L4", "L5"]) {
      expect(await screen.findByText(label), `Label fehlt: ${label}`).toBeTruthy();
    }
    expect(screen.queryByText("L6")).toBeNull();
    expect(screen.queryByText("L7")).toBeNull();
    expect(screen.queryByText("ZuSchwach")).toBeNull();

    // Lange Live-Labels werden dezent auf 30 Zeichen gekürzt (voller Text im Tooltip).
    expect(await screen.findByText("Ein sehr langer Vault-Titel d…")).toBeTruthy();
    expect(screen.queryByText("Ein sehr langer Vault-Titel der gekuerzt wird")).toBeNull();

    // Tap auf ungelabelten Knoten unter der Schwelle → Fokus erzwingt das Label.
    fireEvent.click(container.querySelector('[data-node-id="weak"]')!);
    expect(await screen.findByText("ZuSchwach")).toBeTruthy();
  });
});
