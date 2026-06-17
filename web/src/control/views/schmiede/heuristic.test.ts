import { describe, expect, it } from "vitest";
import { score } from "./heuristic";

const STRONG_AUDIT = `You are a security audit engineer.
Scope: Review files recently modified in src/ — do NOT touch anything outside.
Output: Numbered list. Each item: [file:line] [Critical|High|Medium|Low] [Issue] [Fix].
Do not modify any files.
Done-when: a prioritized report is delivered.
Stop: if exploitability is uncertain, mark [Uncertain].`;

describe("score", () => {
  it("scores a strong audit prompt at full marks over the applicable checks only", () => {
    const r = score(STRONG_AUDIT, "audit");
    // audit has 6 applicable checks (4 universal + read-only + severity-label);
    // plan-first/behavior-preservation/regression-test/clarification-gate are na
    // and must NOT be counted toward score or max.
    expect(r.max).toBe(6);
    expect(r.score).toBe(6);
  });

  it("does not let non-applicable (na) checks inflate the score or max", () => {
    // A bare refactor prompt satisfies none of its 5 applicable checks.
    // Buggy behaviour counted the 5 na checks as 'not fail' → score 5/10.
    const r = score("Refactor the thing.", "refactor");
    expect(r.max).toBe(5);
    expect(r.score).toBe(0);
  });

  it("marks non-applicable checks as na (e.g. plan-first on audit)", () => {
    const r = score(STRONG_AUDIT, "audit");
    expect(r.checks.find((c) => c.id === "plan-first")?.status).toBe("na");
  });

  it("fails the done-when check when the prompt lacks it", () => {
    const r = score("You are a dev. Just do something useful.", "feature");
    expect(r.checks.find((c) => c.id === "done-when")?.status).toBe("fail");
  });

  it("fails read-only for an audit prompt without a read-only pledge", () => {
    const noPledge = `You are an auditor. Output: numbered list with [Critical] severity. Done-when: report done. Stop if unsure.`;
    const r = score(noPledge, "audit");
    expect(r.checks.find((c) => c.id === "read-only")?.status).toBe("fail");
  });

  it("read-only is na for a feature prompt", () => {
    const r = score("Implement X. Done-when: tests pass. Stop if ambiguous.", "feature");
    expect(r.checks.find((c) => c.id === "read-only")?.status).toBe("na");
  });
});
