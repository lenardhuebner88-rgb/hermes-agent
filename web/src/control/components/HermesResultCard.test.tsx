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
  });
});
