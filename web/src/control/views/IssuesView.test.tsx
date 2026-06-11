import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { IssueRow, IssuesPanel, type IssueGroup, type RunsIssuesResponse } from "./IssuesView";

// Härtung (e): Render-Tests nach RunTimelineView-Muster — die View hatte
// bisher keinerlei Tests, obwohl sie maskierte Signaturen + Outcome-Zähler
// operatorsichtbar macht.

const group = (over: Partial<IssueGroup>): IssueGroup => ({
  signature: "pid N not alive",
  profile: "premium",
  count: 3,
  first_seen: 1000,
  last_seen: 2000,
  outcomes: { crashed: 3 },
  example_run_id: 712,
  example_task_id: "t_31160808",
  example_text: "pid 3463889 not alive",
  ...over,
});

const fixture = (issues: IssueGroup[], over: Partial<RunsIssuesResponse> = {}): RunsIssuesResponse => ({
  days: 30,
  now: 5000,
  total_failed_runs: issues.reduce((a, g) => a + g.count, 0),
  group_count: issues.length,
  truncated: false,
  issues,
  ...over,
});

describe("IssueRow", () => {
  it("zeigt maskierte Signatur, Zähler und Outcome-Verteilung; Beispiel bleibt eingeklappt", () => {
    const html = renderToStaticMarkup(<IssueRow issue={group({})} />);
    expect(html).toContain("pid N not alive");
    expect(html).toContain("3×");
    expect(html).toContain("crashed·3");
    // Detail (unmaskiertes Beispiel) erst nach Klick — initial nicht im Markup
    expect(html).not.toContain("pid 3463889 not alive");
  });
});

describe("IssuesPanel", () => {
  it("rendert Pods (Gruppen/Fehl-Runs) und alle Gruppen-Zeilen", () => {
    const html = renderToStaticMarkup(
      <IssuesPanel data={fixture([group({}), group({ signature: "Iteration budget exhausted (N/N)", profile: "research", count: 5, outcomes: { timed_out: 3, gave_up: 2 } })])} />,
    );
    expect(html).toContain("Issue-Gruppen");
    expect(html).toContain("Iteration budget exhausted (N/N)");
    expect(html).toContain("timed_out·3");
    expect(html).toContain("gave_up·2");
  });

  it("leere Liste → Empty-State, kein Listen-Markup", () => {
    const html = renderToStaticMarkup(<IssuesPanel data={fixture([])} />);
    expect(html).toContain("Keine wiederkehrenden Fehler");
  });

  it("truncated-Flag wird als Warnung sichtbar", () => {
    const html = renderToStaticMarkup(<IssuesPanel data={fixture([group({})], { truncated: true })} />);
    expect(html).toContain("Liste gekappt");
  });
});
