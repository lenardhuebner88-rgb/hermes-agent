import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { ProposalList } from "./StrategistView";
import type { StrategistProposal } from "../lib/strategist";

// Static render tests (RunTimelineView/ResearchView pattern): no polling, just
// the pure list. Asserts the held proposal surfaces its annotations + actions.

const proposal = (over: Partial<StrategistProposal> = {}): StrategistProposal => ({
  id: "t_abc123",
  title: "Autonomie-Hebel: Heiler-Ursachen automatisch reparieren",
  created_by: "strategist-cron",
  created_at: 1781161012,
  subtask_count: 3,
  target_metric: "Autonomie-% 62 → 75",
  roi: "hoch",
  counter_metric: "Fehl-Eskalations-Rate < 5%",
  ...over,
});

describe("ProposalList", () => {
  it("renders the proposal title, source, subtask count and annotations", () => {
    const html = renderToStaticMarkup(
      <ProposalList proposals={[proposal()]} pending={null} busy={false} onAct={() => {}} onPending={() => {}} />,
    );
    expect(html).toContain("Autonomie-Hebel");
    expect(html).toContain("Stratege"); // proposalSource(strategist-cron)
    expect(html).toContain("3 Teilaufgaben");
    expect(html).toContain("Autonomie-% 62 → 75");
    expect(html).toContain("Fehl-Eskalations-Rate &lt; 5%");
    // Action buttons present (idle state).
    expect(html).toContain("Freigeben");
    expect(html).toContain("Verwerfen");
  });

  it("shows the unannotated note and em-dashes when annotations are absent", () => {
    const html = renderToStaticMarkup(
      <ProposalList
        proposals={[proposal({ target_metric: null, roi: null, counter_metric: null })]}
        pending={null}
        busy={false}
        onAct={() => {}}
        onPending={() => {}}
      />,
    );
    expect(html).toContain("ohne Annotation");
    expect(html).toContain("—");
  });

  it("renders the confirm step when an action is pending", () => {
    const html = renderToStaticMarkup(
      <ProposalList
        proposals={[proposal()]}
        pending={{ id: "t_abc123", kind: "veto" }}
        busy={false}
        onAct={() => {}}
        onPending={() => {}}
      />,
    );
    expect(html).toContain("Bestätigen");
    expect(html).toContain("Abbrechen");
  });

  it("singularises one subtask", () => {
    const html = renderToStaticMarkup(
      <ProposalList proposals={[proposal({ subtask_count: 1 })]} pending={null} busy={false} onAct={() => {}} onPending={() => {}} />,
    );
    expect(html).toContain("1 Teilaufgabe");
    expect(html).not.toContain("1 Teilaufgaben");
  });
});
