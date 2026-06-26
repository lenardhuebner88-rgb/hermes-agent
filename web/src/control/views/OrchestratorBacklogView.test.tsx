import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { OrchestratorQueueTable } from "./OrchestratorBacklogView";
import { CommissionBanner, OrchestratorHeroPanel } from "./orchestrator/OrchestratorSections";
import type { OrchestrationItem } from "../lib/schemas";

const item = (overrides: Partial<OrchestrationItem>): OrchestrationItem => ({
  id: "f-a",
  title: "Dashboard Queue",
  status: "todo",
  priority: "high",
  dependsOn: [],
  planGate: false,
  created: "2026-06-01",
  root: "/home/piet/.hermes/hermes-agent",
  owner: "Piet",
  source: "MiniMax-Audit",
  lastProof: "smoke ok",
  ...overrides,
});

describe("OrchestratorQueueTable", () => {
  it("renders the queue-first operator columns", () => {
    const rows = [item({ id: "ready", title: "Ready Item" })];
    const html = renderToStaticMarkup(
      <OrchestratorQueueTable
        items={rows}
        allItems={rows}
        nowSec={Date.parse("2026-06-04T12:00:00Z") / 1000}
        nextTaskId="ready"
        onOpen={() => undefined}
      />,
    );

    expect(html).toContain("Dispatch Queue");
    expect(html).toContain("Risk/Priority");
    expect(html).toContain("Last Proof");
    expect(html).toContain("Next Action");
    expect(html).toContain("Ready Item");
    expect(html).toContain("Beauftragen");
  });

  it("keeps unknown statuses visible instead of mapping them to backlog", () => {
    const rows = [item({ id: "adr", title: "ADR", status: "decided", priority: "urgent", owner: "", lastProof: "" })];
    const html = renderToStaticMarkup(
      <OrchestratorQueueTable
        items={rows}
        allItems={rows}
        nowSec={Date.parse("2026-06-04T12:00:00Z") / 1000}
        nextTaskId={null}
        onOpen={() => undefined}
      />,
    );

    expect(html).toContain("decided");
    expect(html).toContain("urgent");
    expect(html).toContain("unowned");
    expect(html).toContain("Status klären");
    expect(html).not.toContain(">backlog<");
  });

  it("shows missing dependencies as blocking in the row action", () => {
    const rows = [item({ id: "blocked", dependsOn: ["ghost"] })];
    const html = renderToStaticMarkup(
      <OrchestratorQueueTable
        items={rows}
        allItems={rows}
        nowSec={Date.parse("2026-06-04T12:00:00Z") / 1000}
        nextTaskId={null}
        onOpen={() => undefined}
      />,
    );

    expect(html).toContain("Dependency klären: ghost");
  });
});

describe("CommissionBanner", () => {
  it("keeps the full next-task title available when the visible label is clamped", () => {
    const title = "dispatch_once spawnt zu wenige Worker bei max_spawn + max_in_progress";
    const html = renderToStaticMarkup(
      <CommissionBanner nextId="bug-dispatch-once-double-cap" nextTitle={title} prompt="Bitte beauftragen" />,
    );

    expect(html).toContain(`title="${title}"`);
    expect(html).toContain("line-clamp-2");
    expect(html).toContain("sm:truncate");
  });
});

describe("OrchestratorHeroPanel", () => {
  it("marks the updated timestamp as a machine-readable time element", () => {
    const nowSec = Date.parse("2026-06-25T20:46:00Z") / 1000;
    const html = renderToStaticMarkup(
      <OrchestratorHeroPanel activeTotal={8} loading={false} nowSec={nowSec} />,
    );

    expect(html).toContain("<time");
    expect(html).toContain('dateTime="2026-06-25T20:46:00.000Z"');
    expect(html).toContain("Stand");
  });
});
