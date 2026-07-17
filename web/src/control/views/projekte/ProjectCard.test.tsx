// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
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
};

function renderCard(overrides: Partial<Parameters<typeof ProjectCard>[0]> = {}) {
  const props = {
    project: PROJECT,
    agents: [LIVE_AGENT],
    parentName: null,
    attention: "active" as const,
    now: 1784238000 + 3600,
    onOpen: vi.fn(),
    onKillSession: vi.fn(),
    ...overrides,
  };
  render(<ProjectCard {...props} />);
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
