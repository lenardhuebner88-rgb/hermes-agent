import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { HermesResultCard } from "./HermesResultCard";
import type { KanbanResult } from "../lib/types";

const baseResult: KanbanResult = {
  run_id: "408",
  task_id: "t_408",
  task_title: "Codex receipt sichtbar machen",
  task_status: "done",
  task_assignee: "coder",
  profile: "coder",
  run_role: "implementation",
  run_role_label: "Implementation / coder run",
  run_role_source: "claimed_event",
  status: "done",
  outcome: "completed",
  started_at: 100,
  ended_at: 160,
  duration_seconds: 60,
  summary: "Summary line one\nSummary line two",
  summary_preview: "Summary line one",
  followups: ["Receipt pruefen"],
  artifacts: ["/home/piet/receipt.md"],
  verification: ["pytest tests/plugins/test_kanban_dashboard_plugin.py"],
  verification_state: "approved",
  verifier_verdict: "APPROVED",
  verifier_evidence: ["python3 check.py -> stdout: CHECK OK"],
  result_quality: {
    state: "verifier_approved",
    label: "Verifier-approved",
    tone: "emerald",
    description: "Independent verifier gate passed.",
  },
  deliverables: [
    {
      filename: "RESULT.md",
      relative_path: "RESULT.md",
      size: 42,
      mtime: 1780000000,
      content_type: "text/markdown",
      url: "/api/plugins/kanban/tasks/t_408/deliverables/RESULT.md",
    },
    {
      filename: "chart.png",
      relative_path: "artifacts/chart.png",
      size: 1024,
      mtime: 1780000010,
      content_type: "image/png",
      url: "/api/plugins/kanban/tasks/t_408/deliverables/artifacts/chart.png",
    },
  ],
  residual_risk: "Noch nicht live verifiziert",
};

describe("HermesResultCard", () => {
  it("renders summary, followups, risk, and evidence paths", () => {
    const html = renderToStaticMarkup(<HermesResultCard result={baseResult} now={200} />);
    expect(html).toContain("Codex receipt sichtbar machen");
    expect(html).toContain("Summary line one");
    expect(html).toContain("Summary line two");
    expect(html).toContain("Receipt pruefen");
    expect(html).toContain("Noch nicht live verifiziert");
    expect(html).toContain("/home/piet/receipt.md");
    expect(html).toContain("pytest tests/plugins/test_kanban_dashboard_plugin.py");
    expect(html).toContain("Verifier-approved");
    expect(html).toContain("Independent verifier gate passed.");
    expect(html).toContain("python3 check.py -&gt; stdout: CHECK OK");
  });

  it("renders preserved RESULT.md deliverables as real endpoint links", () => {
    const html = renderToStaticMarkup(<HermesResultCard result={baseResult} now={200} />);

    expect(html).toContain("Deliverables");
    expect(html).toContain("RESULT.md");
    expect(html).toContain("artifacts/chart.png");
    expect(html).toContain("/api/plugins/kanban/tasks/t_408/deliverables/RESULT.md");
  });

  it("marks done results without verifier approval as ungated", () => {
    const ungated: KanbanResult = {
      ...baseResult,
      verification_state: "ungated",
      verifier_verdict: null,
      verifier_evidence: [],
      result_quality: {
        state: "ungated",
        label: "Ungated",
        tone: "amber",
        description: "Completed without an independent verifier gate.",
      },
    };

    const html = renderToStaticMarkup(<HermesResultCard result={ungated} now={200} />);

    expect(html).toContain("Ungated");
    expect(html).toContain("Completed without an independent verifier gate.");
    expect(html).not.toContain("Verifier-approved");
  });

  it("renders rejected and unknown-legacy result-quality badges distinctly", () => {
    const rejected: KanbanResult = {
      ...baseResult,
      verification_state: "request_changes",
      verifier_verdict: "REQUEST_CHANGES",
      result_quality: {
        state: "rejected_needs_work",
        label: "Rejected / needs work",
        tone: "red",
        description: "Verifier gate requested changes before this should count as done.",
      },
    };
    const legacy: KanbanResult = {
      ...baseResult,
      profile: null,
      verifier_verdict: null,
      verifier_evidence: [],
      result_quality: {
        state: "unknown_legacy",
        label: "Unknown legacy",
        tone: "zinc",
        description: "Legacy run has no verifier metadata or profile lineage.",
      },
    };

    const rejectedHtml = renderToStaticMarkup(<HermesResultCard result={rejected} now={200} />);
    expect(rejectedHtml).toContain("Rejected / needs work");
    expect(rejectedHtml).toContain("Verifier gate requested changes before this should count as done.");
    const legacyHtml = renderToStaticMarkup(<HermesResultCard result={legacy} now={200} />);
    expect(legacyHtml).toContain("Unknown legacy");
    expect(legacyHtml).toContain("Legacy run has no verifier metadata or profile lineage.");
  });

  it("renders explicit run-lineage labels for implementation, verifier, and legacy rows", () => {
    const implementation: KanbanResult = {
      ...baseResult,
      run_role: "implementation",
      run_role_label: "Implementation / coder run",
      run_role_source: "claimed_event",
    };
    const verifier: KanbanResult = {
      ...baseResult,
      run_role: "verification",
      run_role_label: "Verifier / review run",
      run_role_source: "claimed_event",
      profile: "coder",
    };
    const legacy: KanbanResult = {
      ...baseResult,
      run_role: "legacy_unknown",
      run_role_label: "Unknown / legacy run",
      run_role_source: "missing_claim_event",
      profile: null as unknown as KanbanResult["profile"],
    };

    expect(renderToStaticMarkup(<HermesResultCard result={implementation} now={200} />)).toContain("Implementation / coder run");
    expect(renderToStaticMarkup(<HermesResultCard result={verifier} now={200} />)).toContain("Verifier / review run");
    const legacyHtml = renderToStaticMarkup(<HermesResultCard result={legacy} now={200} />);
    expect(legacyHtml).toContain("Unknown / legacy run");
    expect(legacyHtml).toContain("Profile unknown");
  });
});
