import { Check, ShieldAlert, X } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { cn } from "@/lib/utils";
import { toDiffLines } from "../lib/diff";
import { de } from "../i18n/de";
import { getProposalSeverity, proposalAgeDays, severityTone, type ProposalPriorityGroup } from "../lib/autoresearch";
import type { Density } from "../hooks/useDensity";
import type { Proposal, ProposalSeverity } from "../lib/types";
import { DiffView, ModeBadge, StatusPill, ToneCallout } from "./atoms";

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
  batchStatus?: { status: "pending" | "ok" | "fail"; detail?: string };
  priorityGroup?: ProposalPriorityGroup;
  onApply: (proposal: Proposal) => void;
  onSkip: (proposal: Proposal) => void;
  onSelectedChange?: (proposal: Proposal, selected: boolean) => void;
}

function proposalTitle(proposal: Proposal): string {
  return proposal.title?.trim() || `${proposal.target}${proposal.section ? ` · ${proposal.section}` : ""}`;
}

export function ProposalCard({ proposal, density, busy, selected, selectable, batchStatus, priorityGroup, onApply, onSkip, onSelectedChange }: Props) {
  const lines = toDiffLines(proposal.diff_before_after);
  const isCode = proposal.mode === "code";
  const isTesting = proposal.status === "testing";
  const isDone = proposal.status === "applied" || proposal.status === "skipped";
  const isReverted = proposal.status === "proposed" && proposal.last_outcome === "reverted_no_improvement";
  const isActionable = proposal.status === "proposed";
  const category = proposal.category?.trim();
  const severity = getProposalSeverity(proposal);
  const evidence = proposal.evidence?.trim() ? proposal.evidence : null;
  const ageDays = isActionable && !isReverted ? proposalAgeDays(proposal) : null;
  return (
    <article className={cn("hc-card space-y-4 p-4", density === "compact" && "p-3")}>
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0 space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <ModeBadge mode={proposal.mode} />
            <StatusPill tone={severityTone(severity)} label={`${de.autoresearch.severity}: ${SEVERITY_LABEL[severity]}`} />
            {category ? <StatusPill tone="cyan" label={`${de.autoresearch.category}: ${category}`} /> : null}
            {priorityGroup ? <StatusPill tone={priorityGroup.tone} label={priorityGroup.label} /> : null}
            {batchStatus?.status === "pending" ? <span className="inline-flex items-center gap-1.5 rounded-full border border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] px-2 py-0.5 text-xs text-[var(--hc-accent-text)]"><Spinner />{de.autoresearch.batchPending}</span> : null}
            {batchStatus?.status === "ok" ? <StatusPill tone="emerald" label={de.autoresearch.batchOk} /> : null}
            {batchStatus?.status === "fail" ? <StatusPill tone="red" label={de.autoresearch.batchFail} /> : null}
            {isTesting ? <span className="inline-flex items-center gap-1.5 rounded-full border border-amber-500/20 bg-amber-500/10 px-2 py-0.5 text-xs text-amber-200"><Spinner />{de.autoresearch.testing}</span> : null}
            {isReverted ? <span className="rounded-full border border-zinc-500/20 bg-zinc-500/10 px-2 py-0.5 text-xs text-zinc-200">Zurückgerollt</span> : null}
            {ageDays !== null ? <span className={cn("rounded-full border px-2 py-0.5 text-xs", ageDays > 7 ? "border-amber-500/20 bg-amber-500/10 text-amber-200" : "border-zinc-500/20 bg-zinc-500/10 text-zinc-200")}>{de.autoresearch.ageDays(ageDays)}</span> : null}
            {isDone ? <span className="rounded-full border border-emerald-500/20 bg-emerald-500/10 px-2 py-0.5 text-xs text-emerald-200">{proposal.status === "applied" ? "Erledigt" : "Übersprungen"}</span> : null}
          </div>
          <h3 className="text-lg font-semibold leading-snug text-white">{proposalTitle(proposal)}</h3>
          <div className="space-y-1">
            <p className="hc-eyebrow">{de.autoresearch.why}</p>
            <p className="text-sm leading-6 hc-soft">{proposal.rationale_plain || "Keine Begründung geliefert."}</p>
          </div>
        </div>
        {selectable ? (
          <label className="flex shrink-0 cursor-pointer items-center gap-2 rounded-lg border border-white/10 bg-white/[.03] px-3 py-2 text-sm text-white">
            <input
              type="checkbox"
              checked={!!selected}
              onChange={(event) => onSelectedChange?.(proposal, event.target.checked)}
              className="h-4 w-4 accent-[var(--hc-accent)]"
              aria-label={de.autoresearch.selectProposal}
            />
            <span>{de.autoresearch.select}</span>
          </label>
        ) : null}
      </div>

      {evidence ? (
        <div className="space-y-1">
          <p className="hc-eyebrow">{de.autoresearch.evidence}</p>
          <blockquote className="whitespace-pre-wrap rounded-lg border border-white/10 bg-black/20 px-3 py-2 text-sm leading-6 text-zinc-100">
            {evidence}
          </blockquote>
        </div>
      ) : null}

      {isCode && !isDone ? (
        <ToneCallout tone="amber">
          {isTesting ? <Spinner /> : <ShieldAlert className="mr-2 inline h-4 w-4" />}
          {isTesting ? de.autoresearch.codeGateTesting : de.autoresearch.codeGate}
        </ToneCallout>
      ) : null}

      <div className="space-y-2">
        <p className="hc-eyebrow">{de.autoresearch.fixDiff}</p>
        <DiffView lines={lines} showLineNumbers={density === "compact"} collapsible defaultCollapsed />
      </div>

      {batchStatus?.status === "fail" && batchStatus.detail ? <ToneCallout tone="red">{batchStatus.detail}</ToneCallout> : null}

      {isDone ? (
        <ToneCallout tone={proposal.status === "applied" ? "emerald" : "amber"}>{proposal.result || (proposal.status === "applied" ? de.autoresearch.applied : de.autoresearch.skipped)}</ToneCallout>
      ) : isTesting ? (
        <ToneCallout tone="violet"><Spinner />{proposal.result || de.autoresearch.codeGateTesting}</ToneCallout>
      ) : isActionable ? (
        <div className="flex flex-col gap-2 sm:flex-row sm:justify-end">
          <Button outlined className="hc-hit" onClick={() => onSkip(proposal)} disabled={busy} prefix={busy ? <Spinner /> : <X className="h-4 w-4" />}>
            {isReverted ? "Archivieren" : de.autoresearch.skip}
          </Button>
          <Button className="hc-hit" onClick={() => onApply(proposal)} disabled={busy} prefix={busy ? <Spinner /> : <Check className="h-4 w-4" />}>
            {isReverted ? "Erneut prüfen" : isCode ? de.autoresearch.applyCode : de.autoresearch.apply}
          </Button>
        </div>
      ) : null}
    </article>
  );
}
