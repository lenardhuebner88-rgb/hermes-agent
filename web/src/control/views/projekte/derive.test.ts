import { describe, expect, it } from "vitest";
import { parseOrThrow, ProjectsAgentsResponseSchema, ProjectsResponseSchema } from "../../lib/schemas";
import {
  attentionTone,
  computeAttention,
  countAgentsByProject,
  groupAgentsByKind,
  groupAgentsByProject,
  kanbanTaskTone,
  killTarget,
  loopOutcomeTone,
  parentDisplayName,
  sortProjectsByAttention,
  splitAgentsBySource,
  type ProjectAttention,
} from "./derive";
import type { ProjectEntry } from "../../lib/schemas";

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
      kanban: { open: 1, running: 0, blocked: 1, review: 0, done_7d: 189, needs_input: 0 },
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

// ── Stufe 7 — Attention ampel ──────────────────────────────────────────────

function projectFixture(
  slug: string,
  overrides: Partial<ProjectEntry> & {
    kanban?: ProjectEntry["kanban"];
    loops?: ProjectEntry["loops"];
  } = {},
): ProjectEntry {
  return parseOrThrow(
    ProjectsResponseSchema,
    {
      generated_at: 1,
      registry_errors: [],
      projects: [
        {
          slug,
          name: slug,
          repo_path: `/tmp/${slug}`,
          parent: null,
          links: [],
          last_commit: null,
          kanban: overrides.kanban ?? {
            open: 0,
            running: 0,
            blocked: 0,
            review: 0,
            done_7d: 0,
            needs_input: 0,
          },
          loops: overrides.loops ?? { active: 0, packs: [] },
          errors: [],
          ...overrides,
        },
      ],
    },
    "attention-fixture",
  ).projects[0];
}

describe("computeAttention", () => {
  it("returns alert when blocked > 0", () => {
    const p = projectFixture("blocked-only", {
      kanban: { open: 0, running: 0, blocked: 2, review: 0, done_7d: 0, needs_input: 0 },
    });
    expect(computeAttention(p, 0)).toBe("alert");
  });

  it("returns alert when needs_input > 0 even if blocked == 0", () => {
    const p = projectFixture("needs-input-only", {
      kanban: { open: 1, running: 0, blocked: 0, review: 0, done_7d: 0, needs_input: 1 },
    });
    expect(computeAttention(p, 0)).toBe("alert");
  });

  it("returns active when agents > 0 and no alert signals", () => {
    const p = projectFixture("agents-only", {
      kanban: { open: 0, running: 0, blocked: 0, review: 0, done_7d: 0, needs_input: 0 },
      loops: { active: 0, packs: [] },
    });
    expect(computeAttention(p, 3)).toBe("active");
  });

  it("returns active when loops.active > 0 and no alert signals", () => {
    const p = projectFixture("loops-only", {
      kanban: { open: 0, running: 0, blocked: 0, review: 0, done_7d: 0, needs_input: 0 },
      loops: { active: 1, packs: [{ name: "builder-reviewer", running: true, last_heartbeat_at: null }] },
    });
    expect(computeAttention(p, 0)).toBe("active");
  });

  it("returns quiet when nothing is happening", () => {
    const p = projectFixture("idle", {
      kanban: { open: 2, running: 0, blocked: 0, review: 0, done_7d: 5, needs_input: 0 },
      loops: { active: 0, packs: [] },
    });
    expect(computeAttention(p, 0)).toBe("quiet");
  });

  it("returns quiet when kanban is null and no agents/loops", () => {
    const p = projectFixture("no-board", { kanban: null, loops: null });
    expect(computeAttention(p, 0)).toBe("quiet");
  });

  it("prefers alert over active when both blocked and agents present", () => {
    const p = projectFixture("both", {
      kanban: { open: 0, running: 1, blocked: 1, review: 0, done_7d: 0, needs_input: 0 },
      loops: { active: 1, packs: [] },
    });
    expect(computeAttention(p, 2)).toBe("alert");
  });
});

describe("sortProjectsByAttention", () => {
  it("orders alert → active → quiet and is stable within a bucket", () => {
    // Registry order: quiet-a, alert-b, active-c, quiet-d, alert-e, active-f
    const projects = [
      projectFixture("quiet-a"),
      projectFixture("alert-b", {
        kanban: { open: 0, running: 0, blocked: 1, review: 0, done_7d: 0, needs_input: 0 },
      }),
      projectFixture("active-c", { loops: { active: 1, packs: [] } }),
      projectFixture("quiet-d"),
      projectFixture("alert-e", {
        kanban: { open: 0, running: 0, blocked: 0, review: 0, done_7d: 0, needs_input: 2 },
      }),
      projectFixture("active-f"), // agents via count map
    ];
    const counts = {
      "quiet-a": 0,
      "alert-b": 0,
      "active-c": 0,
      "quiet-d": 0,
      "alert-e": 5, // agents present but alert still wins
      "active-f": 1,
    };
    const sorted = sortProjectsByAttention(projects, counts);
    expect(sorted.map((p) => p.slug)).toEqual([
      "alert-b",
      "alert-e",
      "active-c",
      "active-f",
      "quiet-a",
      "quiet-d",
    ]);
    // Does not mutate input.
    expect(projects.map((p) => p.slug)).toEqual([
      "quiet-a",
      "alert-b",
      "active-c",
      "quiet-d",
      "alert-e",
      "active-f",
    ]);
  });
});

describe("attentionTone", () => {
  it("maps alert/active/quiet onto existing SignalTones", () => {
    const cases: Array<[ProjectAttention, string]> = [
      ["alert", "alert"],
      ["active", "warn"],
      ["quiet", "neutral"],
    ];
    for (const [attention, tone] of cases) {
      expect(attentionTone(attention)).toBe(tone);
    }
  });
});

// ── Sessions sichtbar & killbar (2026-07-17) ───────────────────────────────

describe("killTarget", () => {
  it("returns (session, window) from the structured fields on tmux rows", () => {
    const parsed = parseOrThrow(ProjectsAgentsResponseSchema, {
      generated_at: 1,
      errors: [],
      agents: [
        {
          kind: "kimi",
          label: "work:2 kimi",
          task: null,
          project: "hermes-infra",
          since: 1,
          source: "tmux",
          tmux_session: "work",
          tmux_window: "2",
        },
      ],
    }, "test");
    expect(killTarget(parsed.agents[0])).toEqual({ session: "work", window: "2" });
  });

  it("refuses coordination claims even if someone smuggles fields in", () => {
    expect(
      killTarget({ source: "coordination", tmux_session: "work", tmux_window: "2" }),
    ).toBeNull();
  });

  it("refuses tmux rows without structured fields (old backend payload)", () => {
    // The REAL_AGENTS_PAYLOAD fixture predates the fields → all null after parse.
    const parsed = parseOrThrow(ProjectsAgentsResponseSchema, REAL_AGENTS_PAYLOAD, "test");
    for (const agent of parsed.agents) {
      expect(killTarget(agent)).toBeNull();
    }
  });

  it("refuses blank/whitespace-only session or window", () => {
    expect(killTarget({ source: "tmux", tmux_session: "  ", tmux_window: "2" })).toBeNull();
    expect(killTarget({ source: "tmux", tmux_session: "work", tmux_window: "" })).toBeNull();
  });
});

describe("splitAgentsBySource", () => {
  it("separates real tmux processes from vault claims, preserving order", () => {
    const parsed = parseOrThrow(ProjectsAgentsResponseSchema, REAL_AGENTS_PAYLOAD, "test");
    const { live, claims } = splitAgentsBySource(parsed.agents);
    expect(live.map((a) => a.label)).toEqual(["work:2 kimi", "review:1 claude", "odd pane"]);
    // The fixture has no coordination row; kanban/loop are excluded by design.
    expect(claims).toEqual([]);
  });

  it("keeps coordination claims and only those as check-ins", () => {
    const parsed = parseOrThrow(ProjectsAgentsResponseSchema, {
      generated_at: 1,
      errors: [],
      agents: [
        { kind: "claude", label: "2026-07-17_x_claude_claim", task: "A", project: "p", since: 1, source: "coordination" },
        { kind: "kanban", label: "t_1", task: "B", project: "p", since: 1, source: "kanban" },
        { kind: "loop", label: "pack", task: null, project: null, since: 1, source: "loop" },
      ],
    }, "test");
    const { live, claims } = splitAgentsBySource(parsed.agents);
    expect(live).toEqual([]);
    expect(claims.map((a) => a.label)).toEqual(["2026-07-17_x_claude_claim"]);
  });

  it("handles an empty list", () => {
    expect(splitAgentsBySource([])).toEqual({ live: [], claims: [] });
  });
});
