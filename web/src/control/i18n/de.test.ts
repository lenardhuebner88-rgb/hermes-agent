import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { de } from "./de";

const flowViewSrc = readFileSync(fileURLToPath(new URL("../views/FlowView.tsx", import.meta.url)), "utf8");

describe("German gated Flow copy", () => {
  it("explains that the gated release frees held subtasks without a force-run promise", () => {
    expect(de.flow.capture.gateGateHint).toBe("Subtasks werden gehalten, bis du „Kette starten“ klickst.");
    expect(de.flow.plan.release).toBe("Kette starten");
    expect(de.flow.plan.heldGate).toBe("Gibt gehaltene Subtasks frei; Start bleibt von Dependencies, Queue/Assignee und Worker-Kapazität abhängig.");
  });

  it("uses the chain-start CTA for the release confirmation button", () => {
    expect(de.flow.plan.releaseConfirmButton).toBe("Kette starten");
    expect(flowViewSrc).toContain("de.flow.plan.releaseConfirmButton");
  });

  it("offers chain-first choices when a held subtask is dispatched directly", () => {
    expect(de.flow.singleDispatch.startChain).toBe("Ganze Kette starten");
    expect(de.flow.singleDispatch.startSingle).toBe("Nur diesen Task starten");
    expect(de.flow.singleDispatch.cancel).toBe("Abbrechen");
    expect(flowViewSrc).toContain("de.flow.singleDispatch.startChain");
  });
});
