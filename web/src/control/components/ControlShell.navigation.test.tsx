// @vitest-environment jsdom
import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { ControlShell, type ControlTab } from "./ControlShell";
import type { DecisionInboxData } from "../hooks/useControlData";

vi.mock("./NotificationBridge", () => ({ NotificationBridge: () => null }));
vi.mock("./Overlay", () => ({ Overlay: ({ children }: { children: ReactNode }) => <div>{children}</div> }));
vi.mock("../lib/clock", () => ({ useClientNowSeconds: () => 1783025500 }));

const baseProps = {
  active: "fleet" as ControlTab,
  density: "compact" as const,
  inbox: {
    items: [],
    summary: { total: 0, critical: 0, warnings: 0 },
    snapshot: { interventions: [] },
    worstTone: "emerald",
    loading: false,
    sourceErrors: [],
  } as unknown as DecisionInboxData,
  openProposals: 0,
  inboxTotal: 0,
  inboxTone: "emerald" as const,
  libraryUnread: 0,
  strategistCount: 0,
  health: { data: null, error: null, isStale: false, lastUpdated: null },
  onNavigate: vi.fn(),
  onOpenCommand: vi.fn(),
};

function renderShell() {
  return render(
    <MemoryRouter>
      <ControlShell {...baseProps}>
        <main>content</main>
      </ControlShell>
    </MemoryRouter>,
  );
}

describe("ControlShell primary navigation", () => {
  it("keeps desktop and compact rails on the five PlanSpec primary tabs", () => {
    renderShell();

    for (const label of ["Fleet", "Start", "Terminals", "Statistik", "Bibliothek"]) {
      expect(screen.getAllByRole("button", { name: label }).length).toBeGreaterThan(0);
    }

    for (const retired of ["Flow", "Ketten", "Hermes", "Puls", "Pressure", "Ops"]) {
      expect(screen.queryByRole("button", { name: retired })).toBeNull();
    }
  });
});
