import { useMemo, useState } from "react";
import { Anchor, Play, Square, Wrench } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { cn } from "@/lib/utils";
import {
  duplicateLoop,
  extractDetail,
  landLoop,
  saveLoopFile,
  startLoop,
  stopLoop,
  toggleLoopTimer,
  useLoopDetail,
  useLoopFiles,
  useLoopModels,
  useLoops,
} from "../hooks/useControlData";
import { de } from "../i18n/de";
import { Led, StatusPill, ToneCallout } from "../components/atoms";
import { Disclosure } from "../components/primitives";
import { FleetEmptyState, FleetPanel } from "../components/fleet/atoms";
import { toneClasses } from "../lib/tones";
import {
  isLoopPackError,
  type LoopDetailResponse,
  type LoopFile,
  type LoopFilesResponse,
  type LoopHeartbeatHistoryEntry,
  type LoopModelsResponse,
  type LoopPack,
  type LoopPackError,
  type LoopPackSummary,
  type ToneName,
} from "../lib/types";
import { fmtDur, nowSec } from "../lib/derive";

const t = de.loops;

const CONTROL_CLASS =
  "min-h-11 w-full rounded-md border border-[var(--hc-border)] bg-black/25 px-2 py-1.5 text-base text-white sm:min-h-9 sm:text-sm";

/** Reihenfolge der Queue-Stufenleiste; 90-bounced steht separat (rot) daneben. */
const QUEUE_STAGE_KEYS = ["00-planned", "10-building", "20-verified", "30-landed"] as const;
const QUEUE_STAGE_LABEL: Record<(typeof QUEUE_STAGE_KEYS)[number], string> = {
  "00-planned": t.queuePlanned,
  "10-building": t.queueBuilding,
  "20-verified": t.queueVerified,
  "30-landed": t.queueLanded,
};

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

/** Ledger-Zeilen tragen ihr Ergebnis als Marker (✅/❌/⚠️/⏸); LAND-Zeilen sind
 *  eigene Meilensteine (nicht bloß eine Runde) und werden zusätzlich hervorgehoben. */
function ledgerLineTone(line: string): ToneName | null {
  if (line.includes("✅")) return "emerald";
  if (line.includes("❌") || line.includes("⛔")) return "red";
  if (line.includes("⚠️") || line.includes("⏸")) return "amber";
  return null;
}

function LedgerFeed({ lines }: { lines: string[] }) {
  return (
    <div className="mt-1 max-h-48 space-y-0.5 overflow-auto rounded-lg border border-[var(--hc-border)] bg-black/25 p-2 leading-5 hc-mono">
      {lines.map((line, idx) => {
        const isLand = /^LAND\b/.test(line.trim());
        const tone = ledgerLineTone(line) ?? (isLand ? "violet" : null);
        return (
          <div
            key={`${idx}-${line}`}
            className={cn("whitespace-pre-wrap break-words rounded px-1.5 py-0.5", tone ? toneClasses(tone) : "text-zinc-200", isLand && "font-semibold")}
          >
            {line}
          </div>
        );
      })}
    </div>
  );
}

/** Queue-Stufenleiste: 00-planned→10-building→20-verified→30-landed als Boxen,
 *  90-bounced separat (rot). Stufen mit >0 heben sich hervor. */
function LoopQueueStages({ queue }: { queue: Record<string, number> }) {
  const bounced = queue["90-bounced"] ?? 0;
  return (
    <div className="mt-3 flex flex-wrap items-stretch gap-1.5">
      {QUEUE_STAGE_KEYS.map((key) => {
        const n = queue[key] ?? 0;
        return (
          <div
            key={key}
            className={cn(
              "min-w-[4.25rem] flex-1 rounded-md border px-2 py-1.5 text-center",
              n > 0 ? "border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)]" : "border-[var(--hc-border)] bg-black/20",
            )}
          >
            <p className="hc-mono text-sm font-semibold text-white">{n}</p>
            <p className="hc-type-label hc-dim">{QUEUE_STAGE_LABEL[key]}</p>
          </div>
        );
      })}
      <div className={cn("min-w-[4.25rem] rounded-md border px-2 py-1.5 text-center", bounced > 0 ? toneClasses("red") : "border-[var(--hc-border)] bg-black/20")}>
        <p className="hc-mono text-sm font-semibold">{bounced}</p>
        <p className="hc-type-label hc-dim">{t.queueBounced}</p>
      </div>
    </div>
  );
}

/** Live-Phase-Chip: heartbeat.current → „phase · modell · seit Xm" mit pulsendem
 *  Led-Dot (hc-led-live respektiert prefers-reduced-motion bereits, kein neues
 *  CSS nötig). running ohne current → „zwischen Phasen" (Übergang gerade). */
function LoopHeartbeatChip({ pack, nowMs }: { pack: LoopPackSummary; nowMs: number }) {
  if (!pack.running) return null;
  const current = pack.heartbeat?.current ?? null;
  if (!current) {
    return <StatusPill tone="cyan" dot="ready" label={t.heartbeatBetweenPhases} size="sm" />;
  }
  const startedMs = Date.parse(current.started_at);
  const elapsedSec = Number.isFinite(startedMs) ? Math.max(0, Math.floor((nowMs - startedMs) / 1000)) : 0;
  return (
    <StatusPill tone="cyan" dot="live" label={t.heartbeatCurrent(current.phase, current.model, fmtDur(elapsedSec))} size="sm" />
  );
}

/** Dauer-Historie: die letzten ≤5 Phasen als kleine Chips, jüngste zuerst. */
function LoopHeartbeatHistory({ last }: { last: LoopHeartbeatHistoryEntry[] }) {
  if (last.length === 0) return null;
  const recent = last.slice(-5).reverse();
  return (
    <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
      {recent.map((entry, idx) => (
        <span
          key={`${entry.at}-${entry.phase}-${idx}`}
          className={cn("hc-type-label rounded-full border px-2 py-0.5", toneClasses(entry.rc === 0 ? "emerald" : "red"))}
        >
          {entry.phase} {entry.secs}s {entry.rc === 0 ? "✓" : "✗"}
        </span>
      ))}
    </div>
  );
}

function LoopDetailPanel({ detail }: { detail: LoopDetailResponse }) {
  return (
    <div className="space-y-3 text-xs">
      <div>
        <p className="hc-type-label hc-dim">{t.detailLedger}</p>
        {detail.ledger_tail.length > 0 ? (
          <LedgerFeed lines={detail.ledger_tail} />
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

const WORKSHOP_TEXTAREA_CLASS =
  "min-h-64 w-full resize-y rounded-md border border-[var(--hc-border)] bg-black/25 px-2 py-1.5 text-xs leading-5 text-white hc-mono disabled:opacity-70";

/** Editor für genau eine Datei. Gemountet mit `key={file.name}` vom Elternteil —
 *  ein Datei-/Reload-Wechsel remountet die Komponente statt den Entwurf per
 *  Effect zurückzusetzen (React-Doku: "Resetting state with a key"), damit kein
 *  Merge über Dateien hinweg entsteht und keine setState-in-Effect-Kaskade läuft. */
function LoopWorkstationFileEditor({
  file,
  saveBusy,
  saveError,
  onSave,
}: {
  file: LoopFile;
  saveBusy: boolean;
  saveError: string | null;
  onSave: (filename: string, content: string) => void;
}) {
  const [draft, setDraft] = useState(file.content);
  return (
    <div className="space-y-2">
      {!file.editable ? <ToneCallout tone="amber">{t.workshopReadOnly}</ToneCallout> : null}
      <textarea
        value={draft}
        disabled={!file.editable || saveBusy}
        onChange={(e) => setDraft(e.target.value)}
        rows={14}
        spellCheck={false}
        aria-label={`${t.workshopTitle} ${file.name}`}
        className={WORKSHOP_TEXTAREA_CLASS}
      />
      {file.editable ? (
        <div className="flex flex-wrap items-center gap-2">
          <Button size="xs" disabled={saveBusy || draft === file.content} onClick={() => onSave(file.name, draft)}>
            {saveBusy ? "…" : t.workshopSave}
          </Button>
          {saveError ? <ToneCallout tone="red">{t.workshopSaveFailed}: {saveError}</ToneCallout> : null}
        </div>
      ) : null}
    </div>
  );
}

/** Werkstatt-Panel: Datei-Tabs + Textarea + Speichern (nur editable Packs) +
 *  Duplizieren (immer, egal ob repo/custom). Rein präsentational — Netzwerk-
 *  Aufrufe laufen über die onSave/onDuplicate-Callbacks, wie LoopStartForm. */
function LoopWorkstationPanel({
  files,
  loading,
  error,
  saveBusy,
  saveError,
  onSave,
  duplicateBusy,
  duplicateError,
  onDuplicate,
}: {
  files: LoopFilesResponse | null;
  loading: boolean;
  error: string | null;
  saveBusy: boolean;
  saveError: string | null;
  onSave: (filename: string, content: string) => void;
  duplicateBusy: boolean;
  duplicateError: string | null;
  onDuplicate: (name: string) => void;
}) {
  const fileList = files?.files ?? [];
  const [activeName, setActiveName] = useState<string | null>(null);
  const active = fileList.find((f) => f.name === activeName) ?? fileList[0] ?? null;
  const [dupName, setDupName] = useState("");

  if (loading) return <p className="text-xs hc-dim">{t.loading}</p>;
  if (error) return <ToneCallout tone="red">{t.workshopError}: {error}</ToneCallout>;
  if (!files || fileList.length === 0) return <p className="text-xs hc-dim">{t.workshopEmpty}</p>;

  return (
    <div className="space-y-3 text-xs">
      <p className="hc-type-label hc-dim">{t.workshopTitle}</p>
      <div className="flex flex-wrap gap-1.5">
        {fileList.map((f) => (
          <Button key={f.name} size="xs" ghost={f.name !== active?.name} onClick={() => setActiveName(f.name)}>
            {f.name}
          </Button>
        ))}
      </div>
      {active ? (
        <LoopWorkstationFileEditor key={active.name} file={active} saveBusy={saveBusy} saveError={saveError} onSave={onSave} />
      ) : null}
      <div className="border-t border-[var(--hc-border)] pt-3">
        <p className="hc-type-label hc-dim">{t.workshopDuplicateTitle}</p>
        <div className="mt-1 flex flex-wrap items-center gap-2">
          <input
            type="text"
            value={dupName}
            disabled={duplicateBusy}
            placeholder={t.workshopDuplicatePlaceholder}
            aria-label={t.workshopDuplicateTitle}
            onChange={(e) => setDupName(e.target.value)}
            className="min-h-9 rounded-md border border-[var(--hc-border)] bg-black/25 px-2 py-1.5 text-sm text-white placeholder:text-zinc-500"
          />
          <Button size="xs" disabled={duplicateBusy || !dupName.trim()} onClick={() => onDuplicate(dupName.trim())}>
            {duplicateBusy ? "…" : t.workshopDuplicateSubmit}
          </Button>
        </div>
        {duplicateError ? <div className="mt-2"><ToneCallout tone="red">{t.workshopDuplicateFailed}: {duplicateError}</ToneCallout></div> : null}
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
  landNote?: string;
  startOpen: boolean;
  pendingStop: boolean;
  pendingLand: boolean;
  workshopOpen: boolean;
  files: LoopFilesResponse | null;
  filesLoading: boolean;
  filesError: string | null;
  fileSaveBusy: boolean;
  fileSaveError: string | null;
  duplicateBusy: boolean;
  duplicateError: string | null;
  nowMs: number;
  onSetPendingStop: (name: string | null) => void;
  onSetPendingLand: (name: string | null) => void;
  onToggleDetail: (name: string) => void;
  onToggleWorkshop: (name: string) => void;
  onOpenStart: (name: string) => void;
  onCloseStart: () => void;
  onSubmitStart: (name: string, overrides: Record<string, string>) => void;
  onStop: (name: string) => void;
  onLand: (name: string) => void;
  onToggleTimer: (name: string, enabled: boolean) => void;
  onSaveFile: (pack: string, filename: string, content: string) => void;
  onDuplicate: (source: string, name: string) => void;
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
  landNote,
  startOpen,
  pendingStop,
  pendingLand,
  workshopOpen,
  files,
  filesLoading,
  filesError,
  fileSaveBusy,
  fileSaveError,
  duplicateBusy,
  duplicateError,
  nowMs,
  onSetPendingStop,
  onSetPendingLand,
  onToggleDetail,
  onToggleWorkshop,
  onOpenStart,
  onCloseStart,
  onSubmitStart,
  onStop,
  onLand,
  onToggleTimer,
  onSaveFile,
  onDuplicate,
}: LoopCardProps) {
  const isStable = pack.stability === "stable";
  const statusLabel = pack.stop_requested ? t.stopRequested : pack.running ? t.statusRunning : t.statusIdle;
  const statusTone = pack.stop_requested ? "amber" : pack.running ? "cyan" : "zinc";
  const canLand = !pack.running && pack.commits_ahead > 0;

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
        <LoopHeartbeatChip pack={pack} nowMs={nowMs} />
      </div>
      {pack.heartbeat?.last.length ? <LoopHeartbeatHistory last={pack.heartbeat.last} /> : null}

      {pack.queue ? <LoopQueueStages queue={pack.queue} /> : null}

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
            <>
              <Button size="xs" disabled={busy} onClick={() => onOpenStart(pack.name)}>
                <Play className="h-3.5 w-3.5" />{t.actions.start}
              </Button>
              {canLand ? (
                pendingLand ? (
                  <span className="inline-flex flex-wrap items-center gap-2">
                    <span className="hc-type-label hc-soft">{t.confirmLand}</span>
                    <Button size="xs" disabled={busy} onClick={() => onLand(pack.name)}>{busy ? "…" : t.confirmYes}</Button>
                    <Button size="xs" ghost disabled={busy} onClick={() => onSetPendingLand(null)}>{t.confirmNo}</Button>
                  </span>
                ) : (
                  <Button size="xs" ghost disabled={busy} onClick={() => onSetPendingLand(pack.name)}>
                    <Anchor className="h-3.5 w-3.5" />{t.actions.land}
                  </Button>
                )
              ) : null}
            </>
          )}
          <Button size="xs" ghost disabled={busy} onClick={() => onToggleWorkshop(pack.name)}>
            <Wrench className="h-3.5 w-3.5" />{t.actions.workshop}
          </Button>
        </div>
      </div>

      {actionError ? <div className="mt-2"><ToneCallout tone="red">{actionError}</ToneCallout></div> : null}
      {landNote ? <div className="mt-2"><ToneCallout tone="emerald">{landNote}</ToneCallout></div> : null}

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

      {workshopOpen ? (
        <div className="mt-3 border-t border-[var(--hc-border)] pt-3">
          <LoopWorkstationPanel
            files={files}
            loading={filesLoading}
            error={filesError}
            saveBusy={fileSaveBusy}
            saveError={fileSaveError}
            onSave={(filename, content) => onSaveFile(pack.name, filename, content)}
            duplicateBusy={duplicateBusy}
            duplicateError={duplicateError}
            onDuplicate={(name) => onDuplicate(pack.name, name)}
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
  landNoteByPack: Record<string, string>;
  startOpenPack: string | null;
  pendingStopPack: string | null;
  pendingLandPack: string | null;
  workshopOpenPack: string | null;
  files: LoopFilesResponse | null;
  filesLoading: boolean;
  filesError: string | null;
  fileSaveBusy: boolean;
  fileSaveError: string | null;
  duplicateBusy: boolean;
  duplicateError: string | null;
  /** Referenz-„jetzt" für die Heartbeat-Dauer — Default Date.now(), im Test injizierbar. */
  nowMs?: number;
  onSetPendingStop: (name: string | null) => void;
  onSetPendingLand: (name: string | null) => void;
  onToggleDetail: (name: string) => void;
  onToggleWorkshop: (name: string) => void;
  onOpenStart: (name: string) => void;
  onCloseStart: () => void;
  onSubmitStart: (name: string, overrides: Record<string, string>) => void;
  onStop: (name: string) => void;
  onLand: (name: string) => void;
  onToggleTimer: (name: string, enabled: boolean) => void;
  onSaveFile: (pack: string, filename: string, content: string) => void;
  onDuplicate: (source: string, name: string) => void;
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
  landNoteByPack,
  startOpenPack,
  pendingStopPack,
  pendingLandPack,
  workshopOpenPack,
  files,
  filesLoading,
  filesError,
  fileSaveBusy,
  fileSaveError,
  duplicateBusy,
  duplicateError,
  nowMs = nowSec() * 1000,
  onSetPendingStop,
  onSetPendingLand,
  onToggleDetail,
  onToggleWorkshop,
  onOpenStart,
  onCloseStart,
  onSubmitStart,
  onStop,
  onLand,
  onToggleTimer,
  onSaveFile,
  onDuplicate,
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
            landNote={landNoteByPack[pack.name]}
            startOpen={startOpenPack === pack.name}
            pendingStop={pendingStopPack === pack.name}
            pendingLand={pendingLandPack === pack.name}
            workshopOpen={workshopOpenPack === pack.name}
            files={workshopOpenPack === pack.name ? files : null}
            filesLoading={workshopOpenPack === pack.name ? filesLoading : false}
            filesError={workshopOpenPack === pack.name ? filesError : null}
            fileSaveBusy={fileSaveBusy}
            fileSaveError={fileSaveError}
            duplicateBusy={duplicateBusy}
            duplicateError={duplicateError}
            nowMs={nowMs}
            onSetPendingStop={onSetPendingStop}
            onSetPendingLand={onSetPendingLand}
            onToggleDetail={onToggleDetail}
            onToggleWorkshop={onToggleWorkshop}
            onOpenStart={onOpenStart}
            onCloseStart={onCloseStart}
            onSubmitStart={onSubmitStart}
            onStop={onStop}
            onLand={onLand}
            onToggleTimer={onToggleTimer}
            onSaveFile={onSaveFile}
            onDuplicate={onDuplicate}
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
  const [pendingLandPack, setPendingLandPack] = useState<string | null>(null);
  const [workshopOpenPack, setWorkshopOpenPack] = useState<string | null>(null);
  const [busyPack, setBusyPack] = useState<string | null>(null);
  const [actionErrorByPack, setActionErrorByPack] = useState<Record<string, string>>({});
  const [landNoteByPack, setLandNoteByPack] = useState<Record<string, string>>({});
  const [fileSaveBusy, setFileSaveBusy] = useState(false);
  const [fileSaveError, setFileSaveError] = useState<string | null>(null);
  const [duplicateBusy, setDuplicateBusy] = useState(false);
  const [duplicateError, setDuplicateError] = useState<string | null>(null);
  const detail = useLoopDetail(selectedPack);
  const files = useLoopFiles(workshopOpenPack);

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

  const handleLand = async (name: string) => {
    setBusyPack(name);
    clearActionError(name);
    try {
      const result = await landLoop(name);
      setLandNoteByPack((prev) => ({ ...prev, [name]: `${t.landStarted} (${result.log}): ${result.note}` }));
      setSelectedPack(name); // Detail-Ledger nach Auslösen sichtbar machen
      await loops.reload();
    } catch (e) {
      setActionErrorByPack((prev) => ({ ...prev, [name]: `${t.landFailed}: ${extractDetail(e)}` }));
    } finally {
      setBusyPack(null);
      setPendingLandPack(null);
    }
  };

  const handleToggleWorkshop = (name: string) => {
    setWorkshopOpenPack((prev) => (prev === name ? null : name));
    setFileSaveError(null);
    setDuplicateError(null);
  };

  const handleSaveFile = async (pack: string, filename: string, content: string) => {
    setFileSaveBusy(true);
    setFileSaveError(null);
    try {
      await saveLoopFile(pack, filename, content);
      await files.reload();
    } catch (e) {
      setFileSaveError(extractDetail(e));
    } finally {
      setFileSaveBusy(false);
    }
  };

  const handleDuplicate = async (source: string, name: string) => {
    setDuplicateBusy(true);
    setDuplicateError(null);
    try {
      await duplicateLoop(source, name);
      await loops.reload();
    } catch (e) {
      setDuplicateError(extractDetail(e));
    } finally {
      setDuplicateBusy(false);
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
        landNoteByPack={landNoteByPack}
        startOpenPack={startOpenPack}
        pendingStopPack={pendingStopPack}
        pendingLandPack={pendingLandPack}
        workshopOpenPack={workshopOpenPack}
        files={files.data}
        filesLoading={files.loading}
        filesError={files.error}
        fileSaveBusy={fileSaveBusy}
        fileSaveError={fileSaveError}
        duplicateBusy={duplicateBusy}
        duplicateError={duplicateError}
        onSetPendingStop={setPendingStopPack}
        onSetPendingLand={setPendingLandPack}
        onToggleDetail={handleToggleDetail}
        onToggleWorkshop={handleToggleWorkshop}
        onOpenStart={handleOpenStart}
        onCloseStart={handleCloseStart}
        onSubmitStart={(name, overrides) => void handleSubmitStart(name, overrides)}
        onStop={(name) => void handleStop(name)}
        onLand={(name) => void handleLand(name)}
        onToggleTimer={(name, enabled) => void handleToggleTimer(name, enabled)}
        onSaveFile={(pack, filename, content) => void handleSaveFile(pack, filename, content)}
        onDuplicate={(source, name) => void handleDuplicate(source, name)}
      />
    </div>
  );
}
