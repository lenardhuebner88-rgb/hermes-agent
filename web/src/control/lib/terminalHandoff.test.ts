import { describe, expect, it } from "vitest";
import {
  buildPlanSpecDraft,
  buildStructuredHandoffRequest,
  canHandoffFromInventory,
  defaultSlug,
  findingsFromError,
  handoffRequestKey,
  HANDOFF_SCHEMA_VERSION,
  stripAnsi,
} from "./terminalHandoff";

describe("stripAnsi", () => {
  it("removes CSI / SGR escape sequences", () => {
    expect(stripAnsi("\x1b[31mred\x1b[0m text")).toBe("red text");
    expect(stripAnsi("plain")).toBe("plain");
  });
});

describe("buildPlanSpecDraft (AC-3 defaults)", () => {
  it("always lands freigabe: operator and an explicit live_test_depth", () => {
    const draft = buildPlanSpecDraft("captured output", { title: "My Task", liveTestDepth: "contract" });
    expect(draft).toContain("freigabe: operator");
    expect(draft).toContain("live_test_depth: contract");
    expect(draft).toContain('title: "My Task"');
    expect(draft).toContain("captured output");
  });

  it("emits a NON-binding taskgraph_hints by default (no auto-promotion)", () => {
    const draft = buildPlanSpecDraft("x");
    // The active taskgraph_hints block is non-binding; binding:true only appears
    // inside the commented activation template the operator must fill in.
    expect(draft).toMatch(/taskgraph_hints:\n {2}binding: false/);
    expect(draft).toContain("live_test_depth: smoke"); // default depth, explicit
  });

  it("strips ANSI from the captured body", () => {
    const draft = buildPlanSpecDraft("\x1b[32m$ ls\x1b[0m");
    expect(draft).toContain("$ ls");
    expect(draft).not.toContain("\x1b");
  });
});

describe("defaultSlug", () => {
  it("sanitises and never empties", () => {
    expect(defaultSlug("Fix the Login Bug!")).toBe("fix-the-login-bug");
    expect(defaultSlug("")).toBe("terminal-handoff");
    expect(defaultSlug("***")).toBe("terminal-handoff");
  });
});

describe("findingsFromError", () => {
  it("extracts detail.findings from a fetchJSON error string", () => {
    const err = new Error('400: {"detail":{"findings":["freigabe is required","bad lane"]}}');
    expect(findingsFromError(err)).toEqual(["freigabe is required", "bad lane"]);
  });
  it("returns null when there is no JSON detail", () => {
    expect(findingsFromError(new Error("500: Internal Server Error"))).toBeNull();
  });
});

describe("W3-S2 structured handoff helpers", () => {
  it("disables handoff without terminal_run_id/manifest", () => {
    expect(canHandoffFromInventory({})).toBe(false);
    expect(canHandoffFromInventory({ terminal_run_id: "tr1", has_manifest: false })).toBe(false);
    expect(canHandoffFromInventory({ terminal_run_id: "tr1", has_manifest: true })).toBe(true);
    expect(canHandoffFromInventory({ upgrade_required: true, terminal_run_id: "tr1" })).toBe(false);
  });

  it("builds structured request without content field", () => {
    const req = buildStructuredHandoffRequest({
      session: "work",
      window: "hermes",
      title: "T",
      rawText: "capture",
      terminalRunId: "tr1",
    });
    expect(req.schema_version).toBe(HANDOFF_SCHEMA_VERSION);
    expect((req as { content?: string }).content).toBeUndefined();
    expect(req.raw.text).toBe("capture");
    expect(req.terminal_run_id).toBe("tr1");
  });

  it("freezes request keys for race isolation", () => {
    expect(
      handoffRequestKey({ session: "a", window: "b", terminal_run_id: "tr", requestId: "1" }),
    ).toBe("a::b::tr::1");
    expect(
      handoffRequestKey({ session: "a", window: "b", terminal_run_id: "tr", requestId: "2" }),
    ).not.toBe("a::b::tr::1");
  });
});

