import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { HermesReviewCard } from "./HermesReviewCard";
import type { KanbanReview } from "../lib/types";

const review: KanbanReview = {
  task_id: "t_review",
  task_title: "Fix add()",
  task_status: "review",
  task_assignee: "coder",
  created_at: 100,
  submitted_at: 160,
  run_id: "501",
  reviewer_profile: "verifier",
  summary_preview: "REQUEST_CHANGES — pytest failed",
  verification_state: "request_changes",
  verifier_verdict: "REQUEST_CHANGES",
  verifier_evidence: ["pytest tests/foo.py -> stdout: FAILED test_add"],
};

describe("HermesReviewCard", () => {
  it("renders review verdict and quoted verifier command evidence", () => {
    const html = renderToStaticMarkup(<HermesReviewCard review={review} now={200} />);

    expect(html).toContain("Fix add()");
    expect(html).toContain("REQUEST_CHANGES");
    expect(html).toContain("pytest tests/foo.py -&gt; stdout: FAILED test_add");
    expect(html).toContain("verifier");
  });
});
