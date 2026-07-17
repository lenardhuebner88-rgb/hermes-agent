import { describe, expect, it } from "vitest";
import { parseOrThrow, ProjectsAgentsResponseSchema, ProjectsResponseSchema } from "../../lib/schemas";
import {
  countAgentsByProject,
  groupAgentsByKind,
  groupAgentsByProject,
  kanbanTaskTone,
  loopOutcomeTone,
  parentDisplayName,
} from "./derive";

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
// Extended for Stufe 5: unassigned loop + unknown kind + second project.
const REAL_AGENTS_PAYLOAD = {
  generated_at: 1784238541,
  errors: ["tmux: could not reach one session"],
  agents: [
    { kind: "kimi", label: "work:2 kimi", task: null, project: "hermes-infra", since: 1784238000, source: "tmux" },
    { kind: "claude", label: "review:1 claude", task: "PR-Review", project: "hermes-infra", since: 1784237000, source: "tmux" },
    { kind: "loop", label: "builder-reviewer", task: null, project: null, since: null, source: "loop" },
    { kind: "kanban", label: "builder · fo-board", task: "Slice FO-12", project: "family-organizer", since: 1784237500, source: "kanban" },
    { kind: "mystery-cli", label: "odd pane", task: null, project: null, since: 1784236000, source: "tmux" },
  ],
};

describe("countAgentsByProject (real /api/projects/agents fixture)", () => {
  it("aggregates agents by project slug, ignoring project:null", () => {
    const parsed = parseOrThrow(ProjectsAgentsResponseSchema, REAL_AGENTS_PAYLOAD, "test");
    expect(countAgentsByProject(parsed.agents)).toEqual({
      "hermes-infra": 2,
      "family-organizer": 1,
    });
  });

  it("returns an empty map for an empty agent list", () => {
    expect(countAgentsByProject([])).toEqual({});
  });

  it("ignores agents without a resolved project", () => {
    expect(countAgentsByProject([{ project: null }, { project: null }])).toEqual({});
  });
});

describe("groupAgentsByProject (real /api/projects/agents fixture)", () => {
  it("groups full agent rows by project and drops project:null", () => {
    const parsed = parseOrThrow(ProjectsAgentsResponseSchema, REAL_AGENTS_PAYLOAD, "test");
    const groups = groupAgentsByProject(parsed.agents);
    expect(Object.keys(groups).sort()).toEqual(["family-organizer", "hermes-infra"]);
    expect(groups["hermes-infra"]).toHaveLength(2);
    expect(groups["hermes-infra"].map((a) => a.kind).sort()).toEqual(["claude", "kimi"]);
    expect(groups["family-organizer"]).toHaveLength(1);
    expect(groups["family-organizer"][0].kind).toBe("kanban");
    // Unassigned loop + unknown stay out of project groups.
    expect(groups["hermes-infra"].some((a) => a.project == null)).toBe(false);
  });

  it("returns an empty map for an empty agent list", () => {
    expect(groupAgentsByProject([])).toEqual({});
  });
});

describe("groupAgentsByKind (real /api/projects/agents fixture)", () => {
  it("orders non-empty kind groups and keeps project:null inside kind", () => {
    const parsed = parseOrThrow(ProjectsAgentsResponseSchema, REAL_AGENTS_PAYLOAD, "test");
    // mystery-cli must degrade to "unknown" via the schema transform.
    expect(parsed.agents.some((a) => a.kind === "unknown")).toBe(true);

    const groups = groupAgentsByKind(parsed.agents);
    expect(groups.map(([kind]) => kind)).toEqual(["claude", "kimi", "kanban", "loop", "unknown"]);

    const byKind = Object.fromEntries(groups);
    expect(byKind.claude).toHaveLength(1);
    expect(byKind.claude[0].task).toBe("PR-Review");
    expect(byKind.kimi).toHaveLength(1);
    expect(byKind.kanban[0].label).toBe("builder · fo-board");
    // Unassigned loop is NOT a separate group — it sits under kind "loop".
    expect(byKind.loop).toHaveLength(1);
    expect(byKind.loop[0].project).toBeNull();
    expect(byKind.unknown).toHaveLength(1);
    expect(byKind.unknown[0].label).toBe("odd pane");
    expect(byKind.unknown[0].project).toBeNull();
  });

  it("omits empty kinds and returns [] for no agents", () => {
    expect(groupAgentsByKind([])).toEqual([]);
  });

  it("preserves encounter order within a kind bucket", () => {
    const agents = parseOrThrow(
      ProjectsAgentsResponseSchema,
      {
        generated_at: 1,
        errors: [],
        agents: [
          { kind: "codex", label: "a", task: null, project: "p", since: 1, source: "tmux" },
          { kind: "codex", label: "b", task: null, project: null, since: 2, source: "coordination" },
        ],
      },
      "test",
    ).agents;
    const groups = groupAgentsByKind(agents);
    expect(groups).toHaveLength(1);
    expect(groups[0][0]).toBe("codex");
    expect(groups[0][1].map((a) => a.label)).toEqual(["a", "b"]);
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

// Real last_outcome.verdict values from loops/ledger.jsonl (ok / landed / fail /
// bounced / blocked / stopped) — map onto Leitstand SignalTone for the drawer.
describe("loopOutcomeTone (real ledger verdicts)", () => {
  it("tints landed/passed/ok as ok", () => {
    expect(loopOutcomeTone("landed")).toBe("ok");
    expect(loopOutcomeTone("passed")).toBe("ok");
    expect(loopOutcomeTone("ok")).toBe("ok");
    expect(loopOutcomeTone("LANDED")).toBe("ok");
  });

  it("tints fail/stopped/bounced/blocked as warn", () => {
    expect(loopOutcomeTone("fail")).toBe("warn");
    expect(loopOutcomeTone("stopped")).toBe("warn");
    expect(loopOutcomeTone("bounced")).toBe("warn");
    expect(loopOutcomeTone("blocked")).toBe("warn");
  });

  it("returns neutral for empty/unknown verdicts", () => {
    expect(loopOutcomeTone(null)).toBe("neutral");
    expect(loopOutcomeTone(undefined)).toBe("neutral");
    expect(loopOutcomeTone("")).toBe("neutral");
    expect(loopOutcomeTone("running")).toBe("neutral");
  });
});

describe("kanbanTaskTone", () => {
  it("tints blocked+needs_input as alert, other blocked as warn", () => {
    expect(kanbanTaskTone("blocked", "needs_input")).toBe("alert");
    expect(kanbanTaskTone("blocked", "dependency")).toBe("warn");
    expect(kanbanTaskTone("blocked", null)).toBe("warn");
  });

  it("tints running as ok and open statuses as neutral", () => {
    expect(kanbanTaskTone("running", null)).toBe("ok");
    expect(kanbanTaskTone("todo", null)).toBe("neutral");
    expect(kanbanTaskTone("ready", null)).toBe("neutral");
  });
});
