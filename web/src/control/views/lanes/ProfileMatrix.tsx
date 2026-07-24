import { useState } from "react";
import { Plus, X, Zap } from "lucide-react";
import { cn } from "@/lib/utils";
import { DrawerShell, SignalChip, SignalLabel } from "../../components/leitstand";
import {
  probeKey,
  type EditorRow,
  type LaneFallbackProvider,
  type LaneModelOption,
  type ModelProbeResult,
} from "./api";
import { ModelSelect } from "./ModelSelect";
import { ReasoningControl } from "./ReasoningControl";
import { providerDot } from "./providerColors";
import { PROBE_STATUS_LABEL, probeTone, t } from "./strings";

// ProfileMatrix — one row per catalog profile: role identity (provider dot +
// name + description), filtered ModelSelect, Reasoning segment, fallback count
// (+ drawer editor), probe latency/LED + per-row Blitz, and an override badge
// (Lane ≠ Profil-Default). SaveBar persists + activates; the hint reminds the
// operator the change takes effect at the next spawn (hot-read, no restart).

// Reasoning column gets a fixed floor so the joined segment strip never wraps;
// role/model split the remaining flex. Fallback/probe/override stay narrow.
const COLS =
  "min-[52rem]:grid-cols-[minmax(0,1fr)_minmax(0,1.2fr)_minmax(11.5rem,auto)_minmax(0,4.75rem)_minmax(0,8.5rem)_minmax(0,5rem)]";

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

function ProbeCell({
  row,
  models,
  probes,
  probing,
  busy,
  onProbeRow,
}: {
  row: EditorRow;
  models: LaneModelOption[];
  probes: Record<string, ModelProbeResult>;
  probing: boolean;
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
        className="inline-flex size-9 shrink-0 items-center justify-center rounded-card border border-line text-ink-3 transition-colors duration-150 hover:border-live hover:text-live disabled:opacity-40"
      >
        <Zap className="h-3.5 w-3.5" />
      </button>
    </div>
  );
}

function FallbackDrawer({
  row,
  onClose,
  onCommit,
}: {
  row: EditorRow;
  onClose: () => void;
  onCommit: (profile: string, fallbackProviders: LaneFallbackProvider[]) => void;
}) {
  const [draft, setDraft] = useState<LaneFallbackProvider[]>(() =>
    row.fallbackProviders.map((fb) => ({ ...fb })),
  );
  const update = (index: number, patch: Partial<LaneFallbackProvider>) =>
    setDraft((prev) => prev.map((fb, i) => (i === index ? { ...fb, ...patch } : fb)));

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
        {draft.map((fb, index) => (
          <div key={index} className="grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto] items-center gap-2">
            <input
              type="text"
              value={fb.provider}
              aria-label={`${t.fallbackProvider} ${index + 1}`}
              placeholder={t.fallbackProvider}
              onChange={(e) => update(index, { provider: e.target.value })}
              className="min-h-11 w-full rounded-card border border-line bg-surface-2 px-2 font-data text-micro text-ink placeholder:text-ink-3 focus:border-live focus:outline-none"
            />
            <input
              type="text"
              value={fb.model}
              aria-label={`${t.fallbackModel} ${index + 1}`}
              placeholder={t.fallbackModel}
              onChange={(e) => update(index, { model: e.target.value })}
              className="min-h-11 w-full rounded-card border border-line bg-surface-2 px-2 font-data text-micro text-ink placeholder:text-ink-3 focus:border-live focus:outline-none"
            />
            <button
              type="button"
              aria-label={`${t.fallbackRemove} ${index + 1}`}
              onClick={() => setDraft((prev) => prev.filter((_, i) => i !== index))}
              className="inline-flex size-11 items-center justify-center rounded-card border border-line text-ink-3 hover:border-status-alert hover:text-status-alert"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        ))}
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
  onModelChange,
  onReasoningChange,
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
  onModelChange: (profile: string, choice: string) => void;
  onReasoningChange: (profile: string, value: string | null) => void;
  onFallbackChange: (profile: string, fallbackProviders: LaneFallbackProvider[]) => void;
  onProbeRow: (row: EditorRow) => void;
  onSave: () => void;
  onDiscard: () => void;
  saveError: string | null;
}) {
  const [fallbackRow, setFallbackRow] = useState<string | null>(null);
  const drawerRow = fallbackRow ? rows.find((r) => r.profile === fallbackRow) ?? null : null;

  return (
    <section aria-label={t.matrixEyebrow} className="rounded-panel border border-line bg-surface-1">
      <div className="flex items-baseline justify-between gap-3 border-b border-line px-3 py-2.5">
        <span className="font-display text-micro font-semibold uppercase tracking-[0.12em] text-ink-3">
          {t.matrixEyebrow}
        </span>
        <span className="font-data text-micro tabular-nums text-ink-3">{rows.length}</span>
      </div>

      {/* column headers (desktop) */}
      <div className={cn("hidden gap-3 border-b border-line-soft px-3 py-2 min-[52rem]:grid", COLS)}>
        {[t.colRole, t.colModel, t.colReasoning, t.colFallback, t.colProbe, t.colOverride].map((label) => (
          <span key={label} className="font-display text-micro font-semibold uppercase tracking-[0.1em] text-ink-3">
            {label}
          </span>
        ))}
      </div>

      <ul>
        {rows.map((row) => {
          const override = rowHasOverride(row);
          return (
            <li
              key={row.profile}
              className={cn("grid grid-cols-1 gap-3 border-b border-line-soft px-3 py-2.5 last:border-b-0 min-[52rem]:items-center", COLS)}
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
                </div>
              </div>

              {/* Modell */}
              <ModelSelect row={row} models={models} disabled={busy || row.locked} onChange={(choice) => onModelChange(row.profile, choice)} />

              {/* Reasoning */}
              <div className="min-w-0">
                <ReasoningControl
                  value={row.reasoning ?? null}
                  support={row.reasoningSupport ?? []}
                  disabled={busy || row.locked}
                  ariaLabel={`Reasoning für ${row.profile}`}
                  hint={row.reasoningHint ?? undefined}
                  onChange={(value) => onReasoningChange(row.profile, value)}
                />
                {row.defaultReasoning ? (
                  <div className="mt-1 font-data text-micro text-ink-3">{t.currently(row.defaultReasoning)}</div>
                ) : null}
              </div>

              {/* Fallback */}
              <button
                type="button"
                title={t.fallbackEdit}
                aria-label={`${t.fallbackEdit}: ${row.profile}`}
                disabled={busy || row.locked}
                onClick={() => setFallbackRow(row.profile)}
                className="inline-flex min-h-9 items-center justify-center rounded-card border border-line px-2 font-data text-micro tabular-nums text-ink-2 transition-colors duration-150 hover:border-live hover:text-live disabled:opacity-40"
              >
                {t.fallbacks(row.fallbackProviders.length)}
              </button>

              {/* Probe */}
              <ProbeCell
                row={row}
                models={models}
                probes={probes}
                probing={Boolean(probing[row.profile])}
                busy={busy}
                onProbeRow={onProbeRow}
              />

              {/* Override */}
              <div>
                {override ? (
                  <SignalChip tone="neutral" label={t.override} />
                ) : (
                  <span className="text-micro text-ink-3">{t.standard}</span>
                )}
              </div>
            </li>
          );
        })}
      </ul>

      {/* SaveBar — mobile (<tab/600px): the hint takes its own line (basis-full)
          and the two actions share the next row side-by-side (flex-1), so they
          never stack into two full-width blocks (W5 phone bug: „Speichern und
          Verwerfen komisch dargestellt"). Desktop (tab+): one row, hint pushed
          left (mr-auto), actions right — unchanged. The pb inset reserves the iOS
          home-indicator safe area so the buttons are never clipped (env()=0
          elsewhere → identical to the old py-3). */}
      <div className="sticky bottom-0 flex flex-wrap items-center gap-2 border-t border-line bg-surface-1/95 px-3 pt-3 pb-[calc(0.75rem+env(safe-area-inset-bottom))] tab:flex-nowrap tab:justify-end">
        {saveError ? (
          <p className="text-micro text-status-alert basis-full tab:basis-auto tab:mr-auto">{saveError}</p>
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
        <FallbackDrawer row={drawerRow} onClose={() => setFallbackRow(null)} onCommit={onFallbackChange} />
      ) : null}
    </section>
  );
}
