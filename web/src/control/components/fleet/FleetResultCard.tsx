/**
 * FleetResultCard — the verdichtete "Letzte Ergebnisse" card from the
 * screenshot: a Completed badge, a coloured run-role chip, a Details toggle,
 * the task title, and a "Dauer · vor X · receipt + test log" meta line. The
 * Details toggle reveals the real summary, evidence, deliverables and verifier
 * quotes inline — compact face, full proof on demand. Real data only.
 */
import { useState } from "react";
import { ChevronRight, ExternalLink, FileText, Quote, TriangleAlert } from "lucide-react";
import { cn } from "@/lib/utils";
import { fmtDur, fmtRelativeTime } from "../../lib/derive";
import { roleChip } from "../../lib/fleet";
import { de } from "../../i18n/de";
import type { KanbanResult } from "../../lib/types";
import { RoleChip } from "./atoms";
import { SignalChip } from "../leitstand";

function receiptLabel(result: KanbanResult): string {
  const hasReceipt = (result.artifacts ?? []).some((a) => a.toLowerCase().includes("receipt")) || (result.deliverables ?? []).length > 0;
  const hasTest = (result.verification ?? []).length > 0 || (result.verifier_evidence ?? []).length > 0;
  if (hasReceipt && hasTest) return de.fleet.receiptTestLog;
  if (hasReceipt) return de.fleet.receiptOnly;
  if (hasTest) return "Test-Log";
  return "kein Beleg";
}

function formatBytes(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${bytes} B`;
}

export function FleetResultCard({ result, now }: { result: KanbanResult; now: number }) {
  const [open, setOpen] = useState(false);
  const role = roleChip(result.profile, result.run_role);
  const verifierEvidence = result.verifier_evidence ?? [];
  const deliverables = result.deliverables ?? [];

  return (
    <article className="hc-surface-card p-3.5">
      <div className="flex flex-wrap items-center gap-2">
        <SignalChip tone="ok" label="Abgeschlossen" />
        <RoleChip role={role} />
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          className="ml-auto inline-flex min-h-12 items-center gap-1 rounded-card border border-line px-3 text-xs text-ink-2 transition hover:bg-surface-3"
        >
          {de.fleet.details}
          <ChevronRight className={cn("h-3.5 w-3.5 transition-transform", open && "rotate-90")} />
        </button>
      </div>

      <h3 className="mt-2.5 line-clamp-2 text-sm font-semibold leading-snug text-ink" title={result.task_title}>{result.task_title}</h3>
      <p className="mt-1.5 font-data text-micro text-ink-3">
        ⏱ {fmtDur(result.duration_seconds)} · {fmtRelativeTime(result.ended_at, now)} · {receiptLabel(result)}
      </p>

      {open ? (
        <div className="mt-3 space-y-3 border-t border-line pt-3">
          <p className="font-data text-micro text-ink-3">{result.task_id} · Run {result.run_id} · {result.run_role_label}</p>
          {result.summary ? (
            <div className="whitespace-pre-wrap rounded-card border border-line bg-surface-1 p-3 text-sm leading-6 text-ink">{result.summary}</div>
          ) : null}
          {result.verification.length ? (
            <div className="flex flex-wrap gap-1.5">
              {result.verification.map((item) => (
                <span key={item} className="inline-flex max-w-full items-center gap-1 rounded-card border border-line bg-surface-2 px-2.5 py-1 text-xs text-ink-2">
                  <FileText className="h-3.5 w-3.5 shrink-0" />
                  <span className="truncate" title={item}>{item}</span>
                </span>
              ))}
            </div>
          ) : null}
          {deliverables.length ? (
            <ul className="space-y-1.5">
              {deliverables.map((item) => (
                <li key={item.relative_path} className="flex items-center justify-between gap-2 rounded-card border border-line bg-surface-2 px-3 py-1.5">
                  <span className="min-w-0">
                    <span className="block truncate text-sm font-medium text-ink" title={item.relative_path}>{item.relative_path}</span>
                    <span className="font-data text-micro text-ink-3">{item.content_type} · {formatBytes(item.size)}</span>
                  </span>
                  <a className="inline-flex shrink-0 items-center gap-1 rounded-card border border-live/30 px-2 py-1 text-xs text-bronze-hi hover:bg-live/10" href={item.url} target="_blank" rel="noreferrer">
                    <ExternalLink className="h-3.5 w-3.5" />
                    Öffnen
                  </a>
                </li>
              ))}
            </ul>
          ) : null}
          {verifierEvidence.length ? (
            <div className="space-y-1.5">
              <p className="flex items-center gap-1.5 font-display text-micro font-semibold uppercase tracking-[0.08em] text-ink-3"><Quote className="h-3.5 w-3.5" />Verifier-Belege</p>
              {verifierEvidence.map((line) => (
                <blockquote key={line} className="rounded-card border border-line bg-surface-2 px-3 py-2 text-sm leading-6 text-ink-2">{line}</blockquote>
              ))}
            </div>
          ) : null}
          {result.followups.length ? (
            <ul className="space-y-1 text-sm text-ink-2">
              {result.followups.map((item) => <li key={item} className="flex gap-2"><span className="mt-2 h-1.5 w-1.5 shrink-0 rounded-full bg-brand" /><span>{item}</span></li>)}
            </ul>
          ) : null}
          {result.residual_risk ? <div role="alert" className="flex items-start gap-2 rounded-card border border-status-warn/30 bg-status-warn/10 px-3 py-2 text-sec text-status-warn"><TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" /><span><span className="font-medium">{de.hermes.residualRisk}:</span> {result.residual_risk}</span></div> : null}
        </div>
      ) : null}
    </article>
  );
}
