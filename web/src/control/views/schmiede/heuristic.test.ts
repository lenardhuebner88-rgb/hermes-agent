import { describe, expect, it } from "vitest";
import { score } from "./heuristic";

const STRONG_AUDIT = `You are a security audit engineer.
Scope: Review files recently modified in src/ — do NOT touch anything outside.
Output: Numbered list. Each item: [file:line] [Critical|High|Medium|Low] [Issue] [Fix].
Do not modify any files.
Done-when: a prioritized report is delivered.
Stop: if exploitability is uncertain, mark [Uncertain].`;

describe("score", () => {
  it("scores a strong audit prompt >= 8 with max 10", () => {
    const r = score(STRONG_AUDIT, "audit");
    expect(r.max).toBe(10);
    expect(r.score).toBeGreaterThanOrEqual(8);
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
