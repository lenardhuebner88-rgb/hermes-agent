import { useMemo, useState } from "react";
import { Play, Square } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import {
  extractDetail,
  startLoop,
  stopLoop,
  toggleLoopTimer,
  useLoopDetail,
  useLoopModels,
  useLoops,
} from "../hooks/useControlData";
import { de } from "../i18n/de";
import { Led, StatusPill, ToneCallout } from "../components/atoms";
import { Disclosure, Stat } from "../components/primitives";
import { FleetEmptyState, FleetPanel } from "../components/fleet/atoms";
import {
  isLoopPackError,
  type LoopDetailResponse,
  type LoopModelsResponse,
  type LoopPack,
  type LoopPackError,
  type LoopPackSummary,
} from "../lib/types";

const t = de.loops;

const CONTROL_CLASS =
  "min-h-11 w-full rounded-md border border-[var(--hc-border)] bg-black/25 px-2 py-1.5 text-base text-white sm:min-h-9 sm:text-sm";

const QUEUE_STAGES: Array<{ key: string; label: string }> = [
  { key: "00-planned", label: t.queuePlanned },
  { key: "10-building", label: t.queueBuilding },
  { key: "20-verified", label: t.queueVerified },
  { key: "90-bounced", label: t.queueBounced },
];

/** Pro Phase gebaute overrides — nur Felder, die vom Manifest-Default abweichen. */
function buildPhaseOverrides(
  pack: LoopPackSummary,
  phaseValues: Record<string, { engine: string; model: string }>,
): Record<string, string> {
  const overrides: Record<string, string> = {};
  for (const [phase, original] of Object.entries(pack.phases)) {
    const current = phaseValues[phase];
    if (!current) continue;
    const upper = phase.toUpperCase();
    if (current.engine !== original.engine) overrides[`PHASE_${upper}_ENGINE`] = current.engine;
    if (current.model !== original.model) overrides[`PHASE_${upper}_MODEL`] = current.model;
  }
  return overrides;
}

function LoopStartForm({
  pack,
  models,
  busy,
  onSubmit,
  onCancel,
}: {
  pack: LoopPackSummary;
  models: LoopModelsResponse | null;
  busy: boolean;
  onSubmit: (overrides: Record<string, string>) => void;
  onCancel: () => void;
}) {
  const phaseNames = useMemo(() => Object.keys(pack.phases), [pack]);
  const [phaseValues, setPhaseValues] = useState<Record<string, { engine: string; model: string }>>(() =>
    Object.fromEntries(phaseNames.map((name) => [name, { ...pack.phases[name] }])),
  );
  const defaultMaxRounds = String(pack.stop.max_rounds ?? "");
  const defaultMaxHours = String(pack.stop.max_hours ?? "");
  const [maxRounds, setMaxRounds] = useState(defaultMaxRounds);
  const [maxHours, setMaxHours] = useState(defaultMaxHours);
  // Pack-Params dynamisch (focus/fokus/services/…): ein Feld pro Manifest-Param.
  // Ein hartkodiertes FOCUS-Feld wäre für Packs ohne diesen Param ein stiller No-Op.
  const paramNames = useMemo(() => Object.keys(pack.params), [pack]);
  const [paramValues, setParamValues] = useState<Record<string, string>>(() => ({ ...pack.params }));

  const engines = models?.engines ?? {};
  const engineNames = Object.keys(engines);

  const handleSubmit = () => {
    const overrides = buildPhaseOverrides(pack, phaseValues);
    if (maxRounds.trim() && maxRounds !== defaultMaxRounds) overrides.MAX_ROUNDS = maxRounds.trim();
    if (maxHours.trim() && maxHours !== defaultMaxHours) overrides.MAX_HOURS = maxHours.trim();
    for (const name of paramNames) {
      const value = (paramValues[name] ?? "").trim();
      if (value && value !== pack.params[name]) overrides[name.toUpperCase()] = value;
    }
    onSubmit(overrides);
  };

  return (
    <div className="space-y-3">
      <p className="hc-type-label hc-dim">{t.startPanelTitle}</p>
      {phaseNames.map((phase) => {
        const value = phaseValues[phase];
        const engineModels = engines[value.engine]?.models ?? [];
        return (
          <div key={phase} className="grid gap-2 sm:grid-cols-[minmax(0,90px)_minmax(0,1fr)_minmax(0,1fr)] sm:items-end">
            <span className="hc-type-label text-white">{phase}</span>
            <label className="min-w-0">
              <span className="hc-type-label hc-dim">{t.phaseEngine}</span>
              <select
                value={value.engine}
                disabled={busy}
                aria-label={`${t.phaseEngine} ${phase}`}
                onChange={(e) => {
                  const engine = e.target.value;
                  const firstModel = engines[engine]?.models[0] ?? "";
                  setPhaseValues((prev) => ({ ...prev, [phase]: { engine, model: firstModel } }));
                }}
                className={CONTROL_CLASS}
              >
                {engineNames.map((name) => {
                  const disabled = (engines[name]?.models.length ?? 0) === 0;
                  return (
                    <option key={name} value={name} disabled={disabled}>
                      {engines[name]?.label ?? name}
                      {disabled ? ` — ${t.neuralwattDisabled}` : ""}
                    </option>
                  );
                })}
              </select>
            </label>
            <label className="min-w-0">
              <span className="hc-type-label hc-dim">{t.phaseModel}</span>
              <select
                value={value.model}
                disabled={busy || engineModels.length === 0}
                aria-label={`${t.phaseModel} ${phase}`}
                onChange={(e) => {
                  const model = e.target.value;
                  setPhaseValues((prev) => ({ ...prev, [phase]: { ...prev[phase], model } }));
                }}
                className={CONTROL_CLASS}
              >
                {engineModels.map((model) => (
                  <option key={model} value={model}>{model}</option>
                ))}
              </select>
            </label>
          </div>
        );
      })}
      <div className="grid gap-2 sm:grid-cols-2">
        <label className="min-w-0">
          <span className="hc-type-label hc-dim">{t.maxRoundsLabel}</span>
          <input
            type="number"
            min={1}
            value={maxRounds}
            disabled={busy}
            onChange={(e) => setMaxRounds(e.target.value)}
            className={CONTROL_CLASS}
          />
        </label>
        <label className="min-w-0">
          <span className="hc-type-label hc-dim">{t.maxHoursLabel}</span>
          <input
            type="number"
            min={1}
            value={maxHours}
            disabled={busy}
            onChange={(e) => setMaxHours(e.target.value)}
            className={CONTROL_CLASS}
          />
        </label>
      </div>
      {paramNames.map((name) => (
        <label key={name} className="block min-w-0">
          <span className="hc-type-label hc-dim">{t.paramLabel} · {name}</span>
          <textarea
            value={paramValues[name] ?? ""}
            disabled={busy}
            rows={2}
            aria-label={`${t.paramLabel} ${name}`}
            onChange={(e) => setParamValues((prev) => ({ ...prev, [name]: e.target.value }))}
            className="mt-1 min-h-16 w-full resize-y rounded-md border border-[var(--hc-border)] bg-black/25 px-2 py-1.5 text-base text-white placeholder:text-zinc-500 sm:text-sm"
          />
        </label>
      ))}
      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" disabled={busy} onClick={handleSubmit}>
          {busy ? "…" : t.submitStart}
        </Button>
        <Button size="sm" ghost disabled={busy} onClick={onCancel}>{t.cancelStart}</Button>
      </div>
    </div>
  );
}

function LoopDetailPanel({ detail }: { detail: LoopDetailResponse }) {
  return (
    <div className="space-y-3 text-xs">
      <div>
        <p className="hc-type-label hc-dim">{t.detailLedger}</p>
        {detail.ledger_tail.length > 0 ? (
          <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap break-words rounded-lg border border-[var(--hc-border)] bg-black/25 p-2 leading-5 hc-mono text-zinc-200">
            {detail.ledger_tail.join("\n")}
          </pre>
        ) : (
          <p className="hc-dim">{t.detailNoLedger}</p>
        )}
      </div>
      {detail.queue_entries ? (
        <div>
          <p className="hc-type-label hc-dim">{t.detailQueue}</p>
          <ul className="mt-1 space-y-1">
            {Object.entries(detail.queue_entries).map(([stage, files]) => (
              <li key={stage}>
                <span className="hc-mono">{stage}</span>: {files.length > 0 ? files.join(", ") : "—"}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      <div>
        <p className="hc-type-label hc-dim">{t.detailCommits}</p>
        {detail.commits.length > 0 ? (
          <ul className="mt-1 space-y-0.5 hc-mono">
            {detail.commits.map((line) => <li key={line}>{line}</li>)}
          </ul>
        ) : (
          <p className="hc-dim">{t.detailNoCommits}</p>
        )}
      </div>
      <div>
        <p className="hc-type-label hc-dim">{t.detailOverrides}</p>
        {Object.keys(detail.overrides).length > 0 ? (
          <ul className="mt-1 space-y-0.5 hc-mono">
            {Object.entries(detail.overrides).map(([key, value]) => <li key={key}>{key}={value}</li>)}
          </ul>
        ) : (
          <p className="hc-dim">{t.detailNoOverrides}</p>
        )}
      </div>
    </div>
  );
}

function LoopErrorCard({ pack }: { pack: LoopPackError }) {
  return (
    <FleetPanel
      eyebrow={<span className="truncate normal-case tracking-normal text-white">{pack.name}</span>}
      meta={<StatusPill tone="red" label={t.manifestError} size="sm" />}
    >
      <ToneCallout tone="red">{pack.error}</ToneCallout>
    </FleetPanel>
  );
}

interface LoopCardProps {
  pack: LoopPackSummary;
  models: LoopModelsResponse | null;
  selected: boolean;
  detail: LoopDetailResponse | null;
  detailLoading: boolean;
  detailError: string | null;
  busy: boolean;
  actionError?: string;
  startOpen: boolean;
  pendingStop: boolean;
  onSetPendingStop: (name: string | null) => void;
  onToggleDetail: (name: string) => void;
  onOpenStart: (name: string) => void;
  onCloseStart: () => void;
  onSubmitStart: (name: string, overrides: Record<string, string>) => void;
  onStop: (name: string) => void;
  onToggleTimer: (name: string, enabled: boolean) => void;
}

function LoopCard({
  pack,
  models,
  selected,
  detail,
  detailLoading,
  detailError,
  busy,
  actionError,
  startOpen,
  pendingStop,
  onSetPendingStop,
  onToggleDetail,
  onOpenStart,
  onCloseStart,
  onSubmitStart,
  onStop,
  onToggleTimer,
}: LoopCardProps) {
  const isStable = pack.stability === "stable";
  const statusLabel = pack.stop_requested ? t.stopRequested : pack.running ? t.statusRunning : t.statusIdle;
  const statusTone = pack.stop_requested ? "amber" : pack.running ? "cyan" : "zinc";

  return (
    <FleetPanel
      eyebrow={
        <span className="inline-flex min-w-0 flex-wrap items-center gap-2">
          <Led kind={pack.running ? "live" : "idle"} />
          <span className="truncate normal-case tracking-normal text-white">{pack.name}</span>
          <StatusPill tone={isStable ? "emerald" : "amber"} label={isStable ? t.stabilityStable : t.stabilityExperimental} size="sm" />
          <StatusPill tone="zinc" label={pack.type === "pipeline" ? t.typePipeline : t.typeSweep} size="sm" />
        </span>
      }
      meta={pack.commits_ahead > 0 ? <span title={t.commitsAheadHint}>{t.commitsAhead(pack.commits_ahead)}</span> : null}
    >
      <p className="text-sm hc-soft">{pack.description}</p>

      <div className="mt-2 flex flex-wrap items-center gap-2">
        <StatusPill tone={statusTone} label={statusLabel} size="sm" />
      </div>

      {pack.queue ? (
        <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
          {QUEUE_STAGES.map((stage) => (
            <Stat key={stage.key} label={stage.label} value={pack.queue?.[stage.key] ?? 0} />
          ))}
        </div>
      ) : null}

      <div className="mt-3 flex flex-wrap items-center justify-between gap-2 border-t border-[var(--hc-border)] pt-3">
        <label className="inline-flex items-center gap-2 text-xs hc-soft">
          <input
            type="checkbox"
            checked={pack.timer_enabled}
            disabled={busy}
            aria-label={`${t.timerLabel} ${pack.name}`}
            onChange={(e) => onToggleTimer(pack.name, e.target.checked)}
          />
          {t.timerLabel}: {pack.timer_enabled ? t.timerOn : t.timerOff}
        </label>
        <div className="flex flex-wrap items-center gap-2">
          {pack.running ? (
            pendingStop ? (
              <span className="inline-flex flex-wrap items-center gap-2">
                <span className="hc-type-label hc-soft">{t.confirmStop}</span>
                <Button size="xs" disabled={busy} onClick={() => onStop(pack.name)}>{busy ? "…" : t.confirmYes}</Button>
                <Button size="xs" ghost disabled={busy} onClick={() => onSetPendingStop(null)}>{t.confirmNo}</Button>
              </span>
            ) : (
              <Button size="xs" ghost disabled={busy} onClick={() => onSetPendingStop(pack.name)}>
                <Square className="h-3.5 w-3.5" />{t.actions.stop}
              </Button>
            )
          ) : (
            <Button size="xs" disabled={busy} onClick={() => onOpenStart(pack.name)}>
              <Play className="h-3.5 w-3.5" />{t.actions.start}
            </Button>
          )}
        </div>
      </div>

      {actionError ? <div className="mt-2"><ToneCallout tone="red">{actionError}</ToneCallout></div> : null}

      {startOpen ? (
        <div className="mt-3 border-t border-[var(--hc-border)] pt-3">
          <LoopStartForm
            pack={pack}
            models={models}
            busy={busy}
            onSubmit={(overrides) => onSubmitStart(pack.name, overrides)}
            onCancel={onCloseStart}
          />
        </div>
      ) : null}

      <div className="mt-3 border-t border-[var(--hc-border)] pt-3">
        <Disclosure
          open={selected}
          onToggle={() => onToggleDetail(pack.name)}
          summary={<span className="text-xs hc-soft">{t.actions.detail}</span>}
        >
          {detailLoading ? <p className="text-xs hc-dim">{t.loading}</p> : null}
          {detailError ? <ToneCallout tone="red">{t.detailError}</ToneCallout> : null}
          {detail ? <LoopDetailPanel detail={detail} /> : null}
        </Disclosure>
      </div>
    </FleetPanel>
  );
}

export interface LoopsGridProps {
  packs: LoopPack[];
  models: LoopModelsResponse | null;
  selectedPack: string | null;
  detail: LoopDetailResponse | null;
  detailLoading: boolean;
  detailError: string | null;
  busyPack: string | null;
  actionErrorByPack: Record<string, string>;
  startOpenPack: string | null;
  pendingStopPack: string | null;
  onSetPendingStop: (name: string | null) => void;
  onToggleDetail: (name: string) => void;
  onOpenStart: (name: string) => void;
  onCloseStart: () => void;
  onSubmitStart: (name: string, overrides: Record<string, string>) => void;
  onStop: (name: string) => void;
  onToggleTimer: (name: string, enabled: boolean) => void;
}

/** Pure presentation grid — exported for tests (rendered with fixtures), mirrors
 *  the LanesEditor pattern. `LoopsView` below wires the live hooks. */
export function LoopsGrid({
  packs,
  models,
  selectedPack,
  detail,
  detailLoading,
  detailError,
  busyPack,
  actionErrorByPack,
  startOpenPack,
  pendingStopPack,
  onSetPendingStop,
  onToggleDetail,
  onOpenStart,
  onCloseStart,
  onSubmitStart,
  onStop,
  onToggleTimer,
}: LoopsGridProps) {
  if (packs.length === 0) {
    return <FleetEmptyState title={t.empty} desc={t.subtitle} />;
  }
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      {packs.map((pack) =>
        isLoopPackError(pack) ? (
          <LoopErrorCard key={pack.name} pack={pack} />
        ) : (
          <LoopCard
            key={pack.name}
            pack={pack}
            models={models}
            selected={selectedPack === pack.name}
            detail={selectedPack === pack.name ? detail : null}
            detailLoading={selectedPack === pack.name ? detailLoading : false}
            detailError={selectedPack === pack.name ? detailError : null}
            busy={busyPack === pack.name}
            actionError={actionErrorByPack[pack.name]}
            startOpen={startOpenPack === pack.name}
            pendingStop={pendingStopPack === pack.name}
            onSetPendingStop={onSetPendingStop}
            onToggleDetail={onToggleDetail}
            onOpenStart={onOpenStart}
            onCloseStart={onCloseStart}
            onSubmitStart={onSubmitStart}
            onStop={onStop}
            onToggleTimer={onToggleTimer}
          />
        ),
      )}
    </div>
  );
}

export function LoopsView() {
  const loops = useLoops();
  const models = useLoopModels();
  const [selectedPack, setSelectedPack] = useState<string | null>(null);
  const [startOpenPack, setStartOpenPack] = useState<string | null>(null);
  const [pendingStopPack, setPendingStopPack] = useState<string | null>(null);
  const [busyPack, setBusyPack] = useState<string | null>(null);
  const [actionErrorByPack, setActionErrorByPack] = useState<Record<string, string>>({});
  const detail = useLoopDetail(selectedPack);

  const packs = loops.data?.packs ?? [];

  const clearActionError = (name: string) =>
    setActionErrorByPack((prev) => {
      if (!(name in prev)) return prev;
      const next = { ...prev };
      delete next[name];
      return next;
    });

  const handleToggleDetail = (name: string) => setSelectedPack((prev) => (prev === name ? null : name));
  const handleOpenStart = (name: string) => {
    clearActionError(name);
    setStartOpenPack(name);
  };
  const handleCloseStart = () => setStartOpenPack(null);

  const handleSubmitStart = async (name: string, overrides: Record<string, string>) => {
    setBusyPack(name);
    clearActionError(name);
    try {
      await startLoop(name, overrides);
      setStartOpenPack(null);
      await loops.reload();
    } catch (e) {
      setActionErrorByPack((prev) => ({ ...prev, [name]: `${t.startFailed}: ${extractDetail(e)}` }));
    } finally {
      setBusyPack(null);
    }
  };

  const handleStop = async (name: string) => {
    setBusyPack(name);
    clearActionError(name);
    try {
      await stopLoop(name);
      await loops.reload();
    } catch (e) {
      setActionErrorByPack((prev) => ({ ...prev, [name]: `${t.stopFailed}: ${extractDetail(e)}` }));
    } finally {
      setBusyPack(null);
      setPendingStopPack(null);
    }
  };

  const handleToggleTimer = async (name: string, enabled: boolean) => {
    setBusyPack(name);
    clearActionError(name);
    try {
      await toggleLoopTimer(name, enabled);
      await loops.reload();
    } catch (e) {
      setActionErrorByPack((prev) => ({ ...prev, [name]: `${t.timerFailed}: ${extractDetail(e)}` }));
    } finally {
      setBusyPack(null);
    }
  };

  return (
    <div className="space-y-5">
      <header>
        <p className="hc-eyebrow">{t.eyebrow}</p>
        <div className="mt-1 flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <h2 className="hc-type-title text-white">{t.title}</h2>
          <span className="hc-mono text-sm hc-dim">{t.subtitle}</span>
        </div>
      </header>

      {loops.error ? <ToneCallout tone="amber">{t.error}</ToneCallout> : null}

      <div className="flex items-center gap-2 text-xs hc-soft">{t.packCount(packs.length)}</div>

      <LoopsGrid
        packs={packs}
        models={models.data}
        selectedPack={selectedPack}
        detail={detail.data}
        detailLoading={detail.loading}
        detailError={detail.error}
        busyPack={busyPack}
        actionErrorByPack={actionErrorByPack}
        startOpenPack={startOpenPack}
        pendingStopPack={pendingStopPack}
        onSetPendingStop={setPendingStopPack}
        onToggleDetail={handleToggleDetail}
        onOpenStart={handleOpenStart}
        onCloseStart={handleCloseStart}
        onSubmitStart={(name, overrides) => void handleSubmitStart(name, overrides)}
        onStop={(name) => void handleStop(name)}
        onToggleTimer={(name, enabled) => void handleToggleTimer(name, enabled)}
      />
    </div>
  );
}
