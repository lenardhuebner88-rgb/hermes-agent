import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const src = readFileSync(fileURLToPath(new URL("./ChainVizView.tsx", import.meta.url)), "utf8");
const controlPage = readFileSync(fileURLToPath(new URL("../ControlPage.tsx", import.meta.url)), "utf8");
const hooks = readFileSync(fileURLToPath(new URL("../hooks/useControlData.ts", import.meta.url)), "utf8");

describe("ChainVizView live wiring", () => {
  it("polls the chain-graph endpoint and delegates SVG DAG rendering to KettenGraph", () => {
    expect(src).toMatch(/useChainGraph/);
    expect(hooks).toMatch(/chain-graph/);
    // Renders a bezier SVG DAG via KettenGraph (not a flat grid with outgoingByNode chips).
    expect(src).toMatch(/KettenGraph/);
    // Passes nodes + edges from the chain-graph hook down to KettenGraph.
    expect(src).toMatch(/graph\.data\.nodes/);
    expect(src).toMatch(/graph\.data\.edges/);
  });

  it("falls back to first active chain when no ?root= param is set", () => {
    expect(src).toMatch(/activeChains\[0\]\.rootId/);
    expect(src).toMatch(/useSearchParams/);
  });

  it("uses richer latest_run fields for runtime and heartbeat (via ChainNodeCard)", () => {
    const cardSrc = readFileSync(
      fileURLToPath(new URL("./ketten/ChainNodeCard.tsx", import.meta.url)),
      "utf8",
    );
    expect(cardSrc).toMatch(/latest_run/);
    expect(cardSrc).toMatch(/runtime_seconds/);
    expect(cardSrc).toMatch(/last_heartbeat_at/);
  });

  it("is routed as the /control/ketten tab", () => {
    expect(controlPage).toMatch(/ChainVizView/);
    expect(controlPage).toMatch(/\/control\/ketten/);
    expect(controlPage).toMatch(/path="ketten"/);
  });
});
