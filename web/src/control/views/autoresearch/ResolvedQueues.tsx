import type { Density } from "../../hooks/useDensity";
import type { AutoresearchResolvedSummary } from "../../lib/autoresearchResolvedSummary";
import type { Proposal } from "../../lib/types";
import { de } from "../../i18n/de";
import { Disclosure, Stagger, StaggerItem } from "../../components/primitives";
import { ProposalCard } from "../../components/ProposalCard";
import { ResolvedQueueSummaryPanel } from "./panels";

export function ResolvedQueues({ summary, reverted, applied, skipped, density, archiveBusy, archiveDisabled, onArchiveReverted, onApply, onSkip }: { summary: AutoresearchResolvedSummary | null; reverted: Proposal[]; applied: Proposal[]; skipped: Proposal[]; density: Density; archiveBusy: boolean; archiveDisabled: boolean; onArchiveReverted: () => void; onApply: (proposal: Proposal) => void; onSkip: (proposal: Proposal) => void }) {
  return (
    <>
      {summary ? <ResolvedQueueSummaryPanel summary={summary} archiveBusy={archiveBusy} archiveDisabled={archiveDisabled} onArchiveReverted={onArchiveReverted} /> : null}
      {reverted.length > 0 ? (
        <Disclosure className="space-y-3 border-t border-line pt-4" summary={<span className="text-lg font-semibold text-ink" title={de.autoresearch.revertedExplain}>{de.autoresearch.revertedSummary(reverted.length)}</span>}>
          <div><p className="text-sm text-ink-2">{de.autoresearch.revertedExplain}</p></div>
          <Stagger className="grid gap-3 opacity-85">{reverted.map((proposal) => <StaggerItem key={proposal.id}><ProposalCard proposal={proposal} density={density} onApply={onApply} onSkip={onSkip} /></StaggerItem>)}</Stagger>
        </Disclosure>
      ) : null}
      {applied.length > 0 ? <ResolvedQueue title={`Erledigt (${applied.length})`} proposals={applied} density={density} onApply={onApply} onSkip={onSkip} /> : null}
      {skipped.length > 0 ? <ResolvedQueue title={`Übersprungen (${skipped.length})`} proposals={skipped} density={density} onApply={onApply} onSkip={onSkip} /> : null}
    </>
  );
}

function ResolvedQueue({ title, proposals, density, onApply, onSkip }: { title: string; proposals: Proposal[]; density: Density; onApply: (proposal: Proposal) => void; onSkip: (proposal: Proposal) => void }) {
  return (
    <Disclosure className="space-y-3" summary={<span className="text-lg font-semibold text-ink">{title}</span>}>
      <Stagger className="grid gap-3">{proposals.map((proposal) => <StaggerItem key={proposal.id}><ProposalCard proposal={proposal} density={density} onApply={onApply} onSkip={onSkip} /></StaggerItem>)}</Stagger>
    </Disclosure>
  );
}
