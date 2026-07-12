// @vitest-environment jsdom

import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { _resetPollingStore } from "./pollingStore";
import { useBoard } from "./useControlData";

const api = vi.hoisted(() => ({ fetchJSON: vi.fn() }));
vi.mock("@/lib/api", () => api);

function boardPayload(id: string, title: string) {
  return {
    columns: [
      {
        name: "scheduled",
        tasks: [
          {
            id,
            title,
            status: "scheduled",
            assignee: null,
            priority: 0,
            created_at: 1_783_884_263,
            started_at: null,
            completed_at: null,
            branch_name: null,
            latest_summary: null,
            link_counts: { parents: 0, children: 0 },
            comment_count: 0,
            progress: null,
            age: { created_age_seconds: 10, started_age_seconds: null, time_to_complete_seconds: null },
            tenant: null,
            root_id: id,
            epic_id: null,
          },
        ],
      },
    ],
    tenants: [],
    assignees: [],
    latest_event_id: 1,
    source_errors: [],
    now: 1_783_884_273,
  };
}

describe("useBoard board-key isolation", () => {
  beforeEach(() => {
    _resetPollingStore();
    api.fetchJSON.mockReset();
  });

  afterEach(() => {
    cleanup();
    _resetPollingStore();
  });

  it("never exposes the previous board payload while the new board key is loading", async () => {
    let resolveHealth!: (value: unknown) => void;
    const healthPending = new Promise((resolve) => {
      resolveHealth = resolve;
    });
    const defaultPayload = boardPayload("t_default_root", "Default root");
    const healthPayload = boardPayload("t_health_root", "Health root");
    api.fetchJSON.mockImplementation((url: string) =>
      url.includes("board=health-track") ? healthPending : Promise.resolve(defaultPayload),
    );

    const renderSnapshots: Array<{ board: string | null; taskId: string | null }> = [];
    const { result, rerender } = renderHook(
      ({ board }) => {
        const value = useBoard(board);
        renderSnapshots.push({ board, taskId: value.data?.columns[0]?.tasks[0]?.id ?? null });
        return value;
      },
      { initialProps: { board: null as string | null } },
    );
    await waitFor(() => expect(result.current.data?.columns[0]?.tasks[0]?.id).toBe("t_default_root"));

    rerender({ board: "health-track" });

    expect(renderSnapshots.filter((snapshot) => snapshot.board === "health-track").map((snapshot) => snapshot.taskId)).not.toContain("t_default_root");
    expect(result.current.data).toBeNull();
    expect(result.current.loading).toBe(true);

    await act(async () => resolveHealth(healthPayload));
    await waitFor(() => expect(result.current.data?.columns[0]?.tasks[0]?.id).toBe("t_health_root"));
  });
});
