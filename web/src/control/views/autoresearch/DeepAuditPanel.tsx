import { SearchCode } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import type { useDeepAudit } from "../../hooks/useControlData";
import type { getAdvancedRunChecklist, getDeepAuditGuidance } from "../../lib/autoresearchRunGuidance";
import { ToneCallout } from "../../components/atoms";
import { AdvancedRunChecklistPanel, DeepAuditFindings, Metric, RunGuidanceCard } from "./panels";
import { CodeAuditSlotPicker } from "./LaneModelPanel";

type DeepAuditHook = ReturnType<typeof useDeepAudit>;
type DeepAuditGuidance = ReturnType<typeof getDeepAuditGuidance>;
type AdvancedRunChecklist = ReturnType<typeof getAdvancedRunChecklist>;

export function DeepAuditPanel({ deepAudit, running, effectiveSubsystem, focus, message, guidance, checklist, onSubsystemChange, onFocusChange, onStart }: { deepAudit: DeepAuditHook; running: boolean; effectiveSubsystem: string; focus: string; message: string | null; guidance: DeepAuditGuidance; checklist: AdvancedRunChecklist; onSubsystemChange: (value: string) => void; onFocusChange: (value: string) => void; onStart: () => void }) {
  return (
    <section id="autoresearch-deep-audit" className="hc-card scroll-mt-6 p-4 sm:p-5">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0 flex-1 space-y-3">
          <div><p className="hc-eyebrow">Deep-Audit</p><h2 className="mt-1 text-lg font-semibold text-white">Subsystem-Audit</h2><p className="mt-1 max-w-2xl text-sm hc-soft">Teuer: ca. 1-2 Mio Token pro Lauf. Startet nur per Klick und schreibt keine Code-Änderungen.</p></div>
          <div className="grid gap-3 sm:grid-cols-3">
            <Metric label="Status" value={deepAudit.status?.state ?? (deepAudit.loading ? "lädt" : "idle")} />
            <Metric label="Subsystem" value={deepAudit.status?.subsystem ?? (effectiveSubsystem || "-")} />
            <Metric label="Findings" value={String(deepAudit.findings?.findings.length ?? 0)} />
          </div>
          {deepAudit.error ? <ToneCallout tone="red">{deepAudit.error}</ToneCallout> : null}
          {message ? <ToneCallout tone={message.includes("fehlgeschlagen") ? "red" : "emerald"}>{message}</ToneCallout> : null}
        </div>
        <div className="flex min-w-64 flex-col gap-2 rounded-lg border border-white/10 bg-white/[.03] p-3">
          <RunGuidanceCard guidance={guidance} />
          <label className="text-xs hc-soft" htmlFor="deep-audit-subsystem">Subsystem</label>
          <select id="deep-audit-subsystem" value={effectiveSubsystem} onChange={(event) => onSubsystemChange(event.target.value)} className="hc-hit rounded-lg border border-white/10 bg-black/30 px-3 text-sm text-white outline-none focus:border-[var(--hc-accent-border)]">
            {deepAudit.subsystems.map((name) => <option key={name} value={name} className="bg-[#16181d] text-white">{name}</option>)}
          </select>
          <label className="text-xs hc-soft" htmlFor="deep-audit-focus">Focus</label>
          <input id="deep-audit-focus" value={focus} onChange={(event) => onFocusChange(event.target.value)} placeholder="optional" className="hc-hit rounded-lg border border-white/10 bg-black/30 px-3 text-sm text-white outline-none focus:border-[var(--hc-accent-border)]" />
          <CodeAuditSlotPicker />
          <AdvancedRunChecklistPanel checklist={checklist} />
          <Button className="hc-hit" onClick={onStart} disabled={deepAudit.loading || deepAudit.busy || running || !effectiveSubsystem} prefix={deepAudit.busy || running ? <Spinner /> : <SearchCode className="h-4 w-4" />}>Deep-Audit starten</Button>
        </div>
      </div>
      <DeepAuditFindings findings={deepAudit.findings?.findings ?? []} proposals={deepAudit.findings?.proposals ?? []} />
    </section>
  );
}
