import { SearchCode, TriangleAlert } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import type { useDeepAudit } from "../../hooks/proposalsDeepAudit";
import type { getAdvancedRunChecklist, getDeepAuditGuidance } from "../../lib/autoresearchRunGuidance";
import { SignalLabel, signalToneFromLegacy } from "../../components/leitstand";
import { Eyebrow } from "../../components/primitives";
import { AdvancedRunChecklistPanel, DeepAuditFindings, Metric, RunGuidanceCard } from "./panels";
import { CodeAuditSlotPicker } from "./LaneModelPanel";

type DeepAuditHook = ReturnType<typeof useDeepAudit>;
type DeepAuditGuidance = ReturnType<typeof getDeepAuditGuidance>;
type AdvancedRunChecklist = ReturnType<typeof getAdvancedRunChecklist>;

export function DeepAuditPanel({ deepAudit, running, effectiveSubsystem, focus, message, guidance, checklist, onSubsystemChange, onFocusChange, onStart }: { deepAudit: DeepAuditHook; running: boolean; effectiveSubsystem: string; focus: string; message: string | null; guidance: DeepAuditGuidance; checklist: AdvancedRunChecklist; onSubsystemChange: (value: string) => void; onFocusChange: (value: string) => void; onStart: () => void }) {
  return (
    <section id="autoresearch-deep-audit" className="rounded-panel border border-line bg-surface-1 scroll-mt-6 p-4 sm:p-5">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0 flex-1 space-y-3">
          <div><Eyebrow>Tiefenprüfung</Eyebrow><h2 className="mt-1 font-display text-h2 font-semibold text-ink">Subsystem-Audit</h2><p className="mt-1 max-w-2xl text-body text-ink-2">Teuer: ca. 1-2 Mio Token pro Lauf. Startet nur per Klick und schreibt keine Code-Änderungen.</p></div>
          <div className="grid gap-3 sm:grid-cols-3">
            <Metric label="Status" value={deepAudit.status?.state ?? (deepAudit.loading ? "lädt" : "idle")} />
            <Metric label="Subsystem" value={deepAudit.status?.subsystem ?? (effectiveSubsystem || "-")} />
            <Metric label="Findings" value={String(deepAudit.findings?.findings.length ?? 0)} />
          </div>
          {deepAudit.error ? <div className="flex items-start gap-2 rounded-card border border-status-alert/30 bg-status-alert/10 px-3 py-2 text-sec text-status-alert"><TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />{deepAudit.error}</div> : null}
          {message ? <div className={`flex items-center gap-2 rounded-card border px-3 py-2 text-sec ${message.includes("fehlgeschlagen") ? "border-status-alert/30 bg-status-alert/10 text-status-alert" : "border-line bg-surface-2 text-ink-2"}`}>{message.includes("fehlgeschlagen") ? <TriangleAlert aria-hidden className="size-4 shrink-0" /> : <SignalLabel tone={signalToneFromLegacy("emerald")} label="Gestartet" />}{message}</div> : null}
        </div>
        <div className="flex min-w-64 flex-col gap-2 rounded-panel border border-line bg-surface-2 p-3">
          <RunGuidanceCard guidance={guidance} />
          <label className="text-xs text-ink-2" htmlFor="deep-audit-subsystem">Subsystem</label>
          <select id="deep-audit-subsystem" value={effectiveSubsystem} onChange={(event) => onSubsystemChange(event.target.value)} className="min-h-12 rounded-panel border border-line bg-surface-0 px-3 text-sm text-ink outline-none focus:border-live">
            {deepAudit.subsystems.map((name) => <option key={name} value={name} className="bg-surface-1 text-ink">{name}</option>)}
          </select>
          <label className="text-xs text-ink-2" htmlFor="deep-audit-focus">Focus</label>
          <input id="deep-audit-focus" value={focus} onChange={(event) => onFocusChange(event.target.value)} placeholder="optional" className="min-h-12 rounded-panel border border-line bg-surface-0 px-3 text-sm text-ink outline-none focus:border-live" />
          <CodeAuditSlotPicker />
          <AdvancedRunChecklistPanel checklist={checklist} />
          <Button className="min-h-12" onClick={onStart} disabled={deepAudit.loading || deepAudit.busy || running || !effectiveSubsystem} prefix={deepAudit.busy || running ? <Spinner /> : <SearchCode className="h-4 w-4" />}>Deep-Audit starten</Button>
        </div>
      </div>
      <DeepAuditFindings findings={deepAudit.findings?.findings ?? []} proposals={deepAudit.findings?.proposals ?? []} />
    </section>
  );
}
