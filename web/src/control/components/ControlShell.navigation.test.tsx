// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
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

function renderShellWith(active: ControlTab) {
  return render(
    <MemoryRouter>
      <ControlShell {...baseProps} active={active} density="airy">
        <main>content</main>
      </ControlShell>
    </MemoryRouter>,
  );
}

describe("ControlShell primary navigation", () => {
  afterEach(() => cleanup());

  it("keeps desktop and compact rails on the five PlanSpec primary tabs", () => {
    renderShell();

    for (const label of ["Fleet", "Start", "Terminals", "Statistik", "Bibliothek"]) {
      expect(screen.getAllByRole("button", { name: label }).length).toBeGreaterThan(0);
    }

    for (const retired of ["Flow", "Ketten", "Hermes", "Puls", "Pressure", "Ops"]) {
      expect(screen.queryByRole("button", { name: retired })).toBeNull();
    }
  });

  it("hides the legacy shell header on the Start mobile bleed route", () => {
    renderShellWith("inbox");

    const header = screen.getByText("Hermes Control").closest("header");
    expect(header?.className).toContain("hidden");
    expect(header?.className).toContain("lg:flex");
  });

  it("hides the legacy shell header on the Statistik mobile bleed route (own Masthead)", () => {
    renderShellWith("statistik");

    const header = screen.getByText("Hermes Control").closest("header");
    expect(header?.className).toContain("hidden");
    expect(header?.className).toContain("lg:flex");
  });

  it("keeps the legacy shell header on a non-bleed route (Backlog unchanged)", () => {
    renderShellWith("backlog");

    const header = screen.getByText("Hermes Control").closest("header");
    expect(header?.className).not.toContain("hidden");
  });
});
