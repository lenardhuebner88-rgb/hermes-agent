import { Play, Square, TriangleAlert } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { AUTORESEARCH_AREAS, type describeLoopStatus } from "../../lib/autoresearch";
import type { getResearchLoopGuidance, getResearchLoopStartChecklist, getResearchLoopStartControl, getResearchLoopStartSummary, ResearchLoopPresetId } from "../../lib/autoresearchRunGuidance";
import type { useAutoresearchStatus } from "../../hooks/useControlData";
import type { AutoresearchRun } from "../../lib/types";
import { de } from "../../i18n/de";
import { SignalLabel, signalToneFromLegacy } from "../../components/leitstand";
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
      <Panel className="p-4 sm:p-5" eyebrow="Iterativer Research-Loop" title={loop.running ? `Iteration ${loop.iterationLabel}` : "kein Lauf aktiv"} actions={<span className="font-data tabular-nums text-xs text-ink-2">Heartbeat {loop.heartbeatLabel}</span>}>
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0 flex-1 space-y-3">
          <div className="h-2 overflow-hidden rounded-full bg-white/10"><div className="h-full rounded-full bg-live" style={{ width: `${loop.progressPercent}%` }} /></div>
          <div className="grid gap-3 text-sm sm:grid-cols-3">
            <Metric label="Letzter Schritt" value={loop.stepLabel} />
            <Metric label="Letzte Bewertung" value={loop.evalLabel} />
            <Metric label="Request" value={status?.request_id || "-"} />
          </div>
          {loop.routeHint ? <div className="flex items-start gap-2 rounded-card border border-status-warn/30 bg-status-warn/10 px-3 py-2 text-sec text-status-warn"><TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />{loop.routeHint}: {status?.route_status ?? "unbekannt"}</div> : null}
          {!routeOk ? null : null}
          <LastRun status={status} latestRun={latestRun} />
          {loopMessage ? <div className={`flex items-center gap-2 rounded-card border px-3 py-2 text-sec ${loopMessage.includes("fehlgeschlagen") ? "border-status-alert/30 bg-status-alert/10 text-status-alert" : "border-line bg-surface-2 text-ink-2"}`}>{loopMessage.includes("fehlgeschlagen") ? <TriangleAlert aria-hidden className="size-4 shrink-0" /> : <SignalLabel tone={signalToneFromLegacy("emerald")} label="Gesendet" />}{loopMessage}</div> : null}
        </div>
        <div className="flex min-w-56 flex-col gap-2 rounded-panel border border-line bg-surface-2 p-3">
          <RunGuidanceCard guidance={researchLoopGuidance} />
          <LoopPresetPicker selectedId={selectedLoopPresetId} disabled={loop.running || !!loopBusy} onSelect={onApplyPreset} />
          <Disclosure className="rounded-panel border border-line bg-surface-2 p-2" open={!selectedLoopPresetId} summary={<span className="flex min-h-12 items-center text-xs font-semibold text-ink">Feinsteuerung {selectedLoopPresetId ? "" : "· eigene Werte"}</span>}>
            <div className="flex flex-col gap-2">
              <label className="text-xs text-ink-2" htmlFor="loop-area">{de.autoresearch.triggerArea}</label>
              <select id="loop-area" value={area} onChange={(event) => onAreaChange(event.target.value)} className="min-h-12 rounded-panel border border-line bg-surface-0 px-3 text-sm text-ink outline-none focus:border-live">
                {AUTORESEARCH_AREAS.map((a) => <option key={a.value} value={a.value} className="bg-surface-1 text-ink">{a.value} — {a.scope}</option>)}
              </select>
              <label className="text-xs text-ink-2" htmlFor="loop-focus">{de.autoresearch.triggerFocus}</label>
              <input id="loop-focus" type="text" inputMode="text" pattern="[a-z0-9][a-z0-9_-]*" placeholder={de.autoresearch.triggerFocusPlaceholder} value={focus} onChange={(event) => onFocusChange(event.target.value)} className="min-h-12 rounded-panel border border-line bg-surface-0 px-3 text-sm text-ink outline-none focus:border-live" />
              <p className="-mt-1 text-micro text-ink-3">{de.autoresearch.triggerFocusHint}</p>
              <label className="text-xs text-ink-2" htmlFor="loop-min-use">{de.autoresearch.triggerMinUse}</label>
              <input id="loop-min-use" type="number" min={1} step={1} placeholder={de.autoresearch.triggerMinUsePlaceholder} value={minUseCount} onChange={(event) => onMinUseCountChange(event.target.value)} className="min-h-12 rounded-panel border border-line bg-surface-0 px-3 text-sm text-ink outline-none focus:border-live" />
              <label className="text-xs text-ink-2" htmlFor="loop-iterations">Max. Iterationen</label>
              <input id="loop-iterations" type="number" min={1} max={50} value={maxIterations} onChange={(event) => onMaxIterationsChange(event.target.value)} className="min-h-12 rounded-panel border border-line bg-surface-0 px-3 text-sm text-ink outline-none focus:border-live" />
            </div>
          </Disclosure>
          <TargetingPreview summary={researchLoopStartSummary} />
          <StartChecklistPanel checklist={researchLoopStartChecklist} />
          <Button className="min-h-12" onClick={onStartLoop} disabled={researchLoopStart.disabled} title={researchLoopStart.title} prefix={loopBusy === "start" ? <Spinner /> : <Play className="h-4 w-4" />}>{researchLoopStart.label}</Button>
          <Button outlined className="min-h-12" onClick={onStopLoop} disabled={!loop.running || !!loopBusy} prefix={loopBusy === "stop" ? <Spinner /> : <Square className="h-4 w-4" />}>Stop</Button>
        </div>
      </div>
      </Panel>
    </section>
  );
}
