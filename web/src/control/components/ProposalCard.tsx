import { Check, ShieldAlert, X } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { cn } from "@/lib/utils";
import { toDiffLines } from "../lib/diff";
import { de } from "../i18n/de";
import type { Density } from "../hooks/useDensity";
import type { Proposal } from "../lib/types";
import { DiffView, ModeBadge, ToneCallout } from "./atoms";

interface Props {
  proposal: Proposal;
  density: Density;
  busy?: boolean;
  onApply: (proposal: Proposal) => void;
  onSkip: (proposal: Proposal) => void;
}

function proposalTitle(proposal: Proposal): string {
  return proposal.title?.trim() || `${proposal.target}${proposal.section ? ` · ${proposal.section}` : ""}`;
}

export function ProposalCard({ proposal, density, busy, onApply, onSkip }: Props) {
  const lines = toDiffLines(proposal.diff_before_after);
  const isCode = proposal.mode === "code";
  const isTesting = proposal.status === "testing";
  const isDone = proposal.status === "applied" || proposal.status === "skipped";
  const isActionable = proposal.status === "proposed";
  return (
    <article className={cn("hc-card space-y-4 p-4", density === "compact" && "p-3")}>
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0 space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <ModeBadge mode={proposal.mode} />
            {isTesting ? <span className="inline-flex items-center gap-1.5 rounded-full border border-amber-500/20 bg-amber-500/10 px-2 py-0.5 text-xs text-amber-200"><Spinner />{de.autoresearch.testing}</span> : null}
            {isDone ? <span className="rounded-full border border-emerald-500/20 bg-emerald-500/10 px-2 py-0.5 text-xs text-emerald-200">{proposal.status === "applied" ? "Erledigt" : "Übersprungen"}</span> : null}
          </div>
          <h3 className="text-lg font-semibold leading-snug text-white">{proposalTitle(proposal)}</h3>
          <div className="space-y-1">
            <p className="hc-eyebrow">{de.autoresearch.why}</p>
            <p className="text-sm leading-6 hc-soft">{proposal.rationale_plain || "Keine Begründung geliefert."}</p>
          </div>
        </div>
      </div>

      {isCode && !isDone ? (
        <ToneCallout tone="amber">
          {isTesting ? <Spinner /> : <ShieldAlert className="mr-2 inline h-4 w-4" />}
          {isTesting ? de.autoresearch.codeGateTesting : de.autoresearch.codeGate}
        </ToneCallout>
      ) : null}

      <DiffView lines={lines} showLineNumbers={density === "compact"} collapsible defaultCollapsed />

      {isDone ? (
        <ToneCallout tone={proposal.status === "applied" ? "emerald" : "amber"}>{proposal.result || (proposal.status === "applied" ? de.autoresearch.applied : de.autoresearch.skipped)}</ToneCallout>
      ) : isTesting ? (
        <ToneCallout tone="violet"><Spinner />{proposal.result || de.autoresearch.codeGateTesting}</ToneCallout>
      ) : isActionable ? (
        <div className="flex flex-col gap-2 sm:flex-row sm:justify-end">
          <Button outlined className="hc-hit" onClick={() => onSkip(proposal)} disabled={busy} prefix={busy ? <Spinner /> : <X className="h-4 w-4" />}>
            {de.autoresearch.skip}
          </Button>
          <Button className="hc-hit" onClick={() => onApply(proposal)} disabled={busy} prefix={busy ? <Spinner /> : <Check className="h-4 w-4" />}>
            {isCode ? de.autoresearch.applyCode : de.autoresearch.apply}
          </Button>
        </div>
      ) : null}
    </article>
  );
}
