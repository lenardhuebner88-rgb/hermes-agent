import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { runLaneLabel, runLaneTone } from "../lib/autoresearch";
import { DeepAuditFindings } from "./AutoresearchView";
import type { DeepAuditFinding } from "../hooks/useControlData";

describe("AutoresearchView Deep-Audit", () => {
  it("keeps the deep-audit run label and tone", () => {
    expect(runLaneLabel("deep-audit")).toBe("Deep-Audit");
    expect(runLaneTone("deep-audit")).toBe("amber");
  });

  it("renders structured findings with fileline, evidence, and proposal count", () => {
    const finding: DeepAuditFinding = {
      fileline: "hermes_cli/autoresearch_runs.py:23",
      severity: "high",
      category: "bug_risk",
      title: "Run lane omitted",
      problem: "The run lane allowlist can drop audit runs.",
      evidence: "_VALID_LANES",
      fix_hint: "Keep the deep-audit lane in the run history allowlist.",
    };
    const html = renderToStaticMarkup(<DeepAuditFindings findings={[finding]} proposals={["deep-audit-x"]} />);
    expect(html).toContain("hermes_cli/autoresearch_runs.py:23");
    expect(html).toContain("_VALID_LANES");
    expect(html).toContain("1 in Queue");
    expect(html).toContain("Run lane omitted");
  });
});
