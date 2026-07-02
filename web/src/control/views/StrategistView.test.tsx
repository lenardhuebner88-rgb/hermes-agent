import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { OutcomeList, ProposalList } from "./StrategistView";
import type { StrategistProposal } from "../lib/strategist";
import type { LeverOutcome } from "../lib/schemas";

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
  grounding: null,
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

  it("renders the grounding evidence when the strategist annotated it", () => {
    const html = renderToStaticMarkup(
      <ProposalList
        proposals={[proposal({ grounding: "git log + grep belegen: kein vorhandenes Ziel" })]}
        pending={null}
        busy={false}
        onAct={() => {}}
        onPending={() => {}}
      />,
    );
    expect(html).toContain("Beleg anzeigen");
    expect(html).toContain("git log + grep belegen");
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

  it("manual variant marks the origin and suppresses the strategist annotation scaffold", () => {
    const html = renderToStaticMarkup(
      <ProposalList
        variant="manual"
        proposals={[proposal({
          created_by: "Hermes Orchestrator / Piet GO ingest only",
          source: "metric",
          target_metric: null,
          roi: null,
          counter_metric: null,
        })]}
        pending={null}
        busy={false}
        onAct={() => {}}
        onPending={() => {}}
      />,
    );
    // Honest provenance badge instead of the misleading "Aus Kennzahl".
    expect(html).toContain("Manuell ingestiert");
    expect(html).not.toContain("Aus Kennzahl");
    // The empty Ziel/ROI/Gegen-Metrik grid (and its "ohne Annotation" note) is gone.
    expect(html).not.toContain("Gegen-Metrik");
    expect(html).not.toContain("ohne Annotation");
    // Operator can still release or veto.
    expect(html).toContain("Freigeben");
    expect(html).toContain("Verwerfen");
  });
});

// Ziel-4: Wirkungs-Historie-Sektion — kompakte Zeilenliste geshippter Lever.
const outcome = (over: Partial<LeverOutcome> = {}): LeverOutcome => ({
  lever_key: "lever-heal-timeouts",
  root_task_id: "t_xyz789",
  proposed_at: 1781000000,
  baseline: { autonomy_pct: 62 },
  metric_key: "autonomy_pct",
  shipped_at: 1781100000,
  measured_at: 1781800000,
  current: { autonomy_pct: 68 },
  delta: { autonomy_pct: 6 },
  verdict: "improved",
  status: "measured",
  ...over,
});

describe("OutcomeList", () => {
  it("shows the empty state when there are no outcomes yet", () => {
    const html = renderToStaticMarkup(<OutcomeList outcomes={[]} />);
    expect(html).toContain("Noch keine gemessenen Outcomes");
  });

  it("renders a row per outcome with lever_key, status and metric_key", () => {
    const html = renderToStaticMarkup(<OutcomeList outcomes={[outcome()]} />);
    expect(html).toContain("lever-heal-timeouts");
    expect(html).toContain("Gemessen"); // status label
    expect(html).toContain("autonomy_pct");
  });

  it("shows the signed delta for the outcome's own metric_key", () => {
    const html = renderToStaticMarkup(<OutcomeList outcomes={[outcome()]} />);
    expect(html).toContain("+6");
  });

  it("omits the delta when the outcome hasn't been measured yet", () => {
    const html = renderToStaticMarkup(
      <OutcomeList outcomes={[outcome({ status: "shipped", verdict: null, delta: null, measured_at: null })]} />,
    );
    expect(html).not.toContain("+6");
  });

  it("renders all four verdicts with distinct labels", () => {
    const html = renderToStaticMarkup(
      <OutcomeList
        outcomes={[
          outcome({ lever_key: "l-improved", verdict: "improved" }),
          outcome({ lever_key: "l-worsened", verdict: "worsened" }),
          outcome({ lever_key: "l-unchanged", verdict: "unchanged" }),
          outcome({ lever_key: "l-unknown", verdict: "unknown" }),
        ]}
      />,
    );
    expect(html).toContain("verbessert");
    expect(html).toContain("verschlechtert");
    expect(html).toContain("unverändert");
    expect(html).toContain("unbekannt");
  });

  it("treats a null verdict (not measured yet) the same as unknown", () => {
    const html = renderToStaticMarkup(
      <OutcomeList outcomes={[outcome({ status: "shipped", verdict: null })]} />,
    );
    expect(html).toContain("unbekannt");
  });
});
