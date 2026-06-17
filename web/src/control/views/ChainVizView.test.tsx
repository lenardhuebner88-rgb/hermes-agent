import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const src = readFileSync(fileURLToPath(new URL("./ChainVizView.tsx", import.meta.url)), "utf8");
const controlPage = readFileSync(fileURLToPath(new URL("../ControlPage.tsx", import.meta.url)), "utf8");
const hooks = readFileSync(fileURLToPath(new URL("../hooks/useControlData.ts", import.meta.url)), "utf8");

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
});
