import type { Density } from "../../hooks/useDensity";
import type { AutoresearchResolvedSummary } from "../../lib/autoresearchResolvedSummary";
import type { AutoresearchQualityMetrics, Proposal } from "../../lib/types";
import { de } from "../../i18n/de";
import { Disclosure, Stagger, StaggerItem } from "../../components/primitives";
import { ProposalCard } from "../../components/ProposalCard";
import { ResolvedQueueSummaryPanel } from "./panels";

type Props = {
  summary: AutoresearchResolvedSummary | null;
  reverted: Proposal[];
  delivery: Proposal[];
  integrated: Proposal[];
  history: Proposal[];
  metrics: AutoresearchQualityMetrics | null;
  density: Density;
  archiveBusy: boolean;
  archiveDisabled: boolean;
  onArchiveReverted: () => void;
  onApply: (proposal: Proposal) => void;
  onSkip: (proposal: Proposal) => void;
};

function rate(value: number | null): string {
  return value == null ? "–" : `${Math.round(value * 100)} %`;
}

export function ResolvedQueues({ summary, reverted, delivery, integrated, history, metrics, density, archiveBusy, archiveDisabled, onArchiveReverted, onApply, onSkip }: Props) {
  const revertedIds = new Set(reverted.map((proposal) => proposal.id));
  const technicalHistory = history.filter((proposal) => !revertedIds.has(proposal.id));
  return (
    <>
      {metrics ? (
        <section className="rounded-panel border border-line bg-surface-1 p-4" aria-label="Autoresearch-Qualität">
          <p className="text-xs font-semibold uppercase tracking-wide text-ink-3">Qualität des Loops</p>
          <div className="mt-3 grid gap-2 sm:grid-cols-3 lg:grid-cols-6">
            <Metric label="Operator offen" value={String(metrics.operator_decisions)} />
            <Metric label="Code-Präzision" value={rate(metrics.code_precision)} />
            <Metric label="Gesamtpräzision" value={rate(metrics.precision)} />
            <Metric label="Stale-Rate" value={rate(metrics.stale_rate)} />
            <Metric label="Duplikate" value={rate(metrics.duplicate_rate)} />
            <Metric label="Kosten / akzeptiert" value={metrics.cost_per_accepted_usd == null ? "–" : `$${metrics.cost_per_accepted_usd.toFixed(3)}`} />
          </div>
        </section>
      ) : null}
      {delivery.length > 0 ? <LifecycleQueue title={`In Umsetzung oder verknüpft (${delivery.length})`} proposals={delivery} density={density} onApply={onApply} onSkip={onSkip} defaultOpen /> : null}
      {summary ? <ResolvedQueueSummaryPanel summary={summary} archiveBusy={archiveBusy} archiveDisabled={archiveDisabled} onArchiveReverted={onArchiveReverted} /> : null}
      {reverted.length > 0 ? (
        <Disclosure className="space-y-3 border-t border-line pt-4" summary={<span className="text-lg font-semibold text-ink" title={de.autoresearch.revertedExplain}>{de.autoresearch.revertedSummary(reverted.length)}</span>}>
          <div><p className="text-sm text-ink-2">{de.autoresearch.revertedExplain}</p></div>
          <ProposalGrid proposals={reverted} density={density} onApply={onApply} onSkip={onSkip} />
        </Disclosure>
      ) : null}
      {integrated.length > 0 ? <LifecycleQueue title={`Integriert (${integrated.length})`} proposals={integrated} density={density} onApply={onApply} onSkip={onSkip} /> : null}
      {technicalHistory.length > 0 ? <LifecycleQueue title={`Technische Historie (${technicalHistory.length})`} proposals={technicalHistory} density={density} onApply={onApply} onSkip={onSkip} /> : null}
    </>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return <div className="rounded-card border border-line bg-surface-2 px-3 py-2"><p className="text-xs text-ink-3">{label}</p><p className="mt-1 font-data text-sm font-semibold text-ink">{value}</p></div>;
}

function LifecycleQueue({ title, proposals, density, onApply, onSkip, defaultOpen = false }: { title: string; proposals: Proposal[]; density: Density; onApply: (proposal: Proposal) => void; onSkip: (proposal: Proposal) => void; defaultOpen?: boolean }) {
  return (
    <Disclosure open={defaultOpen || undefined} className="space-y-3" summary={<span className="text-lg font-semibold text-ink">{title}</span>}>
      <ProposalGrid proposals={proposals} density={density} onApply={onApply} onSkip={onSkip} />
    </Disclosure>
  );
}

function ProposalGrid({ proposals, density, onApply, onSkip }: { proposals: Proposal[]; density: Density; onApply: (proposal: Proposal) => void; onSkip: (proposal: Proposal) => void }) {
  return <Stagger className="grid gap-3">{proposals.map((proposal) => <StaggerItem key={proposal.id}><ProposalCard proposal={proposal} density={density} onApply={onApply} onSkip={onSkip} /></StaggerItem>)}</Stagger>;
}
