import { FlaskConical, TriangleAlert } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import type { useTestFoundry } from "../../hooks/useControlData";
import type { getAdvancedRunChecklist, getTestFoundryGuidance } from "../../lib/autoresearchRunGuidance";
import type { TestFoundryResultSummary } from "../../lib/autoresearchTestFoundrySummary";
import { SignalLabel, signalToneFromLegacy } from "../../components/leitstand";
import { Eyebrow } from "../../components/primitives";
import { AdvancedRunChecklistPanel, Metric, RunGuidanceCard, TestFoundryResultPanel } from "./panels";
import { TestHardeningSlotPicker } from "./LaneModelPanel";

type TestFoundryHook = ReturnType<typeof useTestFoundry>;
type FoundryGuidance = ReturnType<typeof getTestFoundryGuidance>;
type AdvancedRunChecklist = ReturnType<typeof getAdvancedRunChecklist>;

export function TestFoundryPanel({ testFoundry, running, effectiveTarget, autoApply, message, summary, guidance, checklist, onTargetChange, onAutoApplyChange, onStart }: { testFoundry: TestFoundryHook; running: boolean; effectiveTarget: string; autoApply: boolean; message: string | null; summary: TestFoundryResultSummary | null; guidance: FoundryGuidance; checklist: AdvancedRunChecklist; onTargetChange: (value: string) => void; onAutoApplyChange: (value: boolean) => void; onStart: () => void }) {
  return (
    <section id="autoresearch-test-foundry" className="rounded-panel border border-line bg-surface-1 scroll-mt-6 p-4 sm:p-5">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0 flex-1 space-y-3">
          <div><Eyebrow>Test-Schmiede</Eyebrow><h2 className="mt-1 font-display text-h2 font-semibold text-ink">Mutation-Test-Härtung</h2><p className="mt-1 max-w-2xl text-body text-ink-2">Härtet die Test-Suite via Mutation-Testing; Läufe können einige Minuten dauern.</p></div>
          <div className="grid gap-3 sm:grid-cols-3">
            <Metric label="Status" value={testFoundry.status?.state ?? (testFoundry.loading ? "lädt" : "idle")} />
            <Metric label="Target" value={testFoundry.status?.target ?? (effectiveTarget || "-")} />
            <Metric label="PID" value={testFoundry.status?.pid ? String(testFoundry.status.pid) : "-"} />
          </div>
          <div className="flex items-start gap-2 rounded-card border border-line bg-surface-2 px-3 py-2 text-sec text-ink-2"><SignalLabel tone={signalToneFromLegacy(autoApply ? "amber" : "zinc")} label={autoApply ? "Auto-Apply an" : "Auto-Apply aus"} /><span>{autoApply ? "Validierte Tests werden auf dem separaten Branch f-test-foundry committet, nie auf main." : "Test-Foundry erzeugt nur Karten zur Prüfung."}</span></div>
          {summary ? <TestFoundryResultPanel summary={summary} raw={testFoundry.status?.last_run} /> : null}
          {testFoundry.error ? <div className="flex items-start gap-2 rounded-card border border-status-alert/30 bg-status-alert/10 px-3 py-2 text-sec text-status-alert"><TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />{testFoundry.error}</div> : null}
          {message ? <div className={`flex items-center gap-2 rounded-card border px-3 py-2 text-sec ${message.includes("fehlgeschlagen") ? "border-status-alert/30 bg-status-alert/10 text-status-alert" : "border-line bg-surface-2 text-ink-2"}`}>{message.includes("fehlgeschlagen") ? <TriangleAlert aria-hidden className="size-4 shrink-0" /> : <SignalLabel tone={signalToneFromLegacy("emerald")} label="Gestartet" />}{message}</div> : null}
        </div>
        <div className="flex min-w-64 flex-col gap-2 rounded-panel border border-line bg-surface-2 p-3">
          <RunGuidanceCard guidance={guidance} />
          <label className="text-xs text-ink-2" htmlFor="test-foundry-target">Target</label>
          <select id="test-foundry-target" value={effectiveTarget} onChange={(event) => onTargetChange(event.target.value)} className="min-h-12 rounded-panel border border-line bg-surface-0 px-3 text-sm text-ink outline-none focus:border-live">
            {testFoundry.targets.map((name) => <option key={name} value={name} className="bg-surface-1 text-ink">{name}</option>)}
          </select>
          <TestHardeningSlotPicker />
          <label className="flex cursor-pointer items-start gap-2 rounded-panel border border-line bg-surface-2 p-2 text-sm text-ink">
            <input type="checkbox" checked={autoApply} onChange={(event) => onAutoApplyChange(event.target.checked)} className="size-12 shrink-0 accent-live" />
            <span><span className="block font-medium">Auto-Apply</span><span className="block text-xs text-ink-2">Beweis-gegatet auf Branch f-test-foundry; main bleibt unberührt.</span></span>
          </label>
          <AdvancedRunChecklistPanel checklist={checklist} />
          <Button className="min-h-12" onClick={onStart} disabled={testFoundry.loading || testFoundry.busy || running || !effectiveTarget} prefix={testFoundry.busy || running ? <Spinner /> : <FlaskConical className="h-4 w-4" />}>Test-Foundry starten</Button>
        </div>
      </div>
    </section>
  );
}
