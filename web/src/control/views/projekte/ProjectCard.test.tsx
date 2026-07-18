// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import type { ProjectAgent, ProjectEntry } from "../../lib/schemas";

import { ProjectCard } from "./ProjectCard";

const PROJECT: ProjectEntry = {
  slug: "hermes-infra",
  name: "Hermes Infra",
  repo_path: "/home/piet/.hermes/hermes-agent",
  parent: null,
  links: [],
  last_commit: null,
  kanban: null,
  loops: null,
  errors: [],
};

const LIVE_AGENT: ProjectAgent = {
  kind: "kimi",
  label: "work:2 kimi",
  task: null,
  project: "hermes-infra",
  since: 1784238000,
  source: "tmux",
  tmux_session: "work",
  tmux_window: "2",
  assignee: null,
  operator: null,
  session_id: null,
  task_id: null,
};

const KANBAN_COUNTS = {
  open: 3,
  running: 1,
  blocked: 2,
  review: 0,
  done_7d: 5,
  needs_input: 0,
};

function renderCard(overrides: Partial<Parameters<typeof ProjectCard>[0]> = {}) {
  const props = {
    project: PROJECT,
    agents: [LIVE_AGENT],
    parentName: null,
    attention: { level: "active" as const, reasons: [] },
    now: 1784238000 + 3600,
    onOpen: vi.fn(),
    onKillSession: vi.fn(),
    ...overrides,
  };
  render(
    <MemoryRouter>
      <ProjectCard {...props} />
    </MemoryRouter>,
  );
  return props;
}

describe("ProjectCard keyboard interaction (Fable obs. 1)", () => {
  afterEach(() => cleanup());

  it("Enter on the card opens the drawer", () => {
    const props = renderCard();
    const card = screen.getByRole("button", { name: "Projekt Hermes Infra öffnen" });
    fireEvent.keyDown(card, { key: "Enter" });
    expect(props.onOpen).toHaveBeenCalledTimes(1);
  });

  it("Enter on the nested kill button opens the kill flow ONLY, not the drawer", () => {
    const props = renderCard();
    const kill = screen.getByRole("button", { name: "Session work:2 kimi beenden" });
    fireEvent.keyDown(kill, { key: "Enter" });
    fireEvent.click(kill);
    expect(props.onOpen).not.toHaveBeenCalled();
    expect(props.onKillSession).toHaveBeenCalledTimes(1);
    expect(props.onKillSession).toHaveBeenCalledWith(LIVE_AGENT);
  });
});

describe("ProjectCard kanban chips → Fleet deep-link", () => {
  afterEach(() => cleanup());

  it("links chips to /control/fleet?board=…&status=… when kanban_project is set", () => {
    const project = {
      ...PROJECT,
      kanban_project: "health-track",
      kanban: KANBAN_COUNTS,
    } as ProjectEntry & { kanban_project: string };

    renderCard({ project, agents: [] });

    // open aggregates multiple statuses → board only (no status param)
    const open = screen.getByRole("link", { name: /Offen 3/ });
    expect(open.getAttribute("href")).toBe("/control/fleet?board=health-track");

    const running = screen.getByRole("link", { name: /Läuft 1/ });
    expect(running.getAttribute("href")).toBe("/control/fleet?board=health-track&status=running");

    const blocked = screen.getByRole("link", { name: /Blockiert 2/ });
    expect(blocked.getAttribute("href")).toBe("/control/fleet?board=health-track&status=blocked");

    const review = screen.getByRole("link", { name: /Review 0/ });
    expect(review.getAttribute("href")).toBe("/control/fleet?board=health-track&status=review");

    // done_7d is time-windowed — honesty: no link
    expect(screen.queryByRole("link", { name: /Erledigt/ })).toBeNull();
    expect(screen.getByText(/Erledigt · 7T 5/)).toBeTruthy();
  });

  it("keeps chips static when kanban_project is null", () => {
    const project = {
      ...PROJECT,
      kanban_project: null,
      kanban: KANBAN_COUNTS,
    } as ProjectEntry & { kanban_project: null };

    renderCard({ project, agents: [] });

    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByText(/Offen 3/)).toBeTruthy();
    expect(screen.getByText(/Blockiert 2/)).toBeTruthy();
  });

  it("chip click does not propagate to card onOpen", () => {
    const project = {
      ...PROJECT,
      kanban_project: "health-track",
      kanban: KANBAN_COUNTS,
    } as ProjectEntry & { kanban_project: string };

    const props = renderCard({ project, agents: [] });
    const blocked = screen.getByRole("link", { name: /Blockiert 2/ });
    fireEvent.click(blocked);
    expect(props.onOpen).not.toHaveBeenCalled();
  });
});

describe("ProjectCard attention badge + reason chips (2.3 Ampel)", () => {
  afterEach(() => cleanup());

  it("shows badge + needs_input / blocked / stale / loop_red chips for alert", () => {
    renderCard({
      agents: [],
      attention: {
        level: "alert",
        reasons: [
          { kind: "needs_input", count: 2 },
          { kind: "blocked", count: 1 },
          { kind: "stale_sessions", count: 1 },
          { kind: "loop_red", count: 1 },
        ],
      },
    });

    // Existing attention marker still present (v2 is additive).
    expect(document.querySelector('[data-attention="alert"]')).toBeTruthy();
    expect(document.querySelector('[data-attention-badge="alert"]')).toBeTruthy();
    expect(screen.getByText("Eingriff")).toBeTruthy();
    expect(screen.getByText("2 Fragen")).toBeTruthy();
    expect(screen.getByText("1 blocked")).toBeTruthy();
    expect(screen.getByText("1 stale")).toBeTruthy();
    expect(screen.getByText("Loop rot")).toBeTruthy();
    expect(document.querySelector('[data-reason="needs_input"]')).toBeTruthy();
    expect(document.querySelector('[data-reason="blocked"]')).toBeTruthy();
    expect(document.querySelector('[data-reason="stale_sessions"]')).toBeTruthy();
    expect(document.querySelector('[data-reason="loop_red"]')).toBeTruthy();
  });

  it("shows active badge without reason-chip noise when only active", () => {
    renderCard({
      attention: { level: "active", reasons: [] },
    });
    expect(document.querySelector('[data-attention="active"]')).toBeTruthy();
    expect(document.querySelector('[data-attention-badge="active"]')).toBeTruthy();
    expect(screen.getByText("Aktiv")).toBeTruthy();
    expect(document.querySelector("[data-attention-reasons]")).toBeNull();
    expect(screen.queryByText("Loop rot")).toBeNull();
  });

  it("quiet card: no badge, no reason chips (no badge noise)", () => {
    renderCard({
      agents: [],
      attention: { level: "quiet", reasons: [] },
    });
    expect(document.querySelector('[data-attention="quiet"]')).toBeTruthy();
    expect(document.querySelector("[data-attention-badge]")).toBeNull();
    expect(document.querySelector("[data-attention-reasons]")).toBeNull();
    expect(screen.queryByText("Eingriff")).toBeNull();
    expect(screen.queryByText("Aktiv")).toBeNull();
  });

  it("keeps KanbanChipLink deep-links when alert reasons are also shown", () => {
    const project = {
      ...PROJECT,
      kanban_project: "health-track",
      kanban: KANBAN_COUNTS,
    } as ProjectEntry & { kanban_project: string };

    renderCard({
      project,
      agents: [],
      attention: {
        level: "alert",
        reasons: [{ kind: "blocked", count: 2 }],
      },
    });

    const blockedLink = screen.getByRole("link", { name: /Blockiert 2/ });
    expect(blockedLink.getAttribute("href")).toBe("/control/fleet?board=health-track&status=blocked");
    // Reason chip is separate text ("2 blocked"), not a replacement for the fleet chip.
    expect(screen.getByText("2 blocked")).toBeTruthy();
  });

  it("gives KanbanChipLink ≥44px mobile hit area, compact from tab", () => {
    const project = {
      ...PROJECT,
      kanban_project: "health-track",
      kanban: KANBAN_COUNTS,
    } as ProjectEntry & { kanban_project: string };

    renderCard({ project, agents: [] });

    // House idiom: min-h-11 (44px) below tab; tab:min-h-7 restores prior density.
    const open = screen.getByRole("link", { name: /Offen 3/ });
    const cls = open.getAttribute("class") ?? "";
    expect(cls).toContain("min-h-11");
    expect(cls).toContain("tab:min-h-7");
  });
});

