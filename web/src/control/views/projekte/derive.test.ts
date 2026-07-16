import { describe, expect, it } from "vitest";
import { parseOrThrow, ProjectsAgentsResponseSchema, ProjectsResponseSchema } from "../../lib/schemas";
import { countAgentsByProject, parentDisplayName } from "./derive";

// Real /api/projects payload (single-project registry, live checkout) — the
// exact shape hermes_cli/projects_overview.py.build_projects_payload() emits.
const REAL_PROJECTS_PAYLOAD = {
  generated_at: 1784238541,
  registry_errors: [],
  projects: [
    {
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
      loops: {
        active: 1,
        packs: [{ name: "builder-reviewer", running: false, last_heartbeat_at: 1784228339 }],
      },
      errors: [],
    },
  ],
};

// Real /api/projects/agents payload shape — field values match the worked
// examples in the endpoint's own docstring/PlanSpec ("work:2 kimi" tmux pane).
const REAL_AGENTS_PAYLOAD = {
  generated_at: 1784238541,
  errors: ["tmux: could not reach one session"],
  agents: [
    { kind: "kimi", label: "work:2 kimi", task: null, project: "hermes-infra", since: 1784238000, source: "tmux" },
    { kind: "claude", label: "review:1 claude", task: "PR-Review", project: "hermes-infra", since: 1784237000, source: "tmux" },
    { kind: "loop", label: "builder-reviewer", task: null, project: null, since: null, source: "loop" },
  ],
};

describe("countAgentsByProject (real /api/projects/agents fixture)", () => {
  it("aggregates agents by project slug, ignoring project:null", () => {
    const parsed = parseOrThrow(ProjectsAgentsResponseSchema, REAL_AGENTS_PAYLOAD, "test");
    expect(countAgentsByProject(parsed.agents)).toEqual({ "hermes-infra": 2 });
  });

  it("returns an empty map for an empty agent list", () => {
    expect(countAgentsByProject([])).toEqual({});
  });

  it("ignores agents without a resolved project", () => {
    expect(countAgentsByProject([{ project: null }, { project: null }])).toEqual({});
  });
});

describe("parentDisplayName", () => {
  it("returns null for a top-level project (real fixture: parent === null)", () => {
    const parsed = parseOrThrow(ProjectsResponseSchema, REAL_PROJECTS_PAYLOAD, "test");
    expect(parentDisplayName(parsed.projects[0].parent, parsed.projects)).toBeNull();
  });

  it("resolves the parent's display name when the parent is in the registry", () => {
    const projects = [
      { slug: "family-organizer", name: "Family Organizer" },
      { slug: "fo-backend", name: "FO Backend" },
    ];
    expect(parentDisplayName("family-organizer", projects)).toBe("Family Organizer");
  });

  it("falls back to the raw slug when the parent is missing from the registry", () => {
    expect(parentDisplayName("ghost-parent", [{ slug: "hermes-infra", name: "Hermes Infra" }])).toBe("ghost-parent");
  });
});
