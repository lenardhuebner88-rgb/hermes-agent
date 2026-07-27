import { useEffect, useState } from "react";
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
// stages the pick into the matrix row (the operator still confirms via Save)
// and flashes „Übernommen ✓" so the staging is visible. Bench compares 2–4
// selected models in a real side-by-side table over one catalog probe.

const MAX_BENCH = 4;
const TOP_N = 5;

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
  benchError,
  benchResults,
  onAdopt,
  onBench,
}: {
  models: LaneModelOption[];
  rows: EditorRow[];
  probes: Record<string, ModelProbeResult>;
  busy: boolean;
  benchRunning: boolean;
  /** Last bench failure (shown inline; the previous table stays). */
  benchError?: string | null;
  benchResults: ModelProbeResult[];
  onAdopt: (role: CompassRole, model: LaneModelOption) => void;
  onBench: (models: LaneModelOption[]) => void;
}) {
  const [role, setRole] = useState<CompassRole>("coder");
  const [selection, setSelection] = useState<string[]>([]);
  const [showAllFits, setShowAllFits] = useState(false);
  const [adoptedId, setAdoptedId] = useState<string | null>(null);

  const candidates = filterSinnvoll(models);
  const rankedAll = rankModelsForRole(candidates, role, probes);
  const ranked = showAllFits ? rankedAll : rankedAll.slice(0, TOP_N);
  const currentId = currentModelId(rows, role);

  // „Übernommen ✓" transient after staging a pick into the matrix row.
  useEffect(() => {
    if (adoptedId === null) return;
    const timer = setTimeout(() => setAdoptedId(null), 2000);
    return () => clearTimeout(timer);
  }, [adoptedId]);

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
        onSelect={(next) => {
          setRole(next);
          setShowAllFits(false);
        }}
        ariaLabelPrefix="Rolle"
      />
      <p className="text-micro text-ink-3">{t.compassHint}</p>

      <div>
        <SectionHeader
          label={t.fitTop}
          rule={false}
          meta={rankedAll.length > TOP_N ? t.of(ranked.length, rankedAll.length) : undefined}
        />
        {/* Dense ranking rows (mockup AB2): rank · dot · name · aktuell-marker ·
            bench-toggle · inline Übernehmen; meter + evidence chips beneath.
            The adopt button collapses to a passive „✓" marker on the current
            model — the ranking reads as a list, not a stack of card-CTAs. */}
        <ul className="mt-2 space-y-1.5">
          {ranked.map((fit, index) => {
            const isCurrent = fit.model.id === currentId;
            const selected = selection.includes(fit.model.id);
            const justAdopted = adoptedId === fit.model.id;
            return (
              <li key={fit.model.id} className="rounded-card border border-line bg-surface-2 px-2.5 py-2">
                <div className="flex items-center gap-2">
                  <span className="w-4 shrink-0 text-right font-data text-micro tabular-nums text-ink-3">{index + 1}</span>
                  <span className={cn("pdot", providerDot(fit.model.provider, fit.model.id))} aria-hidden />
                  <span className="min-w-0 flex-1 truncate font-data text-micro text-ink">{fit.model.label}</span>
                  <button
                    type="button"
                    aria-pressed={selected}
                    title={fit.model.runtime === "hermes" ? t.bench : "nicht bench-bar (claude-cli)"}
                    aria-label={`${t.bench}: ${fit.model.label}`}
                    disabled={fit.model.runtime !== "hermes"}
                    onClick={() => toggleSelect(fit.model.id)}
                    className={cn(
                      "inline-flex size-11 shrink-0 items-center justify-center rounded-card border transition-colors duration-150 disabled:opacity-40 min-[52rem]:size-8",
                      selected ? "border-live bg-live/15 text-bronze-hi" : "border-line text-ink-3 hover:border-live hover:text-live",
                    )}
                  >
                    <Check className="h-3.5 w-3.5" />
                  </button>
                  {isCurrent ? (
                    <span className="shrink-0 px-1 text-micro text-live" title={t.aktuellMarker}>
                      {t.aktuellMarker}
                    </span>
                  ) : (
                    <button
                      type="button"
                      disabled={busy || justAdopted}
                      onClick={() => {
                        onAdopt(role, fit.model);
                        setAdoptedId(fit.model.id);
                      }}
                      className="min-h-11 shrink-0 rounded-card border border-live/50 px-2 text-micro font-medium text-live transition-colors duration-150 hover:border-live hover:bg-live/10 hover:text-bronze-hi disabled:opacity-40 min-[52rem]:min-h-8"
                    >
                      {justAdopted ? t.übernommen : t.übernehmen}
                    </button>
                  )}
                </div>
                <ScoreMeter score={fit.score} className="mt-1.5" />
                {fit.reasons.length > 0 ? (
                  <div className="mt-1 flex flex-wrap gap-1">
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
              </li>
            );
          })}
        </ul>
        {rankedAll.length > TOP_N ? (
          <button
            type="button"
            onClick={() => setShowAllFits((v) => !v)}
            className="mt-2 min-h-11 w-full rounded-card border border-line px-2 text-micro text-ink-2 transition-colors duration-150 hover:border-live hover:text-live"
          >
            {showAllFits ? `Top ${TOP_N}` : `${t.diffAll} (${rankedAll.length})`}
          </button>
        ) : null}
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

        {benchError ? (
          <p className="mt-2 text-micro text-status-alert" role="alert">
            {t.benchFehler}: {benchError}
          </p>
        ) : null}

        {benchResults.length === 0 ? (
          <p className="mt-2 text-micro text-ink-3">
            {selection.length < 2 ? t.benchSelect : t.benchEmpty}
          </p>
        ) : (
          /* Echte Vergleichstabelle: eine Zeile je gebenchtes Modell, eine
             Spalte je Evidenz-Dimension — Zahlen spaltenweise vergleichbar
             (font-data + tabular-nums). */
          <div className="mt-2 overflow-x-auto rounded-card border border-line">
            <table className="w-full min-w-[26rem] border-collapse bg-surface-2 text-left">
              <thead>
                <tr className="border-b border-line-soft">
                  {[t.benchColModel, t.benchColStatus, t.benchColLatency, t.benchColPrice, t.benchColReasoning].map((head) => (
                    <th
                      key={head}
                      scope="col"
                      className="px-2.5 py-1.5 font-display text-micro font-semibold uppercase tracking-[0.1em] text-ink-3"
                    >
                      {head}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
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
                    <tr key={`${probe.provider}::${probe.model}`} className="border-b border-line-soft last:border-b-0">
                      <td className="px-2.5 py-2">
                        <span className="flex items-center gap-1.5">
                          <span className={cn("pdot", providerDot(probe.provider, probe.model))} aria-hidden />
                          <span className="truncate font-data text-micro text-ink">{probe.model}</span>
                        </span>
                      </td>
                      <td className="px-2.5 py-2">
                        <SignalChip tone={probeTone(probe.status)} label={PROBE_STATUS_LABEL[probe.status]} />
                      </td>
                      <td className="px-2.5 py-2 font-data text-micro tabular-nums text-ink-2">
                        {probe.duration_ms != null && probe.duration_ms > 0 ? `${probe.duration_ms} ms` : t.noProbeData}
                      </td>
                      <td className="px-2.5 py-2 font-data text-micro tabular-nums text-ink-2">{price}</td>
                      <td className="px-2.5 py-2 font-data text-micro text-ink-2" title="Reasoning-Support">{reasoning}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
