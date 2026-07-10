import { useState } from "react";
import { Check, ShieldAlert, TriangleAlert, X } from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { cn } from "@/lib/utils";
import { toDiffLines } from "../lib/diff";
import { de } from "../i18n/de";
import { getProposalSeverity, proposalAgeDays, severityTone, type ProposalPriorityGroup } from "../lib/autoresearch";
import { getProposalOperatorBrief, type ProposalOperatorBrief } from "../lib/autoresearchProposalBrief";
import { formatProposalCategory } from "../lib/autoresearchProposalLabels";
import type { Density } from "../hooks/useDensity";
import type { Proposal, ProposalSeverity, ToneName } from "../lib/types";
import { DiffView, ModeBadge } from "./atoms";
import { SignalChip, SignalLabel, signalToneFromLegacy, type SignalTone } from "./leitstand";
import { Eyebrow } from "./primitives";

const SEVERITY_LABEL: Record<ProposalSeverity, string> = {
  critical: de.autoresearch.severityCritical,
  high: de.autoresearch.severityHigh,
  medium: de.autoresearch.severityMedium,
  low: de.autoresearch.severityLow,
};

interface Props {
  proposal: Proposal;
  density: Density;
  busy?: boolean;
  selected?: boolean;
  selectable?: boolean;
  batchSelectable?: boolean;
  batchStatus?: { status: "pending" | "ok" | "fail"; detail?: string };
  priorityGroup?: ProposalPriorityGroup;
  onApply: (proposal: Proposal) => void;
  onSkip: (proposal: Proposal) => void;
  onSelectedChange?: (proposal: Proposal, selected: boolean) => void;
}

function proposalTitle(proposal: Proposal): string {
  return proposal.title?.trim() || `${proposal.target}${proposal.section ? ` · ${proposal.section}` : ""}`;
}

interface DecisionGuide {
  label: string;
  tone: ToneName;
  benefit: string;
  risk: string;
  next: string;
  consequence: string;
}

interface ActionOutcome {
  label: string;
  value: string;
  tone: ToneName;
}

const PROPOSAL_CALLOUT_CLASS: Record<SignalTone, string> = {
  ok: "border-status-ok/30 bg-status-ok/10 text-status-ok",
  warn: "border-status-warn/30 bg-status-warn/10 text-status-warn",
  alert: "border-status-alert/30 bg-status-alert/10 text-status-alert",
  neutral: "border-line bg-surface-2 text-ink-2",
};

function ProposalCallout({ tone, label, children }: { tone: SignalTone; label: string; children: React.ReactNode }) {
  return (
    <div role={tone === "alert" ? "alert" : undefined} className={cn("flex items-start gap-2 rounded-card border px-3 py-2 text-sec", PROPOSAL_CALLOUT_CLASS[tone])}>
      {tone === "alert" ? <TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" /> : <SignalLabel tone={tone} label={label} className="mt-0.5 shrink-0" />}
      <span className="min-w-0">{children}</span>
    </div>
  );
}

function decisionGuide(proposal: Proposal, severity: ProposalSeverity): DecisionGuide {
  if (proposal.last_outcome === "reverted_no_improvement") {
    return {
      label: "Archivieren empfohlen",
      tone: "zinc",
      benefit: "Der Kandidat ist bereits automatisch ohne Verbesserung zurückgerollt.",
      risk: "Ein erneuter Lauf kostet Zeit und sollte nur bewusst passieren.",
      next: "Archivieren, außer du willst genau diesen Kandidaten nochmal prüfen.",
      consequence: "Archivieren räumt ihn weg; erneut prüfen startet ihn bewusst neu.",
    };
  }
  if (proposal.mode === "code") {
    return {
      label: severity === "critical" || severity === "high" ? "Einzeln prüfen" : "Code-Gate prüfen",
      tone: "amber",
      benefit: "Behebt ein konkretes Code-Signal mit Test-Gate statt Blind-Änderung.",
      risk: "Schreibt Code, läuft aber durch die Test-Suite und rollbackt bei rotem Lauf.",
      next: "Diff kurz lesen, dann Code übernehmen, wenn die Änderung fachlich passt.",
      consequence: "Übernehmen schreibt die Code-Änderung und startet direkt die Test-Suite. Bei rotem Lauf wird automatisch zurückgerollt.",
    };
  }
  if (proposal.mode === "test" || proposal.proposal_type === "mutation_test") {
    return {
      label: "Test-Härtung",
      tone: "cyan",
      benefit: "Macht vorhandene Tests stärker und reduziert stille Regressionen.",
      risk: "Kann zusätzliche Laufzeit erzeugen, verändert aber keinen Produktivcode.",
      next: "Übernehmen, wenn Ziel und Diff zum beschriebenen Risiko passen.",
      consequence: "Übernehmen legt den Härtungs-Test als geprüften Vorschlag an. Die Änderung ist auf Test-Sicherheit optimiert.",
    };
  }
  return {
    label: severity === "critical" || severity === "high" ? "Sinnvoll übernehmen" : "Niedriges Risiko",
    tone: severity === "critical" || severity === "high" ? "emerald" : "cyan",
    benefit: "Verbessert Skill-Verhalten ohne Code-Gate oder Branch-Wechsel.",
    risk: "Wirkt direkt auf den Skill-Text; Überspringen hat keine Nebenwirkung.",
    next: "Begründung prüfen und übernehmen, wenn sie zur Arbeitsweise passt.",
    consequence: "Übernehmen schreibt den Skill-Vorschlag direkt. Überspringen verwirft ihn ohne weitere Wirkung.",
  };
}

function actionOutcomes(proposal: Proposal, guide: DecisionGuide): ActionOutcome[] {
  if (proposal.status === "applied") {
    return [
      { label: "Stand", value: "Schon übernommen.", tone: "emerald" },
      { label: "Nächster Klick", value: "Keine Entscheidung offen.", tone: "zinc" },
      { label: "Ergebnis", value: proposal.result || "Änderung wurde angewendet.", tone: "emerald" },
    ];
  }
  if (proposal.status === "skipped") {
    return [
      { label: "Stand", value: "Schon übersprungen.", tone: "zinc" },
      { label: "Nächster Klick", value: "Keine Übernahme offen.", tone: "zinc" },
      { label: "Ergebnis", value: proposal.result || "Vorschlag wurde aussortiert.", tone: "zinc" },
    ];
  }
  if (proposal.status === "testing") {
    return [
      { label: "Stand", value: "Gate läuft gerade.", tone: "violet" },
      { label: "Nächster Klick", value: "Warten, bis das Ergebnis da ist.", tone: "zinc" },
      { label: "Schutz", value: "Währenddessen keine zweite Entscheidung nötig.", tone: "violet" },
    ];
  }
  if (proposal.last_outcome === "reverted_no_improvement") {
    return [
      { label: "Archivieren", value: "Räumt die Karte aus den offenen Entscheidungen.", tone: "zinc" },
      { label: "Erneut prüfen", value: "Startet genau diesen Kandidaten bewusst neu.", tone: "amber" },
      { label: "Schutz", value: "Er war schon getestet und ohne Nutzen zurückgerollt.", tone: "zinc" },
    ];
  }
  if (proposal.mode === "code") {
    return [
      { label: "Übernehmen", value: "Schreibt Code und startet danach das Gate.", tone: "amber" },
      { label: "Überspringen", value: "Keine Datei wird geändert; die Karte ist erledigt.", tone: "zinc" },
      { label: "Schutz", value: "Roter Lauf wird automatisch zurückgerollt.", tone: "violet" },
    ];
  }
  if (proposal.mode === "test" || proposal.proposal_type === "mutation_test") {
    return [
      { label: "Übernehmen", value: "Legt die Test-Härtung an.", tone: "cyan" },
      { label: "Überspringen", value: "Produktivcode bleibt unverändert.", tone: "zinc" },
      { label: "Schutz", value: "Der Vorschlag zielt auf Absicherung, nicht Feature-Code.", tone: "cyan" },
    ];
  }
  return [
    { label: "Übernehmen", value: "Schreibt den Skill-Text direkt.", tone: guide.tone },
    { label: "Überspringen", value: "Keine Änderung; der Vorschlag wird aussortiert.", tone: "zinc" },
    { label: "Schutz", value: "Kein Code-Gate, kein Branch-Wechsel.", tone: "cyan" },
  ];
}

export function ProposalCard({ proposal, density, busy, selected, selectable, batchSelectable = true, batchStatus, priorityGroup, onApply, onSkip, onSelectedChange }: Props) {
  const [reviewConfirmed, setReviewConfirmed] = useState(false);
  const lines = toDiffLines(proposal.diff_before_after);
  const isCode = proposal.mode === "code";
  const isTestHardening = proposal.mode === "test" || proposal.proposal_type === "mutation_test";
  const isTesting = proposal.status === "testing";
  const isDone = proposal.status === "applied" || proposal.status === "skipped";
  const isReverted = proposal.status === "proposed" && proposal.last_outcome === "reverted_no_improvement";
  const isActionable = proposal.status === "proposed";
  const category = formatProposalCategory(proposal.category);
  const severity = getProposalSeverity(proposal);
  const guide = decisionGuide(proposal, severity);
  const brief = getProposalOperatorBrief(proposal);
  const outcomes = actionOutcomes(proposal, guide);
  const evidence = proposal.evidence?.trim() ? proposal.evidence : null;
  const ageDays = isActionable && !isReverted ? proposalAgeDays(proposal) : null;
  const opensDiffByDefault = isActionable && selectable && !batchSelectable;
  const requiresReviewConfirmation = opensDiffByDefault && !isReverted;
  const applyDisabled = !!busy || (requiresReviewConfirmation && !reviewConfirmed);
  return (
    <article id={`autoresearch-proposal-${proposal.id}`} className={cn("scroll-mt-6 space-y-4 rounded-card border border-line bg-surface-2 p-4", density === "compact" && "p-3")}>
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0 space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            {isTestHardening ? <Badge tone="success">Test-Härtung</Badge> : <ModeBadge mode={proposal.mode === "code" ? "code" : "skill"} />}
            <SignalChip tone={signalToneFromLegacy(severityTone(severity))} label={`${de.autoresearch.severity}: ${SEVERITY_LABEL[severity]}`} />
            {category ? <SignalChip tone="neutral" label={`${de.autoresearch.category}: ${category.label}`} /> : null}
            {priorityGroup ? <SignalChip tone={signalToneFromLegacy(priorityGroup.tone)} label={priorityGroup.label} /> : null}
            {batchStatus?.status === "pending" ? <span className="inline-flex items-center gap-1.5 rounded-full border border-line bg-surface-1 px-2.5 py-1 text-micro font-medium text-ink-2"><Spinner />{de.autoresearch.batchPending}</span> : null}
            {batchStatus?.status === "ok" ? <SignalChip tone="ok" label={de.autoresearch.batchOk} /> : null}
            {batchStatus?.status === "fail" ? <SignalChip tone="alert" label={de.autoresearch.batchFail} /> : null}
            {isTesting ? <span className="inline-flex items-center gap-1.5 rounded-full border border-status-warn/30 bg-status-warn/10 px-2.5 py-1 text-micro font-medium text-status-warn"><Spinner />{de.autoresearch.testing}</span> : null}
            {isReverted ? <SignalChip tone="neutral" label="Zurückgerollt" /> : null}
            {ageDays !== null ? <SignalChip tone={ageDays > 7 ? "warn" : "neutral"} label={de.autoresearch.ageDays(ageDays)} className="font-data tabular-nums" /> : null}
            {isDone ? <SignalChip tone={proposal.status === "applied" ? "ok" : "neutral"} label={proposal.status === "applied" ? "Erledigt" : "Übersprungen"} /> : null}
          </div>
          <h3 className="break-words text-emph font-semibold leading-snug text-ink">{proposalTitle(proposal)}</h3>
          {category?.help ? (
            <p className="max-w-3xl text-sec leading-5 text-ink-3">
              <span className="font-semibold text-ink-2">{category.label}:</span> {category.help}
            </p>
          ) : null}
          <ActionOutcomeStrip outcomes={outcomes} />
          <ProposalBriefPanel brief={brief} />
          <div className="space-y-1">
            <Eyebrow>{de.autoresearch.why}</Eyebrow>
            <p className="text-sec leading-6 text-ink-2">{proposal.rationale_plain || "Keine Begründung geliefert."}</p>
          </div>
        </div>
        {selectable ? (
          batchSelectable ? (
            <label className="flex min-h-12 shrink-0 cursor-pointer items-center gap-2 rounded-card border border-line bg-surface-1 px-3 py-2 text-sec text-ink">
              <input
                type="checkbox"
                checked={!!selected}
                onChange={(event) => onSelectedChange?.(proposal, event.target.checked)}
                className="size-12 shrink-0 accent-live"
                aria-label={de.autoresearch.selectProposal}
              />
              <span>{de.autoresearch.select}</span>
            </label>
          ) : (
            <SignalChip tone="warn" label="Einzelreview" title="Diese Karte wird direkt mit Übernehmen oder Überspringen entschieden." />
          )
        ) : null}
      </div>

      {isActionable ? <DecisionGuidePanel guide={guide} /> : null}

      {evidence ? (
        <div className="space-y-1">
          <Eyebrow>{de.autoresearch.evidence}</Eyebrow>
          <blockquote className="whitespace-pre-wrap rounded-card border border-line bg-surface-1 px-3 py-2 text-sec leading-6 text-ink">
            {evidence}
          </blockquote>
        </div>
      ) : null}

      {isCode && !isDone ? (
        <ProposalCallout tone="warn" label="Code-Gate">
          {isTesting ? <Spinner /> : <ShieldAlert className="mr-2 inline h-4 w-4" />}
          {isTesting ? de.autoresearch.codeGateTesting : de.autoresearch.codeGate}
        </ProposalCallout>
      ) : null}

      <div className="space-y-2">
        <Eyebrow>{de.autoresearch.fixDiff}</Eyebrow>
        {opensDiffByDefault ? <p className="text-sec leading-5 text-ink-3">Einzelreview: Diese Änderung ist geöffnet, damit du sie vor Übernehmen oder Überspringen direkt prüfen kannst.</p> : null}
        <DiffView lines={lines} showLineNumbers={density === "compact"} collapsible defaultCollapsed={!opensDiffByDefault} />
      </div>

      {batchStatus?.status === "fail" && batchStatus.detail ? <ProposalCallout tone="alert" label="Fehler">{batchStatus.detail}</ProposalCallout> : null}

      {isDone ? (
        <ProposalCallout tone={proposal.status === "applied" ? "ok" : "warn"} label={proposal.status === "applied" ? "Erledigt" : "Übersprungen"}>{proposal.result || (proposal.status === "applied" ? de.autoresearch.applied : de.autoresearch.skipped)}</ProposalCallout>
      ) : isTesting ? (
        <ProposalCallout tone="neutral" label="Gate läuft"><Spinner />{proposal.result || de.autoresearch.codeGateTesting}</ProposalCallout>
      ) : isActionable ? (
        <div className="space-y-3">
          <ProposalCallout tone={signalToneFromLegacy(guide.tone)} label="Entscheidung">
            <span className="font-semibold">Entscheidung:</span> {guide.consequence}
          </ProposalCallout>
          {requiresReviewConfirmation ? (
            <p className="rounded-card border border-status-warn/30 bg-status-warn/10 px-3 py-2 text-sec leading-5 text-status-warn">
              {de.autoresearch.batchManualReviewHint}
            </p>
          ) : null}
          {requiresReviewConfirmation ? (
            <label className="flex min-h-12 cursor-pointer items-start gap-2 rounded-card border border-status-warn/30 bg-status-warn/10 p-3 text-sec text-status-warn">
              <input
                type="checkbox"
                checked={reviewConfirmed}
                onChange={(event) => setReviewConfirmed(event.target.checked)}
                className="size-12 shrink-0 accent-live"
              />
              <span>
                <span className="block font-medium">Diff geprüft</span>
                <span className="block text-sec leading-5 text-status-warn">Ich habe Änderung und Risiko gelesen; Übernehmen startet danach die beschriebene Aktion.</span>
              </span>
            </label>
          ) : null}
          <div className="flex flex-col gap-2 sm:flex-row sm:justify-end">
            <Button outlined className="min-h-12" onClick={() => onSkip(proposal)} disabled={busy} prefix={busy ? <Spinner /> : <X className="h-4 w-4" />}>
              {isReverted ? "Archivieren" : de.autoresearch.skip}
            </Button>
            <Button className="min-h-12" onClick={() => onApply(proposal)} disabled={applyDisabled} title={requiresReviewConfirmation && !reviewConfirmed ? "Erst Diff geprüft bestätigen." : undefined} prefix={busy ? <Spinner /> : <Check className="h-4 w-4" />}>
              {isReverted ? "Erneut prüfen" : isCode ? de.autoresearch.applyCode : de.autoresearch.apply}
            </Button>
          </div>
        </div>
      ) : null}
    </article>
  );
}

function ActionOutcomeStrip({ outcomes }: { outcomes: ActionOutcome[] }) {
  return (
    <section className="rounded-card border border-line bg-surface-1 p-3" aria-label="Klick-Folgen">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <Eyebrow>Was passiert beim Klick?</Eyebrow>
        <span className="text-sec leading-5 text-ink-3">Kurz vor Diff und Details</span>
      </div>
      <div className="mt-3 grid gap-2 md:grid-cols-3">
        {outcomes.map((outcome) => (
          <div key={outcome.label} className={cn("min-w-0 rounded-card border px-3 py-2", briefFactToneClass(outcome.tone))}>
            <Eyebrow>{outcome.label}</Eyebrow>
            <p className="mt-1 text-sec leading-5 text-ink-2">{outcome.value}</p>
          </div>
        ))}
      </div>
    </section>
  );
}

function ProposalBriefPanel({ brief }: { brief: ProposalOperatorBrief }) {
  return (
    <div className="rounded-card border border-line bg-surface-1 p-3">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <Eyebrow>Kurzbriefing</Eyebrow>
            <SignalChip tone={signalToneFromLegacy(brief.tone)} label={brief.label} />
          </div>
          <h4 className="mt-2 text-sec font-semibold text-ink">{brief.title}</h4>
          <p className="mt-1 text-sec leading-6 text-ink-2">{brief.summary}</p>
        </div>
      </div>
      <div className="mt-3 grid gap-2 lg:grid-cols-3">
        {brief.facts.map((fact) => (
          <div key={fact.label} className={cn("min-w-0 rounded-card border px-3 py-2", briefFactToneClass(fact.tone))}>
            <Eyebrow>{fact.label}</Eyebrow>
            <p className="mt-1 text-sec leading-5 text-ink-2">{fact.value}</p>
          </div>
        ))}
      </div>
    </div>
  );
}

function briefFactToneClass(tone: ToneName): string {
  switch (tone) {
    case "emerald":
      return "border-status-ok/30 bg-status-ok/10";
    case "amber":
      return "border-status-warn/30 bg-status-warn/10";
    case "red":
    case "rose":
      return "border-status-alert/30 bg-status-alert/10";
    default:
      return "border-line bg-surface-2";
  }
}

function DecisionGuidePanel({ guide }: { guide: DecisionGuide }) {
  return (
    <div className="rounded-card border border-line bg-surface-1 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <Eyebrow>Entscheidungshilfe</Eyebrow>
        <SignalChip tone={signalToneFromLegacy(guide.tone)} label={guide.label} />
      </div>
      <div className="mt-3 grid gap-2 md:grid-cols-3">
        <DecisionFact label="Nutzen" value={guide.benefit} />
        <DecisionFact label="Risiko" value={guide.risk} />
        <DecisionFact label="Empfohlen" value={guide.next} />
      </div>
    </div>
  );
}

function DecisionFact({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0 rounded-card border border-line bg-surface-2 px-3 py-2">
      <Eyebrow>{label}</Eyebrow>
      <p className="mt-1 text-sec leading-5 text-ink-2">{value}</p>
    </div>
  );
}
