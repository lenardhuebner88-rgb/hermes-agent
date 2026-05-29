import { Clipboard, ExternalLink, FileText } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { fmtAge, fmtDur } from "../lib/derive";
import { profileLabel } from "../lib/tones";
import { de } from "../i18n/de";
import type { KanbanResult } from "../lib/types";
import { StatusPill, ToneCallout } from "./atoms";

function EvidenceChip({ label, value }: { label: string; value: string }) {
  return (
    <span className="inline-flex max-w-full items-center gap-1 rounded-full border border-white/10 bg-white/[.04] px-2.5 py-1 text-xs text-zinc-200">
      <FileText className="h-3.5 w-3.5 shrink-0" />
      <span className="truncate">{label}: {value}</span>
    </span>
  );
}

export function HermesResultCard({ result, now }: { result: KanbanResult; now: number }) {
  const profile = profileLabel[result.profile] ?? result.profile;
  const copyPath = async (path: string) => {
    await navigator.clipboard?.writeText(path);
  };
  return (
    <article className="hc-card space-y-4 p-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0 space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <StatusPill tone="emerald" label="Completed" dot="ready" />
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
