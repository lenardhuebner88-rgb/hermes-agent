import { FlaskConical } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import type { useTestFoundry } from "../../hooks/useControlData";
import type { getAdvancedRunChecklist, getTestFoundryGuidance } from "../../lib/autoresearchRunGuidance";
import type { TestFoundryResultSummary } from "../../lib/autoresearchTestFoundrySummary";
import { ToneCallout } from "../../components/atoms";
import { AdvancedRunChecklistPanel, Metric, RunGuidanceCard, TestFoundryResultPanel } from "./panels";
import { TestHardeningSlotPicker } from "./LaneModelPanel";

type TestFoundryHook = ReturnType<typeof useTestFoundry>;
type FoundryGuidance = ReturnType<typeof getTestFoundryGuidance>;
type AdvancedRunChecklist = ReturnType<typeof getAdvancedRunChecklist>;

export function TestFoundryPanel({ testFoundry, running, effectiveTarget, autoApply, message, summary, guidance, checklist, onTargetChange, onAutoApplyChange, onStart }: { testFoundry: TestFoundryHook; running: boolean; effectiveTarget: string; autoApply: boolean; message: string | null; summary: TestFoundryResultSummary | null; guidance: FoundryGuidance; checklist: AdvancedRunChecklist; onTargetChange: (value: string) => void; onAutoApplyChange: (value: boolean) => void; onStart: () => void }) {
  return (
    <section id="autoresearch-test-foundry" className="hc-card scroll-mt-6 p-4 sm:p-5">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0 flex-1 space-y-3">
          <div><p className="hc-eyebrow">Test-Foundry</p><h2 className="mt-1 text-lg font-semibold text-white">Mutation-Test-Härtung</h2><p className="mt-1 max-w-2xl text-sm hc-soft">Härtet die Test-Suite via Mutation-Testing; Läufe können einige Minuten dauern.</p></div>
          <div className="grid gap-3 sm:grid-cols-3">
            <Metric label="Status" value={testFoundry.status?.state ?? (testFoundry.loading ? "lädt" : "idle")} />
            <Metric label="Target" value={testFoundry.status?.target ?? (effectiveTarget || "-")} />
            <Metric label="PID" value={testFoundry.status?.pid ? String(testFoundry.status.pid) : "-"} />
          </div>
          <ToneCallout tone={autoApply ? "amber" : "cyan"}>{autoApply ? "Auto-Apply ist an: validierte Tests werden auf dem separaten Branch f-test-foundry committet, nie auf main." : "Auto-Apply ist aus: Test-Foundry erzeugt nur Karten zur Prüfung."}</ToneCallout>
          {summary ? <TestFoundryResultPanel summary={summary} raw={testFoundry.status?.last_run} /> : null}
          {testFoundry.error ? <ToneCallout tone="red">{testFoundry.error}</ToneCallout> : null}
          {message ? <ToneCallout tone={message.includes("fehlgeschlagen") ? "red" : "emerald"}>{message}</ToneCallout> : null}
        </div>
        <div className="flex min-w-64 flex-col gap-2 rounded-lg border border-white/10 bg-white/[.03] p-3">
          <RunGuidanceCard guidance={guidance} />
          <label className="text-xs hc-soft" htmlFor="test-foundry-target">Target</label>
          <select id="test-foundry-target" value={effectiveTarget} onChange={(event) => onTargetChange(event.target.value)} className="hc-hit rounded-lg border border-white/10 bg-black/30 px-3 text-sm text-white outline-none focus:border-[var(--hc-accent-border)]">
            {testFoundry.targets.map((name) => <option key={name} value={name} className="bg-[#16181d] text-white">{name}</option>)}
          </select>
          <TestHardeningSlotPicker />
          <label className="flex cursor-pointer items-start gap-2 rounded-lg border border-white/10 bg-black/20 p-2 text-sm text-white">
            <input type="checkbox" checked={autoApply} onChange={(event) => onAutoApplyChange(event.target.checked)} className="mt-0.5 h-4 w-4 accent-[var(--hc-accent)]" />
            <span><span className="block font-medium">Auto-Apply</span><span className="block text-xs hc-soft">Beweis-gegatet auf Branch f-test-foundry; main bleibt unberührt.</span></span>
          </label>
          <AdvancedRunChecklistPanel checklist={checklist} />
          <Button className="hc-hit" onClick={onStart} disabled={testFoundry.loading || testFoundry.busy || running || !effectiveTarget} prefix={testFoundry.busy || running ? <Spinner /> : <FlaskConical className="h-4 w-4" />}>Test-Foundry starten</Button>
        </div>
      </div>
    </section>
  );
}
