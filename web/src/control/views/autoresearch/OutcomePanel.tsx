import { Activity, BadgeCheck, CircleDollarSign, GitCommitHorizontal, ShieldCheck } from "lucide-react";
import type { AutoresearchOutcomeMetrics, Proposal } from "../../lib/types";
import { de } from "../../i18n/de";
import { KpiTile, SignalChip, signalToneFromLegacy } from "../../components/leitstand";
import { Card, Eyebrow, Text } from "../../components/primitives";

function deriveMetrics(proposals: Proposal[]): AutoresearchOutcomeMetrics {
  const applicable = proposals.filter((proposal) => proposal.outcome_applicability === "applicable");
  const measured = applicable.filter((proposal) => proposal.measurement_status === "measured");
  const count = (verdict: Proposal["outcome_verdict"]) => measured.filter((proposal) => proposal.outcome_verdict === verdict).length;
  const cost = proposals.reduce((sum, proposal) => sum + (proposal.outcome_cost_usd ?? 0), 0);
  const improved = count("improved");
  return {
    applicable: applicable.length,
    not_applicable: proposals.filter((proposal) => proposal.outcome_applicability === "not_applicable").length,
    pending: applicable.filter((proposal) => ["pending", "measuring", "retryable_failure"].includes(proposal.measurement_status ?? "")).length,
    measured: measured.length,
    measurement_coverage: applicable.length ? measured.length / applicable.length : 0,
    improved,
    neutral: count("neutral"),
    worsened: count("worsened"),
    unmeasurable: count("unmeasurable"),
    confounded: count("confounded"),
    measurement_cost_usd: cost,
    cost_per_measured_usd: measured.length && cost ? cost / measured.length : null,
    cost_per_improved_usd: improved && cost ? cost / improved : null,
  };
}

function formatMoney(value: number | null | undefined): string {
  if (value == null) return "—";
  return `${value.toLocaleString("de-DE", { minimumFractionDigits: 2, maximumFractionDigits: 4 })}\u00a0$`;
}

function verdictLabel(verdict: Proposal["outcome_verdict"]): string {
  switch (verdict) {
    case "improved": return de.autoresearch.outcomeImproved;
    case "neutral": return de.autoresearch.outcomeNeutral;
    case "worsened": return de.autoresearch.outcomeWorsened;
    case "unmeasurable": return de.autoresearch.outcomeUnmeasurable;
    case "confounded": return de.autoresearch.outcomeConfounded;
    default: return "offen";
  }
}

function verdictTone(verdict: Proposal["outcome_verdict"]): "emerald" | "amber" | "red" | "zinc" | "cyan" {
  if (verdict === "improved") return "emerald";
  if (verdict === "worsened") return "red";
  if (verdict === "neutral") return "cyan";
  if (verdict === "confounded") return "amber";
  return "zinc";
}

function observationValue(value: Record<string, unknown> | null | undefined): string {
  if (!value) return "—";
  const metric = typeof value.metric === "string" ? value.metric : "Messwert";
  const observed = value.value;
  return `${metric}: ${typeof observed === "number" || typeof observed === "string" ? observed : "—"}`;
}

export function OutcomePanel({
  metrics,
  proposals,
}: {
  metrics: AutoresearchOutcomeMetrics | null | undefined;
  proposals: Proposal[];
}) {
  const data = metrics ?? deriveMetrics(proposals);
  const evidence = proposals
    .filter((proposal) => proposal.measurement_status === "measured" && proposal.outcome_verdict)
    .sort((a, b) => (b.outcome_measured_at ?? 0) - (a.outcome_measured_at ?? 0))
    .slice(0, 5);

  return (
    <section id="autoresearch-outcomes" aria-label={de.autoresearch.outcomeHeading}>
    <Card surface="raised" className="overflow-hidden border-line p-0">
      <div className="space-y-4 p-4 sm:p-5">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <Eyebrow>{de.autoresearch.outcomeHeading}</Eyebrow>
              <SignalChip tone={signalToneFromLegacy(data.improved > 0 ? "emerald" : "zinc")} label={de.autoresearch.outcomeHonesty} />
            </div>
            <Text as="h2" variant="subtitle" className="mt-2 text-ink">Was hat nachweislich geholfen?</Text>
            <p className="mt-1 max-w-3xl text-sm leading-6 text-ink-2">{de.autoresearch.outcomeSubheading}</p>
          </div>
          <div className="flex items-center gap-2 rounded-card border border-line bg-surface-2 px-3 py-2 text-xs text-ink-2">
            <ShieldCheck aria-hidden className="h-4 w-4 text-live" />
            {data.measured} von {data.applicable} anwendbaren Änderungen gemessen
          </div>
        </div>

        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
          <KpiTile label={de.autoresearch.outcomeImproved} value={String(data.improved)} />
          <KpiTile label={de.autoresearch.outcomeMeasured} value={String(data.measured)} />
          <KpiTile label={de.autoresearch.outcomePending} value={String(data.pending)} />
          <KpiTile label={de.autoresearch.outcomeCoverage} value={`${Math.round(data.measurement_coverage * 100)}%`} />
          <KpiTile label={de.autoresearch.outcomeCost} value={formatMoney(data.measurement_cost_usd)} />
        </div>

        <div className="flex flex-wrap gap-2 text-xs">
          <SignalChip tone={signalToneFromLegacy("cyan")} label={`${data.neutral} ${de.autoresearch.outcomeNeutral}`} />
          <SignalChip tone={signalToneFromLegacy("red")} label={`${data.worsened} ${de.autoresearch.outcomeWorsened}`} />
          <SignalChip tone={signalToneFromLegacy("zinc")} label={`${data.unmeasurable} ${de.autoresearch.outcomeUnmeasurable}`} />
          <SignalChip tone={signalToneFromLegacy("amber")} label={`${data.confounded} ${de.autoresearch.outcomeConfounded}`} />
          <SignalChip tone={signalToneFromLegacy("zinc")} label={`${data.not_applicable} ${de.autoresearch.outcomeNotApplicable}`} />
          <span className="inline-flex min-h-7 items-center gap-1 rounded-full border border-line px-2.5 py-1 text-ink-2">
            <CircleDollarSign aria-hidden className="h-3.5 w-3.5" /> pro Messung {formatMoney(data.cost_per_measured_usd)}
          </span>
        </div>

        <section aria-label={de.autoresearch.outcomeEvidence}>
          <div className="mb-2 flex items-center gap-2">
            <Activity aria-hidden className="h-4 w-4 text-live" />
            <Text as="h3" variant="label" className="text-ink">{de.autoresearch.outcomeEvidence}</Text>
          </div>
          {evidence.length === 0 ? (
            <p className="rounded-panel border border-dashed border-line bg-surface-2 px-3 py-4 text-sm leading-6 text-ink-2">{de.autoresearch.outcomeEvidenceEmpty}</p>
          ) : (
            <div className="grid gap-2">
              {evidence.map((proposal) => (
                <article key={proposal.id} className="rounded-panel border border-line bg-surface-2 p-3">
                  <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <SignalChip tone={signalToneFromLegacy(verdictTone(proposal.outcome_verdict))} label={verdictLabel(proposal.outcome_verdict)} />
                        <SignalChip tone={signalToneFromLegacy(proposal.evidence_grade === "contract_verified" ? "emerald" : "zinc")} label={proposal.evidence_grade === "contract_verified" ? de.autoresearch.outcomeContractVerified : de.autoresearch.outcomeLegacy} />
                      </div>
                      <Text as="h4" variant="label" className="mt-2 break-words text-ink">{proposal.title?.trim() || proposal.target}</Text>
                      <p className="mt-1 text-xs leading-5 text-ink-2">Baseline {observationValue(proposal.outcome_baseline)} · Danach {observationValue(proposal.outcome_observation)}</p>
                    </div>
                    <div className="grid shrink-0 gap-1 text-xs text-ink-3 sm:max-w-[300px] sm:text-right">
                      <span className="inline-flex items-center gap-1 sm:justify-end"><BadgeCheck aria-hidden className="h-3.5 w-3.5" />{proposal.probe_contract?.contract_id ?? "Legacy ohne Probevertrag"}</span>
                      <span className="inline-flex items-center gap-1 font-data sm:justify-end"><GitCommitHorizontal aria-hidden className="h-3.5 w-3.5" />{proposal.outcome_integration_sha?.slice(0, 12) ?? "kein Deployment-SHA"}</span>
                      <span>{formatMoney(proposal.outcome_cost_usd)}</span>
                    </div>
                  </div>
                </article>
              ))}
            </div>
          )}
        </section>
      </div>
    </Card>
    </section>
  );
}
