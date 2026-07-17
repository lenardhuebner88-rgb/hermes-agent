import { ArrowDown, Radar, RotateCw, Sparkles, TriangleAlert } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import type { getAutoresearchActionPlan } from "../../lib/autoresearchActionPlan";
import type { getAutoresearchRecommendation } from "../../lib/autoresearchRecommendation";
import type { getProposalOperatorBrief } from "../../lib/autoresearchProposalBrief";
import type { describeLoopStatus } from "../../lib/autoresearch";
import type { describeTopCardMode } from "../../lib/autoresearchDecisionGuide";
import type { useAutoresearchStatus } from "../../hooks/autoresearch";
import type { AutoresearchSectionNavItem } from "../../lib/autoresearchNavigation";
import type { AutoresearchReadinessSummary } from "../../lib/autoresearchReadiness";
import type { Proposal, ToneName } from "../../lib/types";
import { StaleBadge } from "../../components/atoms";
import { KpiTile, SignalChip, signalToneFromLegacy } from "../../components/leitstand";
import { Card, Eyebrow, Text } from "../../components/primitives";
import { CockpitSectionNav, Metric, ReadinessPanel } from "./panels";
import { OperatorActionsDisclosure } from "./OperatorActionsDisclosure";

type Recommendation = ReturnType<typeof getAutoresearchRecommendation>;
type LoopStatus = ReturnType<typeof describeLoopStatus>;
type TopCardMode = ReturnType<typeof describeTopCardMode>;
type TopProposalBrief = ReturnType<typeof getProposalOperatorBrief>;
type ActionPlan = ReturnType<typeof getAutoresearchActionPlan>;

export function AutoresearchHero({
  status,
  statusTone,
  loop,
  recommendation,
  topProposal,
  topCardMode,
  topProposalBrief,
  readiness,
  openCount,
  highPriorityCount,
  revertedCount,
  lastRunLabel,
  sectionNavItems,
  actionPlan,
  storeBusy,
  openSkillCount,
  openSkillManualReviewCount,
  canApplyAllOpenSkills,
  pruneBusy,
  alert,
  onPrimary,
  onFocusProposal,
  onJump,
  onGenerate,
  onGenerateCodeWeaknesses,
  onApplyAll,
  onPrune,
}: {
  status: ReturnType<typeof useAutoresearchStatus>;
  statusTone: ToneName;
  loop: LoopStatus;
  recommendation: Recommendation;
  topProposal: Proposal | null;
  topCardMode: TopCardMode | null;
  topProposalBrief: TopProposalBrief | null;
  readiness: AutoresearchReadinessSummary;
  openCount: number;
  highPriorityCount: number;
  revertedCount: number;
  lastRunLabel: string;
  sectionNavItems: readonly AutoresearchSectionNavItem[];
  actionPlan: ActionPlan;
  storeBusy: string | null;
  openSkillCount: number;
  openSkillManualReviewCount: number;
  canApplyAllOpenSkills: boolean;
  pruneBusy: boolean;
  alert: { show: boolean; deepAuditRunning: boolean; testFoundryRunning: boolean; deepAuditError?: string | null; testFoundryError?: string | null; deepAuditMessage?: string | null; testFoundryMessage?: string | null; researchErrorBadge: boolean };
  onPrimary: () => void;
  onFocusProposal: (id: string) => void;
  onJump: (id: string) => void;
  onGenerate: () => void;
  onGenerateCodeWeaknesses: (scope: "incremental" | "full" | "deep") => void;
  onApplyAll: () => void;
  onPrune: () => void;
}) {
  return (
    <Card surface="raised" className="overflow-hidden border-line p-0">
      <div className="grid gap-0">
        <div className="space-y-5 p-4 sm:p-6">
          <div className="flex flex-wrap items-center gap-2">
            {status.loading ? <Spinner /> : <SignalChip tone={signalToneFromLegacy(statusTone)} label={status.data?.state ?? "unbekannt"} />}
            <SignalChip tone={signalToneFromLegacy(loop.routeTone)} label={`Route ${status.data?.route_status ?? "unbekannt"}`} />
            <SignalChip tone={signalToneFromLegacy(recommendation.tone)} label={recommendation.eyebrow} />
            <span className="rounded-full border border-line px-2.5 py-1 text-xs text-ink-2">{loop.iterationLabel}</span>
            <StaleBadge isStale={status.isStale} lastUpdated={status.lastUpdated} errorObj={status.errorObj} error={status.error} />
          </div>
          {alert.show ? <HeroAlertStrip alert={alert} onJump={onJump} /> : null}
          <div>
            <Eyebrow>Autoresearch-Leitstand</Eyebrow>
            <Text as="h1" variant="title" className="mt-2 max-w-3xl text-ink">{recommendation.title}</Text>
            <p className="mt-3 max-w-2xl text-sm leading-6 text-ink-2 sm:text-base sm:leading-7">{recommendation.detail}</p>
            {topProposal ? (
              <div className="mt-3 max-w-3xl rounded-panel border border-line bg-surface-2 px-3 py-3 text-sm text-ink">
                <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <Eyebrow>Als Erstes</Eyebrow>
                      {topProposalBrief ? <SignalChip tone={signalToneFromLegacy(topProposalBrief.tone)} label={topProposalBrief.label} /> : null}
                      {topCardMode ? <SignalChip tone={signalToneFromLegacy(topCardMode.tone)} label={topCardMode.label} /> : null}
                    </div>
                    <Text as="h2" variant="subtitle" className="mt-2 text-ink">{topProposalBrief?.title ?? "Nächste Karte prüfen."}</Text>
                    <p className="mt-1 text-sm leading-6 text-ink-2">{topProposalBrief?.summary ?? topProposal.title?.trim() ?? topProposal.target}</p>
                    {topCardMode ? <p className="mt-1 text-xs leading-5 text-ink-3">{topCardMode.detail}</p> : null}
                  </div>
                  <Button outlined className="min-h-12 shrink-0 justify-center" onClick={() => onFocusProposal(topProposal.id)} prefix={<ArrowDown className="h-4 w-4" />}>Top-Karte öffnen</Button>
                </div>
                {topProposalBrief ? (
                  <div className="mt-3 grid gap-2 sm:grid-cols-3">
                    {topProposalBrief.facts.map((fact) => (
                      <KpiTile key={fact.label} label={fact.label} value={fact.value} className="min-w-0" />
                    ))}
                  </div>
                ) : null}
              </div>
            ) : null}
            {status.error ? <div className="mt-2 flex items-start gap-2 rounded-card border border-status-alert/30 bg-status-alert/10 px-3 py-2 text-sec text-status-alert"><TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />{status.error}</div> : null}
          </div>
          <div className="flex flex-col gap-2 sm:flex-row sm:flex-wrap">
            <Button className="min-h-12" onClick={onPrimary} disabled={recommendation.kind === "generate" && !!storeBusy} prefix={recommendation.kind === "review" ? <ArrowDown className="h-4 w-4" /> : recommendation.kind === "monitor" || recommendation.kind === "recover" || recommendation.kind === "inspect" ? <Radar className="h-4 w-4" /> : storeBusy === "generate" ? <Spinner /> : <Sparkles className="h-4 w-4" />}>
              {recommendation.primaryLabel}
            </Button>
          </div>
          <ReadinessPanel summary={readiness} />
          <div className="grid gap-3 sm:grid-cols-4">
            <Metric label="Offen" value={String(openCount)} />
            <Metric label="Hoch+" value={String(highPriorityCount)} />
            <Metric label="Zurückgerollt" value={String(revertedCount)} />
            <Metric label="Letzter Lauf" value={lastRunLabel} />
          </div>
          <CockpitSectionNav items={sectionNavItems} onJump={onJump} />
        </div>
        <OperatorActionsDisclosure
          actionPlan={actionPlan}
          storeBusy={storeBusy}
          openSkillCount={openSkillCount}
          openSkillManualReviewCount={openSkillManualReviewCount}
          canApplyAllOpenSkills={canApplyAllOpenSkills}
          pruneBusy={pruneBusy}
          onGenerate={onGenerate}
          onGenerateCodeWeaknesses={onGenerateCodeWeaknesses}
          onApplyAll={onApplyAll}
          onOpenReview={() => onJump("autoresearch-queue")}
          onPrune={onPrune}
        />
      </div>
    </Card>
  );
}

function HeroAlertStrip({ alert, onJump }: { alert: { deepAuditRunning: boolean; testFoundryRunning: boolean; deepAuditError?: string | null; testFoundryError?: string | null; deepAuditMessage?: string | null; testFoundryMessage?: string | null; researchErrorBadge: boolean }; onJump: (id: string) => void }) {
  const isError = !!(alert.deepAuditError || alert.testFoundryError || alert.researchErrorBadge);
  return (
    <div className={`flex items-start gap-2 rounded-card border px-3 py-2 text-sec ${isError ? "border-status-alert/30 bg-status-alert/10 text-status-alert" : "border-status-warn/30 bg-status-warn/10 text-status-warn"}`}>
      <TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />
      <div className="flex w-full flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0">
          <span className="font-semibold">Speziallauf braucht Aufmerksamkeit.</span>{" "}
          <span>
            {alert.deepAuditRunning ? "Deep-Audit läuft. " : ""}
            {alert.testFoundryRunning ? "Test-Foundry läuft. " : ""}
            {alert.deepAuditError ? `Deep-Audit: ${alert.deepAuditError}. ` : ""}
            {alert.testFoundryError ? `Test-Foundry: ${alert.testFoundryError}. ` : ""}
            {alert.deepAuditMessage ? `${alert.deepAuditMessage}. ` : ""}
            {alert.testFoundryMessage ? `${alert.testFoundryMessage}. ` : ""}
            {alert.researchErrorBadge ? "Der letzte Research-Lauf meldet Research-Errors." : ""}
          </span>
        </div>
        <Button outlined className="min-h-12 shrink-0" onClick={() => onJump("autoresearch-advanced")} prefix={<RotateCw className="h-4 w-4" />}>Erweitert öffnen</Button>
      </div>
    </div>
  );
}
