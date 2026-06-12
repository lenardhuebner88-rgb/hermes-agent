import { ArrowDown, Radar, RotateCw, Sparkles } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { cn } from "@/lib/utils";
import type { getAutoresearchActionPlan } from "../../lib/autoresearchActionPlan";
import type { getAutoresearchRecommendation } from "../../lib/autoresearchRecommendation";
import type { getProposalOperatorBrief } from "../../lib/autoresearchProposalBrief";
import type { describeLoopStatus } from "../../lib/autoresearch";
import type { describeTopCardMode } from "../../lib/autoresearchDecisionGuide";
import type { useAutoresearchStatus } from "../../hooks/useControlData";
import type { AutoresearchSectionNavItem } from "../../lib/autoresearchNavigation";
import type { AutoresearchReadinessSummary } from "../../lib/autoresearchReadiness";
import type { Proposal, ToneName } from "../../lib/types";
import { StatusPill, ToneCallout } from "../../components/atoms";
import { Card, Text } from "../../components/primitives";
import { CockpitSectionNav, Metric, ReadinessPanel } from "./panels";
import { reviewStepToneClass } from "./panels.helpers";
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
    <Card surface="raised" className="overflow-hidden border-[var(--hc-border-strong)] p-0">
      <div className="grid gap-0">
        <div className="space-y-5 p-4 sm:p-6">
          <div className="flex flex-wrap items-center gap-2">
            {status.loading ? <Spinner /> : <StatusPill tone={statusTone} label={status.data?.state ?? "unbekannt"} dot={loop.running ? "live" : status.data?.state === "crashed" ? "error" : "idle"} />}
            <StatusPill tone={loop.routeTone} label={`Route ${status.data?.route_status ?? "unbekannt"}`} dot={loop.routeTone === "emerald" ? "ready" : "warn"} />
            <StatusPill tone={recommendation.tone} label={recommendation.eyebrow} />
            <span className="rounded-full border border-white/10 px-2.5 py-1 text-xs hc-soft">{loop.iterationLabel}</span>
          </div>
          {alert.show ? <HeroAlertStrip alert={alert} onJump={onJump} /> : null}
          <div>
            <p className="hc-eyebrow">Autoresearch Cockpit</p>
            <Text as="h1" variant="title" className="mt-2 max-w-3xl text-white">{recommendation.title}</Text>
            <p className="mt-3 max-w-2xl text-sm leading-6 hc-soft sm:text-base sm:leading-7">{recommendation.detail}</p>
            {topProposal ? (
              <div className="mt-3 max-w-3xl rounded-lg border border-white/10 bg-white/[.03] px-3 py-3 text-sm text-white">
                <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <p className="hc-eyebrow">Als Erstes</p>
                      {topProposalBrief ? <StatusPill tone={topProposalBrief.tone} label={topProposalBrief.label} /> : null}
                      {topCardMode ? <StatusPill tone={topCardMode.tone} label={topCardMode.label} /> : null}
                    </div>
                    <Text as="h2" variant="subtitle" className="mt-2 text-white">{topProposalBrief?.title ?? "Nächste Karte prüfen."}</Text>
                    <p className="mt-1 text-sm leading-6 hc-soft">{topProposalBrief?.summary ?? topProposal.title?.trim() ?? topProposal.target}</p>
                    {topCardMode ? <p className="mt-1 text-xs leading-5 hc-dim">{topCardMode.detail}</p> : null}
                  </div>
                  <Button outlined className="hc-hit shrink-0 justify-center" onClick={() => onFocusProposal(topProposal.id)} prefix={<ArrowDown className="h-4 w-4" />}>Top-Karte öffnen</Button>
                </div>
                {topProposalBrief ? (
                  <div className="mt-3 grid gap-2 sm:grid-cols-3">
                    {topProposalBrief.facts.map((fact) => (
                      <div key={fact.label} className={cn("min-w-0 rounded-md border px-2.5 py-2", reviewStepToneClass(fact.tone))}>
                        <p className="text-[10px] font-semibold uppercase tracking-[.12em] hc-dim">{fact.label}</p>
                        <p className="mt-1 line-clamp-2 text-xs leading-5 hc-soft" title={fact.value}>{fact.value}</p>
                      </div>
                    ))}
                  </div>
                ) : null}
              </div>
            ) : null}
            {status.error ? <p className="mt-2 text-sm text-red-200">{status.error}</p> : null}
          </div>
          <div className="flex flex-col gap-2 sm:flex-row sm:flex-wrap">
            <Button className="hc-hit" onClick={onPrimary} disabled={recommendation.kind === "generate" && !!storeBusy} prefix={recommendation.kind === "review" ? <ArrowDown className="h-4 w-4" /> : recommendation.kind === "monitor" || recommendation.kind === "recover" || recommendation.kind === "inspect" ? <Radar className="h-4 w-4" /> : storeBusy === "generate" ? <Spinner /> : <Sparkles className="h-4 w-4" />}>
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
  const tone = alert.deepAuditError || alert.testFoundryError || alert.researchErrorBadge ? "red" : "amber";
  return (
    <ToneCallout tone={tone}>
      <div className="flex w-full flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0">
          <span className="font-semibold text-white">Speziallauf braucht Aufmerksamkeit.</span>{" "}
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
        <Button outlined className="hc-hit shrink-0" onClick={() => onJump("autoresearch-advanced")} prefix={<RotateCw className="h-4 w-4" />}>Erweitert öffnen</Button>
      </div>
    </ToneCallout>
  );
}
