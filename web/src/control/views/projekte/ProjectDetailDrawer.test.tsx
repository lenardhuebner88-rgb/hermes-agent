// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { configure } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { parseOrThrow, ProjectDetailResponseSchema } from "../../lib/schemas";

configure({ asyncUtilTimeout: 5000 });

// Real-shaped GET /api/projects/{slug} payload (frozen contract from
// hermes_cli/projects_overview.build_project_detail).
const REAL_DETAIL_PAYLOAD = {
  generated_at: 1784240000,
  slug: "hermes-infra",
  name: "Hermes Infra",
  repo_path: "/home/piet/.hermes/hermes-agent",
  parent: null,
  links: [{ label: "Control-Dashboard", url: "/control" }],
  recent_commits: [
    {
      hash: "9d8fa62d8",
      message: "projekte-tab: Stufe 6 — drilldown",
      author: "Claude Builder",
      committed_at: 1784239900,
      age_seconds: 100,
      attribution: {
        kind: "kanban",
        pack: null,
        task_id: "t_1a2b3c4d",
        lane: "grok-builder",
        model: "grok-4.5",
        label: null,
      },
    },
    {
      hash: "abc123def",
      message: "projekte-tab: Stufe 5 — agents rail",
      author: "Git Author",
      committed_at: 1784239000,
      age_seconds: 1000,
      attribution: null,
    },
  ],
  kanban_tasks: [
    {
      id: "t_block",
      title: "Needs operator input",
      status: "blocked",
      block_kind: "needs_input",
      priority: 5,
      created_at: 1784238000,
      age_seconds: 2000,
    },
    {
      id: "t_open",
      title: "Open task",
      status: "todo",
      block_kind: null,
      priority: 1,
      created_at: 1784237000,
      age_seconds: 3000,
    },
  ],
  loops: [
    {
      name: "builder-reviewer",
      running: false,
      last_heartbeat_at: 1784235000,
      last_outcome: {
        verdict: "landed",
        phase: "land",
        reason: "main=d38529e17",
        plan: "P1-ship.md",
        ts: 1784234000,
      },
    },
  ],
  agents: [
    {
      kind: "kimi",
      label: "work:2 kimi",
      task: null,
      since: 1784238000,
      source: "tmux",
    },
  ],
  // Stage 12: neueste Receipts dieses Projekts (≤5, gleiche Zeilenform wie
  // der Cross-Agent-Feed).
  receipts: [
    {
      agent: "Codex",
      filename: "2026-07-17-b3-parser-receipt.md",
      title: "B3 coordination parser drift receipt",
      mtime: "2026-07-17T21:04:11+00:00",
      age_seconds: 12600,
      project: "hermes-infra",
      excerpt: "status: blocked",
    },
    {
      agent: "Claude-Code",
      filename: "2026-07-17-projekte-feed-receipt.md",
      title: "Projekte receipts feed frontend",
      mtime: "2026-07-17T20:34:11+00:00",
      age_seconds: 14400,
      project: "hermes-infra",
      excerpt: null,
    },
  ],
  errors: ["git: sample isolation"],
};

// Real-shaped detail payload for the additive task correlation (backend
// 2026-07-17): the tmux agent carries @hermes_session_id/@hermes_task_id plus
// the resolved kanban task title in `task`.
const CORRELATED_DETAIL_PAYLOAD = {
  ...REAL_DETAIL_PAYLOAD,
  agents: [
    {
      kind: "kimi",
      label: "work:3 kimi",
      task: "B1-Frontend: Task-Korrelation im LiveBoard",
      since: 1784239000,
      source: "tmux",
      assignee: null,
      operator: null,
      session_id: "s_9f8e7d6c5b",
      task_id: "t_b1frontend",
    },
  ],
};

const { hookState } = vi.hoisted(() => ({
  hookState: {
    data: null as import("../../lib/schemas").ProjectDetail | null,
    loading: true,
    error: null as string | null,
  },
}));

vi.mock("../../hooks/useControlData", () => ({
  useProjectDetail: vi.fn(() => ({
    data: hookState.data,
    loading: hookState.loading,
    error: hookState.error,
    errorObj: null,
    isStale: false,
    lastUpdated: null,
    reload: vi.fn(),
  })),
  // ReceiptSheet (öffnet sich aus der Receipts-Sektion) holt den Inhalt über
  // denselben Barrel — Skeleton-Zustand genügt für den Öffnen-Test.
  useProjectReceipt: vi.fn(() => ({
    data: null,
    loading: true,
    error: null,
    errorObj: null,
    isStale: false,
    lastUpdated: null,
    reload: vi.fn(),
  })),
}));

import { ProjectDetailBody, ProjectDetailDrawer } from "./ProjectDetailDrawer";

afterEach(() => {
  cleanup();
  hookState.data = null;
  hookState.loading = true;
  hookState.error = null;
});

describe("ProjectDetailResponseSchema (real detail fixture)", () => {
  it("parses the frozen detail payload shape", () => {
    const parsed = parseOrThrow(ProjectDetailResponseSchema, REAL_DETAIL_PAYLOAD, "detail");
    expect(parsed.slug).toBe("hermes-infra");
    expect(parsed.recent_commits).toHaveLength(2);
    expect(parsed.receipts).toHaveLength(2);
    expect(parsed.receipts[0].agent).toBe("Codex");
    expect(parsed.receipts[1].excerpt).toBeNull();
    expect(parsed.kanban_tasks?.[0].block_kind).toBe("needs_input");
    expect(parsed.loops[0].last_outcome?.verdict).toBe("landed");
    expect(parsed.agents[0].kind).toBe("kimi");
    expect(parsed.errors).toEqual(["git: sample isolation"]);
    // Legacy payload without the additive correlation fields parses unchanged.
    expect(parsed.agents[0].session_id).toBeNull();
    expect(parsed.agents[0].task_id).toBeNull();
  });

  it("parses the additive session/task correlation fields on tmux rows", () => {
    const parsed = parseOrThrow(ProjectDetailResponseSchema, CORRELATED_DETAIL_PAYLOAD, "detail-correlated");
    expect(parsed.agents[0].session_id).toBe("s_9f8e7d6c5b");
    expect(parsed.agents[0].task_id).toBe("t_b1frontend");
    expect(parsed.agents[0].task).toBe("B1-Frontend: Task-Korrelation im LiveBoard");
  });

  it("accepts the unknown-slug error body without throwing", () => {
    const parsed = parseOrThrow(
      ProjectDetailResponseSchema,
      { error: "unknown project", slug: "ghost" },
      "detail-404",
    );
    expect(parsed.error).toBe("unknown project");
    expect(parsed.slug).toBe("ghost");
    expect(parsed.recent_commits).toEqual([]);
    expect(parsed.kanban_tasks).toBeNull();
  });
});

describe("ProjectDetailBody (loaded fixture)", () => {
  it("renders commits, kanban, loops, agents and source errors", () => {
    const data = parseOrThrow(ProjectDetailResponseSchema, REAL_DETAIL_PAYLOAD, "detail");
    render(<ProjectDetailBody data={data} now={1784240000} />);

    expect(screen.getByText("projekte-tab: Stufe 6 — drilldown")).toBeTruthy();
    expect(screen.getByText("9d8fa62d8")).toBeTruthy();
    expect(screen.getByText("grok-builder · grok-4.5")).toBeTruthy();
    expect(screen.getByText("Git Author")).toBeTruthy();
    expect(screen.getByText("Needs operator input")).toBeTruthy();
    expect(screen.getByText("needs_input")).toBeTruthy();
    expect(screen.getByText("builder-reviewer")).toBeTruthy();
    expect(screen.getByText("landed")).toBeTruthy();
    expect(screen.getByText(/main=d38529e17/)).toBeTruthy();
    expect(screen.getByText("Kimi")).toBeTruthy();
    expect(screen.getByText("git: sample isolation")).toBeTruthy();
    expect(screen.getByText("Control-Dashboard")).toBeTruthy();
  });

  it("shows the task-id chip in the agent meta line when the backend resolved one", () => {
    const data = parseOrThrow(ProjectDetailResponseSchema, CORRELATED_DETAIL_PAYLOAD, "detail-correlated");
    render(<ProjectDetailBody data={data} now={1784240000} />);
    const chip = screen.getByText("t_b1frontend");
    expect(chip.getAttribute("title")).toBe("t_b1frontend");
  });

  it("renders the receipts section with the shared row shape, no project chip", () => {
    const data = parseOrThrow(ProjectDetailResponseSchema, REAL_DETAIL_PAYLOAD, "detail");
    render(<ProjectDetailBody data={data} now={1784240000} />);
    expect(screen.getByText("Receipts")).toBeTruthy();
    expect(screen.getByText("B3 coordination parser drift receipt")).toBeTruthy();
    expect(screen.getByText("Projekte receipts feed frontend")).toBeTruthy();
    expect(screen.getByText("Codex")).toBeTruthy();
    // Drawer-Zeilen sind schon slug-scoped → kein Projekt-Chip, kein Projekt-
    // name irgendwo im Body.
    expect(screen.queryByText("Hermes Infra")).toBeNull();
  });

  it("shows honest empty states when lists are empty", () => {
    const data = parseOrThrow(
      ProjectDetailResponseSchema,
      {
        generated_at: 1,
        slug: "empty",
        name: "Empty",
        repo_path: "/tmp/empty",
        parent: null,
        links: [],
        recent_commits: [],
        kanban_tasks: null,
        loops: [],
        agents: [],
        errors: [],
      },
      "empty",
    );
    render(<ProjectDetailBody data={data} now={10} />);
    expect(screen.getByText(deEmpty("detailNoCommits"))).toBeTruthy();
    expect(screen.getByText(deEmpty("detailNoKanban"))).toBeTruthy();
    expect(screen.getByText(deEmpty("detailNoLoops"))).toBeTruthy();
    expect(screen.getByText(deEmpty("detailNoAgents"))).toBeTruthy();
    // Stage 12: fehlende Receipts bleiben ein ruhiger Leerzustand.
    expect(screen.getByText("Keine Receipts für dieses Projekt.")).toBeTruthy();
  });
});

function deEmpty(key: "detailNoCommits" | "detailNoKanban" | "detailNoLoops" | "detailNoAgents"): string {
  // Inline the German strings so the test stays colocated with the drawer
  // without importing the full i18n tree (already covered by the render).
  const map = {
    detailNoCommits: "Keine Commits gefunden.",
    detailNoKanban: "Kein Kanban-Board für dieses Projekt.",
    detailNoLoops: "Keine Loop-Packs registriert.",
    detailNoAgents: "Keine Agents an diesem Projekt.",
  };
  return map[key];
}

describe("ProjectDetailDrawer (loading / error / loaded)", () => {
  it("shows loading skeleton when no data yet", () => {
    hookState.loading = true;
    hookState.data = null;
    hookState.error = null;
    render(<ProjectDetailDrawer slug="hermes-infra" onClose={() => undefined} />);
    // SkeletonCard sets aria-busy
    expect(document.querySelector("[aria-busy='true']")).toBeTruthy();
  });

  it("shows error banner when the hook reports an error", () => {
    hookState.loading = false;
    hookState.data = null;
    hookState.error = "404: unknown project";
    render(<ProjectDetailDrawer slug="ghost" onClose={() => undefined} />);
    expect(screen.getByRole("alert")).toBeTruthy();
    expect(screen.getByText("Projektdetails konnten nicht geladen werden.")).toBeTruthy();
  });

  it("renders loaded detail inside DrawerShell dialog", () => {
    hookState.loading = false;
    hookState.error = null;
    hookState.data = parseOrThrow(ProjectDetailResponseSchema, REAL_DETAIL_PAYLOAD, "detail");
    render(<ProjectDetailDrawer slug="hermes-infra" onClose={() => undefined} />);
    expect(screen.getByRole("dialog")).toBeTruthy();
    expect(screen.getByText("Hermes Infra")).toBeTruthy();
    expect(screen.getByText("projekte-tab: Stufe 6 — drilldown")).toBeTruthy();
  });

  it("opens the shared receipt reader sheet from the receipts section", () => {
    hookState.loading = false;
    hookState.error = null;
    hookState.data = parseOrThrow(ProjectDetailResponseSchema, REAL_DETAIL_PAYLOAD, "detail");
    render(<ProjectDetailDrawer slug="hermes-infra" onClose={() => undefined} />);
    fireEvent.click(
      screen.getByRole("button", { name: "Receipt B3 coordination parser drift receipt öffnen" }),
    );
    // Drawer + Lese-Sheet liegen als zwei gestapelte Dialoge übereinander;
    // das Sheet zeigt sofort Titel + Agent aus der Zeile (Body lädt).
    const dialogs = screen.getAllByRole("dialog");
    expect(dialogs).toHaveLength(2);
    expect(dialogs[1].textContent).toContain("B3 coordination parser drift receipt");
    expect(dialogs[1].textContent).toContain("Codex");
  });
});
