import { useState } from "react";
import { Check, Scale } from "lucide-react";
import { cn } from "@/lib/utils";
import { SectionHeader, SignalChip, SubtabChips } from "../../components/leitstand";
import {
  COMPASS_ROLES,
  rankModelsForRole,
  ROLE_REQUIREMENTS,
  type CompassRole,
} from "./fit";
import {
  filterSinnvoll,
  type EditorRow,
  type LaneModelOption,
  type ModelProbeResult,
} from "./api";
import { ScoreMeter } from "./ScoreMeter";
import { providerDot } from "./providerColors";
import { PROBE_STATUS_LABEL, probeTone, t } from "./strings";

// Compass („Kompass") — decision aid Modell→Lane (S2 feature d). Role subtabs
// switch a requirement profile; the pure fit scoring (fit.ts) ranks the curated
// model set 0–100 with the evidence tokens that drove each score. „Übernehmen"
// stages the pick into the matrix row (the operator still confirms via Save).
// Bench compares 2–4 selected models side-by-side over one catalog probe.

const MAX_BENCH = 4;

function currentModelId(rows: EditorRow[], role: CompassRole): string | null {
  const row = rows.find((r) => r.profile === role);
  return row?.model ?? row?.defaultModel ?? null;
}

export function Compass({
  models,
  rows,
  probes,
  busy,
  benchRunning,
  benchResults,
  onAdopt,
  onBench,
}: {
  models: LaneModelOption[];
  rows: EditorRow[];
  probes: Record<string, ModelProbeResult>;
  busy: boolean;
  benchRunning: boolean;
  benchResults: ModelProbeResult[];
  onAdopt: (role: CompassRole, model: LaneModelOption) => void;
  onBench: (models: LaneModelOption[]) => void;
}) {
  const [role, setRole] = useState<CompassRole>("coder");
  const [selection, setSelection] = useState<string[]>([]);

  const candidates = filterSinnvoll(models);
  const ranked = rankModelsForRole(candidates, role, probes).slice(0, 5);
  const currentId = currentModelId(rows, role);

  const toggleSelect = (id: string) =>
    setSelection((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : prev.length >= MAX_BENCH ? prev : [...prev, id],
    );

  const selectedModels = selection
    .map((id) => candidates.find((m) => m.id === id))
    .filter((m): m is LaneModelOption => Boolean(m));

  return (
    <div className="space-y-3">
      <SubtabChips
        items={COMPASS_ROLES.map((r) => ({ id: r, label: ROLE_REQUIREMENTS[r].label }))}
        active={role}
        onSelect={setRole}
        ariaLabelPrefix="Rolle"
      />
      <p className="text-micro text-ink-3">{t.compassHint}</p>

      <div>
        <SectionHeader label={t.fitTop} rule={false} />
        <ul className="mt-2 space-y-2">
          {ranked.map((fit, index) => {
            const isCurrent = fit.model.id === currentId;
            const selected = selection.includes(fit.model.id);
            return (
              <li key={fit.model.id} className="rounded-card border border-line bg-surface-2 p-2.5">
                <div className="flex items-center gap-2">
                  <span className="w-4 shrink-0 text-right font-data text-micro tabular-nums text-ink-3">{index + 1}</span>
                  <span className={cn("pdot", providerDot(fit.model.provider, fit.model.id))} aria-hidden />
                  <span className="min-w-0 flex-1 truncate font-data text-micro text-ink">{fit.model.label}</span>
                  {isCurrent ? <span className="shrink-0 text-micro text-live">{t.aktuellMarker}</span> : null}
                  <button
                    type="button"
                    aria-pressed={selected}
                    title={t.bench}
                    aria-label={`${t.bench}: ${fit.model.label}`}
                    onClick={() => toggleSelect(fit.model.id)}
                    className={cn(
                      "inline-flex size-9 shrink-0 items-center justify-center rounded-card border transition-colors duration-150",
                      selected ? "border-live bg-live/15 text-bronze-hi" : "border-line text-ink-3 hover:border-live hover:text-live",
                    )}
                  >
                    <Check className="h-3.5 w-3.5" />
                  </button>
                </div>
                <ScoreMeter score={fit.score} className="mt-2" />
                {fit.reasons.length > 0 ? (
                  <div className="mt-1.5 flex flex-wrap gap-1">
                    {fit.reasons.map((reason) => (
                      <span
                        key={reason}
                        className="rounded-[5px] border border-line bg-surface-1 px-1.5 py-0.5 font-data text-micro text-ink-2"
                      >
                        {reason}
                      </span>
                    ))}
                  </div>
                ) : null}
                <button
                  type="button"
                  disabled={isCurrent || busy}
                  onClick={() => onAdopt(role, fit.model)}
                  className={cn(
                    "mt-2 min-h-11 w-full rounded-card border px-2.5 text-micro font-medium transition-colors duration-150",
                    isCurrent
                      ? "border-line text-ink-3"
                      : "border-live/50 text-live hover:border-live hover:bg-live/10 hover:text-bronze-hi disabled:opacity-40",
                  )}
                >
                  {isCurrent ? t.übernommen : t.übernehmen}
                </button>
              </li>
            );
          })}
        </ul>
      </div>

      <div className="border-t border-line pt-3">
        <SectionHeader label={t.bench} meta={`${selection.length}/${MAX_BENCH}`} rule={false} />
        <button
          type="button"
          disabled={busy || benchRunning || selection.length < 2}
          onClick={() => onBench(selectedModels)}
          className="mt-2 flex min-h-12 w-full items-center justify-center gap-2 rounded-card border border-live bg-live px-3 text-sec font-semibold text-surface-0 transition-colors duration-150 hover:bg-bronze-hi disabled:cursor-not-allowed disabled:opacity-40"
        >
          <Scale className="h-4 w-4" />
          {benchRunning ? t.benchRunning : benchResults.length > 0 ? t.benchRepeat : t.benchRun}
        </button>

        {benchResults.length === 0 ? (
          <p className="mt-2 text-micro text-ink-3">
            {selection.length < 2 ? t.benchSelect : t.benchEmpty}
          </p>
        ) : (
          <div className="mt-2 grid grid-cols-2 gap-2">
            {benchResults.map((probe) => {
              const model =
                models.find((m) => m.id === probe.model && (m.provider ?? "") === (probe.provider ?? "")) ??
                models.find((m) => m.id === probe.model);
              const price =
                model && (model.price_in_per_mtok_usd != null || model.price_out_per_mtok_usd != null)
                  ? `$${((model.price_in_per_mtok_usd ?? 0) + (model.price_out_per_mtok_usd ?? 0)).toFixed(2)}/1M`
                  : t.noProbeData;
              const reasoning = model?.reasoning_support && model.reasoning_support.length > 0
                ? model.reasoning_support.join("/")
                : "—";
              return (
                <div key={`${probe.provider}::${probe.model}`} className="min-w-0 rounded-card border border-line bg-surface-2 p-2.5">
                  <div className="flex min-w-0 items-center gap-1.5">
                    <span className={cn("pdot", providerDot(probe.provider, probe.model))} aria-hidden />
                    <span className="min-w-0 flex-1 truncate font-data text-micro text-ink">{probe.model}</span>
                  </div>
                  <div className="mt-1.5">
                    <SignalChip tone={probeTone(probe.status)} label={PROBE_STATUS_LABEL[probe.status]} />
                  </div>
                  <dl className="mt-1.5 space-y-0.5 font-data text-micro tabular-nums text-ink-2">
                    <div>{probe.duration_ms != null && probe.duration_ms > 0 ? `${probe.duration_ms} ms` : t.noProbeData}</div>
                    <div>{price}</div>
                    <div title="Reasoning-Support">{reasoning}</div>
                  </dl>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
