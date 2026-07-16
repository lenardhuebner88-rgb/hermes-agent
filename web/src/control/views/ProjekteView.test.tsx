import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ProjectEntry, ProjectAgent } from "../lib/schemas";

const hooks = vi.hoisted(() => ({
  useProjects: vi.fn(),
  useProjectAgents: vi.fn(),
}));

vi.mock("../hooks/useControlData", () => ({
  useProjects: hooks.useProjects,
  useProjectAgents: hooks.useProjectAgents,
}));

import { ProjekteView } from "./ProjekteView";

// Real /api/projects card shape (hermes-infra, single top-level project).
const REAL_PROJECT: ProjectEntry = {
  slug: "hermes-infra",
  name: "Hermes Infra",
  repo_path: "/home/piet/.hermes/hermes-agent",
  parent: null,
  links: [{ label: "Control-Dashboard", url: "/control" }],
  last_commit: {
    hash: "9d8fa62d8",
    message: "projekte-tab: Stufe 1 — ...",
    committed_at: 1784237915,
    age_seconds: 626,
  },
  kanban: { open: 1, running: 0, blocked: 1, review: 0, done_7d: 189 },
  loops: { active: 1, packs: [{ name: "builder-reviewer", running: false, last_heartbeat_at: 1784228339 }] },
  errors: [],
};

const REAL_AGENT: ProjectAgent = {
  kind: "kimi",
  label: "work:2 kimi",
  task: null,
  project: "hermes-infra",
  since: 1784238000,
  source: "tmux",
};

function mockProjects(overrides: Record<string, unknown> = {}) {
  hooks.useProjects.mockReturnValue({
    data: null,
    error: null,
    errorObj: null,
    loading: true,
    lastUpdated: null,
    isStale: false,
    reload: vi.fn(),
    updateData: vi.fn(),
    ...overrides,
  });
}

function mockAgents(overrides: Record<string, unknown> = {}) {
  hooks.useProjectAgents.mockReturnValue({
    data: null,
    error: null,
    errorObj: null,
    loading: true,
    lastUpdated: null,
    isStale: false,
    reload: vi.fn(),
    updateData: vi.fn(),
    ...overrides,
  });
}

describe("ProjekteView", () => {
  beforeEach(() => {
    mockProjects();
    mockAgents();
  });

  it("shows the loading state before the first successful poll", () => {
    const html = renderToStaticMarkup(<ProjekteView />);
    expect(html).toContain("Lade Projekte");
  });

  it("shows a calm empty state when the registry has no projects (no error)", () => {
    mockProjects({ data: { generated_at: 1, registry_errors: [], projects: [] }, loading: false, lastUpdated: 1 });
    mockAgents({ data: { generated_at: 1, errors: [], agents: [] }, loading: false, lastUpdated: 1 });
    const html = renderToStaticMarkup(<ProjekteView />);
    expect(html).toContain("Keine Projekte registriert.");
    expect(html).toContain("projects.yaml");
  });

  it("renders a project card with blocked-warned kanban, commit, loop and agent count", () => {
    mockProjects({ data: { generated_at: 1, registry_errors: [], projects: [REAL_PROJECT] }, loading: false, lastUpdated: 1 });
    mockAgents({ data: { generated_at: 1, errors: [], agents: [REAL_AGENT] }, loading: false, lastUpdated: 1 });
    const html = renderToStaticMarkup(<ProjekteView />);
    expect(html).toContain("Hermes Infra");
    expect(html).toContain("9d8fa62d8");
    expect(html).toContain("projekte-tab: Stufe 1");
    expect(html).toContain("Blockiert 1");
    expect(html).toContain("1 Loop aktiv");
    expect(html).toContain("1 Agent");
    // No "Teil von" hint for a top-level project.
    expect(html).not.toContain("Teil von");
  });

  it("surfaces registry_errors as an honest notice instead of an empty tab", () => {
    mockProjects({
      data: { generated_at: 1, registry_errors: ["projects.yaml: invalid YAML"], projects: [] },
      loading: false,
      lastUpdated: 1,
    });
    mockAgents({ data: { generated_at: 1, errors: [], agents: [] }, loading: false, lastUpdated: 1 });
    const html = renderToStaticMarkup(<ProjekteView />);
    expect(html).toContain("Registry-Fehler");
    expect(html).toContain("projects.yaml: invalid YAML");
  });

  it("surfaces the projects-endpoint error banner distinctly from the agents-endpoint one", () => {
    mockProjects({ error: "network down" });
    mockAgents({ error: "agents timeout" });
    const html = renderToStaticMarkup(<ProjekteView />);
    expect(html).toContain("Projekt-Übersicht konnte nicht geladen werden.");
    expect(html).toContain("Agent-Belegung konnte nicht geladen werden.");
  });
});
