// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it } from "vitest";

import { OrchestratorQueueTable } from "./OrchestratorBacklogView";
import { CommissionBanner, OrchestratorHeroPanel } from "./orchestrator/OrchestratorSections";
import type { OrchestrationItem } from "../lib/schemas";

afterEach(() => {
  cleanup();
});

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

  it("queue table exposes all columns through a horizontal scroll region", () => {
    // Track minima from the 8-col md grid + 7×gap-3 (12px): 1074 + 84 = 1158px ≈ 72.375rem.
    const COLUMN_MINIMA_PX = 220 + 96 + 112 + 112 + 84 + 140 + 150 + 160;
    const GAP_PX = 7 * 12;
    const REQUIRED_MIN_WIDTH_PX = COLUMN_MINIMA_PX + GAP_PX;

    const rows = [item({ id: "ready", title: "Ready Item", source: "MiniMax-Audit", lastProof: "smoke ok" })];
    render(
      <OrchestratorQueueTable
        items={rows}
        allItems={rows}
        nowSec={Date.parse("2026-06-04T12:00:00Z") / 1000}
        nextTaskId="ready"
        onOpen={() => undefined}
        onCommission={() => undefined}
      />,
    );

    const section = screen.getByRole("heading", { name: "Dispatch Queue" }).closest("section");
    expect(section).not.toBeNull();

    const scrollRegion = section!.querySelector(".overflow-x-auto");
    expect(scrollRegion).not.toBeNull();

    // Header labels + data rows share one scroll region so columns stay aligned.
    expect(scrollRegion!.textContent).toContain("Next Action");
    expect(scrollRegion!.textContent).toContain("Source");
    expect(scrollRegion!.textContent).toContain("Last Proof");

    const row = screen.getByRole("button", { name: /Ready Item/ });
    expect(scrollRegion!.contains(row)).toBe(true);
    expect(screen.getByRole("button", { name: "An Fleet kopieren" })).toBeTruthy();
    expect(scrollRegion!.contains(screen.getByRole("button", { name: "An Fleet kopieren" }))).toBe(true);

    // md grids (or a shared host) must declare a min-width covering column track minima.
    const minWidthNodes = [
      scrollRegion as Element,
      ...Array.from(scrollRegion!.querySelectorAll("[class*='min-w-']")),
    ];
    const minWidthPx = minWidthNodes.reduce((best, node) => {
      const match = node.className.match(/(?:^|\s)(?:md:)?min-w-\[(\d+(?:\.\d+)?)(rem|px)\]/);
      if (!match) return best;
      const value = Number(match[1]);
      const px = match[2] === "rem" ? value * 16 : value;
      return Math.max(best, px);
    }, 0);
    expect(minWidthPx).toBeGreaterThanOrEqual(REQUIRED_MIN_WIDTH_PX);
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
