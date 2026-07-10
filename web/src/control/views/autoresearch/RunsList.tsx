import { cn } from "@/lib/utils";
import { formatRunTime } from "../../lib/autoresearch";
import { runLaneLabel, runLaneTone, runModelLabel, runVetoedCount, summarizeProposalRoi, summarizeRecentRuns, sumRunTokens } from "../../lib/autoresearch";
import { getAutoresearchRunCard, getAutoresearchRunSummary, type AutoresearchRunCard, type AutoresearchRunSummary } from "../../lib/autoresearchRunSummary";
import { de } from "../../i18n/de";
import type { AutoresearchRun, Proposal } from "../../lib/types";
import { KpiTile, SignalChip, signalToneFromLegacy } from "../../components/leitstand";
import { Disclosure, Panel, SkeletonCard, Stagger, StaggerItem, Text } from "../../components/primitives";
import { reviewStepToneClass } from "./panels.helpers";

export function RunsList({ runs, proposals, loading = false }: { runs: AutoresearchRun[]; proposals: Proposal[]; loading?: boolean }) {
  if (loading && runs.length === 0) {
    return (
      <section id="autoresearch-history" className="rounded-panel border border-line bg-surface-1 scroll-mt-6 p-4">
        <Text as="h2" variant="subtitle" className="text-ink">{de.autoresearch.recentRuns}</Text>
        <SkeletonCard rows={4} className="mt-3" />
      </section>
    );
  }
  const totalTokens = sumRunTokens(runs);
  const recent = summarizeRecentRuns(runs, 7);
  const proposalRoi = summarizeProposalRoi(proposals, recent.tokens);
  const runSummary = getAutoresearchRunSummary({ runs, acceptanceRate: proposalRoi.acceptanceRate, tokensPerApplied: proposalRoi.tokensPerApplied });
  const runCards = runs.slice(0, 5).map((run) => ({ run, card: getAutoresearchRunCard(run) }));

  return (
    <section id="autoresearch-history" className="rounded-panel border border-line bg-surface-1 scroll-mt-6 p-4">
      <div className="mb-3 flex items-start justify-between gap-3">
        <div>
          <Text as="h2" variant="subtitle" className="text-ink">{de.autoresearch.recentRuns}</Text>
          <p className="mt-1 text-xs text-ink-2" title={de.autoresearch.roi7dAcceptedNote}><span className="text-ink">{de.autoresearch.roi7dHeading}:</span> {recent.runs > 0 ? de.autoresearch.roi7dLine(recent.runs, recent.tokens, recent.proposed, recent.scanned) : de.autoresearch.roi7dEmpty}</p>
          <p className="mt-1 text-xs text-ink-2">{de.autoresearch.roi7dAcceptedLine(proposalRoi.acceptanceRate, proposalRoi.applied, proposalRoi.decided, proposalRoi.tokensPerApplied)}</p>
        </div>
        {totalTokens > 0 ? <span className="text-xs text-ink-2">{de.autoresearch.runsTokensTotal}: <span className="font-data tabular-nums text-ink">{totalTokens.toLocaleString("de-DE")}</span></span> : null}
      </div>
      <RunSummaryPanel summary={runSummary} />
      {runs.length === 0 ? <p className="text-sm text-ink-2">{de.autoresearch.recentRunsEmpty}</p> : (
        <div className="space-y-3">
          <Stagger className="grid gap-3 lg:grid-cols-2">
            {runCards.map(({ run, card }, i) => <StaggerItem key={`${run.at}-${run.request_id ?? i}`}><RunHistoryCard run={run} card={card} /></StaggerItem>)}
          </Stagger>
          {runs.length > runCards.length ? <p className="text-xs text-ink-3">Weitere {runs.length - runCards.length} ältere Läufe stehen in der technischen Tabelle.</p> : null}
          <Disclosure className="rounded-panel border border-line bg-surface-2 p-3" summary={<span className="text-sm font-medium text-ink">Technische Tabelle anzeigen</span>}>
            <div className="overflow-x-auto">
              <table className="w-full text-left text-sm">
                <thead className="text-ink-3"><tr className="border-b border-line"><th className="py-1 pr-3 font-medium">{de.autoresearch.runsColTime}</th><th className="py-1 pr-3 font-medium">{de.autoresearch.runsColLane}</th><th className="py-1 pr-3 text-right font-medium">{de.autoresearch.runsColTokens}</th><th className="py-1 pr-3 text-right font-medium">{de.autoresearch.runsColProposed}</th><th className="py-1 pr-3 text-right font-medium">{de.autoresearch.runsColScanned}</th><th className="py-1 pr-3 text-right font-medium">{de.autoresearch.runsColErrors}</th><th className="py-1 text-right font-medium">{de.autoresearch.runsColVetoed}</th></tr></thead>
                <tbody className="text-ink-2">
                  {runs.map((run, i) => {
                    const model = runModelLabel(run);
                    return (
                      <tr key={`${run.at}-${run.request_id ?? i}`} className="border-b border-line-soft last:border-0">
                        <td className="py-1 pr-3 font-data tabular-nums text-xs">{formatRunTime(run.at)}</td>
                        <td className="py-1 pr-3"><div className="flex min-w-40 flex-wrap items-center gap-1.5"><SignalChip tone={signalToneFromLegacy(runLaneTone(run.lane))} label={runLaneLabel(run.lane)} />{model ? <span className="max-w-52 truncate rounded-full border border-line bg-surface-2 px-2 py-1 text-xs text-ink-2" title={model}>{model}</span> : null}</div></td>
                        <td className="py-1 pr-3 text-right font-data tabular-nums">{run.tokens ? run.tokens.toLocaleString("de-DE") : "—"}</td><td className="py-1 pr-3 text-right font-data tabular-nums">{run.proposed}</td><td className="py-1 pr-3 text-right font-data tabular-nums">{run.scanned}</td><td className={cn("py-1 pr-3 text-right font-data tabular-nums", run.errors > 0 ? "text-status-alert" : "")}>{run.errors}</td><td className="py-1 text-right font-data tabular-nums text-ink-2">{runVetoedCount(run)}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </Disclosure>
        </div>
      )}
    </section>
  );
}

function RunHistoryCard({ run, card }: { run: AutoresearchRun; card: AutoresearchRunCard }) {
  const model = runModelLabel(run);
  return (
    <article className={cn("rounded-panel border p-3", reviewStepToneClass(card.tone))}>
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0"><div className="flex flex-wrap items-center gap-2"><SignalChip tone={signalToneFromLegacy(runLaneTone(run.lane))} label={runLaneLabel(run.lane)} /><SignalChip tone={signalToneFromLegacy(card.tone)} label={card.label} /><span className="font-data tabular-nums text-xs text-ink-2">{formatRunTime(run.at)}</span></div><Text as="h3" variant="label" className="mt-2 text-ink">{card.title}</Text><p className="mt-1 text-xs leading-5 text-ink-2">{card.detail}</p><p className="mt-2 text-xs text-ink"><span className="font-semibold">Danach:</span> {card.next}</p></div>
        {model ? <span className="max-w-52 truncate rounded-full border border-line bg-surface-2 px-2 py-1 text-xs text-ink-2" title={model}>{model}</span> : null}
      </div>
      <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-5">{card.facts.map((fact) => <KpiTile key={fact.label} label={fact.label} value={<span title={fact.value}>{fact.value}</span>} />)}</div>
    </article>
  );
}

function RunSummaryPanel({ summary }: { summary: AutoresearchRunSummary }) {
  return (
    <Panel eyebrow="Lauf-Auswertung" title={summary.title} actions={<SignalChip tone={signalToneFromLegacy(summary.tone)} label={summary.label} />} className="mb-4 p-3" surface="panel2">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
        <div className="min-w-0"><p className="max-w-3xl text-sm leading-6 text-ink-2">{summary.detail}</p><p className="mt-2 text-sm text-ink"><span className="font-semibold">Nächster Schritt:</span> {summary.next}</p></div>
        <div className="grid shrink-0 gap-2 sm:grid-cols-5 lg:min-w-[520px]">{summary.facts.map((fact) => <KpiTile key={fact.label} label={fact.label} value={<span title={fact.value}>{fact.value}</span>} />)}</div>
      </div>
    </Panel>
  );
}
