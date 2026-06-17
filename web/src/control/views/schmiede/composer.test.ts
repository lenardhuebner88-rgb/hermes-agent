import { describe, expect, it } from "vitest";
import { compose } from "./composer";
import type { ForgeSelection, PromptForgeCatalog } from "./catalog";

const CATALOG: PromptForgeCatalog = {
  version: 1,
  blocks: [
    { id: "role", letter: "A", label: "Role", description: "", body: "You are a [role].", source: "", category: "core" },
    { id: "goal", letter: "B", label: "Goal", description: "", body: "goal-block", source: "", category: "core" },
    { id: "scope-constraints", letter: "G", label: "Scope", description: "", body: "scope-block", source: "", category: "core" },
    { id: "grounding", letter: "C", label: "Grounding", description: "", body: "GROUNDING-BLOCK", source: "", category: "long-run" },
    { id: "persistence", letter: "E", label: "Persistence", description: "", body: "KEEP-GOING-BASE", source: "", category: "core" },
    { id: "verification", letter: "I", label: "Verification", description: "", body: "VERIFY-BLOCK", source: "", category: "core" },
    { id: "output-format", letter: "L", label: "Output", description: "", body: "OUTPUT-FORMAT-BLOCK", source: "", category: "optional" },
  ],
  taskTypes: [
    { id: "audit", label: "Audit", blockIds: ["role", "goal", "scope-constraints", "grounding", "output-format", "verification"], typeBody: "AUDIT-BODY do not modify any files", defaultDoneWhen: "report delivered", checklist: [], rawTemplate: "", source: "" },
    { id: "feature", label: "Feature", blockIds: ["role", "goal", "grounding", "scope-constraints", "persistence", "verification"], typeBody: "FEATURE-BODY", defaultDoneWhen: "tests pass", checklist: [], rawTemplate: "", source: "" },
  ],
  modes: [
    { id: "stop-on-doubt", label: "Stop", description: "", overrides: { reversibilityGate: "REV-GATE wait for explicit confirmation", escalation: "ESC-HALT" }, rawPreset: "", source: "" },
    { id: "fully-autonomous", label: "Auto", description: "", overrides: { persistence: "NO-CONFIRM-LOOPS", reversibilityGate: "DENY force-push mass-delete", escalation: "PRECONDITION egress locked" }, rawPreset: "", source: "" },
  ],
  targets: [
    { id: "generic", label: "Generic", mechanicNote: "", wrapMode: "system-prompt", source: "" },
    { id: "claude-goal", label: "claude /goal", mechanicNote: "", wrapMode: "completion-condition", source: "" },
    { id: "claude-loop", label: "claude /loop", mechanicNote: "", wrapMode: "interval-loop", source: "" },
    { id: "codex-goal", label: "codex /goal", mechanicNote: "", wrapMode: "full-auto", source: "" },
  ],
  heuristic: [],
  evalEvidence: [],
};

function sel(over: Partial<ForgeSelection> = {}): ForgeSelection {
  return {
    targetId: "generic",
    taskTypeId: "audit",
    modeId: "stop-on-doubt",
    modelId: "claude-opus-4-8",
    slots: { task: "Fix the login race in auth.ts", scope: "src/auth", ...(over.slots ?? {}) },
    ...over,
  };
}

describe("compose", () => {
  it("fills role, goal and scope slots", () => {
    const out = compose(sel(), CATALOG);
    expect(out).toContain("You are a [role].");
    expect(out).toContain("Goal: Fix the login race in auth.ts");
    expect(out).toContain("Scope: src/auth");
  });

  it("includes the task-type body, verification block and type done-when", () => {
    const out = compose(sel(), CATALOG);
    expect(out).toContain("AUDIT-BODY");
    expect(out).toContain("VERIFY-BLOCK");
    expect(out).toContain("Done-when: report delivered");
  });

  it("includes grounding and output-format blocks when the task type lists them", () => {
    const out = compose(sel({ taskTypeId: "audit" }), CATALOG);
    expect(out).toContain("GROUNDING-BLOCK");
    expect(out).toContain("OUTPUT-FORMAT-BLOCK");
  });

  it("omits blocks the task type does not list (feature has no output-format)", () => {
    const out = compose(sel({ taskTypeId: "feature", slots: { task: "t", scope: "s" } }), CATALOG);
    expect(out).toContain("GROUNDING-BLOCK");
    expect(out).not.toContain("OUTPUT-FORMAT-BLOCK");
  });

  it("uses base persistence unless the mode overrides it", () => {
    expect(compose(sel({ modeId: "stop-on-doubt" }), CATALOG)).toContain("KEEP-GOING-BASE");
    const auto = compose(sel({ modeId: "fully-autonomous" }), CATALOG);
    expect(auto).toContain("NO-CONFIRM-LOOPS");
    expect(auto).not.toContain("KEEP-GOING-BASE");
  });

  it("injects mode reversibility-gate and escalation overrides", () => {
    const out = compose(sel({ modeId: "stop-on-doubt" }), CATALOG);
    expect(out).toContain("REV-GATE");
    expect(out).toContain("ESC-HALT");
  });

  it("wraps generic target as an XML system prompt", () => {
    const out = compose(sel({ targetId: "generic", modelId: "" }), CATALOG);
    expect(out.startsWith("<system_prompt>")).toBe(true);
    expect(out.trimEnd().endsWith("</system_prompt>")).toBe(true);
  });

  it("wraps claude /goal with a transcript-provable completion condition + turn cap", () => {
    const out = compose(sel({ targetId: "claude-goal", slots: { task: "t", scope: "s", maxTurns: 12 } }), CATALOG);
    expect(out).toContain("/goal");
    expect(out).toContain("stop after 12 turns");
    expect(out.toLowerCase()).toContain("transcript");
  });

  it("wraps claude /loop with interval and round protocol", () => {
    const withIv = compose(sel({ targetId: "claude-loop", slots: { task: "t", scope: "s", intervalMinutes: 5 } }), CATALOG);
    expect(withIv).toContain("/loop 5m");
    expect(withIv).toContain("[DONE]");
    const selfPaced = compose(sel({ targetId: "claude-loop", slots: { task: "t", scope: "s" } }), CATALOG);
    expect(selfPaced).toContain("self-paced");
  });

  it("wraps codex /goal full-auto with a hard deny list", () => {
    const out = compose(sel({ targetId: "codex-goal" }), CATALOG);
    expect(out.toLowerCase()).toContain("full-auto");
    expect(out.toLowerCase()).toContain("force-push");
  });

  it("surfaces the chosen model as a hint line", () => {
    expect(compose(sel({ modelId: "gpt-5.5" }), CATALOG)).toContain("gpt-5.5");
  });

  it("falls back to placeholders for empty slots and returns '' on unknown ids", () => {
    expect(compose(sel({ slots: { task: "", scope: "" } }), CATALOG)).toContain("[describe the task");
    expect(compose(sel({ taskTypeId: "nope" }), CATALOG)).toBe("");
  });
});
