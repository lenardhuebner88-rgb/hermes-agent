import { Play, Square } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { AUTORESEARCH_AREAS, type describeLoopStatus } from "../../lib/autoresearch";
import type { getResearchLoopGuidance, getResearchLoopStartChecklist, getResearchLoopStartControl, getResearchLoopStartSummary, ResearchLoopPresetId } from "../../lib/autoresearchRunGuidance";
import type { useAutoresearchStatus } from "../../hooks/useControlData";
import type { AutoresearchRun } from "../../lib/types";
import { de } from "../../i18n/de";
import { ToneCallout } from "../../components/atoms";
import { Disclosure, Panel } from "../../components/primitives";
import { LastRun, LoopPresetPicker, Metric, RunGuidanceCard, StartChecklistPanel, TargetingPreview } from "./panels";

type LoopStatus = ReturnType<typeof describeLoopStatus>;
type ResearchLoopGuidance = ReturnType<typeof getResearchLoopGuidance>;
type ResearchLoopStartControl = ReturnType<typeof getResearchLoopStartControl>;
type ResearchLoopStartSummary = ReturnType<typeof getResearchLoopStartSummary>;
type ResearchLoopStartChecklist = ReturnType<typeof getResearchLoopStartChecklist>;

export function LoopControls({
  loop,
  status,
  latestRun,
  routeOk,
  loopBusy,
  loopMessage,
  area,
  focus,
  minUseCount,
  maxIterations,
  selectedLoopPresetId,
  researchLoopGuidance,
  researchLoopStart,
  researchLoopStartSummary,
  researchLoopStartChecklist,
  onAreaChange,
  onFocusChange,
  onMinUseCountChange,
  onMaxIterationsChange,
  onApplyPreset,
  onStartLoop,
  onStopLoop,
}: {
  loop: LoopStatus;
  status: ReturnType<typeof useAutoresearchStatus>["data"];
  latestRun: AutoresearchRun | null;
  routeOk: boolean;
  loopBusy: "start" | "stop" | null;
  loopMessage: string | null;
  area: string;
  focus: string;
  minUseCount: string;
  maxIterations: string;
  selectedLoopPresetId: ResearchLoopPresetId | null;
  researchLoopGuidance: ResearchLoopGuidance;
  researchLoopStart: ResearchLoopStartControl;
  researchLoopStartSummary: ResearchLoopStartSummary;
  researchLoopStartChecklist: ResearchLoopStartChecklist;
  onAreaChange: (value: string) => void;
  onFocusChange: (value: string) => void;
  onMinUseCountChange: (value: string) => void;
  onMaxIterationsChange: (value: string) => void;
  onApplyPreset: (presetId: ResearchLoopPresetId) => void;
  onStartLoop: () => void;
  onStopLoop: () => void;
}) {
  return (
    <section id="autoresearch-loop" className="scroll-mt-6">
      <Panel className="p-4 sm:p-5" eyebrow="Iterativer Research-Loop" title={loop.running ? `Iteration ${loop.iterationLabel}` : "kein Lauf aktiv"} actions={<span className="hc-mono text-xs hc-soft">Heartbeat {loop.heartbeatLabel}</span>}>
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0 flex-1 space-y-3">
          <div className="h-2 overflow-hidden rounded-full bg-white/10"><div className="h-full rounded-full bg-[var(--hc-accent)]" style={{ width: `${loop.progressPercent}%` }} /></div>
          <div className="grid gap-3 text-sm sm:grid-cols-3">
            <Metric label="Letzter Schritt" value={loop.stepLabel} />
            <Metric label="Letzte Bewertung" value={loop.evalLabel} />
            <Metric label="Request" value={status?.request_id || "-"} />
          </div>
          {loop.routeHint ? <ToneCallout tone="amber">{loop.routeHint}: {status?.route_status ?? "unbekannt"}</ToneCallout> : null}
          {!routeOk ? null : null}
          <LastRun status={status} latestRun={latestRun} />
          {loopMessage ? <ToneCallout tone={loopMessage.includes("fehlgeschlagen") ? "red" : "emerald"}>{loopMessage}</ToneCallout> : null}
        </div>
        <div className="flex min-w-56 flex-col gap-2 rounded-lg border border-white/10 bg-white/[.03] p-3">
          <RunGuidanceCard guidance={researchLoopGuidance} />
          <LoopPresetPicker selectedId={selectedLoopPresetId} disabled={loop.running || !!loopBusy} onSelect={onApplyPreset} />
          <Disclosure className="rounded-lg border border-white/10 bg-black/20 p-2" open={!selectedLoopPresetId} summary={<span className="text-xs font-semibold text-white">Feinsteuerung {selectedLoopPresetId ? "" : "· eigene Werte"}</span>}>
            <div className="flex flex-col gap-2">
              <label className="text-xs hc-soft" htmlFor="loop-area">{de.autoresearch.triggerArea}</label>
              <select id="loop-area" value={area} onChange={(event) => onAreaChange(event.target.value)} className="hc-hit rounded-lg border border-white/10 bg-black/30 px-3 text-sm text-white outline-none focus:border-[var(--hc-accent-border)]">
                {AUTORESEARCH_AREAS.map((a) => <option key={a.value} value={a.value} className="bg-[var(--hc-panel)] text-[var(--hc-text)]">{a.value} — {a.scope}</option>)}
              </select>
              <label className="text-xs hc-soft" htmlFor="loop-focus">{de.autoresearch.triggerFocus}</label>
              <input id="loop-focus" type="text" inputMode="text" pattern="[a-z0-9][a-z0-9_-]*" placeholder={de.autoresearch.triggerFocusPlaceholder} value={focus} onChange={(event) => onFocusChange(event.target.value)} className="hc-hit rounded-lg border border-white/10 bg-black/30 px-3 text-sm text-white outline-none focus:border-[var(--hc-accent-border)]" />
              <p className="-mt-1 text-[11px] hc-dim">{de.autoresearch.triggerFocusHint}</p>
              <label className="text-xs hc-soft" htmlFor="loop-min-use">{de.autoresearch.triggerMinUse}</label>
              <input id="loop-min-use" type="number" min={1} step={1} placeholder={de.autoresearch.triggerMinUsePlaceholder} value={minUseCount} onChange={(event) => onMinUseCountChange(event.target.value)} className="hc-hit rounded-lg border border-white/10 bg-black/30 px-3 text-sm text-white outline-none focus:border-[var(--hc-accent-border)]" />
              <label className="text-xs hc-soft" htmlFor="loop-iterations">Max. Iterationen</label>
              <input id="loop-iterations" type="number" min={1} max={50} value={maxIterations} onChange={(event) => onMaxIterationsChange(event.target.value)} className="hc-hit rounded-lg border border-white/10 bg-black/30 px-3 text-sm text-white outline-none focus:border-[var(--hc-accent-border)]" />
            </div>
          </Disclosure>
          <TargetingPreview summary={researchLoopStartSummary} />
          <StartChecklistPanel checklist={researchLoopStartChecklist} />
          <Button className="hc-hit" onClick={onStartLoop} disabled={researchLoopStart.disabled} title={researchLoopStart.title} prefix={loopBusy === "start" ? <Spinner /> : <Play className="h-4 w-4" />}>{researchLoopStart.label}</Button>
          <Button outlined className="hc-hit" onClick={onStopLoop} disabled={!loop.running || !!loopBusy} prefix={loopBusy === "stop" ? <Spinner /> : <Square className="h-4 w-4" />}>Stop</Button>
        </div>
      </div>
      </Panel>
    </section>
  );
}
