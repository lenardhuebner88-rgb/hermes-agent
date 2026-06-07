import { CheckCheck, FlaskConical, ListChecks, X } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import type { rankAutoresearchReviewQueue, severityDistribution } from "../../lib/autoresearch";
import type { getAutoresearchDecisionGuide, getAutoresearchQueueActionSummary } from "../../lib/autoresearchDecisionGuide";
import { proposalNeedsManualReview } from "../../lib/autoresearchDecisionGuide";
import type { getAutoresearchReviewFlow } from "../../lib/autoresearchReviewFlow";
import type { getAutoresearchQueueModeSummary, AutoresearchEmptyQueueModeGuidance, AutoresearchQueueMode } from "../../lib/autoresearchQueueMode";
import { de } from "../../i18n/de";
import type { Density } from "../../hooks/useDensity";
import type { useProposals } from "../../hooks/useControlData";
import type { Proposal } from "../../lib/types";
import { StatusPill } from "../../components/atoms";
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
}) {
  const decisionHeading = openCount === 0 ? "Keine offenen Entscheidungen" : `${relevanceQueue.summary.shown} ${relevanceQueue.summary.shown === 1 ? "wichtige Karte" : "wichtige Karten"} in dieser Ansicht`;
  const selectionBusy = batchBusy || bulkRevertedBusy || !!storeBusy;

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
            <p className="text-sm hc-soft">{openCount} offen · zuerst nach Nutzen und Risiko sortiert · <span className="underline decoration-dotted underline-offset-2" title={de.autoresearch.revertedExplain}>{de.autoresearch.revertedCount(revertedCount)}</span></p>
            {openCount > 0 ? <SeverityBadges distribution={distribution} /> : null}
          </div>
          <div className="flex flex-col gap-2 sm:items-end">
            <QueueModePicker summary={queueModeSummary} activeMode={queueMode} onChange={onQueueModeChange} />
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-sm hc-soft">{de.autoresearch.selectedCount(selectedIds.length)}</span>
              <Button outlined className="hc-hit" onClick={onSelectQueue} disabled={batchSafeVisibleProposalIds.length === 0 || selectionControlsBusy} title={manualReviewVisibleCount > 0 ? "Markiert nur sichtbare Vorschläge ohne Code, Hoch+-Risiko oder Safety-Bezug." : undefined} prefix={<ListChecks className="h-4 w-4" />}>
                {manualReviewVisibleCount > 0 ? `Sichere markieren (${batchSafeVisibleProposalIds.length})` : de.autoresearch.selectAllVisible}
              </Button>
              <Button outlined className="hc-hit" onClick={onClearSelection} disabled={selectedIds.length === 0 || selectionControlsBusy} prefix={<X className="h-4 w-4" />}>{de.autoresearch.clearSelection}</Button>
              <Button className="hc-hit" onClick={onConfirmSelected} disabled={!canConfirmSelection} title={selectedManualReviewCount > 0 ? "Riskante Auswahl einzeln prüfen oder Auswahl leeren." : undefined} prefix={selectionControlsBusy ? <Spinner /> : <CheckCheck className="h-4 w-4" />}>{de.autoresearch.batchConfirm}</Button>
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
        {relevanceQueue.shortlist.map((item) => (
          <StaggerItem key={item.proposal.id}>
            <ProposalCard
              proposal={item.proposal}
              priorityGroup={item.group}
              density={density}
              busy={storeBusy === item.proposal.id}
              selectable
              batchSelectable={!proposalNeedsManualReview(item.proposal)}
              selected={selectedProposalIds.has(item.proposal.id)}
              batchStatus={batchConfirmById[item.proposal.id]}
              onSelectedChange={(proposal, selected) => onToggleSelection(proposal.id, selected)}
              onApply={onApply}
              onSkip={onSkip}
            />
          </StaggerItem>
        ))}
      </Stagger>

      {relevanceQueue.backlog.length > 0 ? (
        <Disclosure className="hc-card p-4" summary={<span className="text-sm font-medium text-white">Weitere Entscheidungen ({relevanceQueue.summary.remaining}) anzeigen</span>}>
          <div className="grid gap-4">
            {relevanceQueue.backlog.map((item) => (
              <ProposalCard
                key={item.proposal.id}
                proposal={item.proposal}
                priorityGroup={item.group}
                density={density}
                busy={storeBusy === item.proposal.id}
                selectable
                batchSelectable={!proposalNeedsManualReview(item.proposal)}
                selected={selectedProposalIds.has(item.proposal.id)}
                batchStatus={batchConfirmById[item.proposal.id]}
                onSelectedChange={(proposal, selected) => onToggleSelection(proposal.id, selected)}
                onApply={onApply}
                onSkip={onSkip}
              />
            ))}
          </div>
        </Disclosure>
      ) : null}
    </section>
  );
}

function SeverityBadges({ distribution }: { distribution: Distribution }) {
  return (
    <div className="mt-2 flex flex-wrap items-center gap-1.5">
      <span className="text-xs hc-soft">{de.autoresearch.distributionHeading}:</span>
      {distribution.bySeverity.critical > 0 ? <StatusPill tone="red" label={`${de.autoresearch.severityCritical} ${distribution.bySeverity.critical}`} /> : null}
      {distribution.bySeverity.high > 0 ? <StatusPill tone="amber" label={`${de.autoresearch.severityHigh} ${distribution.bySeverity.high}`} /> : null}
      {distribution.bySeverity.medium > 0 ? <StatusPill tone="sky" label={`${de.autoresearch.severityMedium} ${distribution.bySeverity.medium}`} /> : null}
      {distribution.bySeverity.low > 0 ? <StatusPill tone="zinc" label={`${de.autoresearch.severityLow} ${distribution.bySeverity.low}`} /> : null}
    </div>
  );
}
