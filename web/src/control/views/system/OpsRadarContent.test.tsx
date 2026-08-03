// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";
import { OpsRadarContent } from "./OpsRadarContent";
import type { OperatorInventoryResponse, OperatorInventoryWorktree } from "../../lib/types";

const inventory: OperatorInventoryResponse = {
  schema: "hermes-operator-inventory-v1",
  checked_at: 1782070000,
  summary: {
    worktrees_total: 86,
    worktrees_locked: 60,
    worktrees_dirty: 2,
    worktrees_prunable: 0,
    worktrees_orphaned: 1,
    worktrees_status_unknown: 0,
    actors_total: 4,
    actors_canonical: 1,
  },
  next_lever: { action: "inspect_dirty_worktrees", label: "Dirty Worktrees", detail: "2 Worktrees haben echte Git-Aenderungen.", tone: "amber", count: 2, target: "/control/system?filter=dirty", mutation: "none" },
  levers: [
    { action: "inspect_dirty_worktrees", label: "Dirty Worktrees", detail: "2 Worktrees haben echte Git-Aenderungen.", tone: "amber", count: 2, target: "/control/system?filter=dirty", mutation: "none" },
    { action: "inspect_orphan_worktrees", label: "Worktree ohne Worker", detail: "1 Kanban-Worktree hat keinen aktiven Worker-Match.", tone: "rose", count: 1, target: "/control/system?filter=orphaned", mutation: "none" },
  ],
  worktrees: [
    { id: "main:main checkout", path_label: "main checkout", branch: "main", head: "abc123", relation: "main", task_hint: null, state: "clean", locked: false, prunable: false, detached: false, dirty_count: 0, untracked_count: 0, status_checked: true, orphaned: false },
    { id: "kanban:t_123", path_label: "kanban:t_123", branch: "kanban/t_123", head: "def456", relation: "kanban", task_hint: "t_123", state: "dirty", locked: true, prunable: false, detached: false, dirty_count: 3, untracked_count: 1, status_checked: true, orphaned: true },
  ],
  actors: [
    { role: "kanban_worker", label: "Kanban Worker", count: 1, cpu_percent: null, rss_mb: null, oldest_age_seconds: 500, source: "canonical", confidence: "high", stale_count: 0, target: "/control/fleet", controllable: false },
    { role: "codex", label: "Codex", count: 2, cpu_percent: 12.5, rss_mb: 512, oldest_age_seconds: 120, source: "process", confidence: "medium", stale_count: 0, target: "/control/system", controllable: false },
    { role: "claude_code", label: "Claude Code", count: 1, cpu_percent: 4, rss_mb: 256, oldest_age_seconds: 90, source: "process", confidence: "medium", stale_count: 0, target: "/control/system", controllable: false },
  ],
  errors: [],
};

afterEach(() => { cleanup(); });

describe("OpsRadarContent", () => {
  it("renders real inventory levers, worktree ledger, and actor map without raw data", () => {
    const html = renderToStaticMarkup(<MemoryRouter><OpsRadarContent data={inventory} lastUpdated={1782070000} isStale={false} /></MemoryRouter>);

    expect(html).toContain("Ops Radar");
    expect(html).toContain("86 total");
    expect(html).toContain("60 locked");
    expect(html).toContain("Top-Hebel");
    expect(html).toContain("Dirty Worktrees");
    expect(html).toContain("Worktree-Ledger");
    expect(html).toContain("kanban:t_123");
    expect(html).toContain("Actor Map");
    expect(html).toContain("Kanban Worker");
    expect(html).toContain("Claude Code");
    expect(html).toContain("read-only");
    expect(html).not.toContain("/home/");
    expect(html).not.toContain(".worktrees/");
    expect(html.toLowerCase()).not.toContain("cmdline");
    // KpiTile keeps plain values in the data face and the Sheet-A ink token.
    expect(html).toContain(">CPU</p>");
    expect(html).toContain(">RAM</p>");
    expect((html.match(/font-data text-lg font-semibold tabular-nums text-ink">-<\/div>/g) ?? []).length).toBeGreaterThanOrEqual(2);
    expect(html.toLowerCase()).not.toContain("stop");
    expect(html.toLowerCase()).not.toContain("kill");
    expect(html.toLowerCase()).not.toContain("update");
  });

  it("renders real levers with accessible links when embedded in SystemView", () => {
    const html = renderToStaticMarkup(<MemoryRouter><OpsRadarContent embedded data={inventory} lastUpdated={1782070000} isStale={false} /></MemoryRouter>);

    expect(html).toContain("Echte Hebel");
    expect(html).toContain("Dirty Worktrees");
    expect(html).toContain("Worktree ohne Worker");
    expect(html).toContain('/control/system?filter=dirty');
    expect(html).toContain('/control/system?filter=orphaned');
  });

  it("does not leave a loading skeleton behind after a terminal load error", () => {
    const html = renderToStaticMarkup(<MemoryRouter><OpsRadarContent data={null} lastUpdated={null} error="offline" /></MemoryRouter>);

    expect(html).toContain("Ops Radar konnte nicht geladen werden: offline");
    expect(html).not.toContain("hc-skeleton");
    expect(html).not.toContain('aria-busy="true"');
  });
});

// ── Router-basierte Tests: Filter, Legacy-Normalisierung, Fokus ─────────────

function wt(overrides: Partial<OperatorInventoryWorktree> & { id: string; path_label: string; state: OperatorInventoryWorktree["state"] }): OperatorInventoryWorktree {
  return {
    branch: `branch/${overrides.id}`,
    head: "aaa111",
    relation: "kanban",
    task_hint: null,
    locked: false,
    prunable: false,
    detached: false,
    dirty_count: 0,
    untracked_count: 0,
    status_checked: true,
    orphaned: false,
    ...overrides,
  };
}

const richInventory: OperatorInventoryResponse = {
  schema: "hermes-operator-inventory-v1",
  checked_at: 1782070000,
  summary: { worktrees_total: 5, worktrees_locked: 1, worktrees_dirty: 1, worktrees_prunable: 0, worktrees_orphaned: 1, worktrees_status_unknown: 1, actors_total: 2, actors_canonical: 1 },
  next_lever: { action: "inspect_dirty", label: "Dirty", detail: "1 dirty", tone: "amber", count: 1, target: "/control/ops?filter=dirty", mutation: "none" },
  levers: [
    { action: "inspect_dirty", label: "Dirty Worktrees", detail: "1 Worktree hat echte Git-Aenderungen.", tone: "amber", count: 1, target: "/control/ops?filter=dirty", mutation: "none" },
    { action: "inspect_locked", label: "Locked Worktrees", detail: "1 Worktree ist gelockt.", tone: "cyan", count: 1, target: "/control/ops?filter=locked", mutation: "none" },
    { action: "inspect_unknown", label: "Inventar unvollstaendig", detail: "1 Statusprobe unklar.", tone: "amber", count: 1, target: "/control/ops?filter=unknown", mutation: "none" },
    { action: "inspect_agents", label: "Agenten ohne Kanban-Match", detail: "2 Agent-Prozesse.", tone: "rose", count: 2, target: "/control/ops?filter=agents", mutation: "none" },
    { action: "inspect_tests", label: "Tests laufen", detail: "3 Test-Prozesse aktiv.", tone: "amber", count: 3, target: "/control/pressure", mutation: "none" },
  ],
  worktrees: [
    wt({ id: "wt-dirty", path_label: "dirty-tree", state: "dirty", dirty_count: 3 }),
    wt({ id: "wt-clean", path_label: "clean-tree", state: "clean" }),
    wt({ id: "wt-locked", path_label: "locked-tree", state: "locked", locked: true }),
    wt({ id: "wt-unknown", path_label: "unknown-tree", state: "unknown", status_checked: false }),
    wt({ id: "wt-orphan", path_label: "orphan-tree", state: "dirty", orphaned: true }),
  ],
  actors: [
    { role: "kanban_worker", label: "Kanban Worker", count: 1, cpu_percent: null, rss_mb: null, oldest_age_seconds: 500, source: "canonical", confidence: "high", stale_count: 0, target: "/control/fleet", controllable: false },
    { role: "codex", label: "Codex Agent", count: 2, cpu_percent: 12, rss_mb: 512, oldest_age_seconds: 120, source: "process", confidence: "medium", stale_count: 0, target: "/control/system", controllable: false },
  ],
  errors: [],
};

function renderWithRouter(entry: string) {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <OpsRadarContent embedded data={richInventory} lastUpdated={1782070000} isStale={false} />
    </MemoryRouter>,
  );
}

describe("OpsRadarContent – URL-Filter und Legacy-Normalisierung", () => {
  it("filter=dirty zeigt nur Dirty-Zeilen und keine clean/locked", () => {
    renderWithRouter("/control/system?filter=dirty#worktree-ledger");

    expect(screen.getByText("dirty-tree")).toBeTruthy();
    expect(screen.getByText("orphan-tree")).toBeTruthy();
    expect(screen.queryByText("clean-tree")).toBeNull();
    expect(screen.queryByText("locked-tree")).toBeNull();
    expect(screen.queryByText("unknown-tree")).toBeNull();
  });

  it("filter=locked zeigt nur Locked-Zeilen", () => {
    renderWithRouter("/control/system?filter=locked#worktree-ledger");

    expect(screen.getByText("locked-tree")).toBeTruthy();
    expect(screen.queryByText("dirty-tree")).toBeNull();
    expect(screen.queryByText("clean-tree")).toBeNull();
  });

  it("filter=unknown zeigt nur Unknown-Zeilen", () => {
    renderWithRouter("/control/system?filter=unknown#worktree-ledger");

    expect(screen.getByText("unknown-tree")).toBeTruthy();
    expect(screen.queryByText("dirty-tree")).toBeNull();
    expect(screen.queryByText("clean-tree")).toBeNull();
  });

  it("filter=orphaned zeigt nur Orphan-Zeilen", () => {
    renderWithRouter("/control/system?filter=orphaned#worktree-ledger");

    expect(screen.getByText("orphan-tree")).toBeTruthy();
    expect(screen.queryByText("dirty-tree")).toBeNull();
    expect(screen.queryByText("clean-tree")).toBeNull();
  });

  it("ohne Filter bleiben alle Fixture-Zeilen sichtbar", () => {
    renderWithRouter("/control/system");

    expect(screen.getByText("dirty-tree")).toBeTruthy();
    expect(screen.getByText("clean-tree")).toBeTruthy();
    expect(screen.getByText("locked-tree")).toBeTruthy();
    expect(screen.getByText("unknown-tree")).toBeTruthy();
    expect(screen.getByText("orphan-tree")).toBeTruthy();
  });

  it("filter=agents behält alle Actor-Zeilen", () => {
    renderWithRouter("/control/system?filter=agents#actor-map");

    expect(screen.getByText("Kanban Worker")).toBeTruthy();
    expect(screen.getByText("Codex Agent")).toBeTruthy();
    expect(screen.getByText("dirty-tree")).toBeTruthy();
    expect(screen.getByText("clean-tree")).toBeTruthy();
  });

  it("Legacy-Hebel /control/ops?filter=dirty wird zu /control/system?filter=dirty#worktree-ledger", () => {
    renderWithRouter("/control/system");

    const link = screen.getByRole("link", { name: /Dirty Worktrees/ });
    expect(link.getAttribute("href")).toBe("/control/system?filter=dirty#worktree-ledger");
  });

  it("Legacy-Hebel /control/pressure wird zu /control/system#pressure-sources", () => {
    renderWithRouter("/control/system");

    const link = screen.getByRole("link", { name: /Tests laufen/ });
    expect(link.getAttribute("href")).toBe("/control/system#pressure-sources");
  });

  it("Fokus-Hinweis mit Zurücksetzen-Aktion bei aktivem Filter", () => {
    renderWithRouter("/control/system?filter=dirty#worktree-ledger");

    const resetLink = screen.getByRole("link", { name: /Fokus aufheben|Zurücksetzen/ });
    expect(resetLink).toBeTruthy();
    expect(resetLink.getAttribute("href")).toBe("/control/system");
  });

  it("Worktree-Ledger und Actor Map tragen stabile Sprungziel-IDs", () => {
    const { container } = renderWithRouter("/control/system");

    expect(container.querySelector("#worktree-ledger")).toBeTruthy();
    expect(container.querySelector("#actor-map")).toBeTruthy();
  });
});
