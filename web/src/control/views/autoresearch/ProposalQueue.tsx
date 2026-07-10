import { useState } from "react";
import { CheckCheck, FlaskConical, ListChecks, Trash2, X } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import type { rankAutoresearchReviewQueue, severityDistribution } from "../../lib/autoresearch";
import type { getAutoresearchDecisionGuide, getAutoresearchQueueActionSummary } from "../../lib/autoresearchDecisionGuide";
import { proposalNeedsManualReview } from "../../lib/autoresearchDecisionGuide";
import type { getAutoresearchReviewFlow } from "../../lib/autoresearchReviewFlow";
import type { getAutoresearchQueueModeSummary, AutoresearchEmptyQueueModeGuidance, AutoresearchQueueMode } from "../../lib/autoresearchQueueMode";
import type { ProposalGroup, RankedProposalGroupQueue } from "../../lib/proposalGroups";
import { de } from "../../i18n/de";
import type { Density } from "../../hooks/useDensity";
import type { useProposals } from "../../hooks/useControlData";
import type { Proposal } from "../../lib/types";
import { SignalChip, signalToneFromLegacy } from "../../components/leitstand";
import { Panel, Disclosure, Stagger, StaggerItem } from "../../components/primitives";
import { ProposalCard } from "../../components/ProposalCard";
import { DecisionGuidePanel, Empty, EmptyQueueModePanel, QueueActionSummaryPanel, QueueModePicker, ReviewFlowPanel, SelectionActionBar } from "./panels";

type RelevanceQueue = ReturnType<typeof rankAutoresearchReviewQueue>;
type Distribution = ReturnType<typeof severityDistribution>;
type QueueModeSummary = ReturnType<typeof getAutoresearchQueueModeSummary>;
type ReviewFlow = ReturnType<typeof getAutoresearchReviewFlow>;
type DecisionGuide = ReturnType<typeof getAutoresearchDecisionGuide>;
type QueueActionSummary = ReturnType<typeof getAutoresearchQueueActionSummary>;
type ProposalStore = ReturnType<typeof useProposals>;

export function ProposalQueue({
  density,
  openCount,
  revertedCount,
  filteredOpenCount,
  storeLoading,
  storeBusy,
  batchBusy,
  selectionControlsBusy,
  bulkRevertedBusy,
  selectedProposalIds,
  selectedIds,
  selectedManualReviewCount,
  batchSafeVisibleProposalIds,
  manualReviewVisibleCount,
  canConfirmSelection,
  distribution,
  relevanceQueue,
  proposalGroupQueue,
  queueModeSummary,
  queueMode,
  emptyQueueModeGuidance,
  reviewFlow,
  decisionGuide,
  queueActionSummary,
  batchConfirmById,
  onQueueModeChange,
  onSelectQueue,
  onClearSelection,
  onConfirmSelected,
  onRunReviewFlowPrimary,
  onToggleSelection,
  onApply,
  onSkip,
  onSkipBatch,
  onConfirmBatch,
  focusId,
}: {
  density: Density;
  openCount: number;
  revertedCount: number;
  filteredOpenCount: number;
  storeLoading: boolean;
  storeBusy: string | null;
  batchBusy: boolean;
  selectionControlsBusy: boolean;
  bulkRevertedBusy: boolean;
  selectedProposalIds: Set<string>;
  selectedIds: string[];
  selectedManualReviewCount: number;
  batchSafeVisibleProposalIds: string[];
  manualReviewVisibleCount: number;
  canConfirmSelection: boolean;
  distribution: Distribution;
  relevanceQueue: RelevanceQueue;
  proposalGroupQueue: RankedProposalGroupQueue;
  queueModeSummary: QueueModeSummary;
  queueMode: AutoresearchQueueMode;
  emptyQueueModeGuidance: AutoresearchEmptyQueueModeGuidance | null;
  reviewFlow: ReviewFlow;
  decisionGuide: DecisionGuide;
  queueActionSummary: QueueActionSummary;
  batchConfirmById: ProposalStore["batchConfirmById"];
  onQueueModeChange: (mode: AutoresearchQueueMode) => void;
  onSelectQueue: () => void;
  onClearSelection: () => void;
  onConfirmSelected: () => void;
  onRunReviewFlowPrimary: () => void;
  onToggleSelection: (proposalId: string, selected: boolean) => void;
  onApply: (proposal: Proposal) => void;
  onSkip: (proposal: Proposal) => void;
  onSkipBatch: (ids: string[]) => void;
  onConfirmBatch: (ids: string[]) => void;
  focusId?: string | null;
}) {
  const decisionHeading = openCount === 0 ? "Keine offenen Entscheidungen" : `${proposalGroupQueue.summary.shown} ${proposalGroupQueue.summary.shown === 1 ? "wichtige Gruppe" : "wichtige Gruppen"} in dieser Ansicht`;
  const selectionBusy = batchBusy || bulkRevertedBusy || !!storeBusy;
  // Deep-links (Decision Inbox → ?focus=<id>) can target a proposal that lives in the
  // collapsed backlog. Force that disclosure open so the card is mounted before the
  // post-commit scrollIntoView runs — else getElementById returns null and the scroll
  // silently no-ops (the native <details> it replaced kept children mounted).
  const backlogFocused = !!focusId && relevanceQueue.backlog.some((item) => item.proposal.id === focusId);

  return (
    <section id="autoresearch-queue" className="scroll-mt-6 space-y-3">
      <Panel
        eyebrow="Entscheidungen"
        title={decisionHeading}
        surface="panel"
        actions={storeLoading ? <Spinner /> : null}
        className="p-4 sm:p-5"
      >
        <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div>
            <p className="text-sm text-ink-2">{openCount} offen · zuerst nach Nutzen und Risiko sortiert · <span className="underline decoration-dotted underline-offset-2" title={de.autoresearch.revertedExplain}>{de.autoresearch.revertedCount(revertedCount)}</span></p>
            {openCount > 0 ? <SeverityBadges distribution={distribution} /> : null}
          </div>
          <div className="flex flex-col gap-2 sm:items-end">
            <QueueModePicker summary={queueModeSummary} activeMode={queueMode} onChange={onQueueModeChange} />
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-sm text-ink-2">{de.autoresearch.selectedCount(selectedIds.length)}</span>
              <Button outlined className="min-h-12" onClick={onSelectQueue} disabled={batchSafeVisibleProposalIds.length === 0 || selectionControlsBusy} title={manualReviewVisibleCount > 0 ? "Markiert nur sichtbare Vorschläge ohne Code, Hoch+-Risiko oder Safety-Bezug." : undefined} prefix={<ListChecks className="h-4 w-4" />}>
                {manualReviewVisibleCount > 0 ? `Sichere markieren (${batchSafeVisibleProposalIds.length})` : de.autoresearch.selectAllVisible}
              </Button>
              <Button outlined className="min-h-12" onClick={onClearSelection} disabled={selectedIds.length === 0 || selectionControlsBusy} prefix={<X className="h-4 w-4" />}>{de.autoresearch.clearSelection}</Button>
              <Button className="min-h-12" onClick={onConfirmSelected} disabled={!canConfirmSelection} title={selectedManualReviewCount > 0 ? "Riskante Auswahl einzeln prüfen oder Auswahl leeren." : undefined} prefix={selectionControlsBusy ? <Spinner /> : <CheckCheck className="h-4 w-4" />}>{de.autoresearch.batchConfirm}</Button>
            </div>
            <QueueActionSummaryPanel summary={queueActionSummary} />
          </div>
        </div>
      </Panel>

      {openCount > 0 && filteredOpenCount === 0 ? null : <ReviewFlowPanel flow={reviewFlow} busy={selectionBusy} onPrimary={onRunReviewFlowPrimary} />}
      {openCount > 0 && filteredOpenCount === 0 ? null : <DecisionGuidePanel guide={decisionGuide} />}
      {selectedIds.length > 0 ? <SelectionActionBar summary={queueActionSummary} selectedCount={selectedIds.length} canConfirm={canConfirmSelection} busy={selectionControlsBusy} onConfirm={onConfirmSelected} onClear={onClearSelection} /> : null}
      {openCount === 0 && !storeLoading ? <Empty icon={<FlaskConical className="h-5 w-5" />} text="Keine offenen Entscheidungen." /> : null}
      {openCount > 0 && filteredOpenCount === 0 && !storeLoading && emptyQueueModeGuidance ? <EmptyQueueModePanel guidance={emptyQueueModeGuidance} onChangeMode={onQueueModeChange} /> : null}

      <Stagger className="grid gap-4">
        {proposalGroupQueue.shortlist.map((group) => (
          <StaggerItem key={group.key}>
            <ProposalGroupCard
              group={group}
              density={density}
              storeBusy={storeBusy}
              selectedProposalIds={selectedProposalIds}
              batchConfirmById={batchConfirmById}
              focusId={focusId}
              onToggleSelection={onToggleSelection}
              onApply={onApply}
              onSkip={onSkip}
              onSkipBatch={onSkipBatch}
              onConfirmBatch={onConfirmBatch}
            />
          </StaggerItem>
        ))}
      </Stagger>

      {proposalGroupQueue.backlog.length > 0 ? (
        <Disclosure open={backlogFocused || undefined} className="rounded-panel border border-line bg-surface-1 p-4" summary={<span className="text-sm font-medium text-ink">Weitere Gruppen ({proposalGroupQueue.summary.remaining}) anzeigen</span>}>
          <div className="grid gap-4">
            {proposalGroupQueue.backlog.map((group) => (
              <ProposalGroupCard
                key={group.key}
                group={group}
                density={density}
                storeBusy={storeBusy}
                selectedProposalIds={selectedProposalIds}
                batchConfirmById={batchConfirmById}
                focusId={focusId}
                onToggleSelection={onToggleSelection}
                onApply={onApply}
                onSkip={onSkip}
                onSkipBatch={onSkipBatch}
                onConfirmBatch={onConfirmBatch}
              />
            ))}
          </div>
        </Disclosure>
      ) : null}
    </section>
  );
}

function ProposalGroupCard({
  group,
  density,
  storeBusy,
  selectedProposalIds,
  batchConfirmById,
  focusId,
  onToggleSelection,
  onApply,
  onSkip,
  onSkipBatch,
  onConfirmBatch,
}: {
  group: ProposalGroup;
  density: Density;
  storeBusy: string | null;
  selectedProposalIds: Set<string>;
  batchConfirmById: ProposalStore["batchConfirmById"];
  focusId?: string | null;
  onToggleSelection: (proposalId: string, selected: boolean) => void;
  onApply: (proposal: Proposal) => void;
  onSkip: (proposal: Proposal) => void;
  onSkipBatch: (ids: string[]) => void;
  onConfirmBatch: (ids: string[]) => void;
}) {
  const [confirmSkip, setConfirmSkip] = useState(false);
  const focused = !!focusId && group.ids.includes(focusId);
  const busy = !!storeBusy;
  const canBatchApply = group.mode === "skill" && group.proposals.every((proposal) => !proposalNeedsManualReview(proposal));
  const showBatchReason = group.mode !== "skill";
  return (
    <article className="rounded-panel border border-line bg-surface-1 p-4">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <SignalChip tone={signalToneFromLegacy(group.tone)} label={group.severity} />
            <SignalChip tone={signalToneFromLegacy(group.priorityGroup.tone)} label={group.priorityGroup.label} />
            <SignalChip tone={signalToneFromLegacy("cyan")} label={`${group.count} ${group.count === 1 ? "Vorschlag" : "Vorschläge"}`} />
          </div>
          <h3 className="mt-2 break-words text-base font-semibold leading-snug text-ink">{group.title}</h3>
          <p className="mt-1 text-sm leading-6 text-ink-2">{group.categoryLabel} · {group.target}</p>
          {showBatchReason ? <p className="mt-2 text-xs leading-5 text-status-warn">{de.autoresearch.batchManualReviewHint}</p> : null}
        </div>
        <div className="flex shrink-0 flex-col gap-2 sm:flex-row lg:flex-col xl:flex-row">
          {group.mode === "skill" ? (
            <Button className="min-h-12 justify-center" onClick={() => onConfirmBatch(group.ids)} disabled={!canBatchApply || busy} title={canBatchApply ? undefined : "Riskante Skill-Vorschläge einzeln prüfen."} prefix={storeBusy === "confirm-batch" ? <Spinner /> : <CheckCheck className="h-4 w-4" />}>
              {de.autoresearch.groupApply}
            </Button>
          ) : null}
          <Button outlined className="min-h-12 justify-center" onClick={() => setConfirmSkip(true)} disabled={busy} prefix={storeBusy === "skip-batch" ? <Spinner /> : <Trash2 className="h-4 w-4" />}>
            {de.autoresearch.groupSkip}
          </Button>
        </div>
      </div>
      {confirmSkip ? (
        <div className="mt-3 rounded-panel border border-status-warn/30 bg-status-warn/10 p-3">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div>
              <p className="text-sm font-semibold text-status-warn">{de.autoresearch.groupSkipConfirmTitle}</p>
              <p className="mt-1 text-xs leading-5 text-status-warn">{de.autoresearch.groupSkipConfirmBody(group.count)}</p>
            </div>
            <div className="flex shrink-0 flex-col gap-2 sm:flex-row">
              <Button outlined className="min-h-12 justify-center" onClick={() => setConfirmSkip(false)} disabled={busy} prefix={<X className="h-4 w-4" />}>{de.autoresearch.groupSkipCancel}</Button>
              <Button className="min-h-12 justify-center" onClick={() => onSkipBatch(group.ids)} disabled={busy} prefix={storeBusy === "skip-batch" ? <Spinner /> : <Trash2 className="h-4 w-4" />}>{de.autoresearch.groupSkipConfirmAction}</Button>
            </div>
          </div>
        </div>
      ) : null}
      <Disclosure open={focused || undefined} className="mt-3" summary={<span className="text-sm font-medium text-ink">{de.autoresearch.groupExpand(group.count)}</span>}>
        <div className="grid gap-4">
          {group.proposals.map((proposal) => (
            <ProposalCard
              key={proposal.id}
              proposal={proposal}
              priorityGroup={group.priorityGroup}
              density={density}
              busy={storeBusy === proposal.id}
              selectable
              batchSelectable={!proposalNeedsManualReview(proposal)}
              selected={selectedProposalIds.has(proposal.id)}
              batchStatus={batchConfirmById[proposal.id]}
              onSelectedChange={(selectedProposal, selected) => onToggleSelection(selectedProposal.id, selected)}
              onApply={onApply}
              onSkip={onSkip}
            />
          ))}
        </div>
      </Disclosure>
    </article>
  );
}

function SeverityBadges({ distribution }: { distribution: Distribution }) {
  return (
    <div className="mt-2 flex flex-wrap items-center gap-1.5">
      <span className="text-xs text-ink-2">{de.autoresearch.distributionHeading}:</span>
      {distribution.bySeverity.critical > 0 ? <SignalChip tone={signalToneFromLegacy("red")} label={`${de.autoresearch.severityCritical} ${distribution.bySeverity.critical}`} /> : null}
      {distribution.bySeverity.high > 0 ? <SignalChip tone={signalToneFromLegacy("amber")} label={`${de.autoresearch.severityHigh} ${distribution.bySeverity.high}`} /> : null}
      {distribution.bySeverity.medium > 0 ? <SignalChip tone={signalToneFromLegacy("sky")} label={`${de.autoresearch.severityMedium} ${distribution.bySeverity.medium}`} /> : null}
      {distribution.bySeverity.low > 0 ? <SignalChip tone={signalToneFromLegacy("zinc")} label={`${de.autoresearch.severityLow} ${distribution.bySeverity.low}`} /> : null}
    </div>
  );
}
