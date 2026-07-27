import { useEffect, useRef, useState } from "react";
import { Plus, X, Zap } from "lucide-react";
import { cn } from "@/lib/utils";
import { DrawerShell, SignalChip, SignalLabel } from "../../components/leitstand";
import type { Density } from "../../hooks/useDensity";
import {
  modelsForProvider,
  probeKey,
  providerOptions,
  type EditorRow,
  type LaneFallbackProvider,
  type LaneModelOption,
  type LaneSpawnHealth,
  type ModelProbeResult,
} from "./api";
import { ModelSelect } from "./ModelSelect";
import { FastControl } from "./FastControl";
import { ReasoningControl } from "./ReasoningControl";
import { providerDot } from "./providerColors";
import { PROBE_STATUS_LABEL, probeTone, t } from "./strings";

// ProfileMatrix — one row per catalog profile: role identity (provider dot +
// name + spawn-health LED), filtered ModelSelect, Reasoning segment, Fast
// segment, fallback count (+ drawer editor), probe latency/LED + per-row Blitz,
// and an override badge (Lane ≠ Profil-Default). SaveBar persists + activates
// and flashes „Gespeichert ✓" after a successful run. While the matrix panel is
// narrower than 56rem (@container query) rows render as cards (role + model
// prominent, labelled action cluster); from 56rem panel width the same nodes
// join the 7-column grid whose header row is sticky.

// Reasoning column gets a fixed floor so the joined segment strip never wraps;
// role/model split the remaining flex. Fast/Fallback/Probe/Override stay narrow.
const COLS =
  "@min-[56rem]:grid-cols-[minmax(0,1fr)_minmax(0,1.2fr)_minmax(11.5rem,auto)_minmax(0,5rem)_minmax(0,4.75rem)_minmax(0,8.5rem)_minmax(0,5rem)]";

function rowHasOverride(row: EditorRow): boolean {
  return row.choice !== "" || row.provider != null || row.model != null;
}

function rowProbe(
  row: EditorRow,
  models: LaneModelOption[],
  probes: Record<string, ModelProbeResult>,
): ModelProbeResult | null {
  const modelId = row.model ?? row.defaultModel ?? null;
  if (!modelId) return null;
  const provider = row.worker_runtime === "claude-cli" ? "" : row.provider ?? row.defaultProvider ?? "";
  const fresh = probes[probeKey(provider, modelId)];
  if (fresh) return fresh;
  return models.find((m) => m.id === modelId)?.probe ?? null;
}

function spawnHealthTone(health: LaneSpawnHealth): "ok" | "alert" | "neutral" {
  if (health.status === "healthy") return "ok";
  if (health.status === "unhealthy") return "alert";
  return "neutral";
}

function ProbeCell({
  row,
  models,
  probes,
  probing,
  probeError,
  busy,
  onProbeRow,
}: {
  row: EditorRow;
  models: LaneModelOption[];
  probes: Record<string, ModelProbeResult>;
  probing: boolean;
  probeError?: string | null;
  busy: boolean;
  onProbeRow: (row: EditorRow) => void;
}) {
  const probe = rowProbe(row, models, probes);
  const cliOnly = row.worker_runtime === "claude-cli";
  return (
    <div className="flex items-center gap-2">
      <div className="min-w-0 flex-1">
        {probing ? (
          <SignalLabel tone="neutral" label={t.probing} />
        ) : probeError ? (
          <span className="text-micro text-status-alert" title={probeError}>
            {t.probeFehler}
          </span>
        ) : probe ? (
          <div className="min-w-0">
            <SignalLabel tone={probeTone(probe.status)} label={PROBE_STATUS_LABEL[probe.status]} />
            {probe.duration_ms != null && probe.duration_ms > 0 ? (
              <div className="font-data text-micro tabular-nums text-ink-2">{probe.duration_ms} ms</div>
            ) : null}
          </div>
        ) : (
          <span className="text-micro text-ink-3">{t.probeUngeprüft}</span>
        )}
      </div>
      <button
        type="button"
        title={cliOnly ? "nicht probe-bar (claude-cli)" : t.probeMessen}
        aria-label={`${t.probeMessen}: ${row.profile}`}
        disabled={busy || probing || cliOnly || row.locked}
        onClick={() => onProbeRow(row)}
        className="inline-flex size-11 shrink-0 items-center justify-center rounded-card border border-line text-ink-3 transition-colors duration-150 hover:border-live hover:text-live disabled:opacity-40 @min-[56rem]:size-9"
      >
        <Zap className="h-3.5 w-3.5" />
      </button>
    </div>
  );
}

function FallbackDrawer({
  row,
  models,
  onClose,
  onCommit,
}: {
  row: EditorRow;
  models: LaneModelOption[];
  onClose: () => void;
  onCommit: (profile: string, fallbackProviders: LaneFallbackProvider[]) => void;
}) {
  const [draft, setDraft] = useState<LaneFallbackProvider[]>(() =>
    row.fallbackProviders.map((fb) => ({ ...fb })),
  );
  const providers = providerOptions(models);
  const update = (index: number, patch: Partial<LaneFallbackProvider>) =>
    setDraft((prev) => prev.map((fb, i) => (i === index ? { ...fb, ...patch } : fb)));
  const incomplete = draft.some((fb) => !fb.provider || !fb.model);

  return (
    <DrawerShell
      eyebrow={row.profile}
      title={t.fallbackTitle}
      onClose={onClose}
      ariaLabel={t.fallbackTitle}
      footer={
        <button
          type="button"
          onClick={() => {
            const normalizedDraft = draft
              .filter((fb) => fb.provider && fb.model)
              .map((fb) => ({
                provider: fb.provider,
                model: fb.model,
                base_url: fb.base_url ?? null,
              }));
            const normalizedCurrent = row.fallbackProviders
              .filter((fb) => fb.provider && fb.model)
              .map((fb) => ({
                provider: fb.provider,
                model: fb.model,
                base_url: fb.base_url ?? null,
              }));
            if (JSON.stringify(normalizedDraft) !== JSON.stringify(normalizedCurrent)) {
              onCommit(row.profile, draft.filter((fb) => fb.provider && fb.model));
            }
            onClose();
          }}
          className="min-h-12 w-full rounded-card border border-live bg-live/15 text-sec font-medium text-bronze-hi"
        >
          {t.apply}
        </button>
      }
    >
      <div className="space-y-2">
        {draft.length === 0 ? <p className="text-sec text-ink-3">{t.fallbackEmpty}</p> : null}
        {/* Catalog pickers instead of free text: a fallback can only ever name a
            provider+model the curated catalog actually carries (no typos, no
            un-spawnable combinations). 1-column stack on narrow drawers. */}
        {draft.map((fb, index) => {
          const providerModelOptions = modelsForProvider(fb.provider, models);
          return (
            <div key={index} className="grid grid-cols-1 items-center gap-2 min-[24rem]:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto]">
              <select
                value={fb.provider}
                aria-label={`${t.fallbackProvider} ${index + 1}`}
                onChange={(e) => update(index, { provider: e.target.value, model: "" })}
                className="min-h-11 w-full rounded-card border border-line bg-surface-2 px-2 font-data text-micro text-ink focus:border-live focus:outline-none"
              >
                <option value="">{t.fallbackChooseProvider}</option>
                {providers.map((provider) => (
                  <option key={provider.id} value={provider.id}>
                    {provider.label}
                  </option>
                ))}
              </select>
              <select
                value={fb.model}
                aria-label={`${t.fallbackModel} ${index + 1}`}
                disabled={!fb.provider}
                onChange={(e) => update(index, { model: e.target.value })}
                className="min-h-11 w-full rounded-card border border-line bg-surface-2 px-2 font-data text-micro text-ink focus:border-live focus:outline-none disabled:opacity-40"
              >
                <option value="">{t.fallbackChooseModel}</option>
                {providerModelOptions.map((model) => (
                  <option key={model.id} value={model.id}>
                    {model.label}
                  </option>
                ))}
              </select>
              <button
                type="button"
                aria-label={`${t.fallbackRemove} ${index + 1}`}
                onClick={() => setDraft((prev) => prev.filter((_, i) => i !== index))}
                className="inline-flex size-11 items-center justify-center rounded-card border border-line text-ink-3 hover:border-status-alert hover:text-status-alert"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
          );
        })}
        {incomplete ? <p className="text-micro text-status-warn">{t.fallbackIncomplete}</p> : null}
        <button
          type="button"
          onClick={() => setDraft((prev) => [...prev, { provider: "", model: "" }])}
          className="inline-flex min-h-11 items-center gap-1.5 rounded-card border border-line px-2.5 text-micro text-ink-2 transition-colors duration-150 hover:border-live hover:text-live"
        >
          <Plus className="h-3.5 w-3.5" />
          {t.fallbackAdd}
        </button>
      </div>
    </DrawerShell>
  );
}

export function ProfileMatrix({
  rows,
  models,
  busy,
  dirty,
  probing,
  probes,
  probeErrors,
  spawnHealth,
  savedAt,
  density,
  focusProfile,
  onModelChange,
  onReasoningChange,
  onFastChange,
  onFallbackChange,
  onProbeRow,
  onSave,
  onDiscard,
  saveError,
}: {
  rows: EditorRow[];
  models: LaneModelOption[];
  busy: boolean;
  dirty: boolean;
  probing: Record<string, boolean>;
  probes: Record<string, ModelProbeResult>;
  probeErrors?: Record<string, string>;
  spawnHealth?: Record<string, LaneSpawnHealth | null>;
  /** Epoch ms of the last successful save — the SaveBar flashes „Gespeichert ✓". */
  savedAt?: number | null;
  density?: Density;
  /** Deep-linked profile (?profile=): row gets scrolled into view + ring. */
  focusProfile?: string | null;
  onModelChange: (profile: string, choice: string) => void;
  onReasoningChange: (profile: string, value: string | null) => void;
  onFastChange: (profile: string, value: "fast" | null) => void;
  onFallbackChange: (profile: string, fallbackProviders: LaneFallbackProvider[]) => void;
  onProbeRow: (row: EditorRow) => void;
  onSave: () => void;
  onDiscard: () => void;
  saveError: string | null;
}) {
  const [fallbackRow, setFallbackRow] = useState<string | null>(null);
  const drawerRow = fallbackRow ? rows.find((r) => r.profile === fallbackRow) ?? null : null;
  const focusRef = useRef<HTMLLIElement>(null);
  const compact = density === "compact";

  useEffect(() => {
    if (focusProfile) focusRef.current?.scrollIntoView({ block: "center" });
  }, [focusProfile]);

  return (
    <section aria-label={t.matrixEyebrow} className="rounded-panel border border-line bg-surface-1">
      <div className="flex items-baseline justify-between gap-3 border-b border-line px-3 py-2.5">
        <span className="font-display text-micro font-semibold uppercase tracking-[0.12em] text-ink-3">
          {t.matrixEyebrow}
        </span>
        <span className="font-data text-micro tabular-nums text-ink-3">{rows.length}</span>
      </div>

      {/* @container: the card→grid switch MUST key on the matrix panel's own
          width, not the viewport — in the 3-zone workbench (≥1248px) the panel
          is only ~600px wide while the 7-column grid needs ~860px, so a
          viewport breakpoint collapsed the fr tracks to 0 and overlapped the
          role/model/reasoning cells (2026-07-27 visual review). */}
      <div className="@container">
        {/* column headers (desktop, sticky while the long matrix scrolls) */}
        <div className={cn("sticky top-0 z-10 hidden gap-3 border-b border-line-soft bg-surface-1 px-3 py-2 @min-[56rem]:grid", COLS)}>
          {[t.colRole, t.colModel, t.colReasoning, t.colFast, t.colFallback, t.colProbe, t.colOverride].map((label) => (
            <span key={label} className="font-display text-micro font-semibold uppercase tracking-[0.1em] text-ink-3">
              {label}
            </span>
          ))}
        </div>

        <ul>
        {rows.map((row) => {
          const override = rowHasOverride(row);
          const health = spawnHealth?.[row.profile] ?? null;
          const focused = focusProfile === row.profile;
          return (
            <li
              key={row.profile}
              ref={focused ? focusRef : undefined}
              data-profile={row.profile}
              className={cn(
                "grid grid-cols-1 gap-3 border-b border-line-soft px-3 last:border-b-0 @min-[56rem]:items-center",
                compact ? "py-1.5" : "py-2.5",
                COLS,
                focused && "bg-surface-2 shadow-[inset_3px_0_0_var(--color-live)]",
              )}
            >
              {/* Rolle */}
              <div className="flex min-w-0 items-start gap-2">
                <span
                  className={cn("pdot mt-1.5", providerDot(row.defaultProvider, row.model ?? row.defaultModel))}
                  aria-hidden
                />
                <div className="min-w-0">
                  <div className="truncate text-sec font-medium text-ink" title={row.description}>
                    {row.profile}
                  </div>
                  {row.description ? (
                    <div className="line-clamp-2 text-micro text-ink-3">{row.description}</div>
                  ) : null}
                  {health ? (
                    <div className="mt-0.5" title={health.reason ?? undefined}>
                      <SignalLabel tone={spawnHealthTone(health)} label={t.spawnHealth(health.status)} />
                    </div>
                  ) : null}
                </div>
              </div>

              {/* Modell */}
              <ModelSelect row={row} models={models} disabled={busy || row.locked} onChange={(choice) => onModelChange(row.profile, choice)} />

              {/* Narrow panel (<56rem container): Reasoning + Fast + Fallback +
                  Probe + Override share one compact WRAPPING action row beneath
                  the full-width name + model select — every cluster item carries
                  a visible micro label (no naked icon/number controls). Wide
                  panel (≥56rem): the wrapper is `display:contents`, so these
                  five rejoin the parent 7-column grid as columns 3–7 and the
                  labels hide. */}
              <div className="flex flex-wrap items-end gap-x-4 gap-y-2 @min-[56rem]:contents">
                {/* Reasoning — S1 selective unlock: claude-cli rows stay LOCKED for
                    model/fallback/probe, but their Reasoning segment is genuinely
                    active (it persists `claude_effort` → `--effort` at spawn). The
                    unlock is keyed EXPLICITLY on worker_runtime === "claude-cli" —
                    it does NOT generalize to any other locked-row type: a locked
                    hermes row keeps its Reasoning disabled (operator-approved scope). */}
                <div
                  className="min-w-0"
                  title={row.defaultReasoning ? t.currently(row.defaultReasoning) : undefined}
                >
                  <span className="mb-1 block text-micro uppercase tracking-wide text-ink-3 @min-[56rem]:hidden">
                    {t.colReasoning}
                  </span>
                  <ReasoningControl
                    value={row.reasoning ?? null}
                    support={row.reasoningSupport ?? []}
                    disabled={busy || (row.locked && row.worker_runtime !== "claude-cli")}
                    ariaLabel={`Reasoning für ${row.profile}`}
                    hint={row.reasoningHint ?? undefined}
                    onChange={(value) => onReasoningChange(row.profile, value)}
                  />
                </div>

                {/* Fast — same selective unlock as Reasoning: a claude-cli row's
                    toggle is genuinely active (persists the `claude_fast_mode`
                    spawn bool); unsupported models render disabled + hint. */}
                <div
                  className="min-w-0"
                  title={row.defaultFastMode === "fast" ? t.currently(t.fastFast) : undefined}
                >
                  <span className="mb-1 block text-micro uppercase tracking-wide text-ink-3 @min-[56rem]:hidden">
                    {t.colFast}
                  </span>
                  <FastControl
                    value={row.fastMode ?? null}
                    supported={row.fastSupport ?? false}
                    disabled={busy || (row.locked && row.worker_runtime !== "claude-cli")}
                    ariaLabel={`Fast Mode für ${row.profile}`}
                    hint={row.fastHint ?? undefined}
                    onChange={(value) => onFastChange(row.profile, value)}
                  />
                </div>

                {/* Fallback — 44px touch target on mobile (min-h-11), the denser
                    36px (min-h-9) on desktop. Labelled (Fallback N), never a
                    naked count. */}
                <div className="min-w-0">
                  <span className="mb-1 block text-micro uppercase tracking-wide text-ink-3 @min-[56rem]:hidden">
                    {t.colFallback}
                  </span>
                  <button
                    type="button"
                    title={t.fallbackEdit}
                    aria-label={`${t.fallbackEdit}: ${row.profile}`}
                    disabled={busy || row.locked}
                    onClick={() => setFallbackRow(row.profile)}
                    className="inline-flex min-h-11 items-center justify-center rounded-card border border-line px-2 font-data text-micro tabular-nums text-ink-2 transition-colors duration-150 hover:border-live hover:text-live disabled:opacity-40 @min-[56rem]:min-h-9"
                  >
                    {t.fallbacks(row.fallbackProviders.length)}
                  </button>
                </div>

                {/* Probe */}
                <div className="min-w-0">
                  <span className="mb-1 block text-micro uppercase tracking-wide text-ink-3 @min-[56rem]:hidden">
                    {t.colProbe}
                  </span>
                  <ProbeCell
                    row={row}
                    models={models}
                    probes={probes}
                    probing={Boolean(probing[row.profile])}
                    probeError={probeErrors?.[row.profile] ?? null}
                    busy={busy}
                    onProbeRow={onProbeRow}
                  />
                </div>

                {/* Override */}
                <div className="min-w-0">
                  <span className="mb-1 block text-micro uppercase tracking-wide text-ink-3 @min-[56rem]:hidden">
                    {t.colOverride}
                  </span>
                  {override ? (
                    <SignalChip tone="neutral" label={t.override} />
                  ) : (
                    <span className="text-micro text-ink-3">{t.standard}</span>
                  )}
                </div>
              </div>
            </li>
          );
        })}
        </ul>
      </div>

      {/* SaveBar — sticky bottom, backdrop-blur over the scrolling matrix.
          After a successful save the hint flips to a transient „Gespeichert ✓"
          (the parent nulls savedAt after the flash window). Mobile (<tab/600px):
          the hint takes its own line (basis-full) and the two actions share the
          next row side-by-side (flex-1); desktop (tab+): one row, hint left,
          actions right. The pb inset reserves the iOS home-indicator safe area.
          lp-savebar: below `tab` the fixed BottomBar would cover a bottom-0
          sticky bar — lanes.css lifts it above the nav. */}
      <div className="lp-savebar sticky bottom-0 flex flex-wrap items-center gap-2 border-t border-line bg-surface-1/95 px-3 pt-3 pb-[calc(0.75rem+env(safe-area-inset-bottom))] backdrop-blur tab:flex-nowrap tab:justify-end">
        {saveError ? (
          <p className="text-micro text-status-alert basis-full tab:basis-auto tab:mr-auto">{saveError}</p>
        ) : savedAt ? (
          <span className="basis-full tab:basis-auto tab:mr-auto">
            <SignalChip tone="ok" label={t.savedOk} />
          </span>
        ) : (
          <span className="text-micro text-ink-3 basis-full tab:basis-auto tab:mr-auto">{t.saveHint}</span>
        )}
        <button
          type="button"
          disabled={busy || !dirty}
          onClick={onDiscard}
          className="min-h-12 flex-1 rounded-card border border-line px-3 text-sec text-ink-2 transition-colors duration-150 hover:text-ink disabled:opacity-40 tab:flex-none"
        >
          {t.discard}
        </button>
        <button
          type="button"
          disabled={busy || !dirty}
          onClick={onSave}
          className="min-h-12 flex-1 rounded-card border border-live bg-live px-3 text-sec font-semibold text-surface-0 transition-colors duration-150 hover:bg-bronze-hi disabled:cursor-not-allowed disabled:opacity-40 tab:flex-none"
        >
          {busy ? t.saving : t.save}
        </button>
      </div>

      {drawerRow ? (
        <FallbackDrawer row={drawerRow} models={models} onClose={() => setFallbackRow(null)} onCommit={onFallbackChange} />
      ) : null}
    </section>
  );
}
