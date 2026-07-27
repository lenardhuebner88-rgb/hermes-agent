import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { RefreshCw, TriangleAlert } from "lucide-react";
import { DrawerShell, FleetEmptyState, SignalChip, SignalLabel, SubtabChips } from "../components/leitstand";
import type { Density } from "../hooks/useDensity";
import {
  activateLane,
  applyChoice,
  choiceForModel,
  createLane,
  deleteLane,
  editorRows,
  FALLBACK_MODELS,
  filterSinnvoll,
  laneHealthSummary,
  loadLanes,
  persistLaneModels,
  persistPayloadFromEditorRows,
  probeKey,
  removedProfilesFromEditorRows,
  runCatalogProbe,
  runModelProbe,
  UNREACHABLE_PROBE_STATUSES,
  updateLane,
  type EditorRow,
  type Lane,
  type LaneFallbackProvider,
  type LaneModelOption,
  type LaneSpawnHealth,
  type LanesResponse,
  type ModelProbeResult,
} from "./lanes/api";
import { buildPresetDraft, type CompassRole, type LanePresetId } from "./lanes/fit";
import { LaneBar } from "./lanes/LaneBar";
import { LaneDiff } from "./lanes/LaneDiff";
import { ProfileMatrix } from "./lanes/ProfileMatrix";
import { SmokePanel } from "./lanes/SmokePanel";
import { Compass } from "./lanes/Compass";
import { t } from "./lanes/strings";
import "./lanes/lanes.css";

// /lanes → Modell-Werkbank. A lane rail (ansehen ≠ aktivieren, Verwaltung,
// Preset-Entwürfe), a Profil-Matrix (Modell + Reasoning + Fast + Fallback +
// Probe + Override, persist + activate), and an evidence dock with the Rauch
// (probes), Kompass (fit-ranking) and Vergleich (lane diff) subtabs. From the
// 3-zone cut the rail docks left (lanes.css); below, it pins to the top.
// Self-contained on purpose — no shared file (i18n/lib/ControlShell/
// ControlPage/useControlData) is touched.

type RightTab = "rauch" | "kompass" | "vergleich";

const RIGHT_TABS = [
  { id: "rauch" as const, label: t.rauch },
  { id: "kompass" as const, label: t.kompass },
  { id: "vergleich" as const, label: t.vergleich },
];

/** Compact-tier fork (<600px / `tab`): Rauch & Kompass & Vergleich move into a
 *  DrawerShell instead of a second pane. Mirrors useTwoPaneExpanded's
 *  matchMedia guard so it is SSR/test-safe (no matchMedia → not compact →
 *  inline panes). */
function useCompact(): boolean {
  const [compact, setCompact] = useState(
    () =>
      typeof window !== "undefined" &&
      typeof window.matchMedia === "function" &&
      !window.matchMedia("(min-width: 37.5rem)").matches,
  );
  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return;
    const media = window.matchMedia("(min-width: 37.5rem)");
    const onChange = () => setCompact(!media.matches);
    onChange();
    media.addEventListener("change", onChange);
    return () => media.removeEventListener("change", onChange);
  }, []);
  return compact;
}

/** Lade-Skelett in LaneBar- + Matrix-Geometrie (Leer ≠ Ladend ≠ Fehler). */
function LanesSkeleton() {
  return (
    <div aria-label={t.loading} aria-busy="true" role="status" className="space-y-4">
      <div className="flex gap-2 overflow-hidden">
        {[0, 1, 2].map((i) => (
          <div key={i} className="lp-skel h-24 min-w-[11rem] shrink-0" />
        ))}
      </div>
      <div className="space-y-3 rounded-panel border border-line bg-surface-1 p-3">
        <div className="lp-skel h-4 w-44" />
        {[0, 1, 2, 3, 4, 5].map((i) => (
          <div key={i} className="lp-skel h-10 w-full" />
        ))}
      </div>
      <span className="sr-only">{t.loading}</span>
    </div>
  );
}

/** Reachability rollup over the curated hermes set (fresh probes win over the
 *  GET /lanes cache echo) — shared by the health strip and the mobile badges. */
function probeRollup(models: LaneModelOption[], probes: Record<string, ModelProbeResult>) {
  const probeable = filterSinnvoll(models).filter((m) => m.runtime === "hermes");
  const effective = probeable.map((m) => probes[probeKey(m.provider, m.id)] ?? m.probe ?? null);
  return {
    total: probeable.length,
    reachable: effective.filter((p) => p && (p.status === "ok" || p.status === "fallback")).length,
    blocked: effective.filter((p) => p && UNREACHABLE_PROBE_STATUSES.has(p.status)).length,
  };
}

function LanesPlatform({
  data,
  lane,
  busy,
  savedAt,
  density,
  focusProfile,
  onSelect,
  onActivate,
  onCreate,
  onRename,
  onDuplicate,
  onDelete,
  onSave,
  onReload,
}: {
  data: LanesResponse;
  lane: Lane;
  busy: boolean;
  savedAt: number | null;
  density?: Density;
  focusProfile?: string | null;
  onSelect: (laneId: string) => void;
  onActivate: (laneId: string) => void;
  onCreate: (name: string, preset: LanePresetId | null) => void;
  onRename: (laneId: string, name: string) => void;
  onDuplicate: (laneId: string) => void;
  onDelete: (laneId: string) => void;
  onSave: (rows: EditorRow[]) => Promise<void>;
  onReload: () => Promise<void>;
}) {
  const models = useMemo(
    () => (data.models && data.models.length > 0 ? data.models : FALLBACK_MODELS),
    [data.models],
  );
  const [rows, setRows] = useState<EditorRow[]>(() => editorRows(lane, data.profiles, models));
  const [dirty, setDirty] = useState(false);
  const dirtyRef = useRef(false);
  const [subtab, setSubtab] = useState<RightTab>("rauch");
  const [saveError, setSaveError] = useState<string | null>(null);
  // Fresh probe evidence gathered this session, keyed by probeKey(provider,model).
  // Layered over the cached models[].probe (GET /lanes echoes the probe cache).
  const [probes, setProbes] = useState<Record<string, ModelProbeResult>>({});
  const [probing, setProbing] = useState<Record<string, boolean>>({});
  const [probeErrors, setProbeErrors] = useState<Record<string, string>>({});
  const [batchRunning, setBatchRunning] = useState(false);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [catalogTruncated, setCatalogTruncated] = useState(false);
  const [benchRunning, setBenchRunning] = useState(false);
  const [benchError, setBenchError] = useState<string | null>(null);
  const [benchResults, setBenchResults] = useState<ModelProbeResult[]>([]);
  const [jumpProfile, setJumpProfile] = useState<string | null>(null);
  const isCompact = useCompact();
  const [drawerOpen, setDrawerOpen] = useState(false);

  // Rows re-sync against fresh lane data WITHOUT a remount (the old
  // key={id:updated_at} remount killed subtab/drawer/bench state on every
  // reload). A reload while the operator has staged edits keeps the staging —
  // the rows are the source of truth until Speichern/Verwerfen.
  const laneStamp = lane.updated_at;
  const stampRef = useRef(laneStamp);
  useEffect(() => {
    if (laneStamp === stampRef.current) return;
    stampRef.current = laneStamp;
    if (!dirtyRef.current) {
      setRows(editorRows(lane, data.profiles, models));
    }
  }, [laneStamp, lane, data.profiles, models]);

  const health = useMemo(() => laneHealthSummary(lane, data.profiles), [lane, data.profiles]);
  const spawnHealthMap = useMemo<Record<string, LaneSpawnHealth | null>>(
    () => Object.fromEntries(health.rows.map((r) => [r.profile, r.health])),
    [health],
  );
  const rollup = useMemo(() => probeRollup(models, probes), [models, probes]);

  const updateRow = useCallback((profile: string, patch: Partial<EditorRow>) => {
    setRows((prev) => prev.map((row) => (row.profile === profile ? { ...row, ...patch, touched: true } : row)));
    setDirty(true);
    dirtyRef.current = true;
    setSaveError(null);
  }, []);

  const handleSave = useCallback(async () => {
    setSaveError(null);
    try {
      await onSave(rows);
      setDirty(false);
      dirtyRef.current = false;
    } catch (e) {
      setSaveError(e instanceof Error ? e.message : String(e));
    }
  }, [onSave, rows]);

  const handleDiscard = useCallback(() => {
    setRows(editorRows(lane, data.profiles, models));
    setDirty(false);
    dirtyRef.current = false;
    setSaveError(null);
  }, [lane, data.profiles, models]);

  const handleProbeRow = useCallback(async (row: EditorRow) => {
    const modelId = row.model ?? row.defaultModel ?? null;
    if (!modelId) return;
    if (row.worker_runtime === "claude-cli") return;
    const provider = row.provider ?? row.defaultProvider ?? "";
    setProbing((prev) => ({ ...prev, [row.profile]: true }));
    setProbeErrors((prev) => {
      const next = { ...prev };
      delete next[row.profile];
      return next;
    });
    try {
      const result = await runModelProbe({ provider, model: modelId, profile: row.profile, timeoutSeconds: 45 });
      setProbes((prev) => ({ ...prev, [probeKey(provider, modelId)]: result }));
    } catch (e) {
      // Inline statt still: die Zeile zeigt „Probe fehlgeschlagen" + Grund.
      setProbeErrors((prev) => ({
        ...prev,
        [row.profile]: e instanceof Error ? e.message : String(e),
      }));
    } finally {
      setProbing((prev) => ({ ...prev, [row.profile]: false }));
    }
  }, []);

  const handleCatalogProbe = useCallback(async () => {
    const targets = filterSinnvoll(models)
      .filter((m) => m.runtime === "hermes")
      .map((m) => ({ provider: m.provider ?? "", model: m.id }));
    if (targets.length === 0) return;
    setBatchRunning(true);
    setCatalogError(null);
    try {
      const { results, truncated } = await runCatalogProbe({ models: targets, profile: null, timeoutSeconds: 45, limit: 8 });
      setCatalogTruncated(truncated);
      setProbes((prev) => {
        const next = { ...prev };
        for (const result of results) next[probeKey(result.provider, result.model)] = result;
        return next;
      });
      // The backend caches each probe; reload so GET /lanes echoes the cache.
      await onReload();
    } catch (e) {
      setCatalogError(e instanceof Error ? e.message : String(e));
    } finally {
      setBatchRunning(false);
    }
  }, [models, onReload]);

  // Compass „Übernehmen": stage the picked model into the role's matrix row
  // (the operator still confirms via SaveBar — adopt stages, persist commits).
  const handleAdopt = useCallback(
    (role: CompassRole, model: LaneModelOption) => {
      const row = rows.find((r) => r.profile === role);
      if (row?.locked) return;
      if (!row) return;
      updateRow(role, applyChoice(row, choiceForModel(model), models));
    },
    [rows, models, updateRow],
  );

  const handleBench = useCallback(async (selected: LaneModelOption[]) => {
    const probeable = selected.filter((model) => model.runtime === "hermes");
    if (probeable.length < 2) {
      setBenchError("Bench benötigt mindestens zwei Hermes-Modelle.");
      return;
    }
    setBenchRunning(true);
    setBenchError(null);
    try {
      const { results } = await runCatalogProbe({
        models: probeable.map((m) => ({ provider: m.provider ?? "", model: m.id })),
        profile: null,
        timeoutSeconds: 45,
        limit: probeable.length,
      });
      setBenchResults(results);
      setProbes((prev) => {
        const next = { ...prev };
        for (const result of results) next[probeKey(result.provider, result.model)] = result;
        return next;
      });
    } catch (e) {
      // Inline: die vorherige Vergleichstabelle bleibt stehen.
      setBenchError(e instanceof Error ? e.message : String(e));
    } finally {
      setBenchRunning(false);
    }
  }, []);

  const matrixFocus = jumpProfile ?? focusProfile ?? null;

  const rightPanel =
    subtab === "rauch" ? (
      <SmokePanel
        models={models}
        probes={probes}
        busy={busy}
        batchRunning={batchRunning}
        catalogError={catalogError}
        truncated={catalogTruncated}
        onCatalogProbe={() => void handleCatalogProbe()}
      />
    ) : subtab === "kompass" ? (
      <Compass
        models={models}
        rows={rows}
        probes={probes}
        busy={busy}
        benchRunning={benchRunning}
        benchError={benchError}
        benchResults={benchResults}
        onAdopt={handleAdopt}
        onBench={(selected) => void handleBench(selected)}
      />
    ) : (
      <LaneDiff
        lanes={data.lanes}
        catalog={data.profiles}
        models={models}
        probes={probes}
        initialA={data.active_id ?? data.lanes[0]?.id ?? null}
        initialB={lane.id !== data.active_id ? lane.id : (data.lanes.find((l) => l.id !== data.active_id)?.id ?? null)}
      />
    );

  return (
    <div className="space-y-4">
      {/* Gesundheits-Strip: passive Spawn-Evidenz + Probe-Rollup, mit Sprung in
          die betroffene Matrix-Zeile. */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 rounded-card border border-line bg-surface-1 px-3 py-2">
        <span className="font-display text-micro font-semibold uppercase tracking-[0.12em] text-ink-3">
          {t.healthEyebrow}
        </span>
        <SignalLabel
          tone={rollup.blocked > 0 ? "warn" : "ok"}
          label={`${t.erreichbar} ${t.of(rollup.reachable, rollup.total)}`}
        />
        {rollup.blocked > 0 ? <SignalChip tone="alert" label={`${rollup.blocked} ${t.blockiert}`} /> : null}
        {health.unhealthy > 0 ? (
          <span className="flex flex-wrap items-center gap-1.5">
            <SignalChip tone="alert" label={t.unhealthy(health.unhealthy)} />
            {health.rows
              .filter((r) => r.health?.status === "unhealthy")
              .map((r) => (
                <button
                  key={r.profile}
                  type="button"
                  title={r.health?.reason ?? t.jumpToRow}
                  onClick={() => setJumpProfile(r.profile)}
                  className="min-h-11 rounded-card border border-line px-2 font-data text-micro text-ink-2 transition-colors duration-150 hover:border-live hover:text-live min-[52rem]:min-h-8"
                >
                  {r.profile} →
                </button>
              ))}
          </span>
        ) : health.unknown === health.rows.length ? (
          <SignalLabel tone="neutral" label={t.spawnUnknown} />
        ) : (
          <SignalLabel tone="ok" label={t.spawnOk} />
        )}
      </div>

      <div className="lp-main">
        {/* Mobile: the lane rail pins to the top (`.lp-lanebar`, lanes.css);
            from the 3-zone cut it docks left as the first grid column. */}
        <div className="lp-lanebar">
          <LaneBar
            lanes={data.lanes}
            activeId={data.active_id}
            selectedId={lane.id}
            profileCount={data.profiles.length}
            busy={busy}
            onSelect={onSelect}
            onActivate={onActivate}
            onCreate={onCreate}
            onRename={onRename}
            onDuplicate={onDuplicate}
            onDelete={onDelete}
          />
        </div>

        <ProfileMatrix
          rows={rows}
          models={models}
          busy={busy}
          dirty={dirty}
          probing={probing}
          probes={probes}
          probeErrors={probeErrors}
          spawnHealth={spawnHealthMap}
          savedAt={savedAt}
          density={density}
          focusProfile={matrixFocus}
          onModelChange={(profile, choice) =>
            updateRow(profile, applyChoice(rows.find((r) => r.profile === profile)!, choice, models))
          }
          onReasoningChange={(profile, value) => updateRow(profile, { reasoning: value })}
          onFastChange={(profile, value) => updateRow(profile, { fastMode: value })}
          onFallbackChange={(profile, fallbackProviders: LaneFallbackProvider[]) =>
            updateRow(profile, { fallbackProviders })
          }
          onProbeRow={(row) => void handleProbeRow(row)}
          onSave={() => void handleSave()}
          onDiscard={handleDiscard}
          saveError={saveError}
        />

        {isCompact ? (
          <div className="grid grid-cols-3 gap-2">
            {RIGHT_TABS.map((tab) => {
              const badge = tab.id === "rauch" && rollup.blocked > 0 ? rollup.blocked : 0;
              const peek =
                tab.id === "rauch"
                  ? `${t.erreichbar} ${t.of(rollup.reachable, rollup.total)}`
                  : tab.id === "kompass"
                    ? t.of(filterSinnvoll(models).length, models.length)
                    : null;
              return (
                <button
                  key={tab.id}
                  type="button"
                  onClick={() => {
                    setSubtab(tab.id);
                    setDrawerOpen(true);
                  }}
                  className="flex min-h-12 flex-col items-center justify-center gap-0.5 rounded-card border border-line bg-surface-1 px-2 text-sec font-medium text-ink-2 transition-colors duration-150 hover:border-live hover:text-live"
                >
                  <span className="flex items-center gap-1.5">
                    {tab.label}
                    {badge > 0 ? <SignalChip tone="warn" label={`${badge} ${t.blockiert}`} /> : null}
                  </span>
                  {peek ? <span className="font-data text-micro tabular-nums text-ink-3">{peek}</span> : null}
                </button>
              );
            })}
          </div>
        ) : (
          /* Evidenz-Dock: gerahmtes Panel mit eigener Sektions-Kante. */
          <section aria-label={t.probeFeed} className="min-w-0 rounded-panel border border-line bg-surface-1 p-3">
            <SubtabChips
              items={RIGHT_TABS.map((tab) => ({
                ...tab,
                warn: tab.id === "rauch" && rollup.blocked > 0,
              }))}
              active={subtab}
              onSelect={setSubtab}
              ariaLabelPrefix="Bereich"
            />
            <div className="mt-3">{rightPanel}</div>
          </section>
        )}
      </div>

      {isCompact && drawerOpen ? (
        <DrawerShell
          eyebrow={t.title}
          title={RIGHT_TABS.find((tab) => tab.id === subtab)?.label ?? t.rauch}
          onClose={() => setDrawerOpen(false)}
          ariaLabel={RIGHT_TABS.find((tab) => tab.id === subtab)?.label ?? t.rauch}
          widthClassName="tab:w-[min(440px,92vw)]"
        >
          <SubtabChips
            items={RIGHT_TABS.map((tab) => ({
              ...tab,
              warn: tab.id === "rauch" && rollup.blocked > 0,
            }))}
            active={subtab}
            onSelect={setSubtab}
            ariaLabelPrefix="Bereich"
            className="mb-3"
          />
          {/* lp-cq: @container context for the drawer body (lanes.css) — KPI
              grids collapse cleanly on narrow sheets. */}
          <div className="lp-cq">{rightPanel}</div>
        </DrawerShell>
      ) : null}
    </div>
  );
}

export function LanesView({ density }: { density?: Density }) {
  const [data, setData] = useState<LanesResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [failCount, setFailCount] = useState(0);
  const [savedAt, setSavedAt] = useState<number | null>(null);
  const [loadedAt, setLoadedAt] = useState<number | null>(null);
  const [focusProfile, setFocusProfile] = useState<string | null>(null);
  const deepLinkRef = useRef(false);

  const reload = useCallback(async () => {
    try {
      setData(await loadLanes());
      setError(null);
      setFailCount(0);
      setLoadedAt(Date.now());
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

  // „Gespeichert ✓" flash window in the SaveBar.
  useEffect(() => {
    if (savedAt === null) return;
    const timer = setTimeout(() => setSavedAt(null), 4000);
    return () => clearTimeout(timer);
  }, [savedAt]);

  const run = useCallback(
    async (op: () => Promise<unknown>) => {
      setBusy(true);
      try {
        await op();
        await reload();
      } catch (e) {
        // Do NOT reload here: a reload would discard the operator's staged rows
        // (the rows-reset effect fires on the fresh data). Keep the edits + the
        // top-banner error so the action is retryable.
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setBusy(false);
      }
    },
    [reload],
  );

  const lanes = data?.lanes ?? [];
  // Ansehen/Bearbeiten ≠ Aktivieren: the selected lane opens in the matrix;
  // only the explicit „Aktivieren" CTA (or a save) flips the live lane.
  const lane = lanes.find((l) => l.id === selectedId) ?? lanes.find((l) => l.active) ?? lanes[0] ?? null;

  // Deep-links ?lane=<id|name> / ?profile=<name> (Fleet one-shot idiom): wait
  // for the catalog, apply once, strip the params. Reads window.location
  // directly (no router dependency — the render tests mount the view bare).
  useEffect(() => {
    if (deepLinkRef.current || data === null || typeof window === "undefined") return;
    const params = new URLSearchParams(window.location.search);
    const laneParam = params.get("lane");
    const profileParam = params.get("profile");
    if (!laneParam && !profileParam) return;
    deepLinkRef.current = true;
    params.delete("lane");
    params.delete("profile");
    const qs = params.toString();
    window.history.replaceState(
      null,
      "",
      `${window.location.pathname}${qs ? `?${qs}` : ""}${window.location.hash}`,
    );
    if (laneParam) {
      const found = data.lanes.find((l) => l.id === laneParam || l.name === laneParam);
      if (found) setSelectedId(found.id);
    }
    if (profileParam) setFocusProfile(profileParam);
  }, [data]);

  // Save path: rethrows so the matrix SaveBar shows saveError inline (the old
  // run() swallowed every failure into the top banner — saveError stayed dead).
  const handleSave = useCallback(
    async (rows: EditorRow[]) => {
      const target = lane;
      if (!target) return;
      setBusy(true);
      try {
        if (!target.active) await activateLane(target.id);
        const payload = persistPayloadFromEditorRows(rows);
        const removed_profiles = removedProfilesFromEditorRows(rows);
        if (Object.keys(payload).length > 0 || removed_profiles.length > 0) {
          const result = await persistLaneModels(payload, removed_profiles);
          if (result.failed.length > 0) {
            throw new Error(
              `Speichern fehlgeschlagen: ${result.failed.map((f) => `${f.profile} (${f.error})`).join(", ")}`,
            );
          }
        }
        await reload();
        setSavedAt(Date.now());
      } finally {
        setBusy(false);
      }
    },
    [lane, reload],
  );

  const models = useMemo(
    () => (data?.models && data.models.length > 0 ? data.models : FALLBACK_MODELS),
    [data],
  );

  // Freshness tick: re-evaluates „veraltet" without an impure Date.now() in
  // render (react-compiler lint). 30s cadence is enough for a 120s threshold.
  const [now, setNow] = useState(0);
  useEffect(() => {
    const tick = () => setNow(Date.now());
    tick();
    const interval = setInterval(tick, 30_000);
    return () => clearInterval(interval);
  }, []);
  const stale = loadedAt !== null && now > 0 && now - loadedAt > 120_000;

  return (
    <section aria-label={t.title} className="lp space-y-4">
      {/* View-Header: Titel + Datenfrische (Stand / veraltet) + manueller Reload. */}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="font-display text-micro font-semibold uppercase tracking-[0.12em] text-ink-3">
          {t.title}
        </h1>
        <div className="flex items-center gap-2">
          {loadedAt !== null ? (
            <span className="font-data text-micro tabular-nums text-ink-3">
              {t.stand(new Date(loadedAt).toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" }))}
            </span>
          ) : null}
          {stale ? <SignalChip tone="warn" label={t.stale} /> : null}
          <button
            type="button"
            onClick={() => void reload()}
            disabled={busy}
            aria-label={t.reload}
            className="inline-flex min-h-11 items-center gap-1.5 rounded-card border border-line px-2.5 text-micro text-ink-2 transition-colors duration-150 hover:border-live hover:text-live disabled:opacity-40"
          >
            <RefreshCw className="h-3.5 w-3.5" aria-hidden />
            {t.reload}
          </button>
        </div>
      </div>

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

      {data === null && !error ? (
        <LanesSkeleton />
      ) : data === null ? null : lane === null ? (
        <div className="space-y-3">
          <FleetEmptyState title={t.emptyLanesTitle} desc={t.emptyLanesDesc} />
          <button
            type="button"
            onClick={() => void reload()}
            disabled={busy}
            className="min-h-11 rounded-card border border-line px-3 text-sec text-ink-2 transition-colors duration-150 hover:border-live hover:text-live disabled:opacity-40"
          >
            {t.reload}
          </button>
        </div>
      ) : (
        <LanesPlatform
          key={lane.id}
          data={data}
          lane={lane}
          busy={busy}
          savedAt={savedAt}
          density={density}
          focusProfile={focusProfile}
          onSelect={setSelectedId}
          onActivate={(laneId) => void run(() => activateLane(laneId))}
          onCreate={(name, preset) =>
            void run(async () => {
              const profiles = preset ? buildPresetDraft(preset, models, data.profiles) : {};
              const res = await createLane(name, profiles);
              setSelectedId(res.lane.id);
            })
          }
          onRename={(laneId, name) => void run(() => updateLane(laneId, { name }))}
          onDuplicate={(laneId) =>
            void run(async () => {
              const source = lanes.find((l) => l.id === laneId);
              if (!source) return;
              const res = await createLane(`${source.name} Kopie`, source.profiles);
              setSelectedId(res.lane.id);
            })
          }
          onDelete={(laneId) =>
            void run(async () => {
              await deleteLane(laneId);
              if (selectedId === laneId) setSelectedId(null);
            })
          }
          onSave={handleSave}
          onReload={reload}
        />
      )}
    </section>
  );
}
