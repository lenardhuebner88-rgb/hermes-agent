import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { renderToStaticMarkup } from "react-dom/server";

import { ChainListPanel } from "./ketten/ChainListPanel";
import { KettenGraph } from "./ketten/KettenGraph";
import { ChainNodeCard } from "./ketten/ChainNodeCard";
import { buildChains } from "../lib/fleet";
import { fmtAge } from "../lib/derive";
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
    cost_usd: 0,
    input_tokens: 0,
    output_tokens: 0,
    cost_usd_equivalent: 0,
    cost_effective_usd: 0,
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
    // Optional-chained + done-chain fallback (B7): activeChains[0]?.rootId ?? doneChains[0]?.rootId ?? null
    expect(src).toMatch(/activeChains\[0\]\?\.rootId/);
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
// render the pure widgets it wires together — ChainListPanel (board →
// buildChains, S1 grouped list replacing the old 236-entry dropdown) and
// KettenGraph (chain-graph endpoint) — with one consistent chain, exactly as
// the view feeds them, and assert they integrate.
describe("ChainListPanel + KettenGraph integration", () => {
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
        <ChainListPanel chains={chains} doneChains={[]} selectedRootId={rootId} onSelect={() => {}} />
        <KettenGraph nodes={nodes} edges={edges} rootId={rootId} />
      </div>,
    );
  }

  it("buildChains surfaces the running chain to the panel", () => {
    expect(chains).toHaveLength(1);
    expect(rootId).toBe("root");
    expect(chains[0].total).toBe(2);
    expect(chains[0].runningCount).toBe(1);
  });

  it("panel and graph render the same chain together (shared root focus)", () => {
    const html = renderComposed();
    // Panel: the running chain shows in the "Läuft jetzt" group with its
    // live indicator + task count — no more scrolling a flat select.
    expect(html).toContain(de.ketten.listGroupRunning);
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
    // Empty board → no chain → panel shows the empty state, graph collapses.
    const emptyChains = buildChains([makeTask("solo", "running", "solo")]).active;
    expect(emptyChains).toHaveLength(0);
    const html = renderToStaticMarkup(
      <div>
        <ChainListPanel chains={emptyChains} doneChains={[]} selectedRootId={null} onSelect={() => {}} />
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
    it("the list panel fills its column rather than overflowing it", () => {
      const html = renderToStaticMarkup(
        <ChainListPanel chains={chains} doneChains={[]} selectedRootId={rootId} onSelect={() => {}} />,
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

// ── Cost badge on ChainNodeCard ─────────────────────────────────────────────
describe("ChainNodeCard cost badge", () => {
  it("renders '—' when cost_usd===0 and tokens===0 (no data)", () => {
    const node = makeNode("n1", "done");
    const html = renderToStaticMarkup(<ChainNodeCard node={node} isRoot={false} />);
    // The badge shows "—" when there are no cost data.
    expect(html).toContain("—");
  });

  it("renders dollar amount when cost_usd > 0 (real API lane)", () => {
    // Real API lane: both cost_usd and cost_effective_usd are set.
    const node = makeNode("n2", "done", { cost_usd: 0.42, cost_effective_usd: 0.42, input_tokens: 1000, output_tokens: 500 });
    const html = renderToStaticMarkup(<ChainNodeCard node={node} isRoot={false} />);
    expect(html).toContain("$0.42");
    // total 1500 tokens → "2 k tok"
    expect(html).toMatch(/\bk\b/);
  });

  it("renders token count even when cost_usd===0 (subscription lane)", () => {
    const node = makeNode("n3", "running", { cost_usd: 0, input_tokens: 50000, output_tokens: 10000 });
    const html = renderToStaticMarkup(<ChainNodeCard node={node} isRoot={false} />);
    // 60000 tokens → "60 k tok"
    expect(html).toContain("60 k tok");
  });

  it("ChainGraphNode type has cost_usd, input_tokens, output_tokens fields", () => {
    const n = makeNode("nx", "todo", { cost_usd: 1.23, input_tokens: 999, output_tokens: 1 });
    expect(n.cost_usd).toBe(1.23);
    expect(n.input_tokens).toBe(999);
    expect(n.output_tokens).toBe(1);
  });
});

// ── Statistik-Tab: MotherLedger source-contract ─────────────────────────────
describe("MotherLedger source contract", () => {
  const statsSrc = readFileSync(
    fileURLToPath(new URL("./StatistikView.tsx", import.meta.url)),
    "utf8",
  );
  const hooksSrc = readFileSync(
    fileURLToPath(new URL("../hooks/useControlData.ts", import.meta.url)),
    "utf8",
  );

  it("StatistikView uses the windowed rollup hook", () => {
    expect(statsSrc).toMatch(/useHermesWindowedRollup/);
  });

  it("StatistikView renders MotherLedger instead of the old costs section", () => {
    expect(statsSrc).toMatch(/MotherLedgerSection/);
    expect(statsSrc).not.toMatch(/<ChainCostsSection/);
  });

  it("hook fetches the windowed-rollup endpoint", () => {
    expect(hooksSrc).toMatch(/runs\/windowed-rollup/);
  });

  it("renders required controls and the redesigned Abo/Echt labels", () => {
    expect(statsSrc).toMatch(/7T/);
    expect(statsSrc).toMatch(/24Std/);
    expect(statsSrc).toMatch(/motherLedgerHeroAbo/);
    expect(statsSrc).toMatch(/motherLedgerHeroReal/);
    expect(statsSrc).toMatch(/motherLedgerColAbo/);
    expect(statsSrc).toMatch(/motherLedgerColReal/);
    expect(statsSrc).toMatch(/sortKey === "tokens"/);
    expect(statsSrc).toMatch(/sortKey === "runs"/);
  });

  it("renders S3 detail labels for separate Abo/real cost, billing mode, runtime and estimates", () => {
    expect(statsSrc).toMatch(/cost_usd_equivalent/);
    expect(statsSrc).toMatch(/cost_usd/);
    expect(statsSrc).toMatch(/motherLedgerAboMarker/);
    expect(statsSrc).toMatch(/motherLedgerRealShort/);
    expect(statsSrc).toMatch(/billing_mode/);
    expect(statsSrc).toMatch(/Laufzeit/);
    expect(statsSrc).toMatch(/gesch\./);
    // A11: hard-coded "Neuralwatt —" extracted to the i18n key de.stats.motherLedgerNeuralwatt
    expect(statsSrc).toMatch(/motherLedgerNeuralwatt/);
  });
});

// ── K5: checked_at <time> semantic wrapper ──────────────────────────────────
describe("checked_at <time> semantic element", () => {
  const EPOCH_SEC = 1_718_700_000; // fixed Unix timestamp (seconds)
  const EXPECTED_ISO = new Date(EPOCH_SEC * 1000).toISOString();

  it("wraps checked_at in <time dateTime=ISO> when truthy", () => {
    const now = EPOCH_SEC + 120; // 2 minutes later → fmtAge = "2m"
    const label = fmtAge(EPOCH_SEC, now);
    const html = renderToStaticMarkup(
      <time dateTime={EXPECTED_ISO}>{de.ketten.checkedAt(label)}</time>,
    );
    expect(html).toContain("<time");
    // renderToStaticMarkup preserves React's camelCase prop name in the attribute.
    expect(html).toContain(`dateTime="${EXPECTED_ISO}"`);
    expect(html).toContain("aktualisiert vor 2m");
  });

  it("source contains the <time dateTime> wrapper expression", () => {
    expect(src).toMatch(/\btime\b.*\bdateTime\b/);
    expect(src).toMatch(/checked_at \* 1000/);
  });

  it("source guards the falsy case (no crash when checked_at is 0)", () => {
    // The view uses `graph.data.checked_at ? <time ...> : fallback`.
    expect(src).toMatch(/graph\.data\.checked_at\s*\?/);
  });
});

// ── Reine Live-Sicht (Teil 3, 2026-07-02) ───────────────────────────────────
// Die Planung-Strip-Sektion ist in den Flow-Tab (Stufe 1) gezogen; der
// Ketten-Tab koppelt stattdessen per Absprung-Link zurück in den Flow.

describe("ChainVizView als reine Live-Sicht", () => {
  it("rendert keinen PlanungStrip mehr", () => {
    expect(src).not.toContain("PlanungStrip");
    expect(src).not.toContain("usePlanSpecs");
    expect(src).not.toContain("useStrategistCount");
  });

  it("verlinkt die fokussierte Kette zurück in den Flow-Tab", () => {
    expect(src).toContain("/control/flow?task=");
    expect(src).toContain("de.ketten.openInFlow");
    expect(de.ketten.openInFlow.length).toBeGreaterThan(0);
  });
});
