import { useCallback, useEffect, useMemo, useState } from "react";
import { Activity, ArrowDown, ArrowUp, Check, ClipboardCheck, Lock, Plus, Trash2, X } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { Led, StatusPill, ToneCallout } from "../components/atoms";
import { FleetEmptyState, FleetPanel } from "../components/fleet/atoms";
import type { Density } from "../hooks/useDensity";
import type { DotKind } from "../lib/tones";
import {
  activateLane,
  createLane,
  deleteLane,
  choiceFromEntry,
  editorRows,
  entryFromChoice,
  importOpenRouterModels,
  laneEntryWarnings,
  modelLabel,
  persistLaneModels,
  laneProfileSpawnHealth,
  loadLanes,
  modelsForProvider,
  providerLabel,
  providerOptions,
  profilesFromEditorRows,
  smokeCheckLaneConfig,
  updateLane,
  FALLBACK_MODELS,
  type EditorRow,
  type LaneFallbackProvider,
  type Lane,
  type LaneModelOption,
  type OpenRouterModelImportResult,
  type OpenRouterModelImportStatus,
  type LaneSpawnCheckResult,
  type LaneSpawnHealth,
  type LaneSpawnHealthStatus,
  type LanesResponse,
} from "./lanes/api";

// Lane strings live here (not in i18n/de.ts) so this feature touches no
// shared files a parallel session may be editing.
const t = {
  title: "Lanes",
  intro:
    "Welches KI-Modell arbeitet in welcher Rolle? Preset oben wählen, pro Rolle das Modell anpassen, dann übernehmen — gilt ab dem nächsten Worker-Start.",
  presetLabel: "Preset",
  apply: "Übernehmen",
  applied: "Aktiv",
  activeSuffix: "(aktiv)",
  dirtyHint: "Änderungen noch nicht übernommen",
  profilesPanel: "Rollen & Modelle",
  presetsPanel: "Presets",
  standardOption: (model: string) => `Standard (${model})`,
  builtin: "Mitgeliefert",
  active: "Aktiv",
  remove: "Löschen",
  confirmDelete: (name: string) => `Preset „${name}" wirklich löschen?`,
  confirmYes: "Bestätigen",
  confirmNo: "Abbrechen",
  newPresetPlaceholder: "Name für neues Preset",
  saveAsPreset: "Auswahl als Preset speichern",
  emptyTitle: "Keine Presets",
  emptyDesc: "Beim ersten Laden werden api-standard und max-abo angelegt.",
  loading: "Lade Modelle …",
  retry: "Erneut versuchen",
  workerCheck: "Worker-Check",
  workerCheckRunning: "Prüfe …",
  smokeOk: "Worker-Check ok",
  smokeWarn: "Worker-Check warnt",
  smokeError: "Worker-Check Fehler",
  smokeUnavailable: "Worker-Check nicht verfügbar",
  overrideChip: "Override",
  defaultChip: "Standard",
  profileDefault: "Profil-Default",
  laneOverride: "Lane-Override",
  taskOverride: "Task-Override",
  lockBadge: "Claude-CLI — später",
  fallbackMissing: "Fallback fehlt",
  smokePending: "Smoke ausstehend",
  primaryLabel: "Primary",
  fallbackLabel: "Fallbacks",
  addFallback: "Fallback hinzufügen",
  safeFallback: "Sicheren Fallback hinzufügen",
  saveLane: "Als Lane speichern",
  configPreview: "Dauerhaft setzen (Preview)",
  wouldChange: "würde ändern",
  readinessTitle: (state: string) => `Spawn-Bereitschaft: ${state}`,
  readinessReady: "bereit",
  readinessChecking: "wird geprüft",
  readinessWarn: "gestört",
  readinessError: "Check-Fehler",
  readinessUnknown: "ungeprüft",
  readySummary: (ready: number, total: number) => `${ready}/${total} bereit`,
  overrideSummary: (n: number) => (n === 1 ? "1 Override" : `${n} Overrides`),
  openRouterImport: "OpenRouter-IDs",
  openRouterPlaceholder: "anthropic/claude-sonnet-4.6\nmoonshotai/kimi-k2.7",
  openRouterImportRun: "Smoken & aufnehmen",
  openRouterImportRunning: "Smoke läuft …",
  openRouterImported: (n: number) => (n === 1 ? "1 neu" : `${n} neu`),
  warningsHint: (n: number) => (n === 1 ? "1 Hinweis" : `${n} Hinweise`),
  warningsExpand: "Hinweise anzeigen",
  warningsCollapse: "Hinweise ausblenden",
  modelsTitle: "Modelle pro Rolle",
  standard: "Standard",
  advanced: "Erweitert",
  permanent: "Dauerhaft",
  permanentModel: (model: string) => `Dauerhaft: ${model}`,
  savePermanently: "Dauerhaft speichern",
  divergenceHint: (model: string) => `läuft aktuell auf ${model}`,
};

// Kurze, nicht-technische Rollen-Hinweise. Fallback: kein Hinweis.
const ROLE_HINTS: Record<string, string> = {
  coder: "Schreibt Code",
  "coder-claude": "Schreibt Code (Claude)",
  reviewer: "Prüft Code",
  critic: "Zweitmeinung",
  research: "Recherche",
  verifier: "Testet & verifiziert",
  admin: "Server-Verwaltung",
  premium: "Schwere Spezialfälle",
  "family-ui": "Family-App Design",
  "fo-brain": "Family-App Planung",
};

const CONTROL_CLASS =
  "min-h-11 w-full rounded-md border border-[var(--hc-border)] bg-black/25 px-2 py-1.5 text-base text-white sm:min-h-9 sm:text-sm";

function groupedModelOptions(models: LaneModelOption[]) {
  const groups = new Map<string, LaneModelOption[]>();
  for (const model of models) {
    const key = model.group || model.provider || "API-Modelle";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key)!.push(model);
  }
  return Array.from(groups.entries()).sort(([a], [b]) => a.localeCompare(b));
}

function SimpleModelSelect({
  value,
  defaultLabel,
  models,
  disabled,
  label,
  onChange,
}: {
  value: string;
  defaultLabel: string;
  models: LaneModelOption[];
  disabled: boolean;
  label: string;
  onChange: (choice: string) => void;
}) {
  return (
    <select
      value={value}
      aria-label={label}
      disabled={disabled}
      onChange={(e) => onChange(e.target.value)}
      className={CONTROL_CLASS}
    >
      <option value="">{t.standardOption(defaultLabel)}</option>
      {groupedModelOptions(models).map(([group, items]) => (
        <optgroup key={group} label={group}>
          {items.map((model) => (
            <option key={model.id} value={choiceFromEntry({ worker_runtime: model.runtime, model: model.id })}>
              {model.label}
            </option>
          ))}
        </optgroup>
      ))}
    </select>
  );
}

function ProviderSelect({
  value,
  models,
  disabled,
  label,
  onChange,
}: {
  value: string | null;
  models: LaneModelOption[];
  disabled: boolean;
  label: string;
  onChange: (provider: string | null) => void;
}) {
  const providers = providerOptions(models);
  const known = providers.some((p) => p.id === value);
  return (
    <select
      value={value ?? ""}
      aria-label={label}
      disabled={disabled}
      onChange={(e) => onChange(e.target.value || null)}
      className={CONTROL_CLASS}
    >
      <option value="">{t.profileDefault}</option>
      {providers.map((provider) => (
        <option key={provider.id} value={provider.id}>
          {provider.label}
        </option>
      ))}
      {value && !known ? <option value={value}>{value}</option> : null}
    </select>
  );
}

function ModelSelect({
  provider,
  value,
  models,
  disabled,
  label,
  defaultLabel,
  onChange,
}: {
  provider: string | null;
  value: string | null;
  models: LaneModelOption[];
  disabled: boolean;
  label: string;
  defaultLabel: string;
  onChange: (model: string | null) => void;
}) {
  const options = modelsForProvider(provider, models);
  const listId = `lane-model-${label.replace(/[^a-z0-9]+/gi, "-").toLowerCase()}`;
  return (
    <>
      <input
        list={listId}
        value={value ?? ""}
        aria-label={label}
        disabled={disabled || !provider}
        placeholder={t.standardOption(defaultLabel)}
        onChange={(e) => onChange(e.target.value || null)}
        className={CONTROL_CLASS}
      />
      <datalist id={listId}>
        {options.map((model) => (
          <option key={`${model.provider}:${model.id}`} value={model.id}>
            {model.label}
          </option>
        ))}
        {value && !options.some((m) => m.id === value) ? <option value={value}>{value}</option> : null}
      </datalist>
    </>
  );
}

interface EditorActions {
  onSelect: (laneId: string) => void;
  onApply: (lane: Lane, rows: EditorRow[], needsSave: boolean) => void;
  onPersist: (rows: EditorRow[]) => Promise<void>;
  onCreate: (name: string, rows: EditorRow[]) => void;
  onDelete: (lane: Lane) => void;
  onImportOpenRouterModels: (rawText: string) => Promise<OpenRouterModelImportResult>;
}

type RowCheckState =
  | { status: "checking"; reason: string }
  | (LaneSpawnCheckResult & { status: LaneSpawnHealthStatus })
  | { status: "error"; reason: string; dispatcher_path?: null; resolved_model?: null };

function rowSmokeTone(status: RowCheckState["status"]): "emerald" | "amber" | "rose" | "zinc" {
  if (status === "healthy") return "emerald";
  if (status === "unhealthy" || status === "unknown") return "amber";
  if (status === "error") return "rose";
  return "zinc";
}

function rowSmokeLabel(state: RowCheckState): string {
  if (state.status === "checking") return t.workerCheckRunning;
  if (state.status === "healthy") return t.smokeOk;
  if (state.status === "unhealthy" || state.status === "unknown") return t.smokeWarn;
  return t.smokeError;
}

function importStatusTone(status: OpenRouterModelImportStatus): "emerald" | "amber" | "red" | "zinc" {
  if (status === "admitted") return "emerald";
  if (status === "already_configured") return "zinc";
  if (status === "invalid") return "amber";
  return "red";
}

function importStatusLabel(status: OpenRouterModelImportStatus): string {
  if (status === "admitted") return "aufgenommen";
  if (status === "already_configured") return "schon drin";
  if (status === "invalid") return "ungültig";
  return "fehlgeschlagen";
}

interface RowReadiness {
  kind: DotKind;
  /** Kurzlabel für Dot-Title + Zeilen-Hinweis. */
  label: string;
  /** Zählt in der Panel-Zusammenfassung als bereit. */
  ready: boolean;
  /** true sobald irgendeine Evidenz (passiv oder Live-Check) vorliegt. */
  known: boolean;
}

/** EINE sichtbare Bereitschaft pro Zeile: ein frischer Worker-Check gewinnt
 *  über die passive Katalog-/Lane-Evidenz (kanban_spawn_health), damit Dot
 *  und Check-Ergebnis einander nie widersprechen. */
function rowReadiness(
  check: RowCheckState | undefined,
  passive: LaneSpawnHealth | null,
): RowReadiness {
  if (check) {
    if (check.status === "checking") return { kind: "ready", label: t.readinessChecking, ready: false, known: true };
    if (check.status === "healthy") return { kind: "live", label: t.readinessReady, ready: true, known: true };
    if (check.status === "error") return { kind: "error", label: t.readinessError, ready: false, known: true };
    return { kind: "warn", label: t.readinessWarn, ready: false, known: true };
  }
  if (passive) {
    if (passive.status === "healthy") return { kind: "live", label: t.readinessReady, ready: true, known: true };
    if (passive.status === "unhealthy") return { kind: "warn", label: t.readinessWarn, ready: false, known: true };
    return { kind: "idle", label: t.readinessUnknown, ready: false, known: true };
  }
  return { kind: "idle", label: t.readinessUnknown, ready: false, known: false };
}

function resolveRowCheckEntry(row: EditorRow, data: LanesResponse) {
  if (row.locked) return { worker_runtime: row.worker_runtime, provider: null, model: row.model };
  if (row.provider || row.model) return { worker_runtime: "hermes" as const, provider: row.provider, model: row.model };
  const profile = data.profiles.find((p) => p.name === row.profile);
  if (!profile) return null;
  return { worker_runtime: profile.worker_runtime, provider: profile.default_provider ?? null, model: profile.default_model ?? null };
}

function hasLaneOverride(row: EditorRow): boolean {
  return row.choice !== "" || Boolean(row.provider) || Boolean(row.model) || row.fallbackProviders.length > 0;
}

function configPreview(row: EditorRow): string | null {
  if (row.locked || !hasLaneOverride(row)) return null;
  const provider = row.provider ?? row.defaultProvider;
  const model = row.model;
  const lines = ["model:"];
  lines.push(`  provider: ${provider ?? ""}`);
  lines.push(`  default: ${model ?? ""}`);
  lines.push("fallback_providers:");
  if (row.fallbackProviders.length === 0) {
    lines.push("  []");
  } else {
    for (const fallback of row.fallbackProviders) {
      lines.push(`  - provider: ${fallback.provider}`);
      lines.push(`    model: ${fallback.model}`);
      if (fallback.base_url) lines.push(`    base_url: ${fallback.base_url}`);
    }
  }
  return lines.join("\n");
}

/** Pure presentation editor — exported for tests (rendered with fixtures).
 *  Gets remounted (key) when the selected lane changes, so rows/dirty reset. */
export function LanesEditor({
  data,
  lane,
  busy,
  actions,
  initialPendingDelete = null,
}: {
  data: LanesResponse;
  lane: Lane;
  busy: boolean;
  actions: EditorActions;
  initialPendingDelete?: string | null;
}) {
  const models = data.models && data.models.length > 0 ? data.models : FALLBACK_MODELS;
  const [rows, setRows] = useState<EditorRow[]>(() => editorRows(lane, data.profiles, models));
  const [dirty, setDirty] = useState(false);
  const [newName, setNewName] = useState("");
  const [rowChecks, setRowChecks] = useState<Record<string, RowCheckState>>({});
  const [openRouterPaste, setOpenRouterPaste] = useState("");
  const [openRouterImporting, setOpenRouterImporting] = useState(false);
  const [openRouterImportResult, setOpenRouterImportResult] = useState<OpenRouterModelImportResult | null>(null);
  // Inline-Zwei-Schritt fürs Löschen (FlowView-Muster) statt window.confirm.
  const [pendingDelete, setPendingDelete] = useState<string | null>(initialPendingDelete);

  const rowWarnings = useMemo(
    () => Object.fromEntries(rows.map((row) => [row.profile, laneEntryWarnings(row)])),
    [rows],
  );
  const warningEntries = useMemo(
    () => rows.flatMap((row) => rowWarnings[row.profile].map((text) => ({ profile: row.profile, text }))),
    [rows, rowWarnings],
  );
  const hasWarnings = warningEntries.length > 0;
  const [warningsExpanded, setWarningsExpanded] = useState(false);
  const applyDisabled = busy || (!dirty && lane.active);

  // Bereitschaft + Override-Zustand pro Zeile — passiv scannbar, ohne dass der
  // Operator erst jede Zeile einzeln per Worker-Check anstoßen muss.
  const readiness = useMemo(
    () =>
      Object.fromEntries(
        rows.map((row) => [
          row.profile,
          rowReadiness(rowChecks[row.profile], laneProfileSpawnHealth(row.profile, lane, data.profiles)),
        ]),
      ) as Record<string, RowReadiness>,
    [rows, rowChecks, lane, data.profiles],
  );
  const overrideCount = rows.filter(
    (row) => row.choice !== "" || row.provider || row.model || row.fallbackProviders.length > 0,
  ).length;
  const readinessKnown = rows.some((row) => readiness[row.profile]?.known);
  const readyCount = rows.filter((row) => readiness[row.profile]?.ready).length;
  const profilesMeta = [
    readinessKnown ? t.readySummary(readyCount, rows.length) : null,
    t.overrideSummary(overrideCount),
  ]
    .filter(Boolean)
    .join(" · ");

  const runRowCheck = useCallback(async (row: EditorRow) => {
    const entry = resolveRowCheckEntry(row, data);
    const warning = laneEntryWarnings(row)[0];
    if (warning) {
      setRowChecks((prev) => ({
        ...prev,
        [row.profile]: {
          status: "unhealthy",
          reason: warning,
          dispatcher_path: entry?.worker_runtime ?? "hermes",
          resolved_model: entry?.model ?? null,
        },
      }));
      return;
    }
    if (!entry) {
      setRowChecks((prev) => ({
        ...prev,
        [row.profile]: {
          status: "unknown",
          reason: `${t.smokeUnavailable}: Profil fehlt im Lane-Katalog`,
          dispatcher_path: "hermes",
          resolved_model: null,
        },
      }));
      return;
    }
    setRowChecks((prev) => ({
      ...prev,
      [row.profile]: { status: "checking", reason: "Spawn-/Worker-Pfad wird geprüft" },
    }));
    try {
      const result = await smokeCheckLaneConfig(row.profile, entry);
      setRowChecks((prev) => ({ ...prev, [row.profile]: result }));
    } catch (e) {
      setRowChecks((prev) => ({
        ...prev,
        [row.profile]: { status: "error", reason: e instanceof Error ? e.message : String(e) },
      }));
    }
  }, [data]);

  const updateRow = useCallback((profile: string, patch: Partial<EditorRow>) => {
    setRows((prev) =>
      prev.map((row) => (row.profile === profile ? { ...row, ...patch } : row)),
    );
    setDirty(true);
    setRowChecks((prev) => {
      const next = { ...prev };
      delete next[profile];
      return next;
    });
  }, []);

  const updateFallback = useCallback((
    profile: string,
    index: number,
    patch: Partial<LaneFallbackProvider>,
  ) => {
    setRows((prev) =>
      prev.map((row) => {
        if (row.profile !== profile) return row;
        const fallbackProviders = row.fallbackProviders.map((fallback, i) =>
          i === index ? { ...fallback, ...patch } : fallback,
        );
        return { ...row, fallbackProviders };
      }),
    );
    setDirty(true);
    setRowChecks((prev) => {
      const next = { ...prev };
      delete next[profile];
      return next;
    });
  }, []);

  const seedFallback = useCallback((row: EditorRow): LaneFallbackProvider | null => {
    const existing = row.defaultFallbackProviders[0];
    if (existing) return { ...existing };
    const primaryProvider = row.provider ?? row.defaultProvider;
    const candidateProvider =
      providerOptions(models).find((provider) => provider.id !== primaryProvider) ??
      providerOptions(models)[0] ??
      null;
    if (!candidateProvider) return null;
    const model = modelsForProvider(candidateProvider.id, models)[0]?.id;
    if (!model) return null;
    return { provider: candidateProvider.id, model };
  }, [models]);

  const addFallback = useCallback((row: EditorRow) => {
    const fallback = seedFallback(row);
    if (!fallback) return;
    updateRow(row.profile, { fallbackProviders: [...row.fallbackProviders, fallback] });
  }, [seedFallback, updateRow]);

  const runOpenRouterImport = useCallback(async () => {
    const raw = openRouterPaste.trim();
    if (!raw) return;
    setOpenRouterImporting(true);
    try {
      const result = await actions.onImportOpenRouterModels(raw);
      setOpenRouterImportResult(result);
      if (result.admitted.length > 0) setOpenRouterPaste("");
    } catch (e) {
      setOpenRouterImportResult({
        admitted: [],
        configured: [],
        results: [{
          id: "openrouter",
          status: "failed",
          reason: e instanceof Error ? e.message : String(e),
        }],
      });
    } finally {
      setOpenRouterImporting(false);
    }
  }, [actions, openRouterPaste]);

  const removeFallback = useCallback((profile: string, index: number) => {
    setRows((prev) =>
      prev.map((row) =>
        row.profile === profile
          ? { ...row, fallbackProviders: row.fallbackProviders.filter((_, i) => i !== index) }
          : row,
      ),
    );
    setDirty(true);
    setRowChecks((prev) => {
      const next = { ...prev };
      delete next[profile];
      return next;
    });
  }, []);

  const moveFallback = useCallback((profile: string, index: number, delta: -1 | 1) => {
    setRows((prev) =>
      prev.map((row) => {
        if (row.profile !== profile) return row;
        const nextIndex = index + delta;
        if (nextIndex < 0 || nextIndex >= row.fallbackProviders.length) return row;
        const fallbackProviders = [...row.fallbackProviders];
        const [item] = fallbackProviders.splice(index, 1);
        fallbackProviders.splice(nextIndex, 0, item);
        return { ...row, fallbackProviders };
      }),
    );
    setDirty(true);
    setRowChecks((prev) => {
      const next = { ...prev };
      delete next[profile];
      return next;
    });
  }, []);

  return (
    <div className="space-y-4">
      <p className="text-sm hc-soft">{t.intro}</p>

      {/* Kopf: Preset wählen + EIN Klick übernehmen. */}
      <FleetPanel eyebrow={t.presetLabel} meta={dirty ? t.dirtyHint : null}>
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
          <select
            value={lane.id}
            aria-label={t.presetLabel}
            disabled={busy}
            onChange={(e) => actions.onSelect(e.target.value)}
            className="min-h-11 w-full rounded-md border border-[var(--hc-border)] bg-black/25 px-2 py-1.5 text-base text-white sm:min-h-9 sm:w-64 sm:text-sm"
          >
            {data.lanes.map((l) => (
              <option key={l.id} value={l.id}>
                {l.name} {l.active ? t.activeSuffix : ""}
              </option>
            ))}
          </select>
          <div className="flex flex-col gap-2 sm:ml-auto sm:flex-row sm:items-center">
            <Button
              size="sm"
              className="hc-hit"
              disabled={applyDisabled}
              onClick={() => actions.onApply(lane, rows, dirty)}
            >
              <Check className="h-4 w-4" />
              {!dirty && lane.active ? t.applied : t.apply}
            </Button>
            {hasWarnings ? (
              <button
                type="button"
                onClick={() => setWarningsExpanded((v) => !v)}
                className="text-xs hc-dim hover:text-white"
                aria-expanded={warningsExpanded}
                aria-label={warningsExpanded ? t.warningsCollapse : t.warningsExpand}
              >
                {t.warningsHint(warningEntries.length)} {warningsExpanded ? "▾" : "▸"}
              </button>
            ) : null}
          </div>
        </div>
        {hasWarnings && warningsExpanded ? (
          <div className="mt-3 space-y-1 border-t border-[var(--hc-border)] pt-3">
            {warningEntries.map(({ profile, text }) => (
              <ToneCallout key={`${profile}:${text}`} tone="amber">
                <span className="font-medium">{profile}:</span> {text}
              </ToneCallout>
            ))}
          </div>
        ) : null}
      </FleetPanel>

      {/* Standardansicht: ein Dropdown pro Rolle. */}
      <FleetPanel eyebrow={t.modelsTitle} meta={profilesMeta}>
        <ul className="space-y-3">
          {rows.map((row) => {
            const ready = readiness[row.profile];
            const override = hasLaneOverride(row);
            const currentChoice = row.choice;
            return (
              <li
                key={row.profile}
                className="rounded-md border border-[var(--hc-border)] bg-black/15 p-3"
              >
                <div className="flex min-w-0 flex-col gap-2">
                  <div className="flex min-w-0 items-start gap-2">
                    <span className="mt-1.5 inline-flex shrink-0" title={t.readinessTitle(ready.label)}>
                      <Led kind={ready.kind} size={8} />
                    </span>
                    <div className="min-w-0">
                      <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
                        <span className="truncate text-sm font-medium text-white" title={row.description}>
                          {row.profile}
                        </span>
                        {override ? (
                          <span className="inline-flex max-w-full items-center truncate rounded-full border border-sky-500/20 bg-sky-500/10 px-1.5 py-0.5 text-[11px] leading-4 text-sky-200">
                            {t.laneOverride}
                          </span>
                        ) : (
                          <span className="inline-flex items-center rounded-full border border-white/10 bg-white/5 px-1.5 py-0.5 text-[11px] leading-4 hc-dim">
                            {t.profileDefault}
                          </span>
                        )}
                      </div>
                      <div className="flex min-w-0 flex-wrap items-center gap-x-1.5 text-xs hc-dim">
                        {ROLE_HINTS[row.profile] ? <span>{ROLE_HINTS[row.profile]}</span> : null}
                        {ready.known && !ready.ready ? (
                          <span className={ready.kind === "ready" ? "hc-soft" : "text-amber-300/90"}>
                            {t.readinessTitle(ready.label)}
                          </span>
                        ) : null}
                      </div>
                    </div>
                  </div>
                  <SimpleModelSelect
                    value={currentChoice}
                    defaultLabel={row.defaultLabel}
                    models={models}
                    disabled={busy}
                    label={`Modell für ${row.profile}`}
                    onChange={(choice) => {
                      const entry = entryFromChoice(choice);
                      if (entry === null) {
                        updateRow(row.profile, {
                          choice: "",
                          worker_runtime: row.worker_runtime,
                          provider: null,
                          model: null,
                        });
                      } else {
                        const model = entry.model ?? "";
                        const catalogEntry = models.find((m) => m.id === model);
                        updateRow(row.profile, {
                          choice,
                          worker_runtime: entry.worker_runtime ?? "hermes",
                          provider: catalogEntry?.provider ?? null,
                          model: model || null,
                        });
                      }
                    }}
                  />
                  <div className="text-xs hc-dim">
                    {t.permanentModel(row.defaultLabel)}
                    {(() => {
                      const profile = data.profiles.find((p) => p.name === row.profile);
                      const profileModel = profile?.default_model;
                      const activeLane = data.lanes.find((l) => l.active) ?? lane;
                      const activeEntry = activeLane.profiles[row.profile];
                      const activeModel = activeEntry?.model ?? profileModel;
                      if (!activeModel || !profileModel || activeModel === profileModel) return null;
                      return (
                        <span className="ml-2 text-amber-300/90">
                          ⚠ {t.divergenceHint(modelLabel(activeModel, models))}
                        </span>
                      );
                    })()}
                  </div>
                </div>
              </li>
            );
          })}
        </ul>
        <div className="sticky bottom-0 mt-3 flex flex-col gap-2 border-t border-[var(--hc-border)] bg-[var(--hc-bg)]/95 pt-3 sm:flex-row sm:items-center sm:justify-end">
          <Button
            size="sm"
            className="hc-hit w-full justify-center sm:w-auto"
            disabled={applyDisabled}
            onClick={() => actions.onPersist(rows)}
          >
            <Check className="h-4 w-4" />
            {t.savePermanently}
          </Button>
          {hasWarnings ? (
            <button
              type="button"
              onClick={() => setWarningsExpanded((v) => !v)}
              className="inline-flex min-h-11 items-center text-xs hc-dim hover:text-white sm:min-h-0"
              aria-expanded={warningsExpanded}
              aria-label={warningsExpanded ? t.warningsCollapse : t.warningsExpand}
            >
              {t.warningsHint(warningEntries.length)} {warningsExpanded ? "▾" : "▸"}
            </button>
          ) : null}
        </div>
      </FleetPanel>

      {/* Erweitert: Provider, Fallbacks, Presets, OpenRouter-Import */}
      <details className="group">
        <summary className="flex min-h-11 cursor-pointer list-none items-center justify-between rounded-md border border-[var(--hc-border)] bg-black/15 p-3 text-sm hc-soft sm:min-h-9">
          <span>{t.advanced}</span>
          <span className="group-open:hidden">▸</span>
          <span className="hidden group-open:inline">▾</span>
        </summary>
        <div className="mt-3">
          <FleetPanel eyebrow={t.profilesPanel} meta={profilesMeta}>
        <div className="mb-3 space-y-2 border-b border-[var(--hc-border)] pb-3">
          <label className="block min-w-0">
            <span className="hc-type-label">{t.openRouterImport}</span>
            <textarea
              value={openRouterPaste}
              aria-label={t.openRouterImport}
              placeholder={t.openRouterPlaceholder}
              disabled={busy || openRouterImporting}
              rows={2}
              onChange={(e) => {
                setOpenRouterPaste(e.target.value);
                setOpenRouterImportResult(null);
              }}
              className="mt-1 min-h-20 w-full resize-y rounded-md border border-[var(--hc-border)] bg-black/25 px-2 py-1.5 text-base text-white placeholder:text-zinc-500 sm:min-h-24 sm:text-sm"
            />
          </label>
          <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
            <Button
              size="sm"
              ghost
              className="hc-hit justify-center"
              disabled={busy || openRouterImporting || openRouterPaste.trim() === ""}
              onClick={() => void runOpenRouterImport()}
            >
              <ClipboardCheck className="h-3.5 w-3.5" />
              {openRouterImporting ? t.openRouterImportRunning : t.openRouterImportRun}
            </Button>
            {openRouterImportResult ? (
              <span className="text-xs hc-dim">{t.openRouterImported(openRouterImportResult.admitted.length)}</span>
            ) : null}
          </div>
          {openRouterImportResult ? (
            <ul className="flex flex-wrap gap-2">
              {openRouterImportResult.results.map((row) => (
                <li key={`${row.id}:${row.status}`} className="min-w-0 max-w-full">
                  <span title={row.reason} className="inline-flex max-w-full items-center gap-2">
                    <StatusPill tone={importStatusTone(row.status)} label={importStatusLabel(row.status)} size="sm" />
                    <span className="min-w-0 break-words text-xs text-zinc-200">{row.id}</span>
                  </span>
                </li>
              ))}
            </ul>
          ) : null}
        </div>
        <ul className="space-y-3">
          {rows.map((row) => {
            const ready = readiness[row.profile];
            const override = hasLaneOverride(row);
            const warnings = rowWarnings[row.profile] ?? [];
            const preview = configPreview(row);
            const activeProvider = row.provider ?? row.defaultProvider;
            const activeModelLabel = row.model ? modelLabel(row.model, models) : row.defaultLabel;
            return (
            <li
              key={row.profile}
              className="rounded-md border border-[var(--hc-border)] bg-black/15 p-3"
            >
              <div className="flex min-w-0 flex-col gap-3">
                <div className="flex min-w-0 items-start justify-between gap-3">
                  <div className="flex min-w-0 items-start gap-2">
                    <span className="mt-1.5 inline-flex shrink-0" title={t.readinessTitle(ready.label)}>
                      <Led kind={ready.kind} size={8} />
                    </span>
                    <div className="min-w-0">
                      <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
                        <span className="truncate text-sm font-medium text-white" title={row.description}>
                          {row.profile}
                        </span>
                        {row.locked ? (
                          <StatusPill tone="zinc" label={t.lockBadge} size="sm" />
                        ) : override ? (
                          <span className="inline-flex max-w-full items-center truncate rounded-full border border-sky-500/20 bg-sky-500/10 px-1.5 py-0.5 text-[11px] leading-4 text-sky-200">
                            {t.laneOverride}
                          </span>
                        ) : (
                          <span className="inline-flex items-center rounded-full border border-white/10 bg-white/5 px-1.5 py-0.5 text-[11px] leading-4 hc-dim">
                            {t.profileDefault}
                          </span>
                        )}
                        {!rowChecks[row.profile] ? <StatusPill tone="zinc" label={t.smokePending} size="sm" /> : null}
                      </div>
                      <div className="flex min-w-0 flex-wrap items-center gap-x-1.5 text-xs hc-dim">
                        {ROLE_HINTS[row.profile] ? <span>{ROLE_HINTS[row.profile]}</span> : null}
                        {ready.known && !ready.ready ? (
                          <span className={ready.kind === "ready" ? "hc-soft" : "text-amber-300/90"}>
                            {t.readinessTitle(ready.label)}
                          </span>
                        ) : null}
                      </div>
                    </div>
                  </div>
                  {row.locked ? <Lock className="mt-0.5 h-4 w-4 shrink-0 text-zinc-400" /> : null}
                </div>

                <div className="grid gap-2 text-xs hc-soft sm:grid-cols-2">
                  <div>
                    <div className="hc-type-label">{t.profileDefault}</div>
                    <div className="mt-1 break-words text-white">
                      {row.defaultProvider ? `${providerLabel(row.defaultProvider, models)} / ` : ""}
                      {row.defaultLabel}
                    </div>
                  </div>
                  <div>
                    <div className="hc-type-label">{t.laneOverride}</div>
                    <div className="mt-1 break-words text-white">
                      {activeProvider ? `${providerLabel(activeProvider, models)} / ` : ""}
                      {activeModelLabel}
                    </div>
                  </div>
                </div>

                <div className="grid gap-2 sm:grid-cols-[minmax(0,180px)_minmax(0,1fr)_auto] sm:items-end">
                  <label className="min-w-0">
                    <span className="hc-type-label">{t.primaryLabel}</span>
                    <ProviderSelect
                      value={row.provider}
                      models={models}
                      disabled={busy || row.locked}
                      label={`Provider für ${row.profile}`}
                      onChange={(provider) =>
                        updateRow(row.profile, {
                          worker_runtime: "hermes",
                          provider,
                          model: provider === row.provider ? row.model : null,
                          choice: provider ? `hermes|${provider === row.provider ? row.model ?? "" : ""}` : "",
                        })
                      }
                    />
                  </label>
                  <label className="min-w-0">
                    <span className="hc-type-label">Model</span>
                    <ModelSelect
                      provider={row.provider ?? row.defaultProvider}
                      value={row.model}
                      models={models}
                      disabled={busy || row.locked}
                      label={`Modell für ${row.profile}`}
                      defaultLabel={row.defaultLabel}
                      onChange={(model) =>
                        updateRow(row.profile, {
                          worker_runtime: "hermes",
                          model,
                          choice: model ? `hermes|${model}` : row.provider ? "hermes|" : "",
                        })
                      }
                    />
                  </label>
                  <Button
                    size="sm"
                    ghost
                    className="hc-hit justify-center"
                    disabled={busy || rowChecks[row.profile]?.status === "checking"}
                    onClick={() => void runRowCheck(row)}
                  >
                    <Activity className="h-3.5 w-3.5" />
                    {rowChecks[row.profile]?.status === "checking" ? t.workerCheckRunning : t.workerCheck}
                  </Button>
                </div>

                <div className="space-y-2">
                  <div className="flex items-center justify-between gap-2">
                    <span className="hc-type-label">{t.fallbackLabel}</span>
                    {!row.locked ? (
                      <Button
                        size="sm"
                        ghost
                        className="hc-hit"
                        disabled={busy}
                        onClick={() => addFallback(row)}
                      >
                        <Plus className="h-3.5 w-3.5" />
                        {row.defaultFallbackProviders.length > 0 ? t.safeFallback : t.addFallback}
                      </Button>
                    ) : null}
                  </div>
                  {row.fallbackProviders.length === 0 ? (
                    <div className="text-xs hc-dim">{t.fallbackMissing}</div>
                  ) : (
                    <ul className="space-y-2">
                      {row.fallbackProviders.map((fallback, idx) => (
                        <li
                          key={`${row.profile}-fallback-${idx}`}
                          className="grid gap-2 rounded-md border border-[var(--hc-border)] bg-black/20 p-2 sm:grid-cols-[minmax(0,180px)_minmax(0,1fr)_auto]"
                        >
                          <ProviderSelect
                            value={fallback.provider}
                            models={models}
                            disabled={busy || row.locked}
                            label={`Fallback-Provider ${idx + 1} für ${row.profile}`}
                            onChange={(provider) =>
                              updateFallback(row.profile, idx, {
                                provider: provider ?? "",
                                model: provider === fallback.provider ? fallback.model : "",
                              })
                            }
                          />
                          <ModelSelect
                            provider={fallback.provider}
                            value={fallback.model}
                            models={models}
                            disabled={busy || row.locked}
                            label={`Fallback-Modell ${idx + 1} für ${row.profile}`}
                            defaultLabel="automatisch"
                            onChange={(model) => updateFallback(row.profile, idx, { model: model ?? "" })}
                          />
                          <div className="flex items-center justify-end gap-1">
                            <button
                              type="button"
                              aria-label={`Fallback ${idx + 1} nach oben`}
                              disabled={busy || row.locked || idx === 0}
                              onClick={() => moveFallback(row.profile, idx, -1)}
                              className="hc-hit inline-flex w-9 items-center justify-center rounded-md border border-[var(--hc-border)] text-xs hc-dim disabled:opacity-40"
                            >
                              <ArrowUp className="h-3.5 w-3.5" />
                            </button>
                            <button
                              type="button"
                              aria-label={`Fallback ${idx + 1} nach unten`}
                              disabled={busy || row.locked || idx === row.fallbackProviders.length - 1}
                              onClick={() => moveFallback(row.profile, idx, 1)}
                              className="hc-hit inline-flex w-9 items-center justify-center rounded-md border border-[var(--hc-border)] text-xs hc-dim disabled:opacity-40"
                            >
                              <ArrowDown className="h-3.5 w-3.5" />
                            </button>
                            <button
                              type="button"
                              aria-label={`Fallback ${idx + 1} entfernen`}
                              disabled={busy || row.locked}
                              onClick={() => removeFallback(row.profile, idx)}
                              className="hc-hit inline-flex w-9 items-center justify-center rounded-md border border-[var(--hc-border)] text-xs hc-dim disabled:opacity-40"
                            >
                              <X className="h-3.5 w-3.5" />
                            </button>
                          </div>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>

                {warnings.length > 0 ? (
                  <div className="space-y-1">
                    {warnings.map((warning) => (
                      <ToneCallout key={warning} tone="amber">{warning}</ToneCallout>
                    ))}
                  </div>
                ) : null}
                {rowChecks[row.profile] ? (
                  <div className="flex w-full max-w-full flex-wrap items-center justify-start gap-2 text-xs hc-soft">
                    <StatusPill
                      tone={rowSmokeTone(rowChecks[row.profile].status)}
                      label={rowSmokeLabel(rowChecks[row.profile])}
                      size="sm"
                    />
                    <span className="min-w-0 max-w-full break-words">
                      {rowChecks[row.profile].reason}
                    </span>
                  </div>
                ) : null}
                {preview ? (
                  <div className="rounded-md border border-[var(--hc-border)] bg-black/25 p-2">
                    <div className="mb-1 text-xs hc-soft">
                      Preview · {t.wouldChange}
                    </div>
                    <pre className="whitespace-pre-wrap break-words text-xs text-zinc-200">{preview}</pre>
                  </div>
                ) : null}
              </div>
            </li>
            );
          })}
        </ul>
        <div className="sticky bottom-0 mt-3 flex flex-col gap-2 border-t border-[var(--hc-border)] bg-[var(--hc-bg)]/95 pt-3 sm:flex-row sm:items-center sm:justify-end">
          <Button
            size="sm"
            className="hc-hit justify-center"
            disabled={applyDisabled}
            onClick={() => actions.onApply(lane, rows, dirty)}
          >
            <Check className="h-4 w-4" />
            {t.saveLane}
          </Button>
          <Button size="sm" ghost className="hc-hit justify-center" disabled>
            {t.configPreview}
          </Button>
        </div>
          </FleetPanel>
        </div>
      </details>

      {/* Presets: anlegen + aufräumen. Auswahl passiert oben. */}
      <FleetPanel eyebrow={t.presetsPanel}>
        <ul className="divide-y divide-[var(--hc-border)]">
          {data.lanes.map((l) => (
            <li key={l.id} className="flex flex-wrap items-center gap-2 py-2 first:pt-0 last:pb-0">
              <span className="min-w-0 break-words text-sm text-white">{l.name}</span>
              {l.active ? <StatusPill tone="emerald" label={t.active} size="sm" /> : null}
              {l.builtin ? (
                <span className="rounded bg-white/5 px-1.5 py-0.5 text-xs hc-dim">{t.builtin}</span>
              ) : null}
              <span className="text-xs hc-dim">
                {Object.keys(l.profiles).length > 0
                  ? t.overrideSummary(Object.keys(l.profiles).length)
                  : t.defaultChip}
              </span>
              {!l.active ? (
                pendingDelete === l.id ? (
                  <span className="ml-auto inline-flex min-w-0 flex-wrap items-center justify-end gap-2">
                    <span className="hc-type-label hc-soft min-w-0 break-words">{t.confirmDelete(l.name)}</span>
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => {
                        actions.onDelete(l);
                        setPendingDelete(null);
                      }}
                      className="inline-flex min-h-11 items-center rounded-full border border-red-400/40 bg-red-400/10 px-2.5 text-xs text-red-200 disabled:opacity-40 sm:min-h-7"
                    >
                      {busy ? "…" : t.confirmYes}
                    </button>
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => setPendingDelete(null)}
                      className="inline-flex min-h-11 items-center rounded-full border border-[var(--hc-border-strong)] px-2.5 text-xs hc-soft sm:min-h-7"
                    >
                      {t.confirmNo}
                    </button>
                  </span>
                ) : (
                  <button
                    type="button"
                    aria-label={`${t.remove} ${l.name}`}
                    disabled={busy}
                    onClick={() => setPendingDelete(l.id)}
                    className="hc-hit ml-auto inline-flex w-11 items-center justify-center rounded-md border border-[var(--hc-border)] text-xs hc-dim hover:text-white"
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                )
              ) : null}
            </li>
          ))}
        </ul>
        <div className="mt-3 flex flex-col gap-2 border-t border-[var(--hc-border)] pt-3 sm:flex-row sm:items-center">
          <input
            type="text"
            value={newName}
            aria-label={t.newPresetPlaceholder}
            placeholder={t.newPresetPlaceholder}
            onChange={(e) => setNewName(e.target.value)}
            className="min-h-11 w-full rounded-md border border-[var(--hc-border)] bg-black/25 px-2 py-1.5 text-base text-white sm:min-h-9 sm:w-64 sm:text-sm"
          />
          <Button
            size="sm"
            ghost
            className="hc-hit"
            disabled={busy || newName.trim() === ""}
            onClick={() => {
              actions.onCreate(newName.trim(), rows);
              setNewName("");
            }}
          >
            <Plus className="h-3.5 w-3.5" />
            {t.saveAsPreset}
          </Button>
        </div>
      </FleetPanel>
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
    // Erst-Load per setTimeout(0) — Hauskonvention (TriageStrip), s.o.
    const firstLoad = window.setTimeout(() => void reload(), 0);
    return () => window.clearTimeout(firstLoad);
  }, [reload]);

  // Selbstheilung: schlägt der Erstload fehl (z. B. "Failed to fetch" am
  // Handy direkt nach dem Foregrounding), retried der View mit mildem
  // Backoff statt für immer "Lade Modelle …" zu zeigen. Nur solange noch
  // keine Daten da sind — Fehler späterer Aktionen überschreibt kein
  // automatischer Reload.
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
  const lane =
    lanes.find((l) => l.id === selectedId) ?? lanes.find((l) => l.active) ?? lanes[0] ?? null;

  const actions: EditorActions = {
    onSelect: (laneId) => setSelectedId(laneId),
    onApply: (target, rows, needsSave) =>
      void run(async () => {
        if (needsSave) {
          await updateLane(target.id, { profiles: profilesFromEditorRows(rows) });
        }
        if (!target.active) await activateLane(target.id);
      }),
    onPersist: (rows) =>
      run(async () => {
        const profiles: Record<string, import("./lanes/api").LanePersistProfileEntry> = {};
        for (const row of rows) {
          if (!row.choice && !row.provider && !row.model) continue;
          profiles[row.profile] = {
            worker_runtime: row.worker_runtime ?? "hermes",
            provider: row.provider,
            model: row.model ?? "",
          };
        }
        if (Object.keys(profiles).length === 0) return;
        const result = await persistLaneModels(profiles);
        if (result.failed.length > 0) {
          throw new Error(
            `Dauerhaft speichern fehlgeschlagen für: ${result.failed
              .map((f) => `${f.profile} (${f.error})`)
              .join(", ")}`,
          );
        }
      }),
    onCreate: (name, rows) =>
      void run(async () => {
        const res = await createLane(name, profilesFromEditorRows(rows));
        setSelectedId(res.lane.id);
      }),
    onDelete: (target) => void run(() => deleteLane(target.id)),
    onImportOpenRouterModels: async (rawText) => {
      setBusy(true);
      try {
        const result = await importOpenRouterModels(rawText);
        await reload();
        return result;
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
        throw e;
      } finally {
        setBusy(false);
      }
    },
  };

  return (
    <section aria-label={t.title} className="space-y-4">
      <h2 className="text-lg font-semibold text-white">{t.title}</h2>
      {error ? (
        <ToneCallout tone="red">
          <span className="flex items-center justify-between gap-3">
            <span>{error}</span>
            <Button size="sm" ghost className="hc-hit" onClick={() => void reload()} disabled={busy}>
              {t.retry}
            </Button>
          </span>
        </ToneCallout>
      ) : null}
      {data === null ? (
        <p className="text-sm hc-dim">{t.loading}</p>
      ) : lane === null ? (
        <FleetEmptyState title={t.emptyTitle} desc={t.emptyDesc} />
      ) : (
        <LanesEditor
          key={`${lane.id}:${lane.updated_at ?? 0}`}
          data={data}
          lane={lane}
          busy={busy}
          actions={actions}
        />
      )}
    </section>
  );
}
