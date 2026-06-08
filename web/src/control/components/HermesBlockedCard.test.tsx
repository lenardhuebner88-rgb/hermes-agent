import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { HermesBlockedCard } from "./HermesBlockedCard";
import { de } from "../i18n/de";
import type { BlockedCompletion } from "../lib/types";

const blockedBase: BlockedCompletion = {
  event_id: 42,
  task_id: "t_42",
  task_title: "Phantom-Karten behauptet",
  task_status: "blocked",
  assignee: "critic",
  kind: "completion_blocked_hallucination",
  created_at: 100,
  summary_preview: "Hat zwei Karten erfunden",
  phantom: ["t_deadbeefcafe", "t_0000ffff1111"],
  failure_output: [],
};

describe("HermesBlockedCard", () => {
  it("renders a hard-block in red with title, preview, kind and phantom chips", () => {
    const html = renderToStaticMarkup(<HermesBlockedCard blocked={blockedBase} now={200} />);
    expect(html).toContain("Phantom-Karten behauptet");
    expect(html).toContain("Hat zwei Karten erfunden");
    expect(html).toContain("completion_blocked_hallucination");
    expect(html).toContain("t_deadbeefcafe");
    expect(html).toContain("t_0000ffff1111");
    expect(html).toContain(de.hermes.blockedKindBlocked);
    expect(html).toContain(de.hermes.blockedHardHint);
    // red tone classes for the hard block
    expect(html).toContain("red");
  });

  it("renders the advisory variant in amber tone", () => {
    const advisory: BlockedCompletion = {
      ...blockedBase,
      event_id: 43,
      kind: "suspected_hallucinated_references",
      summary_preview: null,
      phantom: ["t_abcabcabcabc"],
    };
    const html = renderToStaticMarkup(<HermesBlockedCard blocked={advisory} now={200} />);
    expect(html).toContain(de.hermes.blockedKindAdvisory);
    expect(html).toContain(de.hermes.blockedAdvisoryHint);
    expect(html).toContain("t_abcabcabcabc");
    expect(html).toContain("amber");
  });

  it("renders verifier rejections with quoted failure output and concrete fix target", () => {
    const rejection: BlockedCompletion = {
      ...blockedBase,
      event_id: -501,
      run_id: "501",
      reviewer_profile: "verifier",
      verifier_verdict: "REQUEST_CHANGES",
      kind: "verifier_request_changes",
      summary_preview: "REQUEST_CHANGES — pytest failed",
      phantom: [],
      failure_output: ["pytest tests/test_calc.py -\u003e stdout: FAILED test_add"],
      fix_summary: "Fix add(a, b) to return a + b before resubmitting.",
    };
    const html = renderToStaticMarkup(<HermesBlockedCard blocked={rejection} now={200} />);

    expect(html).toContain(de.hermes.verifierRejectedKind);
    expect(html).toContain(de.hermes.verifierRejectedFixLabel);
    expect(html).toContain("pytest tests/test_calc.py -&gt; stdout: FAILED test_add");
    expect(html).toContain("Fix add(a, b) to return a + b before resubmitting.");
    expect(html).toContain("verifier");
    expect(html).toContain("Run 501");
  });
});
