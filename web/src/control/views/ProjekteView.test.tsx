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
  kanban: { open: 1, running: 0, blocked: 1, review: 0, done_7d: 189, needs_input: 0 },
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
  tmux_session: "work",
  tmux_window: "2",
};

// Coordination claim (source="coordination"): has a task text, is NOT a
// process and must never grow a kill button.
const CLAIM_AGENT: ProjectAgent = {
  kind: "claude",
  label: "2026-07-17_0310_claude_frage-assistent-i1",
  task: "Frage-Assistent I1 — Antwort-Sheet (P0c) + Klick-Regression",
  project: "hermes-infra",
  since: 1784238000,
  source: "coordination",
  tmux_session: null,
  tmux_window: null,
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

  it("renders a project card with blocked-warned kanban, commit, loop and agent chip", () => {
    mockProjects({ data: { generated_at: 1, registry_errors: [], projects: [REAL_PROJECT] }, loading: false, lastUpdated: 1 });
    mockAgents({ data: { generated_at: 1, errors: [], agents: [REAL_AGENT] }, loading: false, lastUpdated: 1 });
    const html = renderToStaticMarkup(<ProjekteView />);
    expect(html).toContain("Hermes Infra");
    expect(html).toContain("9d8fa62d8");
    expect(html).toContain("projekte-tab: Stufe 1");
    expect(html).toContain("Blockiert 1");
    expect(html).toContain("1 Loop aktiv");
    // Stufe 5: kind chip (Kimi) in the rail; the card shows the session row.
    expect(html).toContain("Kimi");
    expect(html).toContain("Alle Agents");
    expect(html).toContain("work:2 kimi");
    // Summary strip: 1 tmux process live, 0 claims; 1 blocked across kanban.
    expect(html).toContain("1 live");
    expect(html).toContain("0 Check-ins");
    expect(html).toContain("1 blockiert");
    // Sessions section on the card: live row with structured kill target.
    expect(html).toContain("Sessions");
    expect(html).toContain('aria-label="Session work:2 kimi beenden"');
    // No "Teil von" hint for a top-level project.
    expect(html).not.toContain("Teil von");
  });

  it("renders coordination agents as quiet check-in rows with task text, never killable", () => {
    mockProjects({ data: { generated_at: 1, registry_errors: [], projects: [REAL_PROJECT] }, loading: false, lastUpdated: 1 });
    mockAgents({
      data: { generated_at: 1, errors: [], agents: [REAL_AGENT, CLAIM_AGENT] },
      loading: false,
      lastUpdated: 1,
    });
    const html = renderToStaticMarkup(<ProjekteView />);
    expect(html).toContain("Check-ins");
    expect(html).toContain("Frage-Assistent I1 — Antwort-Sheet (P0c) + Klick-Regression");
    expect(html).toContain("Claim, kein Prozess");
    expect(html).toContain("1 Check-in");
    // Only the tmux row carries a kill button; the claim row must not.
    expect(html.match(/aria-label="Session [^"]* beenden"/g) ?? []).toHaveLength(1);
  });

  it("shows no kill button for tmux rows missing the structured fields (old backend)", () => {
    const legacyAgent: ProjectAgent = {
      kind: "codex",
      label: "work:1 codex",
      task: null,
      project: "hermes-infra",
      since: 1784238000,
      source: "tmux",
      tmux_session: null,
      tmux_window: null,
    };
    mockProjects({ data: { generated_at: 1, registry_errors: [], projects: [REAL_PROJECT] }, loading: false, lastUpdated: 1 });
    mockAgents({
      data: { generated_at: 1, errors: [], agents: [legacyAgent] },
      loading: false,
      lastUpdated: 1,
    });
    const html = renderToStaticMarkup(<ProjekteView />);
    // Live row renders (label + section), but without tmux_session/tmux_window
    // there is no kill affordance — never label-parsing for a destructive call.
    expect(html).toContain("work:1 codex");
    expect(html).toContain("Sessions");
    expect(html).not.toContain("beenden");
  });

  it("shows unassigned agents in the kind rail with Unzugeordnet, not as a separate group", () => {
    const unassignedLoop = {
      kind: "loop" as const,
      label: "builder-reviewer",
      task: null,
      project: null,
      since: null,
      source: "loop",
      tmux_session: null,
      tmux_window: null,
    };
    mockProjects({ data: { generated_at: 1, registry_errors: [], projects: [REAL_PROJECT] }, loading: false, lastUpdated: 1 });
    mockAgents({
      data: { generated_at: 1, errors: [], agents: [REAL_AGENT, unassignedLoop] },
      loading: false,
      lastUpdated: 1,
    });
    const html = renderToStaticMarkup(<ProjekteView />);
    expect(html).toContain("builder-reviewer");
    expect(html).toContain("Unzugeordnet");
    expect(html).toContain("Loop");
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

  it("renders the card grid in attention order (alert → active → quiet), not registry order", () => {
    // Registry order deliberately quiet-first; the grid must reorder so the
    // blocked project (alert) leads and the idle one trails. Stufe 7 sort.
    const quiet: ProjectEntry = {
      ...REAL_PROJECT,
      slug: "oma-galerie",
      name: "Oma-Galerie",
      kanban: null,
      loops: { active: 0, packs: [] },
    };
    const active: ProjectEntry = {
      ...REAL_PROJECT,
      slug: "health-track",
      name: "Health Track",
      kanban: { open: 2, running: 0, blocked: 0, review: 0, done_7d: 0, needs_input: 0 },
      loops: { active: 1, packs: [] },
    };
    const alert: ProjectEntry = {
      ...REAL_PROJECT,
      slug: "hermes-infra",
      name: "Hermes Infra",
      kanban: { open: 1, running: 0, blocked: 1, review: 0, done_7d: 0, needs_input: 0 },
      loops: { active: 0, packs: [] },
    };
    mockProjects({
      data: { generated_at: 1, registry_errors: [], projects: [quiet, active, alert] },
      loading: false,
      lastUpdated: 1,
    });
    mockAgents({ data: { generated_at: 1, errors: [], agents: [] }, loading: false, lastUpdated: 1 });
    const html = renderToStaticMarkup(<ProjekteView />);
    // The "Alle Agents" rail also mentions names, so restrict to the grid slice
    // before the rail heading to assert card order specifically.
    const grid = html.slice(0, html.indexOf("Alle Agents"));
    const posAlert = grid.indexOf("Hermes Infra");
    const posActive = grid.indexOf("Health Track");
    const posQuiet = grid.indexOf("Oma-Galerie");
    expect(posAlert).toBeGreaterThanOrEqual(0);
    expect(posAlert).toBeLessThan(posActive);
    expect(posActive).toBeLessThan(posQuiet);
  });

  it("marks the attention state on each card (aria-label on the status dot)", () => {
    const alert: ProjectEntry = {
      ...REAL_PROJECT,
      kanban: { open: 0, running: 0, blocked: 0, review: 0, done_7d: 0, needs_input: 2 },
      loops: { active: 0, packs: [] },
    };
    mockProjects({
      data: { generated_at: 1, registry_errors: [], projects: [alert] },
      loading: false,
      lastUpdated: 1,
    });
    mockAgents({ data: { generated_at: 1, errors: [], agents: [] }, loading: false, lastUpdated: 1 });
    const html = renderToStaticMarkup(<ProjekteView />);
    // needs_input > 0 (even with blocked == 0) → alert; the dot carries the label.
    expect(html).toContain('data-attention="alert"');
    // Stufe 8: the attention accent bar (absolute child, not a border utility)
    // is tinted status-alert for an alert card.
    expect(html).toContain("bg-status-alert");
  });
});
