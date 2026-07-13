import { describe, expect, it } from "vitest";
import { getHeldFlowDispatchGuard, getHeldFlowRootGuard } from "./flowDispatchGuard";
import type { BoardTask } from "./types";
import type { TaskDetailResponse } from "./schemas";

function task(id: string, status: BoardTask["status"]): BoardTask {
  return {
    id,
    title: id,
    status,
    assignee: "coder",
    priority: 0,
    created_at: 1,
    started_at: null,
    completed_at: null,
    branch_name: null,
    latest_summary: null,
    link_counts: { parents: 0, children: 0 },
    comment_count: 0,
    progress: null,
    tenant: null,
    root_id: null,
    epic_id: null,
    age: null,
  };
}

function detailWithRoot(rootId: string): TaskDetailResponse {
  return {
    task: null,
    comments: [],
    runs: [],
    events: [{ id: 1, kind: "created", created_at: 1, run_id: null, payload: { from_decompose_of: rootId } }],
    deliverables: [],
    links: { parents: [], children: [rootId], parent_states: [], child_states: [] },
  };
}

function rootDetail(childIds: string[]): TaskDetailResponse {
  return {
    task: null,
    comments: [],
    runs: [],
    events: [{ id: 9, kind: "decomposed", created_at: 2, run_id: null, payload: { child_ids: childIds } }],
    deliverables: [],
    links: { parents: childIds, children: [], parent_states: [], child_states: [] },
  };
}

describe("getHeldFlowDispatchGuard", () => {
  it("detects a gated Flow subtask when scheduled siblings are still held", () => {
    const selected = task("t_child_a", "scheduled");
    const guard = getHeldFlowDispatchGuard(
      selected,
      detailWithRoot("t_root"),
      rootDetail(["t_child_a", "t_child_b", "t_child_c"]),
      [selected, task("t_child_b", "scheduled"), task("t_child_c", "todo"), task("t_root", "todo")],
    );

    expect(guard).toEqual({ rootId: "t_root", heldSiblingIds: ["t_child_b"] });
  });

  it("does not block advanced single dispatch when no scheduled sibling remains", () => {
    const selected = task("t_child_a", "scheduled");
    const guard = getHeldFlowDispatchGuard(
      selected,
      detailWithRoot("t_root"),
      rootDetail(["t_child_a", "t_child_b"]),
      [selected, task("t_child_b", "todo"), task("t_root", "todo")],
    );

    expect(guard).toBeNull();
  });

  it("does not warn for non-gated tasks without a decompose root marker", () => {
    const selected = task("t_plain", "scheduled");
    const guard = getHeldFlowDispatchGuard(
      selected,
      { task: null, comments: [], runs: [], events: [], deliverables: [], links: { parents: [], children: [], parent_states: [], child_states: [] } },
      null,
      [selected],
    );

    expect(guard).toBeNull();
  });
});


describe("getHeldFlowRootGuard", () => {
  it("detects a held PlanSpec root so root dispatch releases the chain instead of patching the root", () => {
    const root = task("t_root", "scheduled");
    const guard = getHeldFlowRootGuard(root, rootDetail(["t_child_a", "t_child_b"]), [
      root,
      task("t_child_a", "scheduled"),
      task("t_child_b", "scheduled"),
    ]);

    expect(guard).toEqual({ rootId: "t_root", heldChildIds: ["t_child_a", "t_child_b"] });
  });

  it("ignores PlanSpec roots whose children are already released", () => {
    const root = task("t_root", "scheduled");
    const guard = getHeldFlowRootGuard(root, rootDetail(["t_child_a"]), [root, task("t_child_a", "todo")]);

    expect(guard).toBeNull();
  });
});
