// @vitest-environment jsdom
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { getAutoresearchDecisionGuide, getAutoresearchQueueActionSummary } from "../../lib/autoresearchDecisionGuide";
import { getAutoresearchQueueModeSummary } from "../../lib/autoresearchQueueMode";
import { getAutoresearchReviewFlow } from "../../lib/autoresearchReviewFlow";
import { severityDistribution } from "../../lib/autoresearch";
import { rankAutoresearchProposalGroups } from "../../lib/proposalGroups";
import type { Proposal } from "../../lib/types";
import { ProposalQueue } from "./ProposalQueue";

const realProposal: Proposal = {
  id: "focus-real-proposal",
  target: "web/src/control/views/AutoresearchView.tsx",
  section: "focusProposal",
  title: "Hero CTA should focus the first proposal",
  category: "bug_risk",
  severity: "high",
  evidence: "The card is absent while its disclosure is closed.",
  rationale_plain: "The decision CTA must reveal its target before scrolling.",
  diff_before_after: "- scrollImmediately()\n+ revealThenScroll()",
  mode: "code",
  status: "proposed",
  finding_state: "verified",
  decision_state: "needs_operator",
  delivery_state: "none",
  operator_action_required: true,
  expected_benefit: "The primary action reaches the actual decision.",
  risk_summary: "A focus request could otherwise remain a no-op.",
  test_plan: "Render the real proposal shape and focus it while collapsed.",
  recommendation: "Open the group before scrolling.",
};

function queueProps(focusId: string | null) {
  const proposals = [realProposal];
  const proposalGroupQueue = rankAutoresearchProposalGroups(proposals, 3);
  return {
    density: "airy" as const,
    focusId,
    openCount: 1,
    revertedCount: 0,
    filteredOpenCount: 1,
    storeLoading: false,
    storeBusy: null,
    batchBusy: false,
    selectionControlsBusy: false,
    bulkRevertedBusy: false,
    selectedProposalIds: new Set<string>(),
    selectedIds: [],
    selectedManualReviewCount: 0,
    batchSafeVisibleProposalIds: [],
    manualReviewVisibleCount: 1,
    canConfirmSelection: false,
    distribution: severityDistribution(proposals),
    proposalGroupQueue,
    queueModeSummary: getAutoresearchQueueModeSummary(proposals, "all"),
    queueMode: "all" as const,
    emptyQueueModeGuidance: null,
    reviewFlow: getAutoresearchReviewFlow({
      openCount: 1,
      decidedCount: 0,
      selectedCount: 0,
      visibleCount: 1,
      batchSafeVisibleCount: 0,
      highPriorityCount: 1,
      selectedManualReviewCount: 0,
      backlogCount: 0,
      revertedCount: 0,
      topTitle: realProposal.title,
    }),
    decisionGuide: getAutoresearchDecisionGuide({
      visibleProposals: proposals,
      selectedProposals: [],
      openCount: 1,
      selectedCount: 0,
      backlogCount: 0,
      revertedCount: 0,
      topTitle: realProposal.title,
    }),
    queueActionSummary: getAutoresearchQueueActionSummary({
      visibleCount: 1,
      batchSafeVisibleCount: 0,
      manualReviewVisibleCount: 1,
      selectedCount: 0,
      selectedManualReviewCount: 0,
    }),
    batchConfirmById: {},
    onQueueModeChange: vi.fn(),
    onSelectQueue: vi.fn(),
    onClearSelection: vi.fn(),
    onConfirmSelected: vi.fn(),
    onRunReviewFlowPrimary: vi.fn(),
    onToggleSelection: vi.fn(),
    onApply: vi.fn(),
    onSkip: vi.fn(),
    onSkipBatch: vi.fn(),
    onConfirmBatch: vi.fn(),
    onClearFocus: vi.fn(),
  };
}

describe("ProposalQueue focus", () => {
  it("shows one real proposal at a time with compact progress and thumb actions", () => {
    const { rerender } = render(<ProposalQueue {...queueProps(null)} />);
    expect(screen.getByText("1 Entscheidung wartet")).toBeTruthy();
    expect(screen.getByText("1 von 1")).toBeTruthy();
    expect(document.getElementById(`autoresearch-proposal-${realProposal.id}`)).not.toBeNull();
    expect(screen.getByRole("button", { name: "Annehmen" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Ablehnen" })).toBeTruthy();

    rerender(<ProposalQueue {...queueProps(realProposal.id)} />);

    expect(document.getElementById(`autoresearch-proposal-${realProposal.id}`)).not.toBeNull();
  });
});
