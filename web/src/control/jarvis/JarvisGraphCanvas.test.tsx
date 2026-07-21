// @vitest-environment jsdom
/**
 * JarvisGraphCanvas — sync tests only (staticLayout + getContext stub).
 * No rAF / waitFor. Covers mount paint, Tap→Fokus→Tap²→open, empty tap,
 * keyboard cycle + Enter, aria-live, unmount cleanup.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import type { PaGraphCluster, PaGraphEdge, PaGraphNode, PaGraphResponse } from "@/lib/api";

import { fitTransform, worldToScreen } from "./graphEngine";
import { JarvisGraphCanvas } from "./JarvisGraphCanvas";

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

function fixtureGraph(): PaGraphResponse {
  const nodes: PaGraphNode[] = [
    node({
      id: "vault:vision",
      label: "vision",
      cluster: "canon",
      weight: 0.9,
      x: 640,
      y: 400,
      href: "vault://00-Canon/vision.md",
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
    node({ id: "proj:low", label: "Schwach", cluster: "projekte", kind: "project", weight: 0.4, x: 600, y: 655 }),
  ];
  const edges: PaGraphEdge[] = [
    { from: "vault:vision", to: "project:hermes", kind: "wikilink" },
    { from: "project:hermes", to: "task:t_abc", kind: "project-task" },
  ];
  return {
    schema: "pa-graph/v1",
    source: "live",
    layout: "precomputed-viewbox-1280x820",
    generated_at: "2026-07-19T19:00:00+00:00",
    refresh: { interval_s: 30 },
    clusters: CLUSTERS,
    nodes,
    edges,
  };
}

type CtxStub = CanvasRenderingContext2D & { __calls: string[] };

function installGetContextStub(): { calls: string[]; restore: () => void } {
  const calls: string[] = [];
  const original = HTMLCanvasElement.prototype.getContext;

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  (HTMLCanvasElement.prototype as any).getContext = function getContextStub(
    this: HTMLCanvasElement,
    type: string,
  ): CanvasRenderingContext2D | null {
    if (type !== "2d") return null;
    const gradient = {
      addColorStop: vi.fn(),
    };
    const ctx = {
      __calls: calls,
      canvas: this,
      setTransform: (..._a: number[]) => {
        calls.push("setTransform");
      },
      clearRect: (..._a: number[]) => {
        calls.push("clearRect");
      },
      save: () => {
        calls.push("save");
      },
      restore: () => {
        calls.push("restore");
      },
      translate: () => {
        calls.push("translate");
      },
      scale: () => {
        calls.push("scale");
      },
      beginPath: () => {
        calls.push("beginPath");
      },
      arc: () => {
        calls.push("arc");
      },
      moveTo: () => {
        calls.push("moveTo");
      },
      quadraticCurveTo: () => {
        calls.push("quadraticCurveTo");
      },
      fill: () => {
        calls.push("fill");
      },
      stroke: () => {
        calls.push("stroke");
      },
      fillRect: () => {
        calls.push("fillRect");
      },
      drawImage: () => {
        calls.push("drawImage");
      },
      fillText: () => {
        calls.push("fillText");
      },
      strokeText: () => {
        calls.push("strokeText");
      },
      measureText: () => ({ width: 40 }),
      createRadialGradient: () => gradient,
      createLinearGradient: () => gradient,
      set lineWidth(_v: number) {},
      set strokeStyle(_v: string | CanvasGradient) {},
      set fillStyle(_v: string | CanvasGradient) {},
      set globalAlpha(_v: number) {},
      set textAlign(_v: CanvasTextAlign) {},
      set textBaseline(_v: CanvasTextBaseline) {},
      set font(_v: string) {},
    } as unknown as CtxStub;
    return ctx;
  };

  return {
    calls,
    restore: () => {
      HTMLCanvasElement.prototype.getContext = original;
    },
  };
}

/** Map world (1280×820) → client coords via the same fitTransform the Canvas uses. */
function worldToClient(canvas: HTMLCanvasElement, wx: number, wy: number) {
  const rect = canvas.getBoundingClientRect();
  const cssW = rect.width || WORLD_FALLBACK_W;
  const cssH = rect.height || WORLD_FALLBACK_H;
  const t = fitTransform(cssW, cssH);
  const s = worldToScreen(wx, wy, t);
  return {
    clientX: rect.left + s.x,
    clientY: rect.top + s.y,
  };
}

const WORLD_FALLBACK_W = 1280;
const WORLD_FALLBACK_H = 820;

function stubLayout(canvas: HTMLCanvasElement) {
  Object.defineProperty(canvas, "clientWidth", { configurable: true, value: WORLD_FALLBACK_W });
  Object.defineProperty(canvas, "clientHeight", { configurable: true, value: WORLD_FALLBACK_H });
  canvas.getBoundingClientRect = () =>
    ({
      x: 0,
      y: 0,
      top: 0,
      left: 0,
      bottom: WORLD_FALLBACK_H,
      right: WORLD_FALLBACK_W,
      width: WORLD_FALLBACK_W,
      height: WORLD_FALLBACK_H,
      toJSON: () => ({}),
    }) as DOMRect;
  if (canvas.parentElement) {
    Object.defineProperty(canvas.parentElement, "clientWidth", {
      configurable: true,
      value: WORLD_FALLBACK_W,
    });
    Object.defineProperty(canvas.parentElement, "clientHeight", {
      configurable: true,
      value: WORLD_FALLBACK_H,
    });
  }
}

describe("JarvisGraphCanvas — staticLayout + getContext stub", () => {
  let stub: ReturnType<typeof installGetContextStub>;

  beforeEach(() => {
    stub = installGetContextStub();
  });

  afterEach(() => {
    cleanup();
    stub.restore();
    vi.clearAllMocks();
  });

  it("mount with staticLayout paints via getContext (no rAF required)", () => {
    const graph = fixtureGraph();
    const { container } = render(
      <MemoryRouter>
        <div style={{ width: 1280, height: 820 }}>
          <JarvisGraphCanvas graph={graph} staticLayout ariaLabel="Estate-Graph (live, 4 Knoten)" />
        </div>
      </MemoryRouter>,
    );
    const canvas = container.querySelector("canvas.jv-brain") as HTMLCanvasElement;
    expect(canvas).toBeTruthy();
    stubLayout(canvas);
    // Force a layout-measure path re-entry by dispatching resize if needed —
    // paint already ran in useLayoutEffect; assert stub saw 2d drawing.
    expect(stub.calls.length).toBeGreaterThan(0);
    expect(stub.calls).toContain("clearRect");
    expect(stub.calls).toContain("drawImage");
    expect(canvas.getAttribute("role")).toBe("application");
    expect(canvas.getAttribute("tabindex")).toBe("0");
    expect(canvas.getAttribute("aria-label")).toBe("Estate-Graph (live, 4 Knoten)");
    expect(canvas.getAttribute("data-static-layout")).toBe("1");
  });

  it("Tap focuses, second Tap opens via onNodeOpen, empty Tap clears focus", () => {
    const graph = fixtureGraph();
    const onNodeOpen = vi.fn();
    const onNodeFocus = vi.fn();
    const { container } = render(
      <MemoryRouter>
        <div style={{ width: 1280, height: 820 }}>
          <JarvisGraphCanvas
            graph={graph}
            staticLayout
            ariaLabel="test"
            onNodeOpen={onNodeOpen}
            onNodeFocus={onNodeFocus}
          />
        </div>
      </MemoryRouter>,
    );
    const canvas = container.querySelector("canvas") as HTMLCanvasElement;
    stubLayout(canvas);

    // Tap Hermes-Infra at (520, 520)
    const p1 = worldToClient(canvas, 520, 520);
    fireEvent.pointerDown(canvas, { ...p1, button: 0, pointerId: 1 });
    fireEvent.pointerUp(canvas, { ...p1, button: 0, pointerId: 1 });

    expect(canvas.getAttribute("data-focus-id")).toBe("project:hermes");
    expect(onNodeFocus).toHaveBeenCalled();
    expect(onNodeOpen).not.toHaveBeenCalled();

    // Second tap on same node → open
    fireEvent.pointerDown(canvas, { ...p1, button: 0, pointerId: 2 });
    fireEvent.pointerUp(canvas, { ...p1, button: 0, pointerId: 2 });
    expect(onNodeOpen).toHaveBeenCalledTimes(1);
    expect(onNodeOpen.mock.calls[0]![0].id).toBe("project:hermes");

    // Empty tap clears focus
    const empty = worldToClient(canvas, 50, 50);
    fireEvent.pointerDown(canvas, { ...empty, button: 0, pointerId: 3 });
    fireEvent.pointerUp(canvas, { ...empty, button: 0, pointerId: 3 });
    expect(canvas.getAttribute("data-focus-id")).toBeNull();
  });

  it("keyboard cycle + Enter focuses then opens; aria-live announces", () => {
    const graph = fixtureGraph();
    const onNodeOpen = vi.fn();
    const { container } = render(
      <MemoryRouter>
        <div style={{ width: 1280, height: 820 }}>
          <JarvisGraphCanvas
            graph={graph}
            staticLayout
            ariaLabel="Estate-Graph (live, 4 Knoten)"
            statusLive="Estate-Graph (live, 4 Knoten)"
            onNodeOpen={onNodeOpen}
          />
        </div>
      </MemoryRouter>,
    );
    const canvas = container.querySelector("canvas") as HTMLCanvasElement;
    stubLayout(canvas);

    // statusLive surfaces in aria-live
    const live = screen.getByTestId("jv-graph-live");
    expect(live.getAttribute("aria-live")).toBe("polite");
    expect(live.textContent).toContain("Estate-Graph (live, 4 Knoten)");

    canvas.focus();
    fireEvent.keyDown(canvas, { key: "ArrowRight" });
    // First labeled node in model.labels order (Map insertion = per-cluster top weights)
    const focusAfter = canvas.getAttribute("data-focus-id");
    expect(focusAfter).toBeTruthy();
    expect(live.textContent?.length ?? 0).toBeGreaterThan(0);

    // Enter on focused = open (second tap semantics)
    fireEvent.keyDown(canvas, { key: "Enter" });
    expect(onNodeOpen).toHaveBeenCalledTimes(1);

    fireEvent.keyDown(canvas, { key: "Escape" });
    expect(canvas.getAttribute("data-focus-id")).toBeNull();
  });

  it("unmount cleans up without throwing", () => {
    const graph = fixtureGraph();
    const { unmount, container } = render(
      <MemoryRouter>
        <JarvisGraphCanvas graph={graph} staticLayout ariaLabel="x" />
      </MemoryRouter>,
    );
    expect(container.querySelector("canvas")).toBeTruthy();
    expect(() => unmount()).not.toThrow();
  });
});
