import { Clipboard, ExternalLink, FileText, Quote } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { fmtAge, fmtDur } from "../lib/derive";
import { profileLabel } from "../lib/tones";
import type { DotKind } from "../lib/tones";
import { de } from "../i18n/de";
import type { KanbanResult, ResultQualityBadge, VerificationState } from "../lib/types";
import { StatusPill, ToneCallout } from "./atoms";

function EvidenceChip({ label, value }: { label: string; value: string }) {
  return (
    <span className="inline-flex max-w-full items-center gap-1 rounded-full border border-white/10 bg-white/[.04] px-2.5 py-1 text-xs text-zinc-200">
      <FileText className="h-3.5 w-3.5 shrink-0" />
      <span className="truncate">{label}: {value}</span>
    </span>
  );
}

function formatBytes(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${bytes} B`;
}

function fallbackResultQuality(verificationState: VerificationState, profile: string | null): ResultQualityBadge {
  if (verificationState === "approved") return { state: "verifier_approved", label: "Verifier-approved", tone: "emerald", description: "Independent verifier gate passed." };
  if (verificationState === "request_changes") return { state: "rejected_needs_work", label: "Rejected / needs work", tone: "red", description: "Verifier gate requested changes before this should count as done." };
  if (!profile) return { state: "unknown_legacy", label: "Unknown legacy", tone: "zinc", description: "Legacy run has no verifier metadata or profile lineage." };
  return { state: "ungated", label: "Ungated", tone: "amber", description: "Completed without an independent verifier gate." };
}

function resultQualityDot(state: ResultQualityBadge["state"]): DotKind {
  if (state === "verifier_approved") return "ready";
  if (state === "rejected_needs_work") return "error";
  if (state === "unknown_legacy") return "idle";
  return "warn";
}

export function HermesResultCard({ result, now }: { result: KanbanResult; now: number }) {
  const profile = result.profile ? (profileLabel[result.profile] ?? result.profile) : "Profile unknown";
  const runRoleLabel = result.run_role_label || "Unknown / legacy run";
  const copyPath = async (path: string) => {
    await navigator.clipboard?.writeText(path);
  };
  const verificationState = result.verification_state ?? "ungated";
  const resultQuality = result.result_quality ?? fallbackResultQuality(verificationState, result.profile);
  const verifierEvidence = result.verifier_evidence ?? [];
  const deliverables = result.deliverables ?? [];
  return (
    <article className="hc-card space-y-4 p-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0 space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <StatusPill tone="emerald" label="Completed" dot="ready" />
            <span title={resultQuality.description}>
              <StatusPill tone={resultQuality.tone} label={resultQuality.label} dot={resultQualityDot(resultQuality.state)} />
            </span>
            <span className="rounded-full border border-cyan-400/20 bg-cyan-400/10 px-2 py-0.5 text-xs font-medium text-cyan-100">{runRoleLabel}</span>
            <span className="rounded-full border border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] px-2 py-0.5 text-xs text-[var(--hc-accent-text)]">{profile}</span>
            <span className="hc-mono text-xs hc-soft">{fmtDur(result.duration_seconds)} · vor {fmtAge(result.ended_at, now)}</span>
          </div>
          <h3 className="line-clamp-2 text-base font-semibold leading-snug text-white">{result.task_title}</h3>
          <p className="hc-mono text-xs hc-dim">{result.task_id} · Run {result.run_id}</p>
        </div>
        <a className="inline-flex items-center gap-2 rounded-md border border-white/15 px-3 py-1.5 text-sm text-zinc-100 hover:bg-white/10" href="/plugins/kanban">
          <ExternalLink className="h-4 w-4" />
          {de.hermes.details}
        </a>
      </div>

      {result.summary ? (
        <div className="whitespace-pre-wrap rounded-lg border border-white/10 bg-black/20 p-3 text-sm leading-6 text-zinc-100">{result.summary}</div>
      ) : null}

      <div className="flex flex-wrap gap-2">
        {result.verification.map((item) => <EvidenceChip key={`v-${item}`} label="Tests" value={item} />)}
        {result.artifacts.map((item) => <EvidenceChip key={`a-${item}`} label={item.includes("receipt") ? "Receipt" : "Artifacts"} value={item} />)}
      </div>

      {deliverables.length ? (
        <div className="space-y-2 rounded-lg border border-emerald-500/15 bg-emerald-500/[.06] p-3">
          <p className="flex items-center gap-1.5 text-xs font-medium uppercase tracking-wide text-emerald-200">
            <FileText className="h-3.5 w-3.5" />
            Deliverables
          </p>
          <ul className="space-y-2">
            {deliverables.map((item) => (
              <li key={item.relative_path} className="flex flex-col gap-2 rounded-md border border-white/10 bg-black/15 px-3 py-2 sm:flex-row sm:items-center sm:justify-between">
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium text-white">{item.relative_path}</p>
                  <p className="hc-mono text-xs hc-dim">{item.content_type} · {formatBytes(item.size)}</p>
                </div>
                <a className="inline-flex shrink-0 items-center gap-1.5 rounded-md border border-emerald-400/30 px-2.5 py-1 text-xs font-medium text-emerald-100 hover:bg-emerald-500/10" href={item.url} target="_blank" rel="noreferrer">
                  <ExternalLink className="h-3.5 w-3.5" />
                  Öffnen
                </a>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {verifierEvidence.length ? (
        <div className="space-y-2">
          <p className="flex items-center gap-1.5 text-xs font-medium uppercase tracking-wide text-cyan-200">
            <Quote className="h-3.5 w-3.5" />
            Verifier evidence
          </p>
          <div className="space-y-2">
            {verifierEvidence.map((line) => (
              <blockquote key={line} className="rounded-lg border border-cyan-500/20 bg-cyan-500/10 px-3 py-2 text-sm leading-6 text-cyan-50">{line}</blockquote>
            ))}
          </div>
        </div>
      ) : null}

      {result.artifacts.length ? (
        <div className="flex flex-wrap gap-2">
          {result.artifacts.map((path) => (
            <Button key={path} outlined size="sm" onClick={() => void copyPath(path)} prefix={<Clipboard className="h-4 w-4" />} className="max-w-full">
              <span className="truncate">{de.hermes.copyPath}</span>
            </Button>
          ))}
        </div>
      ) : null}

      {result.followups.length ? (
        <div className="space-y-2">
          <p className="text-xs font-medium uppercase tracking-wide text-zinc-400">{de.hermes.followups}</p>
          <ul className="space-y-1 text-sm text-zinc-200">
            {result.followups.map((item) => <li key={item} className="flex gap-2"><span className="mt-2 h-1.5 w-1.5 shrink-0 rounded-full bg-cyan-300" /> <span>{item}</span></li>)}
          </ul>
        </div>
      ) : null}

      {result.residual_risk ? <ToneCallout tone="amber"><span className="font-medium">{de.hermes.residualRisk}:</span> {result.residual_risk}</ToneCallout> : null}
    </article>
  );
}
