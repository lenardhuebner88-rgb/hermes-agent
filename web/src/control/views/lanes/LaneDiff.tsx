import { useMemo, useState } from "react";
import { cn } from "@/lib/utils";
import { SectionHeader, SignalChip, SignalLabel } from "../../components/leitstand";
import {
  laneDiffRows,
  type Lane,
  type LaneCatalogProfile,
  type LaneModelOption,
  type ModelProbeResult,
} from "./api";
import { PROBE_STATUS_LABEL, probeTone, t } from "./strings";

// LaneDiff („Vergleich") — S5 workbench: two lanes side-by-side, profile by
// profile. Rows carry probe evidence per cell (fresh session probes win over
// the GET /lanes cache echo); the „nur Abweichungen" filter collapses the
// identical wiring so an operator audits only what actually differs.

function DiffProbe({ probe }: { probe: ModelProbeResult | null }) {
  if (!probe) return <span className="text-micro text-ink-3">{t.probeUngeprüft}</span>;
  return (
    <span className="inline-flex items-center gap-1.5">
      <SignalLabel tone={probeTone(probe.status)} label={PROBE_STATUS_LABEL[probe.status]} />
      {probe.duration_ms != null && probe.duration_ms > 0 ? (
        <span className="font-data text-micro tabular-nums text-ink-2">{probe.duration_ms} ms</span>
      ) : null}
    </span>
  );
}

export function LaneDiff({
  lanes,
  catalog,
  models,
  probes,
  initialA,
  initialB,
}: {
  lanes: Lane[];
  catalog: LaneCatalogProfile[];
  models: LaneModelOption[];
  probes: Record<string, ModelProbeResult>;
  initialA: string | null;
  initialB: string | null;
}) {
  const [aId, setAId] = useState<string | null>(initialA);
  const [bId, setBId] = useState<string | null>(initialB);
  const [onlyDiff, setOnlyDiff] = useState(true);

  const laneA = lanes.find((l) => l.id === aId) ?? null;
  const laneB = lanes.find((l) => l.id === bId) ?? null;

  const rows = useMemo(
    () => (laneA && laneB ? laneDiffRows(laneA, laneB, catalog, models, probes) : []),
    [laneA, laneB, catalog, models, probes],
  );
  const visible = onlyDiff ? rows.filter((r) => r.differs) : rows;
  const diffCount = rows.filter((r) => r.differs).length;

  const selectClass =
    "min-h-11 w-full rounded-card border border-line bg-surface-2 px-2 text-sec text-ink focus:border-live focus:outline-none";

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-2">
        <label className="block">
          <span className="mb-1 block text-micro uppercase tracking-wide text-ink-3">{t.diffLaneA}</span>
          <select value={aId ?? ""} onChange={(e) => setAId(e.target.value || null)} className={selectClass}>
            {lanes.map((lane) => (
              <option key={lane.id} value={lane.id}>
                {lane.name}
              </option>
            ))}
          </select>
        </label>
        <label className="block">
          <span className="mb-1 block text-micro uppercase tracking-wide text-ink-3">{t.diffLaneB}</span>
          <select value={bId ?? ""} onChange={(e) => setBId(e.target.value || null)} className={selectClass}>
            {lanes.map((lane) => (
              <option key={lane.id} value={lane.id}>
                {lane.name}
              </option>
            ))}
          </select>
        </label>
      </div>

      {!laneA || !laneB ? (
        <p className="text-sec text-ink-3">{t.diffNeedTwo}</p>
      ) : (
        <>
          <div className="flex items-center justify-between gap-2">
            <SectionHeader
              label={t.vergleich}
              rule={false}
              meta={t.of(diffCount, rows.length)}
            />
            <button
              type="button"
              role="switch"
              aria-checked={onlyDiff}
              onClick={() => setOnlyDiff((v) => !v)}
              className={cn(
                "min-h-11 shrink-0 rounded-card border px-2.5 text-micro transition-colors duration-150",
                onlyDiff
                  ? "border-live bg-live/15 text-bronze-hi"
                  : "border-line text-ink-2 hover:border-live hover:text-live",
              )}
            >
              {onlyDiff ? t.diffOnlyChanges : t.diffAll}
            </button>
          </div>

          {visible.length === 0 ? (
            <div className="rounded-card border border-dashed border-line p-4">
              <p className="text-sec text-ink-2">{t.diffIdentical}</p>
            </div>
          ) : (
            <div className="overflow-x-auto rounded-card border border-line">
              <table className="w-full min-w-[30rem] border-collapse bg-surface-1 text-left">
                <thead>
                  <tr className="border-b border-line-soft">
                    <th scope="col" className="px-2.5 py-1.5 font-display text-micro font-semibold uppercase tracking-[0.1em] text-ink-3">
                      {t.diffColRole}
                    </th>
                    <th scope="col" className="px-2.5 py-1.5 font-display text-micro font-semibold uppercase tracking-[0.1em] text-ink-3">
                      {laneA.name}
                    </th>
                    <th scope="col" className="px-2.5 py-1.5 font-display text-micro font-semibold uppercase tracking-[0.1em] text-ink-3">
                      {laneB.name}
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {visible.map((row) => (
                    <tr
                      key={row.profile}
                      className={cn("border-b border-line-soft last:border-b-0", row.differs && "bg-surface-2")}
                    >
                      <th scope="row" className="px-2.5 py-2 text-sec font-medium text-ink">
                        <span className="flex items-center gap-2">
                          {row.profile}
                          {row.differs ? <SignalChip tone="warn" label="≠" /> : null}
                        </span>
                      </th>
                      <td className="px-2.5 py-2 align-top">
                        <div className="font-data text-micro text-ink">{row.aLabel}</div>
                        <div className="mt-0.5">
                          <DiffProbe probe={row.aProbe} />
                        </div>
                      </td>
                      <td className="px-2.5 py-2 align-top">
                        <div className="font-data text-micro text-ink">{row.bLabel}</div>
                        <div className="mt-0.5">
                          <DiffProbe probe={row.bProbe} />
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </div>
  );
}
