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
});
