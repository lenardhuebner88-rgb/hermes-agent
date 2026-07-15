import { useMemo, useState } from "react";
import { Check, ChevronLeft, ChevronRight, FlaskConical, X } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import type { RankedProposalGroupQueue } from "../../lib/proposalGroups";
import type { Density } from "../../hooks/useDensity";
import type { Proposal } from "../../lib/types";
import { Panel } from "../../components/primitives";
import { ProposalCard } from "../../components/ProposalCard";

export function ProposalQueue({
  density,
  openCount,
  storeLoading,
  storeBusy,
  proposalGroupQueue,
  onApply,
  onSkip,
  focusId,
}: {
  density: Density;
  openCount: number;
  storeLoading: boolean;
  storeBusy: string | null;
  proposalGroupQueue: RankedProposalGroupQueue;
  onApply: (proposal: Proposal) => void;
  onSkip: (proposal: Proposal) => void;
  focusId?: string | null;
}) {
  const proposals = useMemo(
    () => [...proposalGroupQueue.shortlist, ...proposalGroupQueue.backlog].flatMap((group) => group.proposals),
    [proposalGroupQueue],
  );
  const [manualIndex, setManualIndex] = useState(0);
  const [consumedFocusId, setConsumedFocusId] = useState<string | null>(null);
  const focusIndex = focusId ? proposals.findIndex((proposal) => proposal.id === focusId) : -1;
  const focusIsActive = focusIndex >= 0 && focusId !== consumedFocusId;
  const index = focusIsActive ? focusIndex : Math.min(manualIndex, Math.max(0, proposals.length - 1));

  const consumeFocusAt = (nextIndex: number) => {
    setConsumedFocusId(focusId ?? null);
    setManualIndex(nextIndex);
  };

  const current = proposals[index] ?? null;
  const position = current ? index + 1 : 0;
  const busy = current ? storeBusy === current.id : false;
  const waitingLabel = openCount === 1 ? "1 Entscheidung wartet" : `${openCount} Entscheidungen warten`;

  return (
    <section id="autoresearch-queue" className="scroll-mt-6 space-y-3">
      <Panel eyebrow="Entscheidungen" title={waitingLabel} surface="panel" actions={storeLoading ? <Spinner /> : null} className="p-4 sm:p-5">
        <p className="max-w-2xl text-sm leading-6 text-ink-2">
          Prüfe den Nutzen kurz und entscheide dann direkt: Annehmen übernimmt den Vorschlag, Ablehnen lässt alles unverändert.
        </p>
      </Panel>

      {current ? (
        <div className="space-y-3">
          <div className="flex items-center justify-between gap-3 px-1 text-sm text-ink-2">
            <span className="font-medium text-ink">{position} von {proposals.length}</span>
            {proposals.length > 1 ? (
              <div className="flex gap-2">
                <Button outlined className="min-h-12" onClick={() => consumeFocusAt(Math.max(0, index - 1))} disabled={index === 0} prefix={<ChevronLeft className="h-4 w-4" />}>Zurück</Button>
                <Button outlined className="min-h-12" onClick={() => consumeFocusAt(Math.min(proposals.length - 1, index + 1))} disabled={index === proposals.length - 1} suffix={<ChevronRight className="h-4 w-4" />}>Weiter</Button>
              </div>
            ) : null}
          </div>
          <ProposalCard proposal={current} density={density} busy={busy} onApply={onApply} onSkip={onSkip} showActions={false} />
          <div className="sticky bottom-3 z-10 rounded-panel border border-line bg-surface-1/95 p-3 shadow-lg backdrop-blur sm:ml-auto sm:max-w-md">
            <div className="grid gap-2 sm:grid-cols-2">
              <Button outlined className="min-h-12 justify-center" onClick={() => { consumeFocusAt(index); onSkip(current); }} disabled={busy} prefix={busy ? <Spinner /> : <X className="h-4 w-4" />}>Ablehnen</Button>
              <Button className="min-h-12 justify-center" onClick={() => { consumeFocusAt(index); onApply(current); }} disabled={busy} prefix={busy ? <Spinner /> : <Check className="h-4 w-4" />}>Annehmen</Button>
            </div>
          </div>
        </div>
      ) : !storeLoading ? (
        <Panel eyebrow="Entscheidungen" title="Keine Entscheidung wartet" surface="panel" className="p-4 sm:p-5">
          <div className="flex items-center gap-2 text-sm leading-6 text-ink-2"><FlaskConical className="h-5 w-5 shrink-0 text-live" />Alles ist entschieden. Neue Vorschläge erscheinen hier einzeln und klar prüfbar.</div>
        </Panel>
      ) : null}
    </section>
  );
}
