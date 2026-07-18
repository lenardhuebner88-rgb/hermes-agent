import { describe, expect, it } from "vitest";
import {
  parseOrThrow,
  ProjectsAgentsResponseSchema,
  ProjectSessionsResponseSchema,
  ProjectsResponseSchema,
} from "../../lib/schemas";
import {
  agentSourceRank,
  attentionTone,
  buildSessionRows,
  computeAttention,
  countAgentsByProject,
  countOpenSessions,
  countStaleSessionsByProject,
  filterSessions,
  groupAgentsByProject,
  isLoopOutcomeRed,
  kanbanTaskTone,
  killTarget,
  liveBoardGroups,
  loopOutcomeTone,
  parentDisplayName,
  sortProjectsByAttention,
  splitAgentsBySource,
  type ProjectAttention,
} from "./derive";
import type { ProjectEntry, ProjectSession } from "../../lib/schemas";

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

describe("liveBoardGroups (real /api/projects/agents fixture)", () => {
  it("groups by project, processes first, unassigned trailing", () => {
    const parsed = parseOrThrow(ProjectsAgentsResponseSchema, REAL_AGENTS_PAYLOAD, "test");
    // mystery-cli must degrade to "unknown" via the schema transform.
    expect(parsed.agents.some((a) => a.kind === "unknown")).toBe(true);

    const groups = liveBoardGroups(parsed.agents);
    // hermes-infra holds the tmux processes → outranks the kanban-only
    // family-organizer group; the unassigned agents trail as one null group.
    expect(groups.map((group) => group.slug)).toEqual(["hermes-infra", "family-organizer", null]);

    const infra = groups[0].agents;
    expect(infra).toHaveLength(2);
    expect(infra.map((a) => a.kind).sort()).toEqual(["claude", "kimi"]);
    expect(groups[1].agents[0].label).toBe("builder · fo-board");

    const unassigned = groups[2].agents;
    expect(unassigned.map((a) => a.label)).toEqual(["odd pane", "builder-reviewer"]);
  });

  it("sorts within a group: tmux → kanban → loop → coordination, oldest first", () => {
    const parsed = parseOrThrow(ProjectsAgentsResponseSchema, {
      generated_at: 1,
      errors: [],
      agents: [
        { kind: "claude", label: "claim", task: "A", project: "p", since: 10, source: "coordination" },
        { kind: "kanban", label: "task", task: "B", project: "p", since: 20, source: "kanban" },
        { kind: "kimi", label: "newer-pane", task: null, project: "p", since: 200, source: "tmux" },
        { kind: "kimi", label: "older-pane", task: null, project: "p", since: 100, source: "tmux" },
        { kind: "loop", label: "pack", task: null, project: "p", since: null, source: "loop" },
      ],
    }, "test");
    const groups = liveBoardGroups(parsed.agents);
    expect(groups).toHaveLength(1);
    expect(groups[0].agents.map((a) => a.label)).toEqual([
      "older-pane",
      "newer-pane",
      "task",
      "pack",
      "claim",
    ]);
  });

  it("returns [] for no agents", () => {
    expect(liveBoardGroups([])).toEqual([]);
  });
});

describe("agentSourceRank", () => {
  it("ranks processes before claims; unknown sources last", () => {
    expect(agentSourceRank("tmux")).toBeLessThan(agentSourceRank("kanban"));
    expect(agentSourceRank("kanban")).toBeLessThan(agentSourceRank("loop"));
    expect(agentSourceRank("loop")).toBeLessThan(agentSourceRank("coordination"));
    expect(agentSourceRank("coordination")).toBeLessThan(agentSourceRank("something-new"));
  });
});

// ── Offene Sessions + Spawn-Baum (2026-07-17) ──────────────────────────────

function sessionFixture(overrides: Partial<ProjectSession> & { id: string }): ProjectSession {
  return parseOrThrow(
    ProjectSessionsResponseSchema,
    {
      generated_at: 1,
      errors: [],
      sessions: [
        {
          label: overrides.id,
          source: "cli",
          model: "kimi-k2",
          started_at: 1000,
          ended_at: null,
          end_reason: null,
          is_open: true,
          is_active: false,
          stale_open: false,
          last_active: 1000,
          message_count: 3,
          tokens: 150,
          project: null,
          spawn_kind: null,
          spawned_by_id: null,
          spawned_by_label: null,
          ...overrides,
        },
      ],
    },
    "session-fixture",
  ).sessions[0];
}

describe("filterSessions + countOpenSessions", () => {
  const openIdle = sessionFixture({ id: "open-idle", is_open: true, is_active: false });
  const openActive = sessionFixture({ id: "open-active", is_open: true, is_active: true });
  const staleOpen = sessionFixture({ id: "stale-open", is_open: true, is_active: false, stale_open: true });
  const ended = sessionFixture({ id: "ended", is_open: false, is_active: false, ended_at: 2000 });
  const all = [openIdle, openActive, staleOpen, ended];

  it("open keeps fresh unended sessions and splits out the zombie graveyard", () => {
    expect(filterSessions(all, "open").map((s) => s.id)).toEqual(["open-idle", "open-active"]);
    expect(filterSessions(all, "stale").map((s) => s.id)).toEqual(["stale-open"]);
  });

  it("active narrows to the 300s liveness window", () => {
    expect(filterSessions(all, "active").map((s) => s.id)).toEqual(["open-active"]);
  });

  it("all keeps the full backend window", () => {
    expect(filterSessions(all, "all")).toHaveLength(4);
  });

  it("counts open sessions for the summary strip, stale only on demand", () => {
    expect(countOpenSessions(all)).toBe(2);
    expect(countOpenSessions(all, { includeStale: true })).toBe(3);
    expect(countOpenSessions([])).toBe(0);
  });
});

describe("buildSessionRows (spawn tree)", () => {
  it("nests spawned children under their parent, oldest spawn first", () => {
    const root = sessionFixture({ id: "root", is_active: true, started_at: 100 });
    const childB = sessionFixture({
      id: "child-b",
      spawn_kind: "delegate",
      spawned_by_id: "root",
      spawned_by_label: "root",
      started_at: 300,
    });
    const childA = sessionFixture({
      id: "child-a",
      spawn_kind: "delegate",
      spawned_by_id: "root",
      spawned_by_label: "root",
      started_at: 200,
    });
    const grandchild = sessionFixture({
      id: "grand",
      spawn_kind: "child",
      spawned_by_id: "child-a",
      spawned_by_label: "child-a",
      started_at: 400,
    });
    // Deliberately scrambled input — the tree must rebuild order.
    const rows = buildSessionRows([grandchild, childB, root, childA]);
    expect(rows.map((row) => [row.session.id, row.depth])).toEqual([
      ["root", 0],
      ["child-a", 1],
      ["grand", 2],
      ["child-b", 1],
    ]);
    expect(rows[0].childCount).toBe(2);
    expect(rows[1].childCount).toBe(1);
    expect(rows[3].childCount).toBe(0);
  });

  it("treats a session with a missing parent as root but keeps spawned_by_label", () => {
    const orphan = sessionFixture({
      id: "orphan",
      spawn_kind: "delegate",
      spawned_by_id: "gone",
      spawned_by_label: "Verschwundener Elter",
    });
    const rows = buildSessionRows([orphan]);
    expect(rows).toHaveLength(1);
    expect(rows[0].depth).toBe(0);
    expect(rows[0].session.spawned_by_label).toBe("Verschwundener Elter");
  });

  it("orders roots: active first, then open, then ended — recent activity leads", () => {
    const ended = sessionFixture({ id: "ended", is_open: false, ended_at: 5000, last_active: 4900 });
    const openOld = sessionFixture({ id: "open-old", is_open: true, last_active: 1000 });
    const active = sessionFixture({ id: "active", is_open: true, is_active: true, last_active: 6000 });
    const rows = buildSessionRows([ended, openOld, active]);
    expect(rows.map((row) => row.session.id)).toEqual(["active", "open-old", "ended"]);
  });

  it("survives a corrupt self-parent link without looping", () => {
    const broken = sessionFixture({
      id: "broken",
      spawn_kind: "child",
      spawned_by_id: "broken",
    });
    const rows = buildSessionRows([broken]);
    expect(rows).toHaveLength(1);
    expect(rows[0].depth).toBe(0);
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

describe("countStaleSessionsByProject (real sessions payload shape)", () => {
  it("aggregates stale_open===true by project slug and ignores project:null", () => {
    // Real field set from ProjectSessionSchema / build_sessions_payload.
    const sessions = [
      sessionFixture({ id: "s1", project: "hermes-infra", stale_open: true }),
      sessionFixture({ id: "s2", project: "hermes-infra", stale_open: true }),
      sessionFixture({ id: "s3", project: "health-track", stale_open: true }),
      // Fresh open — must not count.
      sessionFixture({ id: "s4", project: "hermes-infra", stale_open: false }),
      // Unassigned stale graveyard (project null) — not a card signal.
      sessionFixture({ id: "s5", project: null, stale_open: true }),
    ];
    expect(countStaleSessionsByProject(sessions)).toEqual({
      "hermes-infra": 2,
      "health-track": 1,
    });
  });

  it("returns an empty map when nothing is stale", () => {
    expect(
      countStaleSessionsByProject([
        sessionFixture({ id: "fresh", project: "hermes-infra", stale_open: false }),
      ]),
    ).toEqual({});
  });
});

describe("isLoopOutcomeRed (reuses loopOutcomeTone fail set)", () => {
  it("is true exactly when loopOutcomeTone returns warn", () => {
    for (const v of ["fail", "stopped", "bounced", "blocked", "FAIL"]) {
      expect(isLoopOutcomeRed(v)).toBe(true);
      expect(loopOutcomeTone(v)).toBe("warn");
    }
    for (const v of ["landed", "passed", "ok", null, undefined, "", "running"]) {
      expect(isLoopOutcomeRed(v)).toBe(false);
    }
  });
});

describe("computeAttention v2", () => {
  it("returns alert + blocked reason when blocked > 0", () => {
    const p = projectFixture("blocked-only", {
      kanban: { open: 0, running: 0, blocked: 2, review: 0, done_7d: 0, needs_input: 0 },
    });
    expect(computeAttention(p, 0)).toEqual({
      level: "alert",
      reasons: [{ kind: "blocked", count: 2 }],
    });
  });

  it("returns alert + needs_input reason when needs_input > 0 even if blocked == 0", () => {
    const p = projectFixture("needs-input-only", {
      kanban: { open: 1, running: 0, blocked: 0, review: 0, done_7d: 0, needs_input: 1 },
    });
    expect(computeAttention(p, 0)).toEqual({
      level: "alert",
      reasons: [{ kind: "needs_input", count: 1 }],
    });
  });

  it("returns alert + stale_sessions reason from the staleCount arg", () => {
    const p = projectFixture("stale-only", {
      kanban: { open: 0, running: 0, blocked: 0, review: 0, done_7d: 0, needs_input: 0 },
    });
    expect(computeAttention(p, 0, 3)).toEqual({
      level: "alert",
      reasons: [{ kind: "stale_sessions", count: 3 }],
    });
  });

  it("returns alert + loop_red when a pack last_outcome is a fail-family verdict", () => {
    const p = projectFixture("loop-red", {
      kanban: { open: 0, running: 0, blocked: 0, review: 0, done_7d: 0, needs_input: 0 },
      loops: {
        active: 0,
        packs: [
          {
            name: "builder-reviewer",
            running: false,
            last_heartbeat_at: null,
            last_outcome: {
              verdict: "fail",
              phase: "verify",
              reason: "gate red",
              plan: "P1.md",
              ts: 1784234000,
            },
          },
          {
            name: "error-sweep",
            running: false,
            last_heartbeat_at: null,
            last_outcome: {
              verdict: "landed",
              phase: "land",
              reason: null,
              plan: null,
              ts: 1784230000,
            },
          },
          {
            name: "nacht",
            running: false,
            last_heartbeat_at: null,
            last_outcome: {
              verdict: "bounced",
              phase: "review",
              reason: "diff too big",
              plan: null,
              ts: 1784231000,
            },
          },
        ],
      },
    });
    expect(computeAttention(p, 0)).toEqual({
      level: "alert",
      reasons: [{ kind: "loop_red", count: 2 }],
    });
  });

  it("combines all four sources into one alert with all reason chips", () => {
    const p = projectFixture("all-sources", {
      kanban: { open: 1, running: 0, blocked: 1, review: 0, done_7d: 0, needs_input: 2 },
      loops: {
        active: 0,
        packs: [
          {
            name: "builder-reviewer",
            running: false,
            last_heartbeat_at: 1,
            last_outcome: {
              verdict: "stopped",
              phase: "build",
              reason: "timeout",
              plan: null,
              ts: 1,
            },
          },
        ],
      },
    });
    expect(computeAttention(p, 2, 1)).toEqual({
      level: "alert",
      reasons: [
        { kind: "needs_input", count: 2 },
        { kind: "blocked", count: 1 },
        { kind: "stale_sessions", count: 1 },
        { kind: "loop_red", count: 1 },
      ],
    });
  });

  it("does NOT flag loop_red for a RUNNING pack with an old fail verdict (retry in flight)", () => {
    // Der Runner kann nach verdict:"fail" direkt die nächste Runde starten
    // (Lock gehalten, running=true) — dann arbeitet die Automatik, kein
    // Eingriffs-Signal. Rot erst, wenn der Pack liegen geblieben ist.
    const p = projectFixture("retrying", {
      kanban: { open: 0, running: 0, blocked: 0, review: 0, done_7d: 0, needs_input: 0 },
      loops: {
        active: 1,
        packs: [
          {
            name: "builder-reviewer",
            running: true,
            last_heartbeat_at: 1784234000,
            last_outcome: {
              verdict: "fail",
              phase: "verify",
              reason: "gate red",
              plan: "P1.md",
              ts: 1784234000,
            },
          },
        ],
      },
    });
    expect(computeAttention(p, 0)).toEqual({ level: "active", reasons: [] });
  });

  it("returns active when agents > 0 and no alert signals", () => {
    const p = projectFixture("agents-only", {
      kanban: { open: 0, running: 0, blocked: 0, review: 0, done_7d: 0, needs_input: 0 },
      loops: { active: 0, packs: [] },
    });
    expect(computeAttention(p, 3)).toEqual({ level: "active", reasons: [] });
  });

  it("returns active when loops.active > 0 and no alert signals", () => {
    const p = projectFixture("loops-only", {
      kanban: { open: 0, running: 0, blocked: 0, review: 0, done_7d: 0, needs_input: 0 },
      loops: {
        active: 1,
        packs: [
          {
            name: "builder-reviewer",
            running: true,
            last_heartbeat_at: null,
            last_outcome: null,
          },
        ],
      },
    });
    expect(computeAttention(p, 0)).toEqual({ level: "active", reasons: [] });
  });

  it("returns quiet when nothing is happening", () => {
    const p = projectFixture("idle", {
      kanban: { open: 2, running: 0, blocked: 0, review: 0, done_7d: 5, needs_input: 0 },
      loops: { active: 0, packs: [] },
    });
    expect(computeAttention(p, 0)).toEqual({ level: "quiet", reasons: [] });
  });

  it("returns quiet when kanban is null and no agents/loops", () => {
    const p = projectFixture("no-board", { kanban: null, loops: null });
    expect(computeAttention(p, 0)).toEqual({ level: "quiet", reasons: [] });
  });

  it("prefers alert over active when both blocked and agents present", () => {
    const p = projectFixture("both", {
      kanban: { open: 0, running: 1, blocked: 1, review: 0, done_7d: 0, needs_input: 0 },
      loops: { active: 1, packs: [] },
    });
    expect(computeAttention(p, 2)).toEqual({
      level: "alert",
      reasons: [{ kind: "blocked", count: 1 }],
    });
  });

  it("does not treat landed/ok loop outcomes as loop_red", () => {
    const p = projectFixture("loop-ok", {
      loops: {
        active: 1,
        packs: [
          {
            name: "builder-reviewer",
            running: false,
            last_heartbeat_at: 1,
            last_outcome: {
              verdict: "landed",
              phase: "land",
              reason: "main=abc",
              plan: "P1.md",
              ts: 1,
            },
          },
        ],
      },
    });
    expect(computeAttention(p, 0)).toEqual({ level: "active", reasons: [] });
  });
});

describe("ProjectsResponseSchema last_outcome (list pack shape)", () => {
  it("parses Detail-Shape last_outcome on list packs", () => {
    const parsed = parseOrThrow(
      ProjectsResponseSchema,
      {
        generated_at: 1,
        registry_errors: [],
        projects: [
          {
            slug: "hermes-infra",
            name: "Hermes Infra",
            repo_path: "/tmp/h",
            parent: null,
            links: [],
            last_commit: null,
            kanban: null,
            loops: {
              active: 0,
              packs: [
                {
                  name: "builder-reviewer",
                  running: false,
                  last_heartbeat_at: 1784235000,
                  last_outcome: {
                    verdict: "fail",
                    phase: "verify",
                    reason: "gate red",
                    plan: "P1-ship.md",
                    ts: 1784234000,
                  },
                },
              ],
            },
            errors: [],
          },
        ],
      },
      "list-last-outcome",
    );
    expect(parsed.projects[0].loops?.packs[0].last_outcome).toEqual({
      verdict: "fail",
      phase: "verify",
      reason: "gate red",
      plan: "P1-ship.md",
      ts: 1784234000,
    });
  });

  it("tolerates missing last_outcome on older list payloads (→ null)", () => {
    const parsed = parseOrThrow(ProjectsResponseSchema, REAL_PROJECTS_PAYLOAD, "legacy-list");
    // Fixture has packs without last_outcome key — schema catches to null.
    expect(parsed.projects[0].loops?.packs[0].last_outcome).toBeNull();
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

  it("staleCountBySlug (3rd arg) lifts an otherwise-quiet project into the alert bucket", () => {
    // Migrations-Wächter: würde ein Production-Callsite das dritte Argument
    // weglassen (Default {}), bliebe "stale-x" quiet und dieser Test bewiese
    // nichts — deshalb hier explizit die 3-Argument-Form mit Ordnungseffekt.
    const projects = [
      projectFixture("quiet-y"),
      projectFixture("stale-x"),
    ];
    const withoutStale = sortProjectsByAttention(projects, { "quiet-y": 0, "stale-x": 0 });
    expect(withoutStale.map((p) => p.slug)).toEqual(["quiet-y", "stale-x"]);
    const withStale = sortProjectsByAttention(
      projects,
      { "quiet-y": 0, "stale-x": 0 },
      { "stale-x": 2 },
    );
    expect(withStale.map((p) => p.slug)).toEqual(["stale-x", "quiet-y"]);
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
