import { useCallback, useEffect, useMemo, useState } from "react";
import { TriangleAlert } from "lucide-react";
import { SubtabChips } from "../components/leitstand";
import type { Density } from "../hooks/useDensity";
import {
  activateLane,
  applyChoice,
  createLane,
  editorRows,
  FALLBACK_MODELS,
  loadLanes,
  persistLaneModels,
  persistPayloadFromEditorRows,
  type EditorRow,
  type Lane,
  type LaneFallbackProvider,
  type LanesResponse,
} from "./lanes/api";
import { LaneBar } from "./lanes/LaneBar";
import { ProfileMatrix } from "./lanes/ProfileMatrix";
import { t } from "./lanes/strings";
import "./lanes/lanes.css";

// /lanes → Modell-Plattform (greenfield S2). Replaces the old LanesView monolith:
// a lane bar (activate / neue Lane), a Profil-Matrix (Modell + Reasoning +
// Fallback + Probe + Override, persist + activate), and a right pane with the
// Rauch (probes) and Kompass (fit-ranking) subtabs. Self-contained on purpose —
// no shared file (i18n/lib/ControlShell/ControlPage/useControlData) is touched.

type RightTab = "rauch" | "kompass";

const RIGHT_TABS = [
  { id: "rauch" as const, label: t.rauch },
  { id: "kompass" as const, label: t.kompass },
];

function LanesPlatform({
  data,
  lane,
  busy,
  onActivate,
  onCreate,
  onSave,
}: {
  data: LanesResponse;
  lane: Lane;
  busy: boolean;
  onActivate: (laneId: string) => void;
  onCreate: (name: string) => void;
  onSave: (rows: EditorRow[]) => Promise<void>;
}) {
  const models = useMemo(
    () => (data.models && data.models.length > 0 ? data.models : FALLBACK_MODELS),
    [data.models],
  );
  const [rows, setRows] = useState<EditorRow[]>(() => editorRows(lane, data.profiles, models));
  const [dirty, setDirty] = useState(false);
  const [subtab, setSubtab] = useState<RightTab>("rauch");
  const [saveError, setSaveError] = useState<string | null>(null);

  const updateRow = useCallback((profile: string, patch: Partial<EditorRow>) => {
    setRows((prev) => prev.map((row) => (row.profile === profile ? { ...row, ...patch } : row)));
    setDirty(true);
    setSaveError(null);
  }, []);

  const handleSave = useCallback(async () => {
    setSaveError(null);
    try {
      await onSave(rows);
      setDirty(false);
    } catch (e) {
      setSaveError(e instanceof Error ? e.message : String(e));
    }
  }, [onSave, rows]);

  const handleDiscard = useCallback(() => {
    setRows(editorRows(lane, data.profiles, models));
    setDirty(false);
    setSaveError(null);
  }, [lane, data.profiles, models]);

  return (
    <div className="space-y-4">
      <LaneBar
        lanes={data.lanes}
        activeId={data.active_id}
        busy={busy}
        onActivate={onActivate}
        onCreate={onCreate}
      />

      <div className="lp-main">
        <ProfileMatrix
          rows={rows}
          models={models}
          busy={busy}
          dirty={dirty}
          probing={{}}
          probes={{}}
          onModelChange={(profile, choice) =>
            updateRow(profile, applyChoice(rows.find((r) => r.profile === profile)!, choice, models))
          }
          onReasoningChange={(profile, value) => updateRow(profile, { reasoning: value })}
          onFallbackChange={(profile, fallbackProviders: LaneFallbackProvider[]) =>
            updateRow(profile, { fallbackProviders })
          }
          onProbeRow={() => {
            /* Phase B: per-row model probe */
          }}
          onSave={() => void handleSave()}
          onDiscard={handleDiscard}
          saveError={saveError}
        />

        <div className="min-w-0 space-y-3">
          <SubtabChips items={RIGHT_TABS} active={subtab} onSelect={setSubtab} ariaLabelPrefix="Bereich" />
          {subtab === "rauch" ? (
            <div className="rounded-card border border-dashed border-line p-4">
              <p className="text-sec text-ink-2">{t.smokeEmptyTitle}</p>
              <p className="mt-1 text-micro text-ink-3">{t.smokeEmptyEval}</p>
              <p className="mt-1 text-micro text-ink-3">{t.smokeEmptyAction}</p>
            </div>
          ) : (
            <div className="rounded-card border border-dashed border-line p-4">
              <p className="text-sec text-ink-2">{t.kompass}</p>
              <p className="mt-1 text-micro text-ink-3">{t.compassHint}</p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export function LanesView(_props: { density?: Density }) {
  const [data, setData] = useState<LanesResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [failCount, setFailCount] = useState(0);

  const reload = useCallback(async () => {
    try {
      setData(await loadLanes());
      setError(null);
      setFailCount(0);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setFailCount((n) => n + 1);
    }
  }, []);

  useEffect(() => {
    const firstLoad = window.setTimeout(() => void reload(), 0);
    return () => window.clearTimeout(firstLoad);
  }, [reload]);

  // Self-heal a failed first load (e.g. "Failed to fetch" right after mobile
  // foregrounding) with mild backoff — only while no data has arrived yet.
  useEffect(() => {
    if (data !== null || failCount === 0) return;
    const timer = setTimeout(() => void reload(), Math.min(5_000 * failCount, 30_000));
    return () => clearTimeout(timer);
  }, [data, failCount, reload]);

  const run = useCallback(
    async (op: () => Promise<unknown>) => {
      setBusy(true);
      try {
        await op();
        await reload();
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setBusy(false);
      }
    },
    [reload],
  );

  const lanes = data?.lanes ?? [];
  const lane = lanes.find((l) => l.id === selectedId) ?? lanes.find((l) => l.active) ?? lanes[0] ?? null;

  const handleSave = useCallback(
    async (rows: EditorRow[]) => {
      const target = lane;
      if (!target) return;
      await run(async () => {
        const payload = persistPayloadFromEditorRows(rows);
        if (Object.keys(payload).length > 0) {
          const result = await persistLaneModels(payload);
          if (result.failed.length > 0) {
            throw new Error(
              `Speichern fehlgeschlagen: ${result.failed.map((f) => `${f.profile} (${f.error})`).join(", ")}`,
            );
          }
        }
        if (!target.active) await activateLane(target.id);
      });
    },
    [lane, run],
  );

  return (
    <section aria-label={t.title} className="lp space-y-4">
      {error ? (
        <div className="flex items-center justify-between gap-3 rounded-card border border-status-alert/30 bg-status-alert/10 px-3 py-2 text-sec text-status-alert">
          <span className="flex min-w-0 items-start gap-2">
            <TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />
            <span className="min-w-0">{error}</span>
          </span>
          <button
            type="button"
            onClick={() => void reload()}
            disabled={busy}
            className="min-h-11 shrink-0 rounded-card border border-line px-2.5 text-micro text-ink-2 disabled:opacity-40"
          >
            {t.retry}
          </button>
        </div>
      ) : null}

      {data === null ? (
        <p className="text-sec text-ink-3">{t.loading}</p>
      ) : lane === null ? (
        <div className="rounded-card border border-dashed border-line p-4">
          <p className="text-sec text-ink-2">{t.emptyLanesTitle}</p>
          <p className="mt-1 text-micro text-ink-3">{t.emptyLanesDesc}</p>
        </div>
      ) : (
        <LanesPlatform
          key={`${lane.id}:${lane.updated_at ?? 0}`}
          data={data}
          lane={lane}
          busy={busy}
          onActivate={(laneId) => void run(() => activateLane(laneId))}
          onCreate={(name) =>
            void run(async () => {
              const res = await createLane(name, {});
              setSelectedId(res.lane.id);
            })
          }
          onSave={handleSave}
        />
      )}
    </section>
  );
}
