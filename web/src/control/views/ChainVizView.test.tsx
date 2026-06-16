import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const src = readFileSync(fileURLToPath(new URL("./ChainVizView.tsx", import.meta.url)), "utf8");
const controlPage = readFileSync(fileURLToPath(new URL("../ControlPage.tsx", import.meta.url)), "utf8");
const hooks = readFileSync(fileURLToPath(new URL("../hooks/useControlData.ts", import.meta.url)), "utf8");

describe("ChainVizView live wiring", () => {
  it("polls the chain-graph endpoint and renders a horizontal DAG", () => {
    expect(src).toMatch(/useChainGraph/);
    expect(hooks).toMatch(/chain-graph/);
    expect(src).toMatch(/gridTemplateColumns/);
    expect(src).toMatch(/outgoingByNode/);
    expect(src).toMatch(/heartbeat_age_seconds/);
    expect(src).toMatch(/runtime_seconds/);
  });

  it("is routed as the /control/ketten tab", () => {
    expect(controlPage).toMatch(/ChainVizView/);
    expect(controlPage).toMatch(/\/control\/ketten/);
    expect(controlPage).toMatch(/path="ketten"/);
  });
});
