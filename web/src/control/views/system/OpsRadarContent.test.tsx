import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { OpsRadarContent } from "./OpsRadarContent";
import type { OperatorInventoryResponse } from "../../lib/types";

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

describe("OpsRadarContent", () => {
  it("renders real inventory levers, worktree ledger, and actor map without raw data", () => {
    const html = renderToStaticMarkup(<OpsRadarContent data={inventory} lastUpdated={1782070000} isStale={false} />);

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

  it("does not leave a loading skeleton behind after a terminal load error", () => {
    const html = renderToStaticMarkup(<OpsRadarContent data={null} lastUpdated={null} error="offline" />);

    expect(html).toContain("Ops Radar konnte nicht geladen werden: offline");
    expect(html).not.toContain("hc-skeleton");
    expect(html).not.toContain('aria-busy="true"');
  });
});
