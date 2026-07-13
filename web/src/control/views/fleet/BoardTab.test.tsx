// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { BoardResponse, BoardTask } from "../../lib/types";
import { BoardTab } from "./BoardTab";

afterEach(cleanup);

function task(overrides: Partial<BoardTask> = {}): BoardTask {
  return {
    id: "t_truth01",
    title: "Operator truth card",
    status: "blocked",
    assignee: "premium-reviewer",
    priority: 7,
    created_at: 1_783_800_000,
    started_at: 1_783_800_100,
    completed_at: 1_783_800_200,
    due_at: 1_783_900_000,
    last_heartbeat_at: 1_783_800_150,
    branch_name: null,
    latest_summary: null,
    link_counts: { parents: 2, children: 4 },
    comment_count: 3,
    progress: { done: 1, total: 4 },
    age: null,
    tenant: null,
    root_id: "t_truth01",
    epic_id: null,
    ...overrides,
  };
}

function board(tasks: BoardTask[]): BoardResponse {
  return {
    columns: [{ name: "blocked", tasks }],
    tenants: [],
    assignees: tasks.flatMap((item) => item.assignee ? [item.assignee] : []),
    latest_event_id: 1,
    source_errors: [],
    now: 1_783_800_300,
  };
}

describe("BoardTab operator information", () => {
  it("keeps all material card fields discoverable on a read-only board", () => {
    render(
      <BoardTab
        board={board([task()])}
        readOnly
        onOpenNodeDetail={vi.fn()}
      />,
    );

    const row = screen.getByText("Operator truth card").closest(".fleet-boardtab-row");
    expect(row).not.toBeNull();
    const rowQueries = within(row as HTMLElement);
    expect(rowQueries.getByText("premium-reviewer")).toBeTruthy();
    expect(rowQueries.getByText("Prio 7")).toBeTruthy();
    expect(rowQueries.getByText("3 Kommentare")).toBeTruthy();
    expect(rowQueries.getByText("2 Vorgänger")).toBeTruthy();
    expect(rowQueries.getByText("4 Nachfolger")).toBeTruthy();
    expect(rowQueries.getByText("1/4")).toBeTruthy();

    const summary = screen.getByLabelText("Weitere Informationen zu Operator truth card");
    expect(summary.tagName).toBe("SUMMARY");
    fireEvent.click(summary);
    const disclosure = summary.parentElement as HTMLElement;
    for (const label of ["Erstellt", "Gestartet", "Fertig", "Fällig", "Heartbeat"]) {
      expect(within(disclosure).getByText(label)).toBeTruthy();
    }
  });

  it("does not invent zero-value metadata for absent card information", () => {
    render(
      <BoardTab
        board={board([task({
          id: "t_empty01",
          title: "Sparse card",
          assignee: null,
          priority: 0,
          started_at: null,
          completed_at: null,
          due_at: null,
          last_heartbeat_at: null,
          link_counts: { parents: 0, children: 0 },
          comment_count: 0,
          progress: null,
          root_id: "t_empty01",
        })])}
        onOpenNodeDetail={vi.fn()}
      />,
    );

    const row = screen.getByText("Sparse card").closest(".fleet-boardtab-row");
    expect(row).not.toBeNull();
    const text = row?.textContent ?? "";
    expect(text).not.toContain("Prio 0");
    expect(text).not.toContain("0 Kommentare");
    expect(text).not.toContain("0 Vorgänger");
    expect(text).not.toContain("0 Nachfolger");
  });
});
