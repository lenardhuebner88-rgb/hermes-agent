// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { PlanSpecTransitionPreview } from "../../lib/schemas";
import type { PlanSpecRecord } from "./shared";
import { PlanSpecTransitionPanel } from "./PlanSpecTransitionPanel";

function item(overrides: Partial<PlanSpecRecord> = {}): PlanSpecRecord {
  return {
    path: "vault/03-Agents/Codex/plans/transition.md",
    agent: "codex",
    filename: "transition.md",
    topic: "Saubere Übergabe",
    status: "open",
    freigabe: "operator",
    live_test_depth: "smoke",
    binding: true,
    subtask_count: 9,
    valid: true,
    open: true,
    closed_reason: null,
    kanban_root_task_id: null,
    kanban_root_status: null,
    kanban_state: "not_ingested",
    kanban_child_total: 0,
    kanban_child_done: 0,
    kanban_child_blocked: 0,
    kanban_child_running: 0,
    kanban_ingested_at: null,
    ingest_disposition: "clean",
    ingest_would_block: false,
    ingest_findings: [],
    errors: [],
    action_state: "ready",
    action_reason: "bereit",
    dependency_count: 10,
    finding_count: 0,
    target_board: "hermes-agent",
    next_action: "preview_ingest",
    ...overrides,
  };
}

function preview(overrides: Partial<PlanSpecTransitionPreview> = {}): PlanSpecTransitionPreview {
  return {
    source_digest: "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    action: "create_held_chain",
    target_board: "hermes-agent",
    root_cards_created: 1,
    child_tasks_created: 9,
    starts_worker: false,
    live_test_depth: "smoke",
    workspace_mode: "isolated_worktree",
    review_floor: "review",
    auto_scout_tasks: 2,
    lanes_resolvable: true,
    can_ingest: true,
    blocking_findings: [],
    warnings: [],
    ...overrides,
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("PlanSpecTransitionPanel", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    Object.defineProperty(window, "__HERMES_SESSION_TOKEN__", {
      configurable: true,
      value: "test-token",
    });
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("shows the exact read-only transition preview and blocks an invalid ingest", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(preview({
      can_ingest: false,
      lanes_resolvable: false,
      blocking_findings: ["Lane research ist nicht auflösbar"],
      warnings: ["Live-Test-Tiefe wurde auf smoke normalisiert"],
    })));
    render(
      <PlanSpecTransitionPanel
        item={item()}
        boardSlug="default"
        readOnly={false}
        onChanged={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Übergabe prüfen" }));
    expect(await screen.findByText(/1 Root · 9 Tasks auf hermes-agent/)).toBeTruthy();
    expect(screen.getByText("Kein Worker startet ohne separate Freigabe.")).toBeTruthy();
    expect(screen.getByText("isolated_worktree")).toBeTruthy();
    expect(screen.getByText("Lane research ist nicht auflösbar")).toBeTruthy();
    expect(screen.getByText("Live-Test-Tiefe wurde auf smoke normalisiert")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Als gehaltene Kette übergeben" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("pins ingest to the previewed source digest", async () => {
    const onChanged = vi.fn();
    const cleanPreview = preview();
    fetchMock
      .mockResolvedValueOnce(jsonResponse(cleanPreview))
      .mockResolvedValueOnce(detailWithSubtasks([]))
      .mockResolvedValueOnce(jsonResponse({ root_task_id: "t_root", child_ids: ["t_child"] }));
    render(
      <PlanSpecTransitionPanel
        item={item()}
        boardSlug="default"
        readOnly={false}
        onChanged={onChanged}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Übergabe prüfen" }));
    fireEvent.click(await screen.findByRole("button", { name: "Als gehaltene Kette übergeben" }));

    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
    const ingestCall = fetchMock.mock.calls.find(([url]) => String(url).includes("/planspecs/ingest"));
    expect(String(ingestCall?.[0])).toBe("/api/plugins/kanban/planspecs/ingest?board=default");
    expect(JSON.parse(String((ingestCall?.[1] as RequestInit).body))).toEqual({
      path: item().path,
      source_digest: cleanPreview.source_digest,
    });
    expect(screen.getByRole("status").textContent).toContain("gehaltene Kette");
  });

  it("releases an ingested held chain and keeps foreign boards inert", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ ok: true }));
    const held = item({
      kanban_root_task_id: "t_root",
      kanban_root_status: "scheduled",
      kanban_state: "queued",
      action_state: "held",
      next_action: "release",
    });
    const first = render(
      <PlanSpecTransitionPanel
        item={held}
        boardSlug="default"
        readOnly={false}
        onChanged={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Kette freigeben" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      "/api/plugins/kanban/planspecs/approve?board=default",
      expect.objectContaining({ method: "POST" }),
    ));
    first.unmount();

    render(
      <PlanSpecTransitionPanel
        item={held}
        boardSlug="foreign"
        readOnly
        onChanged={vi.fn()}
      />,
    );
    expect(screen.getByText("Fremd-Board · Übergabe und Freigabe sind deaktiviert.")).toBeTruthy();
    expect(screen.queryByRole("button")).toBeNull();
  });

  function detailWithSubtasks(
    subtasks: Array<{ id: string; title: string; lane: string; review_tier?: string | null }>,
  ): Response {
    return jsonResponse({
      goal: "Testziel",
      subtasks: subtasks.map((t) => ({ ...t, deps: [] })),
    });
  }

  it("renders the subtask titles from the detail payload in the preview", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse(preview({ child_tasks_created: 2 })))
      .mockResolvedValueOnce(detailWithSubtasks([
        { id: "s1", title: "Recherche API-Anbieter", lane: "research", review_tier: "review" },
        { id: "s2", title: "Integration Zahlungsanbieter", lane: "coder", review_tier: null },
      ]));
    render(
      <PlanSpecTransitionPanel
        item={item()}
        boardSlug="default"
        readOnly={false}
        onChanged={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Übergabe prüfen" }));
    expect(await screen.findByText(/Recherche API-Anbieter · research · Review review/)).toBeTruthy();
    expect(screen.getByText(/Integration Zahlungsanbieter · coder/)).toBeTruthy();
  });

  it("truncates the subtask list beyond ten entries", async () => {
    const eleven = Array.from({ length: 11 }, (_, i) => ({
      id: `s${i + 1}`,
      title: `Task ${i + 1}`,
      lane: "coder",
      review_tier: null,
    }));
    fetchMock
      .mockResolvedValueOnce(jsonResponse(preview({ child_tasks_created: 11 })))
      .mockResolvedValueOnce(detailWithSubtasks(eleven));
    render(
      <PlanSpecTransitionPanel
        item={item()}
        boardSlug="default"
        readOnly={false}
        onChanged={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Übergabe prüfen" }));
    expect(await screen.findByText(/Task 1 · coder/)).toBeTruthy();
    expect(screen.getByText(/Task 10 · coder/)).toBeTruthy();
    expect(screen.queryByText(/Task 11/)).toBeNull();
    expect(screen.getByText("… und 1 weitere")).toBeTruthy();
  });

  it("keeps the counter view when the detail has no subtasks", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse(preview()))
      .mockResolvedValueOnce(detailWithSubtasks([]));
    render(
      <PlanSpecTransitionPanel
        item={item()}
        boardSlug="default"
        readOnly={false}
        onChanged={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Übergabe prüfen" }));
    expect(await screen.findByText(/1 Root · 9 Tasks auf hermes-agent/)).toBeTruthy();
    expect(screen.queryByRole("list")).toBeNull();
  });
});
