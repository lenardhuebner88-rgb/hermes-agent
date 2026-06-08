import { ExternalLink, FileText, Quote } from "lucide-react";
import { fmtAge } from "../lib/derive";
import { profileLabel } from "../lib/tones";
import type { DotKind } from "../lib/tones";
import type { ResultQualityBadge, TodayDigestItem } from "../lib/types";
import { StatusPill, ToneCallout } from "./atoms";

function formatBytes(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${bytes} B`;
}

function resultQualityDot(state: ResultQualityBadge["state"]): DotKind {
  if (state === "verifier_approved") return "ready";
  if (state === "rejected_needs_work") return "error";
  if (state === "unknown_legacy") return "idle";
  return "warn";
}

export function HermesTodayDigestCard({ item, now }: { item: TodayDigestItem; now: number }) {
  const resultQuality = item.result_quality;
  const profile = item.profile ? (profileLabel[item.profile] ?? item.profile) : "Profile unknown";

  return (
    <article className="hc-card space-y-4 p-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0 space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <span title={resultQuality.description}>
              <StatusPill tone={resultQuality.tone} label={resultQuality.label} dot={resultQualityDot(resultQuality.state)} />
            </span>
            <span className="rounded-full border border-cyan-400/20 bg-cyan-400/10 px-2 py-0.5 text-xs font-medium text-cyan-100">{item.run_role_label}</span>
            <span className="rounded-full border border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] px-2 py-0.5 text-xs text-[var(--hc-accent-text)]">{profile}</span>
            <span className="hc-mono text-xs hc-soft">vor {fmtAge(item.ended_at, now)}</span>
          </div>
          <h3 className="line-clamp-2 text-base font-semibold leading-snug text-white">{item.task_title}</h3>
          <p className="hc-mono text-xs hc-dim">{item.task_id} · Run {item.run_id}</p>
        </div>
      </div>

      <div>
        <p className="text-xs font-medium uppercase tracking-wide text-zinc-400">Was kam raus?</p>
        <p className="mt-1 whitespace-pre-wrap text-sm leading-6 text-zinc-100">{item.task_summary || "Keine Summary im Worker-Handoff."}</p>
      </div>

      {item.deliverable ? (
        <div className="space-y-2 rounded-lg border border-emerald-500/15 bg-emerald-500/[.06] p-3">
          <p className="flex items-center gap-1.5 text-xs font-medium uppercase tracking-wide text-emerald-200">
            <FileText className="h-3.5 w-3.5" />
            Deliverable
          </p>
          <div className="flex flex-col gap-2 rounded-md border border-white/10 bg-black/15 px-3 py-2 sm:flex-row sm:items-center sm:justify-between">
            <div className="min-w-0">
              <p className="truncate text-sm font-medium text-white">{item.deliverable.relative_path}</p>
              <p className="hc-mono text-xs hc-dim">{item.deliverable.content_type} · {formatBytes(item.deliverable.size)}</p>
            </div>
            <a className="inline-flex shrink-0 items-center gap-1.5 rounded-md border border-emerald-400/30 px-2.5 py-1 text-xs font-medium text-emerald-100 hover:bg-emerald-500/10" href={item.deliverable.url} target="_blank" rel="noreferrer">
              <ExternalLink className="h-3.5 w-3.5" />
              Öffnen
            </a>
          </div>
          {item.deliverable_excerpt ? (
            <blockquote className="rounded-lg border border-white/10 bg-black/20 px-3 py-2 text-sm leading-6 text-zinc-100">{item.deliverable_excerpt}</blockquote>
          ) : null}
        </div>
      ) : (
        <ToneCallout tone="amber">Kein preservtes Deliverable gefunden; nutze die Task-Details fuer Roh-Handoff und Artefaktpfade.</ToneCallout>
      )}

      {item.gate_evidence.length ? (
        <div className="space-y-2">
          <p className="flex items-center gap-1.5 text-xs font-medium uppercase tracking-wide text-cyan-200">
            <Quote className="h-3.5 w-3.5" />
            Gate evidence
          </p>
          <div className="space-y-2">
            {item.gate_evidence.map((line) => (
              <blockquote key={line} className="rounded-lg border border-cyan-500/20 bg-cyan-500/10 px-3 py-2 text-sm leading-6 text-cyan-50">{line}</blockquote>
            ))}
          </div>
        </div>
      ) : null}

      {item.residual_risk ? <ToneCallout tone="amber"><span className="font-medium">Restrisiko:</span> {item.residual_risk}</ToneCallout> : null}
    </article>
  );
}
