import { describe, expect, it } from "vitest";
import { getFlowSubtaskStatusExplanation } from "./flowStatus";

describe("getFlowSubtaskStatusExplanation", () => {
  it("explains normal Flow subtask wait states as expected operator states", () => {
    expect(getFlowSubtaskStatusExplanation("scheduled")).toBe("wartet auf Kette starten");
    const readyExplanation = getFlowSubtaskStatusExplanation("ready");
    expect(readyExplanation).toBe("startklar im Snapshot; Start hängt von Queue/Assignee und Worker-Kapazität ab");
    expect(readyExplanation).not.toMatch(/nächsten Dispatcher-Tick|startet/i);
  });

  it("keeps todo neutral because the snapshot alone does not prove parent waiting", () => {
    expect(getFlowSubtaskStatusExplanation("todo")).toBe("wartet; Ursache im Snapshot nicht eindeutig");
  });

  it("explains running and done subtasks without making them sound stuck", () => {
    expect(getFlowSubtaskStatusExplanation("running")).toBe("Worker läuft");
    expect(getFlowSubtaskStatusExplanation("done")).toBe("abgeschlossen");
  });

  it("surfaces the blocked reason when the board snapshot already carries one", () => {
    expect(getFlowSubtaskStatusExplanation("blocked", "fehlendes API-Token")).toBe("Blockiert: fehlendes API-Token");
  });
});
