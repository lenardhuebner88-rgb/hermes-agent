import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { HermesTodayDigestCard } from "./HermesTodayDigestCard";
import type { TodayDigestItem } from "../lib/types";

const digestItem: TodayDigestItem = {
  run_id: "42",
  task_id: "t_digest",
  task_title: "Heute fuer mich Ergebnis-Digest",
  task_summary: "S4 shipped: digest answers what arrived today",
  ended_at: 1780000000,
  profile: "coder",
  run_role: "implementation",
  run_role_label: "Implementation / coder run",
  verification_state: "approved",
  verifier_verdict: "APPROVED",
  verdict_label: "Verified: APPROVED",
  result_quality: {
    state: "verifier_approved",
    label: "Verifier-approved",
    tone: "emerald",
    description: "Independent verifier gate passed.",
  },
  gate_evidence: ["web vitest -> 12 passed"],
  deliverable: {
    filename: "RESULT.md",
    relative_path: "RESULT.md",
    size: 99,
    mtime: 1780000000,
    content_type: "text/markdown",
    url: "/api/plugins/kanban/tasks/t_digest/deliverables/RESULT.md",
  },
  deliverable_excerpt: "Operator-facing excerpt from RESULT.md",
  residual_risk: null,
};

describe("HermesTodayDigestCard", () => {
  it("answers what arrived, where the deliverable is, and whether it was verified", () => {
    const html = renderToStaticMarkup(<HermesTodayDigestCard item={digestItem} now={1780000600} />);

    expect(html).toContain("Heute fuer mich Ergebnis-Digest");
    expect(html).toContain("S4 shipped: digest answers what arrived today");
    expect(html).toContain("RESULT.md");
    expect(html).toContain("/api/plugins/kanban/tasks/t_digest/deliverables/RESULT.md");
    expect(html).toContain("Operator-facing excerpt from RESULT.md");
    expect(html).toContain("Verifier-approved");
    expect(html).toContain("Independent verifier gate passed.");
    expect(html).toContain("web vitest -&gt; 12 passed");
  });
});
