import { Activity, BadgeCheck, CircleDollarSign, GitCommitHorizontal, ShieldCheck } from "lucide-react";
import type { AutoresearchOutcomeMetrics, Proposal } from "../../lib/types";
import { de } from "../../i18n/de";
import { KpiTile, SignalChip, signalToneFromLegacy } from "../../components/leitstand";
import { Card, Disclosure, Eyebrow, Text } from "../../components/primitives";

function costDimensions(proposal: Proposal): { actual: number; apiEquivalent: number; effective: number } {
  const breakdown = proposal.outcome_cost_breakdown ?? {};
  const values = Object.entries(breakdown).filter((entry): entry is [string, number] => typeof entry[1] === "number");
  const breakdownEquivalent = values.reduce((sum, [key, value]) => sum + (key.endsWith("_equivalent_usd") ? value : 0), 0);
  const breakdownActual = values.reduce((sum, [key, value]) => sum + (!key.endsWith("_equivalent_usd") ? value : 0), 0);
  const effective = proposal.outcome_cost_effective_usd ?? proposal.outcome_cost_usd ?? (breakdownActual + breakdownEquivalent);
  const apiEquivalent = proposal.outcome_cost_api_equivalent_usd ?? breakdownEquivalent;
  const actual = proposal.outcome_cost_actual_usd ?? (values.length ? breakdownActual : Math.max(0, effective - apiEquivalent));
  return { actual, apiEquivalent, effective };
}

function deriveMetrics(proposals: Proposal[]): AutoresearchOutcomeMetrics {
  const applicable = proposals.filter((proposal) => proposal.outcome_applicability === "applicable");
  const integrated = applicable.filter((proposal) => proposal.delivery_state === "integrated");
  const measured = applicable.filter((proposal) =>
    ["measured", "exhausted"].includes(proposal.measurement_status ?? "")
    && proposal.outcome_verdict != null
  );
  const verified = measured.filter((proposal) => proposal.evidence_grade === "contract_verified");
  const legacy = measured.filter((proposal) => proposal.evidence_grade !== "contract_verified");
  const count = (items: Proposal[], verdict: Proposal["outcome_verdict"]) => items.filter((proposal) => proposal.outcome_verdict === verdict).length;
  const improved = count(verified, "improved");
  const neutral = count(verified, "neutral");
  const worsened = count(verified, "worsened");
  const unmeasurable = count(verified, "unmeasurable");
  const confounded = count(verified, "confounded");
  const directional = improved + neutral + worsened;
  const costs = verified.map(costDimensions);
  const knownActualCost = costs.reduce((sum, cost) => sum + cost.actual, 0);
  const knownApiEquivalentCost = costs.reduce((sum, cost) => sum + cost.apiEquivalent, 0);
  const knownEffectiveCost = costs.reduce((sum, cost) => sum + cost.effective, 0);
  const costComplete = verified.filter((proposal) => proposal.outcome_cost_status === "complete");
  const costsComplete = costComplete.length === verified.length;
  const interventions = verified.reduce((sum, proposal) => sum + (proposal.outcome_operator_interventions ?? 0), 0);
  return {
    applicable: applicable.length,
    not_applicable: proposals.filter((proposal) => proposal.outcome_applicability === "not_applicable").length,
    pending: applicable.filter((proposal) => ["pending", "measuring", "retryable_failure"].includes(proposal.measurement_status ?? "")).length,
    measured: measured.length,
    verified_measured: verified.length,
    measurement_coverage: integrated.length ? verified.length / integrated.length : 0,
    outcome_coverage: integrated.length ? verified.length / integrated.length : 0,
    directional_coverage: integrated.length ? directional / integrated.length : 0,
    verified_directional_denominator: directional,
    verified_benefit_rate: directional ? improved / directional : null,
    regression_rate: directional ? worsened / directional : null,
    unmeasurable_rate: verified.length ? (unmeasurable + confounded) / verified.length : null,
    verified_improved: improved,
    legacy_improved: count(legacy, "improved"),
    improved,
    neutral,
    worsened,
    unmeasurable,
    confounded,
    measurement_cost_usd: costsComplete ? knownEffectiveCost : null,
    known_measurement_cost_usd: knownEffectiveCost,
    measurement_actual_cost_usd: costsComplete ? knownActualCost : null,
    measurement_api_equivalent_cost_usd: costsComplete ? knownApiEquivalentCost : null,
    measurement_effective_cost_usd: costsComplete ? knownEffectiveCost : null,
    known_measurement_actual_cost_usd: knownActualCost,
    known_measurement_api_equivalent_cost_usd: knownApiEquivalentCost,
    known_measurement_effective_cost_usd: knownEffectiveCost,
    cost_complete_outcomes: costComplete.length,
    unknown_cost_outcomes: verified.length - costComplete.length,
    cost_coverage: verified.length ? costComplete.length / verified.length : 0,
    cost_per_measured_usd: costsComplete && verified.length ? knownEffectiveCost / verified.length : null,
    cost_per_improved_usd: costsComplete && improved ? knownEffectiveCost / improved : null,
    cost_per_verified_benefit_usd: costsComplete && improved ? knownEffectiveCost / improved : null,
    actual_cost_per_verified_benefit_usd: costsComplete && improved ? knownActualCost / improved : null,
    api_equivalent_cost_per_verified_benefit_usd: costsComplete && improved ? knownApiEquivalentCost / improved : null,
    effective_cost_per_verified_benefit_usd: costsComplete && improved ? knownEffectiveCost / improved : null,
    operator_interventions: interventions,
    operator_interventions_per_verified_benefit: improved ? interventions / improved : null,
  };
}

function formatMoney(value: number | null | undefined): string {
  if (value == null) return "—";
  return `${value.toLocaleString("de-DE", { minimumFractionDigits: 2, maximumFractionDigits: 4 })}\u00a0$`;
}

function formatRate(value: number | null | undefined): string {
  return value == null ? "—" : `${Math.round(value * 100)}%`;
}

function outcomeCostLabel(proposal: Proposal): string {
  const cost = costDimensions(proposal);
  if (proposal.outcome_cost_status !== "complete") {
    return `Kosten unvollständig · bekannte Anteile: Ist-Kosten ${formatMoney(cost.actual)} · API-Äquivalent ${formatMoney(cost.apiEquivalent)}`;
  }
  return `Effektiv ${formatMoney(cost.effective)} · Ist-Kosten ${formatMoney(cost.actual)} · API-Äquivalent ${formatMoney(cost.apiEquivalent)}`;
}

function outcomeLabel(proposal: Proposal): string {
  if (proposal.outcome_verdict === "improved") {
    return proposal.evidence_grade === "contract_verified"
      ? de.autoresearch.outcomeImproved
      : de.autoresearch.outcomeLegacyImproved;
  }
  switch (proposal.outcome_verdict) {
    case "neutral": return de.autoresearch.outcomeNeutral;
    case "worsened": return de.autoresearch.outcomeWorsened;
    case "unmeasurable": return de.autoresearch.outcomeUnmeasurable;
    case "confounded": return de.autoresearch.outcomeConfounded;
    default:
      if (proposal.measurement_status === "measuring") return de.autoresearch.outcomeMeasuring;
      if (proposal.measurement_status === "exhausted") return de.autoresearch.outcomeExhausted;
      if (proposal.measurement_status === "retryable_failure") return de.autoresearch.outcomeRetryable;
      return de.autoresearch.outcomePending;
  }
}

function outcomeTone(proposal: Proposal): "emerald" | "amber" | "red" | "zinc" | "cyan" {
  if (proposal.outcome_verdict === "improved" && proposal.evidence_grade === "contract_verified") return "emerald";
  if (proposal.outcome_verdict === "worsened") return "red";
  if (proposal.outcome_verdict === "neutral") return "cyan";
  if (proposal.outcome_verdict === "confounded" || proposal.measurement_status === "retryable_failure") return "amber";
  return "zinc";
}

function observationValue(value: Record<string, unknown> | null | undefined): string {
  if (!value) return "—";
  const nested = value.observed_value;
  const observation = nested && typeof nested === "object" ? nested as Record<string, unknown> : value;
  const metric = typeof observation.metric === "string" ? observation.metric : "Messwert";
  const observed = observation.value;
  return `${metric}: ${typeof observed === "number" || typeof observed === "string" ? observed : "—"}`;
}

function compactJson(value: unknown): string {
  if (value == null) return "keine";
  return JSON.stringify(value);
}

export function OutcomePanel({
  metrics,
  proposals,
}: {
  metrics: AutoresearchOutcomeMetrics | null | undefined;
  proposals: Proposal[];
}) {
  const derived = deriveMetrics(proposals);
  const data = metrics ? { ...derived, ...metrics } : derived;
  const integratedCount = proposals.filter((proposal) => proposal.outcome_applicability === "applicable" && proposal.delivery_state === "integrated").length;
  const evidence = proposals
    .filter((proposal) => proposal.outcome_applicability === "applicable" && (
      proposal.probe_contract
      || ["measuring", "measured", "retryable_failure", "exhausted"].includes(proposal.measurement_status ?? "")
    ))
    .sort((a, b) => (b.outcome_measured_at ?? 0) - (a.outcome_measured_at ?? 0))
    .slice(0, 5);
  const hasVerifiedEvidence = evidence.some((proposal) => proposal.evidence_grade === "contract_verified");
  const aggregateCostLabel = data.measurement_effective_cost_usd == null
    ? `Effektiv — · bekannte Anteile: Ist-Kosten ${formatMoney(data.known_measurement_actual_cost_usd)} · API-Äquivalent ${formatMoney(data.known_measurement_api_equivalent_cost_usd)}`
    : `Effektiv ${formatMoney(data.measurement_effective_cost_usd)} · Ist-Kosten ${formatMoney(data.measurement_actual_cost_usd)} · API-Äquivalent ${formatMoney(data.measurement_api_equivalent_cost_usd)}`;

  return (
    <section id="autoresearch-outcomes" aria-label={de.autoresearch.outcomeHeading}>
    <Card surface="raised" className="overflow-hidden border-line p-0">
      <div className="space-y-4 p-4 sm:p-5">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <Eyebrow>{de.autoresearch.outcomeHeading}</Eyebrow>
              <SignalChip tone={signalToneFromLegacy(data.verified_improved > 0 ? "emerald" : "zinc")} label={de.autoresearch.outcomeHonesty} />
            </div>
            <Text as="h2" variant="subtitle" className="mt-2 text-ink">Was hat nachweislich geholfen?</Text>
            <p className="mt-1 max-w-3xl text-sm leading-6 text-ink-2">{de.autoresearch.outcomeSubheading}</p>
          </div>
          <div className="flex items-center gap-2 rounded-card border border-line bg-surface-2 px-3 py-2 text-xs text-ink-2">
            <ShieldCheck aria-hidden className="h-4 w-4 text-live" />
            {data.verified_measured} von {integratedCount} anwendbaren Änderungen gemessen
          </div>
        </div>

        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
          <KpiTile label={de.autoresearch.outcomeImproved} value={String(data.verified_improved)} />
          <KpiTile label={de.autoresearch.outcomeIntegrated} value={String(integratedCount)} />
          <KpiTile label={de.autoresearch.outcomeCoverage} value={formatRate(data.outcome_coverage)} />
          <KpiTile label={de.autoresearch.outcomeBenefitRate} value={`${formatRate(data.verified_benefit_rate)} · n=${data.verified_directional_denominator}`} />
          <KpiTile label={de.autoresearch.outcomeCostPerBenefit} value={formatMoney(data.effective_cost_per_verified_benefit_usd)} />
        </div>

        <div className="flex flex-wrap gap-2 text-xs">
          <SignalChip tone={signalToneFromLegacy("zinc")} label={`${data.legacy_improved} ${de.autoresearch.outcomeLegacyImproved}`} />
          <SignalChip tone={signalToneFromLegacy("cyan")} label={`${data.neutral} ${de.autoresearch.outcomeNeutral}`} />
          <SignalChip tone={signalToneFromLegacy("red")} label={`${data.worsened} ${de.autoresearch.outcomeWorsened} · ${formatRate(data.regression_rate)}`} />
          <SignalChip tone={signalToneFromLegacy("zinc")} label={`${data.unmeasurable} ${de.autoresearch.outcomeUnmeasurable}`} />
          <SignalChip tone={signalToneFromLegacy("amber")} label={`${data.confounded} ${de.autoresearch.outcomeConfounded}`} />
          <SignalChip tone={signalToneFromLegacy("zinc")} label={`${data.pending} ${de.autoresearch.outcomePending}`} />
          <span className="inline-flex min-h-7 items-center gap-1 rounded-full border border-line px-2.5 py-1 text-ink-2">
            <CircleDollarSign aria-hidden className="h-3.5 w-3.5" /> {aggregateCostLabel} · Kostenabdeckung {data.cost_complete_outcomes}/{data.verified_measured} · Eingriffe/Nutzen {data.operator_interventions_per_verified_benefit ?? "—"}
          </span>
        </div>

        <Disclosure
          defaultOpen={false}
          className="rounded-panel border border-line bg-surface-2 px-3 py-2"
          summary={
            <span className="flex min-h-12 min-w-0 flex-1 items-center justify-between gap-3">
              <span className="flex min-w-0 items-center gap-2 font-semibold text-ink"><Activity aria-hidden className="h-4 w-4 shrink-0 text-live" />{de.autoresearch.outcomeEvidence}</span>
              <SignalChip tone={hasVerifiedEvidence ? "ok" : "neutral"} label={hasVerifiedEvidence ? "Bestätigter Beleg vorhanden" : "Noch kein bestätigter Beleg"} />
            </span>
          }
        >
          <section aria-label={de.autoresearch.outcomeEvidence} className="pt-3">
          {evidence.length === 0 ? (
            <p className="rounded-panel border border-dashed border-line bg-surface-1 px-3 py-4 text-sm leading-6 text-ink-2">{de.autoresearch.outcomeEvidenceEmpty}</p>
          ) : (
            <div className="grid gap-2">
              {evidence.map((proposal) => (
                <article key={proposal.id} className="rounded-panel border border-line bg-surface-1 p-3">
                  <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                    <div className="min-w-0 space-y-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <SignalChip tone={signalToneFromLegacy(outcomeTone(proposal))} label={outcomeLabel(proposal)} />
                        <SignalChip tone={signalToneFromLegacy(proposal.evidence_grade === "contract_verified" ? "emerald" : "zinc")} label={proposal.evidence_grade === "contract_verified" ? de.autoresearch.outcomeContractVerified : de.autoresearch.outcomeLegacy} />
                      </div>
                      <Text as="h4" variant="label" className="pt-1 break-words text-ink">{proposal.title?.trim() || proposal.target}</Text>
                      <p className="text-xs leading-5 text-ink-2">Nutzenannahme: {proposal.probe_contract?.claim ?? "Für diese Änderung wurde keine kurze Nutzenannahme gespeichert."}</p>
                      <p className="text-xs leading-5 text-ink-2">Vorher {observationValue(proposal.outcome_baseline)} · Nachher {observationValue(proposal.outcome_observation)}</p>
                      <p className="break-words text-xs leading-5 text-ink-3">Erfolgskriterium {compactJson(proposal.probe_contract?.success_rule)} · Schutzmetriken {compactJson(proposal.probe_contract?.counter_rules)} · Beobachtungszeit {compactJson(proposal.probe_contract?.observation_window)}</p>
                      <p className="break-words text-xs leading-5 text-ink-3">Messumgebung {compactJson(proposal.probe_contract?.environment_requirements)} · Messbudget {compactJson(proposal.probe_contract?.measurement_budget)}</p>
                      <p className="break-all font-data text-[11px] text-ink-3">Beleg {String(proposal.outcome_observation?.evidence_ref ?? proposal.outcome_baseline?.evidence_ref ?? "Kein technischer Beleg gespeichert")}</p>
                    </div>
                    <div className="grid shrink-0 gap-1 text-xs text-ink-3 sm:max-w-[320px] sm:text-right">
                      <span className="inline-flex items-center gap-1 sm:justify-end"><BadgeCheck aria-hidden className="h-3.5 w-3.5" />{proposal.probe_contract?.contract_id ?? "Kein Messvertrag gespeichert"}</span>
                      <span className="inline-flex items-center gap-1 font-data sm:justify-end"><GitCommitHorizontal aria-hidden className="h-3.5 w-3.5" />{proposal.outcome_integration_sha?.slice(0, 12) ?? "Kein Integrationsstand gespeichert"}</span>
                      <span>{outcomeCostLabel(proposal)} · {proposal.outcome_operator_interventions ?? 0} Eingriffe</span>
                    </div>
                  </div>
                </article>
              ))}
            </div>
          )}
          </section>
        </Disclosure>
      </div>
    </Card>
    </section>
  );
}
