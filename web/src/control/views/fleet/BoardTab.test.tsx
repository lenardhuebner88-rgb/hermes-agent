// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { BoardArchiveResponse, BoardResponse, BoardTask } from "../../lib/types";
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
  it("shows every active filter in a resettable chip even when no tasks match", () => {
    render(<BoardTab board={board([task()])} onOpenNodeDetail={vi.fn()} />);

    fireEvent.change(screen.getByLabelText("Tasks durchsuchen"), { target: { value: "keine-treffer" } });
    fireEvent.change(screen.getByLabelText("Nach Status filtern"), { target: { value: "ready" } });
    fireEvent.change(screen.getByLabelText("Nach Assignee filtern"), { target: { value: "premium-reviewer" } });

    const chip = screen.getByRole("status", { name: "Aktive Board-Filter" });
    expect(within(chip).getByText(/Status: Startklar/)).toBeTruthy();
    expect(within(chip).getByText(/Assignee: premium-reviewer/)).toBeTruthy();
    expect(within(chip).getByText(/Suche: keine-treffer/)).toBeTruthy();

    fireEvent.click(within(chip).getByRole("button", { name: "Alle Filter zurücksetzen" }));
    expect(screen.queryByRole("status", { name: "Aktive Board-Filter" })).toBeNull();
    expect((screen.getByLabelText("Tasks durchsuchen") as HTMLInputElement).value).toBe("");
    expect((screen.getByLabelText("Nach Status filtern") as HTMLSelectElement).value).toBe("all");
    expect((screen.getByLabelText("Nach Assignee filtern") as HTMLSelectElement).value).toBe("all");
  });

  it("loads the archive separately, reports counts, and follows the cursor", async () => {
    const archivedOne = task({ id: "t_archive1", title: "Archive one", status: "archived", archived_at: 1_783_900_003 });
    const archivedTwo = task({ id: "t_archive2", title: "Archive two", status: "archived", archived_at: 1_783_900_002 });
    const archivedThree = task({ id: "t_archive3", title: "Archive three", status: "archived", archived_at: 1_783_900_001 });
    const page = (items: BoardTask[], nextCursor: string | null): BoardArchiveResponse => ({
      tasks: items,
      total_count: 3,
      filtered_count: 3,
      loaded_count: items.length,
      limit: 2,
      has_more: nextCursor !== null,
      next_cursor: nextCursor,
      query: "",
      assignee: null,
      assignees: ["premium-reviewer"],
      latest_event_id: 4,
      now: 1_783_900_004,
    });
    const loadArchivePage = vi.fn()
      .mockResolvedValueOnce(page([archivedOne, archivedTwo], "1783900002:t_archive2"))
      .mockResolvedValueOnce(page([archivedThree], null));
    render(
      <BoardTab
        board={board([task()])}
        boardSlug="default"
        loadArchivePage={loadArchivePage}
        onOpenNodeDetail={vi.fn()}
      />,
    );

    fireEvent.change(screen.getByLabelText("Nach Status filtern"), { target: { value: "archived" } });

    expect(await screen.findByText("Archive one")).toBeTruthy();
    expect(screen.queryByText("Operator truth card")).toBeNull();
    expect(screen.getByText("2 von 3 Archivkarten geladen")).toBeTruthy();
    expect(loadArchivePage).toHaveBeenCalledWith(
      { board: "default", q: "", assignee: null, limit: 50, cursor: null },
      expect.any(AbortSignal),
    );

    fireEvent.click(screen.getByRole("button", { name: "Weitere Archivkarten laden" }));
    expect(await screen.findByText("Archive three")).toBeTruthy();
    expect(screen.getByText("3 von 3 Archivkarten geladen")).toBeTruthy();
    await waitFor(() => expect(loadArchivePage).toHaveBeenLastCalledWith(
      { board: "default", q: "", assignee: null, limit: 50, cursor: "1783900002:t_archive2" },
      expect.any(AbortSignal),
    ));
  });

  it.each([
    ["long", "L".repeat(400)],
    ["rtl", "مرحبا بالعالم ".repeat(30)],
    ["combining", "e\u0301".repeat(200)],
    ["emoji", "👩🏽‍💻🚀".repeat(80)],
  ])("expands the complete %s title on tap", (_kind, title) => {
    render(<BoardTab board={board([task({ title })])} onOpenNodeDetail={vi.fn()} />);

    const titleNode = document.querySelector(".fleet-boardtab-title");
    expect(titleNode?.textContent).toBe(title);
    expect(titleNode?.getAttribute("title")).toBeNull();
    expect(titleNode?.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(titleNode as HTMLElement);
    expect(titleNode?.getAttribute("aria-expanded")).toBe("true");
  });

  it("uses the row drawer as the only detail affordance on a read-only board", () => {
    const onOpenNodeDetail = vi.fn();
    render(
      <BoardTab
        board={board([task()])}
        readOnly
        onOpenNodeDetail={onOpenNodeDetail}
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

    const meta = row?.querySelector(".fleet-boardtab-meta");
    expect(meta?.getAttribute("title")).toBe(
      "t_truth0 · premium-reviewer · Prio 7 · 3 Kommentare · 2 Vorgänger · 4 Nachfolger · 1/4",
    );

    expect(screen.queryByLabelText("Weitere Informationen zu Operator truth card")).toBeNull();
    fireEvent.click(row as HTMLElement);
    expect(onOpenNodeDetail).toHaveBeenCalledWith("t_truth01");
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

  it("opens adversarial timestamp cards in the drawer instead of expanding inline", () => {
    const onOpenNodeDetail = vi.fn();
    render(
      <BoardTab
        board={board([task({
          id: "t_timebad",
          title: "Adversarial time card",
          created_at: 1_783_800_200,
          started_at: 1_783_800_100,
          completed_at: 1_783_800_050,
          due_at: 1_783_800_300 + 86_400,
          last_heartbeat_at: 1_783_800_300 * 1000,
        })])}
        onOpenNodeDetail={onOpenNodeDetail}
      />,
    );

    const row = screen.getByText("Adversarial time card").closest(".fleet-boardtab-row");
    fireEvent.click(row as HTMLElement);
    expect(onOpenNodeDetail).toHaveBeenCalledWith("t_timebad");
    expect(screen.queryByLabelText("Weitere Informationen zu Adversarial time card")).toBeNull();
  });
});
