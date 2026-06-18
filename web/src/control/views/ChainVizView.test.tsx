import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { renderToStaticMarkup } from "react-dom/server";

import { ChainSelector } from "./ketten/ChainSelector";
import { KettenGraph } from "./ketten/KettenGraph";
import { buildChains } from "../lib/fleet";
import { de } from "../i18n/de";
import type { BoardTask, ChainGraphEdge, ChainGraphNode } from "../lib/types";

const src = readFileSync(fileURLToPath(new URL("./ChainVizView.tsx", import.meta.url)), "utf8");
const controlPage = readFileSync(fileURLToPath(new URL("../ControlPage.tsx", import.meta.url)), "utf8");
const hooks = readFileSync(fileURLToPath(new URL("../hooks/useControlData.ts", import.meta.url)), "utf8");

function makeTask(id: string, status: BoardTask["status"], root_id: string): BoardTask {
  return {
    id,
    title: id.toUpperCase(),
    status,
    assignee: "coder",
    priority: 0,
    created_at: 0,
    started_at: null,
    completed_at: null,
    branch_name: null,
    latest_summary: null,
    link_counts: { parents: 0, children: 0 },
    comment_count: 0,
    progress: null,
    age: null,
    tenant: null,
    root_id,
    epic_id: null,
  };
}

function makeNode(
  id: string,
  status: ChainGraphNode["status"] = "todo",
  extra: Partial<ChainGraphNode> = {},
): ChainGraphNode {
  return {
    id,
    title: id.toUpperCase(),
    status,
    assignee: "coder",
    level: 0,
    parents: [],
    children: [],
    created_at: 0,
    started_at: null,
    completed_at: null,
    last_heartbeat_at: null,
    runtime_seconds: null,
    progress: null,
    latest_run: null,
    ...extra,
  };
}

describe("ChainVizView live wiring", () => {
  it("polls the chain-graph endpoint and delegates pipeline DAG rendering to KettenGraph", () => {
    expect(src).toMatch(/useChainGraph/);
    expect(hooks).toMatch(/chain-graph/);
    // Renders a vertical pipeline DAG via KettenGraph (not a flat grid with outgoingByNode chips).
    expect(src).toMatch(/KettenGraph/);
    // Passes nodes + edges from the chain-graph hook down to KettenGraph.
    expect(src).toMatch(/graph\.data\.nodes/);
    expect(src).toMatch(/graph\.data\.edges/);
  });

  it("falls back to first active chain when no ?root= param is set", () => {
    expect(src).toMatch(/activeChains\[0\]\.rootId/);
    expect(src).toMatch(/useSearchParams/);
  });

  it("prefers richer latest_run runtime over task-level runtime (via ChainNodeCard)", () => {
    const cardSrc = readFileSync(
      fileURLToPath(new URL("./ketten/ChainNodeCard.tsx", import.meta.url)),
      "utf8",
    );
    expect(cardSrc).toMatch(/latest_run/);
    expect(cardSrc).toMatch(/runtime_seconds/);
    // Heartbeat age was removed from the card — liveness now shows as the
    // pulsing node dot on the pipeline line (KettenGraph), and the card
    // renders a progress bar instead.
    expect(cardSrc).toMatch(/progress/);
  });

  it("is routed as the /control/ketten tab", () => {
    expect(controlPage).toMatch(/ChainVizView/);
    expect(controlPage).toMatch(/\/control\/ketten/);
    expect(controlPage).toMatch(/path="ketten"/);
  });

  it("carries the Variante-A pipeline subtitle (not the old DAG wording)", () => {
    // DAG-S2: the tab now frames the chain as a vertical *pipeline*.
    expect(de.ketten.subtitle).toBe(
      "Eine laufende Kette als Pipeline — Fortschritt, Status und live Heartbeat.",
    );
    expect(de.ketten.subtitle).not.toMatch(/DAG/);
    // The view renders the i18n subtitle (no hard-coded string drift).
    expect(src).toMatch(/de\.ketten\.subtitle/);
  });
});

// ── E2E-ish integration: the two leaf widgets the view composes ─────────────
// ChainVizView itself is router/hook-bound (useSearchParams + useBoard +
// useChainGraph), so it is covered by the source-contract specs above. Here we
// render the pure widgets it wires together — ChainSelector (board → buildChains)
// and KettenGraph (chain-graph endpoint) — with one consistent chain, exactly
// as the view feeds them, and assert they integrate.
describe("ChainSelector + KettenGraph integration", () => {
  // A real active chain straight out of buildChains, as the view derives it.
  const tasks: BoardTask[] = [
    makeTask("root", "running", "root"),
    makeTask("child", "todo", "root"),
  ];
  const chains = buildChains(tasks).active;
  const rootId = chains[0].rootId;

  const nodes: ChainGraphNode[] = [
    makeNode("root", "running", { runtime_seconds: 12 }),
    makeNode("child", "todo", { parents: ["root"] }),
  ];
  const edges: ChainGraphEdge[] = [{ from: "root", to: "child" }];

  function renderComposed() {
    return renderToStaticMarkup(
      <div>
        <ChainSelector chains={chains} selectedRootId={rootId} onSelect={() => {}} />
        <KettenGraph nodes={nodes} edges={edges} rootId={rootId} />
      </div>,
    );
  }

  it("buildChains surfaces the running chain to the selector", () => {
    expect(chains).toHaveLength(1);
    expect(rootId).toBe("root");
    expect(chains[0].total).toBe(2);
    expect(chains[0].runningCount).toBe(1);
  });

  it("selector and graph render the same chain together (shared root focus)", () => {
    const html = renderComposed();
    // Selector: the chosen chain is the select's value + the running badge shows.
    expect(html).toMatch(/<select[^>]*\bid="chain-select"/);
    expect(html).toContain("läuft");
    expect(html).toContain("2 Tasks");
    // Graph: both pipeline stations of that same chain are rendered…
    expect(html).toContain("ROOT");
    expect(html).toContain("CHILD");
    // …as a vertical pipeline with the running root marked live, not a flat grid.
    expect(html).toContain("data-pipeline-line");
    expect(html).toContain('data-node-dot="running"');
    expect(html).not.toMatch(/grid-template-columns|gridTemplateColumns/);
  });

  it("renders nothing extra when there is no active chain", () => {
    // Empty board → no chain → selector shows the empty option, graph collapses.
    const emptyChains = buildChains([makeTask("solo", "running", "solo")]).active;
    expect(emptyChains).toHaveLength(0);
    const html = renderToStaticMarkup(
      <div>
        <ChainSelector chains={emptyChains} selectedRootId={null} onSelect={() => {}} />
        <KettenGraph nodes={[]} edges={[]} rootId="" />
      </div>,
    );
    expect(html).toContain(de.ketten.noChains);
  });

  // ── Responsiv-Check: Mobile 375px ────────────────────────────────────────
  // No jsdom/layout in this runner, so we assert the overflow-safe CSS contract
  // that keeps the composed widgets inside a 375px column instead of forcing a
  // horizontal scrollbar. The pixel-accurate viewport pass is the visual check.
  describe("mobile (375px) layout contract", () => {
    it("the select fills its column rather than overflowing it", () => {
      const html = renderToStaticMarkup(
        <ChainSelector chains={chains} selectedRootId={rootId} onSelect={() => {}} />,
      );
      expect(html).toMatch(/class="[^"]*\bw-full\b/);
    });

    it("the pipeline graph is width-capped and shrink-safe (min-w-0 / max-w-full)", () => {
      const html = renderToStaticMarkup(
        <KettenGraph nodes={nodes} edges={edges} rootId={rootId} />,
      );
      // These two guards are what stop the long pipeline cards from blowing out
      // a 375px-wide flex column (min-w-0 lets it shrink, max-w-full caps it).
      expect(html).toMatch(/\bmin-w-0\b/);
      expect(html).toMatch(/\bmax-w-full\b/);
    });

    it("the view header stacks vertically on mobile, side-by-side only ≥lg", () => {
      // Source contract: mobile-first flex-col, horizontal split gated behind lg:.
      expect(src).toMatch(/flex-col[^"]*\blg:flex-row\b/);
    });
  });
});
