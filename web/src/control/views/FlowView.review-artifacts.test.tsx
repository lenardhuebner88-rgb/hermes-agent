import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { FlowReceiptRail, FlowRunCard, RecoveryDecisionCard } from "./FlowView";
import type { BoardTask, TaskArtifactLink } from "../lib/types";
import type { StageAction } from "../lib/fleet";
import type { KanbanDecision, TaskDetailResponse } from "../lib/schemas";

const baseTask: BoardTask = {
  id: "t_active_verifier",
  title: "Gate Review Card",
  status: "review",
  assignee: "coder",
  priority: 0,
  created_at: 100,
  started_at: null,
  completed_at: null,
  branch_name: null,
  latest_summary: null,
  link_counts: { parents: 0, children: 0 },
  comment_count: 0,
  progress: null,
  age: { created_age_seconds: 100, started_age_seconds: null, time_to_complete_seconds: null },
  tenant: "flow-capture",
  root_id: null,
  epic_id: null,
};

const resultArtifact: TaskArtifactLink = {
  filename: "RESULT.md",
  relative_path: "RESULT.md",
  path: "/home/piet/.hermes/reports/by-task/t_result_artifact/RESULT.md",
  source: "metadata.artifacts",
  size: 42,
  mtime: 1780000000,
  content_type: "text/markdown",
  url: "/api/plugins/kanban/tasks/t_result_artifact/deliverables/RESULT.md",
};

const noop = vi.fn();
const noopAct = vi.fn<(task: BoardTask, action: StageAction) => undefined>();

describe("FlowView review gate and RESULT artifacts", () => {
  it("surfaces the next operator action in recovery decision cards", () => {
    const decision: KanbanDecision = {
      kind: "operator_escalation",
      task_id: "t_release_gate",
      title: "Release-Gate Dashboard build + runtime activation check",
      reason: "settled block with no operator escalation",
      age_seconds: 120,
      suggested_command: "hermes kanban show t_release_gate",
      operator_escalation: {
        task: { id: "t_release_gate", title: "Release-Gate", status: "blocked", assignee: "operator" },
        source: "kanban",
        signal_key: "release-gate",
        why_now: "worker loop cannot proceed alone",
        attempts_already_made: 1,
        evidence: {},
        recommended_human_action: "Inspect the task, answer any operator question, and decide whether to unblock or close.",
        blocked_action_boundary: ["DB schema/data mutation", "destructive delete"],
      },
    };

    const html = renderToStaticMarkup(<RecoveryDecisionCard row={decision} />);

    expect(html).toContain("Nächste Aktion");
    expect(html).toContain("Inspect the task");
    expect(html).toContain("Release-Gate Dashboard");
    expect(html).toContain("Grenze:");
  });

  it("active-verifier fixture hides Ausliefern and shows Verifier läuft", () => {
    const html = renderToStaticMarkup(
      <FlowRunCard
        task={baseTask}
        enriched={{ activeVerifier: true, activeRunId: "960", reviewRunState: "active" }}
        selected={false}
        busy={false}
        now={200}
        dispatchChoice={null}
        onSelect={noop}
        onAct={noopAct}
      />,
    );

    expect(html).toContain("Verifier läuft");
    expect(html).toContain("Run 960");
    expect(html).not.toContain("Ausliefern");
  });

  it("post-approved review fixture avoids the invalid review-to-done action while polling catches up", () => {
    const html = renderToStaticMarkup(
      <FlowRunCard
        task={{ ...baseTask, id: "t_review_approved" }}
        enriched={{ verdict: "APPROVED", reviewRunState: "approved" }}
        selected={false}
        busy={false}
        now={200}
        dispatchChoice={null}
        onSelect={noop}
        onAct={noopAct}
      />,
    );

    expect(html).toContain("Verifier APPROVED");
    expect(html).toContain("wartet auf Board-Refresh");
    expect(html).not.toContain("Ausliefern");
  });

  it("post-approved review fixture restores manual action copy after grace fallback", () => {
    const html = renderToStaticMarkup(
      <FlowRunCard
        task={{ ...baseTask, id: "t_review_approved_stale" }}
        enriched={{ verdict: "APPROVED", reviewRunState: "approved" }}
        selected={false}
        busy={false}
        now={200}
        dispatchChoice={null}
        manualReviewFallback
        onSelect={noop}
        onAct={noopAct}
      />,
    );

    expect(html).toContain("Verifier APPROVED");
    expect(html).toContain("Übergang ausgeblieben");
    expect(html).toContain("Ausliefern");
    expect(html).toContain("Nacharbeit");
  });

  it("done artifact fixture shows a RESULT CTA target from metadata artifacts in the detail rail", () => {
    const detail: TaskDetailResponse = {
      task: { id: "t_result_artifact", title: "Spec-Draft fertig", status: "done", assignee: "coder", latest_summary: "RESULT.md gesichert" },
      runs: [],
      events: [],
      deliverables: [],
      links: { parents: [], children: [] },
    };
    const html = renderToStaticMarkup(
      <FlowReceiptRail
        taskId="t_result_artifact"
        task={{ ...baseTask, id: "t_result_artifact", status: "done", title: "Spec-Draft fertig" }}
        detail={detail}
        enriched={{ resultArtifactLinks: [resultArtifact], deliverableCount: 1 }}
        loading={false}
        now={200}
        boardTasks={[]}
        snapshotLabel="frisch"
        onRelease={noop}
        releaseBusy={false}
      />,
    );

    expect(html).toContain("Spec-Draft / RESULT");
    expect(html).toContain("RESULT.md");
    expect(html).toContain("/api/plugins/kanban/tasks/t_result_artifact/deliverables/RESULT.md");
  });

  it("copy split fixture keeps PlanSpec for flow_plan and Spec-Draft/RESULT for single-task artifacts", () => {
    const srcHtml = renderToStaticMarkup(
      <FlowReceiptRail
        taskId="t_flow_plan"
        task={{ ...baseTask, id: "t_flow_plan", title: "Plan root" }}
        detail={{
          task: { id: "t_flow_plan", title: "Plan root", status: "todo", assignee: "planner", latest_summary: null },
          runs: [],
          events: [{ id: 1, kind: "flow_plan", created_at: 190, run_id: null, payload: { spec: "/vault/plan.md" } }],
          deliverables: [],
          links: { parents: [], children: [] },
        }}
        loading={false}
        now={200}
        boardTasks={[]}
        snapshotLabel="frisch"
        onRelease={noop}
        releaseBusy={false}
      />,
    );

    expect(srcHtml).toContain("PlanSpec öffnen");
    expect(srcHtml).not.toContain("Spec-Draft / RESULT");

    const artifactHtml = renderToStaticMarkup(
      <FlowReceiptRail
        taskId="t_single_result"
        task={{ ...baseTask, id: "t_single_result", status: "done", title: "Single Task Result" }}
        detail={{ task: null, runs: [], events: [], deliverables: [], links: { parents: [], children: [] } }}
        enriched={{ resultArtifactLinks: [resultArtifact], deliverableCount: 1 }}
        loading={false}
        now={200}
        boardTasks={[]}
        snapshotLabel="frisch"
        onRelease={noop}
        releaseBusy={false}
      />,
    );

    expect(artifactHtml).toContain("Spec-Draft / RESULT");
    expect(artifactHtml).not.toContain("PlanSpec öffnen");
  });
});
