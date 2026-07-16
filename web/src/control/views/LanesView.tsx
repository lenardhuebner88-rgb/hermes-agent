import { useCallback, useEffect, useMemo, useState } from "react";
import { Activity, ArrowDown, ArrowUp, Check, ClipboardCheck, Lock, Plus, ShieldCheck, Trash2, TriangleAlert, X } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { FleetEmptyState, FleetPanel, SignalChip, SignalLabel, signalToneFromLegacy } from "../components/leitstand";
import { Eyebrow } from "../components/primitives";
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
  runLaneAuthSmoke,
  FALLBACK_MODELS,
  type EditorRow,
  type LaneFallbackProvider,
  type Lane,
  type LaneModelOption,
  type OpenRouterModelImportResult,
  type OpenRouterModelImportStatus,
  type LaneSpawnCheckResult,
  type LaneAuthSmokeResponse,
  type LaneAuthSmokeResult,
  type LaneAuthSmokeScope,
  type LaneAuthSmokeSummary,
  type LaneSpawnHealth,
  type LaneSpawnHealthStatus,
  type LanesResponse,
} from "./lanes/api";
import {
  authSmokeButtonLabel,
  authSmokeDisabled,
  authSmokeRenderableResults,
  laneAuthSmokeTone,
} from "./lanes/authSmoke";

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
  authDirtyHint: "Ungespeicherte Aenderungen: erst uebernehmen, dann Auth pruefen.",
  authCheckSummary: (ok: number, total: number) => `${ok}/${total} Live OK`,
  authScopeSummary: (checked: number, total: number) => `${checked}/${total} Rollen geprueft`,
  authExact: "Antwort exakt",
  authExactViaFallback: "Exakte Antwort ueber Fallback",
  authNotExact: "Antwort nicht exakt",
  authDecisionReady: "Lane einsatzbereit",
  authDecisionRestricted: "Lane eingeschraenkt",
  authDecisionBlocked: "Lane blockiert",
  authFallbackActive: "Fallback aktiv",
  authUnchecked: (n: number) => `${n} nicht geprueft`,
  authSkipped: (n: number) => `${n} uebersprungen`,
  authBlocked: (n: number) => `${n} blockiert`,
  authFallbackCount: (n: number) => `${n} Fallback`,
  authStatusOk: "Live OK",
  authStatusFallback: "Fallback",
  authStatusAuth: "Auth Fehler",
  authStatusQuota: "Quota/Rate",
  authStatusTimeout: "Timeout",
  authStatusConfig: "Config",
  authStatusSkipped: "Skipped",
  authStatusError: "Fehler",
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
  laneStatus: "Lane-Zustand",
  activePreset: "Aktives Preset",
  selectedPreset: "Ausgewählt",
  unsaved: "Änderungen offen",
  saved: "Gespeichert",
  standard: "Standard",
  advanced: "Erweitert",
  permanent: "Dauerhaft",
  activeLaneConfig: (model: string) => `Aktiv in Lane: ${model}`,
  permanentModel: (model: string) => `Dauerhafte Profil-Konfiguration: ${model}`,
  savePermanently: "Dauerhaft speichern",
  divergenceHint: (model: string) => `läuft aktuell auf ${model}`,
  meteredBadge: "OpenRouter (metered)",
  cloudMaxBadge: "Claude Max / claude -p",
  meteredHint: "Diese Rolle läuft über OpenRouter — kostet echte Credits (kein Abo-Kontingent).",
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
  "min-h-12 w-full rounded-card border border-line bg-surface-2 px-2 py-1.5 text-body text-ink focus:border-live";

function groupedModelOptions(models: LaneModelOption[]) {
  const groups = new Map<string, LaneModelOption[]>();
  for (const model of models) {
    const key = model.group || model.provider || "API-Modelle";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key)!.push(model);
  }
  return Array.from(groups.entries()).sort(([a], [b]) => a.localeCompare(b));
}

function modelWithProviderLabel(provider: string | null | undefined, model: string, models: LaneModelOption[]): string {
  const providerText = providerLabel(provider, models);
  if (!providerText || providerText === "auto") return model;
  return `${providerText} / ${model}`;
}

/** Effektiver Provider einer Zeile: Lane-Override gewinnt über Profil-Default. */
function rowUsesClaudeCli(row: EditorRow): boolean {
  return row.worker_runtime === "claude-cli";
}

function rowUsesOpenRouter(row: EditorRow): boolean {
  if (rowUsesClaudeCli(row)) return false;
  return (row.provider ?? row.defaultProvider) === "openrouter";
}

/** Sichtbare Markierung, dass eine Rolle über OpenRouter (metered) läuft. In der
 *  Standardansicht ist der Provider sonst nicht erkennbar — diese Lane kostet echte
 *  Credits statt Abo-Kontingent, das soll auf einen Blick auffallen. */
function MeteredBadge() {
  return (
    <span title={t.meteredHint}><SignalChip tone="warn" label={t.meteredBadge} className="max-w-full" /></span>
  );
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
              {model.group ? `${model.label} · ${model.group}` : model.label}
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
  onRunAuthSmoke: (laneId: string) => Promise<LaneAuthSmokeResponse>;
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

function laneAuthSmokeLabel(result: LaneAuthSmokeResult): string {
  if (result.status === "ok") return t.authStatusOk;
  if (result.status === "fallback") return t.authStatusFallback;
  if (result.status === "auth_error") return t.authStatusAuth;
  if (result.status === "quota_or_rate_limit") return t.authStatusQuota;
  if (result.status === "timeout") return t.authStatusTimeout;
  if (result.status === "config_error") return t.authStatusConfig;
  if (result.status === "skipped") return t.authStatusSkipped;
  return t.authStatusError;
}

function laneAuthSmokeReason(result: LaneAuthSmokeResult): string {
  if (result.reason) return result.reason;
  const observed = `${result.observed_provider ?? "-"}/${result.observed_model ?? "-"}`;
  return `requested ${result.requested_provider || "-"}/${result.requested_model || "-"}; observed ${observed}`;
}

function authSmokeDecisionLabel(summary: LaneAuthSmokeSummary | null): string {
  if (!summary) return "";
  if (summary.decision === "ready") return t.authDecisionReady;
  if (summary.decision === "restricted") return t.authDecisionRestricted;
  return t.authDecisionBlocked;
}

function authSmokeDecisionTone(summary: LaneAuthSmokeSummary | null): "emerald" | "amber" | "red" {
  if (summary?.decision === "ready") return "emerald";
  if (summary?.decision === "restricted") return "amber";
  return "red";
}

function authSmokeExactLabel(result: LaneAuthSmokeResult): string | null {
  if (result.status === "skipped" || result.status === "config_error") return null;
  if (result.response_exact && result.fallback_activated) return t.authExactViaFallback;
  return result.response_exact ? t.authExact : t.authNotExact;
}

function authSmokeUncheckedCount(summary: LaneAuthSmokeSummary | null): number {
  if (!summary) return 0;
  return Math.max(0, summary.total_role_count - summary.checked_role_count);
}

function authSmokeSummaryParts(summary: LaneAuthSmokeSummary | null): string {
  if (!summary) return "";
  const unchecked = authSmokeUncheckedCount(summary);
  return [
    `${summary.ok_count} OK`,
    summary.blocking_roles.length > 0 ? t.authBlocked(summary.blocking_roles.length) : null,
    summary.fallback_roles.length > 0 ? t.authFallbackCount(summary.fallback_roles.length) : null,
    summary.skipped_roles.length > 0 ? t.authSkipped(summary.skipped_roles.length) : null,
    unchecked > 0 ? t.authUnchecked(unchecked) : null,
  ].filter(Boolean).join(" · ");
}

function authSmokeSortRank(result: LaneAuthSmokeResult): number {
  if (["auth_error", "quota_or_rate_limit", "timeout", "config_error", "error"].includes(result.status)) return 0;
  if (result.status === "fallback" || result.fallback_activated) return 1;
  if (result.status === "ok") return 2;
  if (result.status === "skipped") return 4;
  return 3;
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
  if (rowUsesClaudeCli(row)) return { worker_runtime: "claude-cli" as const, provider: null, model: row.model };
  if (row.locked) return { worker_runtime: row.worker_runtime, provider: null, model: row.model };
  if (row.provider || row.model) return { worker_runtime: row.worker_runtime, provider: row.provider, model: row.model };
  const profile = data.profiles.find((p) => p.name === row.profile);
  if (!profile) return null;
  return { worker_runtime: profile.worker_runtime, provider: profile.default_provider ?? null, model: profile.default_model ?? null };
}

function hasLaneOverride(row: EditorRow): boolean {
  return row.choice !== "" || Boolean(row.provider) || Boolean(row.model) || row.fallbackProviders.length > 0;
}

function configPreview(row: EditorRow): string | null {
  if (row.locked || !hasLaneOverride(row)) return null;
  if (rowUsesClaudeCli(row)) {
    const lines = ["worker_runtime: claude-cli"];
    if (row.model) lines.push(`claude_model: ${row.model}`);
    return lines.join("\n");
  }
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
  initialAuthSmokeResults = [],
  initialAuthSmokeSummary = null,
  initialAuthSmokeScope = null,
  initialPendingDelete = null,
}: {
  data: LanesResponse;
  lane: Lane;
  busy: boolean;
  actions: EditorActions;
  initialAuthSmokeResults?: LaneAuthSmokeResult[];
  initialAuthSmokeSummary?: LaneAuthSmokeSummary | null;
  initialAuthSmokeScope?: LaneAuthSmokeScope | null;
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
  const [authSmokeRunning, setAuthSmokeRunning] = useState(false);
  const [authSmokeResults, setAuthSmokeResults] = useState<LaneAuthSmokeResult[]>(initialAuthSmokeResults);
  const [authSmokeSummary, setAuthSmokeSummary] = useState<LaneAuthSmokeSummary | null>(initialAuthSmokeSummary);
  const [authSmokeScope, setAuthSmokeScope] = useState<LaneAuthSmokeScope | null>(initialAuthSmokeScope);
  const [authSmokeError, setAuthSmokeError] = useState<string | null>(null);
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
  const authSmokeVisibleResults = authSmokeRenderableResults(authSmokeResults, {
    running: authSmokeRunning,
    error: authSmokeError,
  });
  const authSmokeSortedResults = useMemo(
    () => [...authSmokeVisibleResults].sort((a, b) => authSmokeSortRank(a) - authSmokeSortRank(b)),
    [authSmokeVisibleResults],
  );
  const isAuthSmokeDisabled = authSmokeDisabled({
    busy,
    running: authSmokeRunning,
    hasLaneId: Boolean(lane.id),
    dirty,
  });

  const clearAuthSmokeForDraftEdit = useCallback(() => {
    setAuthSmokeResults([]);
    setAuthSmokeSummary(null);
    setAuthSmokeScope(null);
    setAuthSmokeError(t.authDirtyHint);
  }, []);

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
    clearAuthSmokeForDraftEdit();
    setRowChecks((prev) => {
      const next = { ...prev };
      delete next[profile];
      return next;
    });
  }, [clearAuthSmokeForDraftEdit]);

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
    clearAuthSmokeForDraftEdit();
    setRowChecks((prev) => {
      const next = { ...prev };
      delete next[profile];
      return next;
    });
  }, [clearAuthSmokeForDraftEdit]);

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

  const runAuthSmoke = useCallback(async () => {
    setAuthSmokeRunning(true);
    setAuthSmokeError(null);
    setAuthSmokeResults([]);
    setAuthSmokeSummary(null);
    setAuthSmokeScope(null);
    try {
      const result = await actions.onRunAuthSmoke(lane.id);
      setAuthSmokeResults(result.results);
      setAuthSmokeSummary(result.summary ?? null);
      setAuthSmokeScope(result.scope ?? null);
    } catch (e) {
      setAuthSmokeResults([]);
      setAuthSmokeError(e instanceof Error ? e.message : String(e));
    } finally {
      setAuthSmokeRunning(false);
    }
  }, [actions, lane.id]);

  const removeFallback = useCallback((profile: string, index: number) => {
    setRows((prev) =>
      prev.map((row) =>
        row.profile === profile
          ? { ...row, fallbackProviders: row.fallbackProviders.filter((_, i) => i !== index) }
          : row,
      ),
    );
    setDirty(true);
    clearAuthSmokeForDraftEdit();

    setRowChecks((prev) => {
      const next = { ...prev };
      delete next[profile];
      return next;
    });
  }, [clearAuthSmokeForDraftEdit]);

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
    clearAuthSmokeForDraftEdit();
    setRowChecks((prev) => {
      const next = { ...prev };
      delete next[profile];
      return next;
    });
  }, [clearAuthSmokeForDraftEdit]);

  return (
    <div className="space-y-4">
      <p className="text-sec text-ink-2">{t.intro}</p>

      {/* Kopf: Preset wählen + EIN Klick übernehmen. */}
      <FleetPanel eyebrow={t.presetLabel} meta={dirty ? t.dirtyHint : null}>
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
          <select
            value={lane.id}
            aria-label={t.presetLabel}
            disabled={busy}
            onChange={(e) => actions.onSelect(e.target.value)}
            className="min-h-12 w-full rounded-card border border-line bg-surface-2 px-2 py-1.5 text-body text-ink  sm:w-64"
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
              className="min-h-12"
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
                className="min-h-12 rounded-card px-2 text-sec text-live hover:bg-live/10 hover:text-bronze-hi"
                aria-expanded={warningsExpanded}
                aria-label={warningsExpanded ? t.warningsCollapse : t.warningsExpand}
              >
                {t.warningsHint(warningEntries.length)} {warningsExpanded ? "▾" : "▸"}
              </button>
            ) : null}
          </div>
        </div>
        {hasWarnings && warningsExpanded ? (
          <div className="mt-3 space-y-1 border-t border-line pt-3">
            {warningEntries.map(({ profile, text }) => (
              <div key={`${profile}:${text}`} className="flex items-start gap-2 rounded-card border border-status-warn/30 bg-status-warn/10 px-3 py-2 text-sec text-status-warn"><TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" /><span><strong>{profile}:</strong> {text}</span></div>
            ))}
          </div>
        ) : null}
      </FleetPanel>

      <section
        aria-label={t.laneStatus}
        className="rounded-card border border-line bg-surface-1 px-3 py-2"
      >
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
          <div className="min-w-0">
            <Eyebrow>{t.laneStatus}</Eyebrow>
            <p className="mt-0.5 truncate text-sec font-medium text-ink" title={lane.name}>
              {lane.name} · {lane.active ? t.activePreset : t.selectedPreset}
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <SignalChip
              tone={signalToneFromLegacy(dirty ? "amber" : lane.active ? "emerald" : "zinc")}
              label={dirty ? t.unsaved : lane.active ? t.active : t.selectedPreset}
            />
            <SignalLabel tone="neutral" label={t.overrideSummary(overrideCount)} />
            {readinessKnown ? (
              <SignalLabel tone={readyCount === rows.length ? "ok" : "warn"} label={t.readySummary(readyCount, rows.length)} />
            ) : null}
          </div>
        </div>
      </section>

      {/* Standardansicht: ein Dropdown pro Rolle. */}
      <FleetPanel eyebrow={t.modelsTitle} meta={profilesMeta}>
        <div className="mb-3 space-y-2 border-b border-line pb-3">
          <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
            <Button
              size="sm"
              ghost
              className="min-h-12 justify-center"
              disabled={isAuthSmokeDisabled}
              onClick={() => void runAuthSmoke()}
            >
              <ShieldCheck className="h-3.5 w-3.5" />
              {authSmokeButtonLabel(dirty, authSmokeRunning)}
            </Button>
            {authSmokeSummary ? (
              <span className="text-micro text-ink-3">
                {authSmokeSummaryParts(authSmokeSummary)}
              </span>
            ) : authSmokeVisibleResults.length > 0 ? (
              <span className="text-micro text-ink-3">
                {t.authCheckSummary(authSmokeVisibleResults.filter((result) => result.status === "ok").length, authSmokeVisibleResults.length)}
              </span>
            ) : null}
          </div>
          {authSmokeError ? <div className="flex items-start gap-2 rounded-card border border-status-alert/30 bg-status-alert/10 px-3 py-2 text-sec text-status-alert"><TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />{authSmokeError}</div> : null}
          {authSmokeSummary ? (
            <div className="rounded-card border border-line bg-surface-2 px-3 py-2">
              <SignalChip tone={signalToneFromLegacy(authSmokeDecisionTone(authSmokeSummary))} label={authSmokeDecisionLabel(authSmokeSummary)} />
              <span className="mt-1 block text-sec text-ink-2">{authSmokeSummary.recommended_next_action}</span>
              <span className="block text-micro text-ink-3">
                {authSmokeScope
                  ? t.authScopeSummary(authSmokeScope.checked_role_count, authSmokeScope.total_role_count)
                  : t.authScopeSummary(authSmokeSummary.checked_role_count, authSmokeSummary.total_role_count)}
              </span>
            </div>
          ) : null}
          {authSmokeVisibleResults.length > 0 ? (
            <ul className="divide-y divide-line border-y border-line text-micro">
              {authSmokeSortedResults.map((result) => {
                const observed = `${result.observed_provider ?? "-"}/${result.observed_model ?? "-"}`;
                const exactLabel = authSmokeExactLabel(result);
                return (
                  <li
                    key={`${result.role}:${result.requested_provider}:${result.requested_model}`}
                    className="grid gap-2 py-2 sm:grid-cols-[minmax(0,110px)_auto_minmax(0,1fr)] sm:items-start"
                  >
                    <span className="min-w-0 break-words font-medium text-ink">{result.role}</span>
                    <span className="flex flex-wrap gap-1">
                      <SignalChip tone={signalToneFromLegacy(laneAuthSmokeTone(result.status))} label={laneAuthSmokeLabel(result)} />
                      {result.fallback_activated ? (
                        <SignalChip tone="warn" label={t.authFallbackActive} />
                      ) : null}
                    </span>
                    <span className="min-w-0 space-y-0.5 break-words text-ink-2">
                      <span className="block">
                        {result.requested_provider || "-"}/{result.requested_model || "-"} -&gt; {observed}
                      </span>
                      {exactLabel ? <span className="block">{exactLabel}</span> : null}
                      <span className="block text-ink-3">{laneAuthSmokeReason(result)}</span>
                    </span>
                  </li>
                );
              })}
            </ul>
          ) : null}
        </div>
        <ul className="space-y-3">
          {rows.map((row) => {
            const ready = readiness[row.profile];
            const override = hasLaneOverride(row);
            const currentChoice = row.choice;
            return (
              <li
                key={row.profile}
                className="rounded-card border border-line bg-surface-1 p-3"
              >
                <div className="flex min-w-0 flex-col gap-2">
                  <div className="flex min-w-0 items-start gap-2">
                    <SignalLabel tone={ready.kind === "ready" || ready.kind === "live" ? "ok" : ready.kind === "warn" ? "warn" : ready.kind === "error" ? "alert" : "neutral"} label={t.readinessTitle(ready.label)} className="mt-1.5 shrink-0" />
                    <div className="min-w-0">
                      <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
                        <span className="truncate text-sec font-medium text-ink" title={row.description}>
                          {row.profile}
                        </span>
                        {override ? <SignalLabel tone="neutral" label={t.laneOverride} /> : <SignalLabel tone="neutral" label={t.profileDefault} />}
                        {rowUsesClaudeCli(row) ? <SignalChip tone="neutral" label={t.cloudMaxBadge} /> : null}
                        {rowUsesOpenRouter(row) ? <MeteredBadge /> : null}
                      </div>
                      <div className="flex min-w-0 flex-wrap items-center gap-x-1.5 text-micro text-ink-3">
                        {ROLE_HINTS[row.profile] ? <span>{ROLE_HINTS[row.profile]}</span> : null}
                        {ready.known && !ready.ready ? (
                          <span className={ready.kind === "ready" ? "text-ink-2" : "text-status-warn"}>
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
                  <div className="space-y-0.5 text-micro text-ink-3">
                    {(() => {
                      const profile = data.profiles.find((p) => p.name === row.profile);
                      const profileModel = profile?.default_model;
                      const permanentLabel = modelWithProviderLabel(
                        profile?.default_provider,
                        row.defaultLabel,
                        models,
                      );
                      const activeLane = data.lanes.find((l) => l.active) ?? lane;
                      const activeEntry = activeLane.profiles[row.profile];
                      const activeModel = activeEntry?.model ?? profileModel;
                      const activeRuntime = activeEntry?.worker_runtime ?? profile?.worker_runtime;
                      const activeProvider = activeRuntime === "claude-cli"
                        ? null
                        : activeEntry?.provider ?? profile?.default_provider;
                      const activeLabel = activeModel
                        ? activeRuntime === "claude-cli"
                          ? modelLabel(activeModel, models)
                          : modelWithProviderLabel(activeProvider, modelLabel(activeModel, models), models)
                        : permanentLabel;
                      const differs = activeModel && profileModel && activeModel !== profileModel;
                      return (
                        <>
                          <div className={differs ? "text-status-warn" : undefined}>
                            {t.activeLaneConfig(activeLabel)}
                          </div>
                          <div>{t.permanentModel(permanentLabel)}</div>
                        </>
                      );
                    })()}
                  </div>
                </div>
              </li>
            );
          })}
        </ul>
        <div className="sticky bottom-0 mt-3 flex flex-col gap-2 border-t border-line bg-surface-1/95 pt-3 sm:flex-row sm:items-center sm:justify-end">
          <Button
            size="sm"
            className="min-h-12 w-full justify-center sm:w-auto"
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
              className="inline-flex min-h-12 items-center rounded-card px-2 text-sec text-live hover:bg-live/10 hover:text-bronze-hi"
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
        <summary className="flex min-h-12 cursor-pointer list-none items-center justify-between rounded-card border border-line bg-surface-1 p-3 text-sec text-ink-2 ">
          <span>{t.advanced}</span>
          <span className="group-open:hidden">▸</span>
          <span className="hidden group-open:inline">▾</span>
        </summary>
        <div className="mt-3">
          <FleetPanel eyebrow={t.profilesPanel} meta={profilesMeta}>
        <div className="mb-3 space-y-2 border-b border-line pb-3">
          <label className="block min-w-0">
            <span className="text-micro">{t.openRouterImport}</span>
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
              className="mt-1 min-h-20 w-full resize-y rounded-card border border-line bg-surface-2 px-2 py-1.5 text-body text-ink placeholder:text-ink-3 sm:min-h-24"
            />
          </label>
          <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
            <Button
              size="sm"
              ghost
              className="min-h-12 justify-center"
              disabled={busy || openRouterImporting || openRouterPaste.trim() === ""}
              onClick={() => void runOpenRouterImport()}
            >
              <ClipboardCheck className="h-3.5 w-3.5" />
              {openRouterImporting ? t.openRouterImportRunning : t.openRouterImportRun}
            </Button>
            {openRouterImportResult ? (
              <span className="text-micro text-ink-3">{t.openRouterImported(openRouterImportResult.admitted.length)}</span>
            ) : null}
          </div>
          {openRouterImportResult ? (
            <ul className="flex flex-wrap gap-2">
              {openRouterImportResult.results.map((row) => (
                <li key={`${row.id}:${row.status}`} className="min-w-0 max-w-full">
                  <span title={row.reason} className="inline-flex max-w-full items-center gap-2">
                    <SignalChip tone={signalToneFromLegacy(importStatusTone(row.status))} label={importStatusLabel(row.status)} />
                    <span className="min-w-0 break-words text-micro text-ink">{row.id}</span>
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
            const activeProvider = rowUsesClaudeCli(row) ? null : row.provider ?? row.defaultProvider;
            const activeModelLabel = row.model ? modelLabel(row.model, models) : row.defaultLabel;
            const activeRuntimeLabel = rowUsesClaudeCli(row) ? t.cloudMaxBadge : null;
            return (
            <li
              key={row.profile}
              className="rounded-card border border-line bg-surface-1 p-3"
            >
              <div className="flex min-w-0 flex-col gap-3">
                <div className="flex min-w-0 items-start justify-between gap-3">
                  <div className="flex min-w-0 items-start gap-2">
                    <SignalLabel tone={ready.kind === "ready" || ready.kind === "live" ? "ok" : ready.kind === "warn" ? "warn" : ready.kind === "error" ? "alert" : "neutral"} label={t.readinessTitle(ready.label)} className="mt-1.5 shrink-0" />
                    <div className="min-w-0">
                      <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
                        <span className="truncate text-sec font-medium text-ink" title={row.description}>
                          {row.profile}
                        </span>
                        {row.locked ? (
                          <SignalChip tone="neutral" label={t.lockBadge} />
                        ) : override ? <SignalLabel tone="neutral" label={t.laneOverride} /> : <SignalLabel tone="neutral" label={t.profileDefault} />}
                        {activeRuntimeLabel ? <SignalChip tone="neutral" label={activeRuntimeLabel} /> : null}
                        {rowUsesOpenRouter(row) ? <MeteredBadge /> : null}
                        {!rowChecks[row.profile] ? <SignalChip tone="neutral" label={t.smokePending} /> : null}
                      </div>
                      <div className="flex min-w-0 flex-wrap items-center gap-x-1.5 text-micro text-ink-3">
                        {ROLE_HINTS[row.profile] ? <span>{ROLE_HINTS[row.profile]}</span> : null}
                        {ready.known && !ready.ready ? (
                          <span className={ready.kind === "ready" ? "text-ink-2" : "text-status-warn"}>
                            {t.readinessTitle(ready.label)}
                          </span>
                        ) : null}
                      </div>
                    </div>
                  </div>
                  {row.locked ? <Lock className="mt-0.5 h-4 w-4 shrink-0 text-ink-3" /> : null}
                </div>

                <div className="grid gap-2 text-micro text-ink-2 sm:grid-cols-2">
                  <div>
                    <div className="text-micro">{t.profileDefault}</div>
                    <div className="mt-1 break-words text-ink">
                      {row.defaultProvider ? `${providerLabel(row.defaultProvider, models)} / ` : ""}
                      {row.defaultLabel}
                    </div>
                  </div>
                  <div>
                    <div className="text-micro">{t.laneOverride}</div>
                    <div className="mt-1 break-words text-ink">
                      {activeRuntimeLabel ? `${activeRuntimeLabel} / ` : ""}
                      {activeProvider ? `${providerLabel(activeProvider, models)} / ` : ""}
                      {activeModelLabel}
                    </div>
                  </div>
                </div>

                <div className="grid gap-2 sm:grid-cols-[minmax(0,180px)_minmax(0,1fr)_auto] sm:items-end">
                  <label className="min-w-0">
                    <span className="text-micro">{t.primaryLabel}</span>
                    <ProviderSelect
                      value={row.provider}
                      models={models}
                      disabled={busy || row.locked || rowUsesClaudeCli(row)}
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
                    <span className="text-micro">Model</span>
                    {rowUsesClaudeCli(row) ? (
                      <SimpleModelSelect
                        value={row.choice}
                        defaultLabel={row.defaultLabel}
                        models={models}
                        disabled={busy || row.locked}
                        label={`Modell für ${row.profile}`}
                        onChange={(choice) => {
                          const entry = entryFromChoice(choice);
                          updateRow(row.profile, {
                            choice,
                            worker_runtime: entry?.worker_runtime ?? row.worker_runtime,
                            provider: null,
                            model: entry?.model ?? null,
                          });
                        }}
                      />
                    ) : (
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
                    )}
                  </label>
                  <Button
                    size="sm"
                    ghost
                    className="min-h-12 justify-center"
                    disabled={busy || rowChecks[row.profile]?.status === "checking"}
                    onClick={() => void runRowCheck(row)}
                  >
                    <Activity className="h-3.5 w-3.5" />
                    {rowChecks[row.profile]?.status === "checking" ? t.workerCheckRunning : t.workerCheck}
                  </Button>
                </div>

                <div className="space-y-2">
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-micro">{t.fallbackLabel}</span>
                    {!row.locked && !rowUsesClaudeCli(row) ? (
                      <Button
                        size="sm"
                        ghost
                        className="min-h-12"
                        disabled={busy}
                        onClick={() => addFallback(row)}
                      >
                        <Plus className="h-3.5 w-3.5" />
                        {row.defaultFallbackProviders.length > 0 ? t.safeFallback : t.addFallback}
                      </Button>
                    ) : null}
                  </div>
                  {row.fallbackProviders.length === 0 && !rowUsesClaudeCli(row) ? (
                    <div className="text-micro text-ink-3">{t.fallbackMissing}</div>
                  ) : (
                    <ul className="space-y-2">
                      {row.fallbackProviders.map((fallback, idx) => (
                        <li
                          key={`${row.profile}-fallback-${idx}`}
                          className="grid gap-2 rounded-card border border-line bg-surface-2 p-2 sm:grid-cols-[minmax(0,180px)_minmax(0,1fr)_auto]"
                        >
                          <ProviderSelect
                            value={fallback.provider}
                            models={models}
                            disabled={busy || row.locked || rowUsesClaudeCli(row)}
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
                            disabled={busy || row.locked || rowUsesClaudeCli(row)}
                            label={`Fallback-Modell ${idx + 1} für ${row.profile}`}
                            defaultLabel="automatisch"
                            onChange={(model) => updateFallback(row.profile, idx, { model: model ?? "" })}
                          />
                          <div className="flex items-center justify-end gap-1">
                            <button
                              type="button"
                              aria-label={`Fallback ${idx + 1} nach oben`}
                              disabled={busy || row.locked || rowUsesClaudeCli(row) || idx === 0}
                              onClick={() => moveFallback(row.profile, idx, -1)}
                              className="inline-flex size-12 items-center justify-center rounded-card border border-line text-micro text-ink-3 disabled:opacity-40"
                            >
                              <ArrowUp className="h-3.5 w-3.5" />
                            </button>
                            <button
                              type="button"
                              aria-label={`Fallback ${idx + 1} nach unten`}
                              disabled={busy || row.locked || rowUsesClaudeCli(row) || idx === row.fallbackProviders.length - 1}
                              onClick={() => moveFallback(row.profile, idx, 1)}
                              className="inline-flex size-12 items-center justify-center rounded-card border border-line text-micro text-ink-3 disabled:opacity-40"
                            >
                              <ArrowDown className="h-3.5 w-3.5" />
                            </button>
                            <button
                              type="button"
                              aria-label={`Fallback ${idx + 1} entfernen`}
                              disabled={busy || row.locked || rowUsesClaudeCli(row)}
                              onClick={() => removeFallback(row.profile, idx)}
                              className="inline-flex size-12 items-center justify-center rounded-card border border-line text-micro text-ink-3 disabled:opacity-40"
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
                      <div key={warning} className="flex items-start gap-2 rounded-card border border-status-warn/30 bg-status-warn/10 px-3 py-2 text-sec text-status-warn"><TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />{warning}</div>
                    ))}
                  </div>
                ) : null}
                {rowChecks[row.profile] ? (
                  <div className="flex w-full max-w-full flex-wrap items-center justify-start gap-2 text-micro text-ink-2">
                    <SignalChip
                      tone={signalToneFromLegacy(rowSmokeTone(rowChecks[row.profile].status))}
                      label={rowSmokeLabel(rowChecks[row.profile])}
                    />
                    <span className="min-w-0 max-w-full break-words">
                      {rowChecks[row.profile].reason}
                    </span>
                  </div>
                ) : null}
                {preview ? (
                  <div className="rounded-card border border-line bg-surface-2 p-2">
                    <div className="mb-1 text-micro text-ink-2">
                      Preview · {t.wouldChange}
                    </div>
                    <pre className="whitespace-pre-wrap break-words text-micro text-ink">{preview}</pre>
                  </div>
                ) : null}
              </div>
            </li>
            );
          })}
        </ul>
        <div className="sticky bottom-0 mt-3 flex flex-col gap-2 border-t border-line bg-surface-1/95 pt-3 sm:flex-row sm:items-center sm:justify-end">
          <Button
            size="sm"
            className="min-h-12 justify-center"
            disabled={applyDisabled}
            onClick={() => actions.onApply(lane, rows, dirty)}
          >
            <Check className="h-4 w-4" />
            {t.saveLane}
          </Button>
          <Button size="sm" ghost className="min-h-12 justify-center" disabled>
            {t.configPreview}
          </Button>
        </div>
          </FleetPanel>
        </div>
      </details>

      {/* Presets: anlegen + aufräumen. Auswahl passiert oben. */}
      <FleetPanel eyebrow={t.presetsPanel}>
        <ul className="divide-y divide-line">
          {data.lanes.map((l) => (
            <li key={l.id} className="flex flex-wrap items-center gap-2 py-2 first:pt-0 last:pb-0">
              <span className="min-w-0 break-words text-sec text-ink">{l.name}</span>
              {l.active ? <SignalChip tone="ok" label={t.active} /> : null}
              {l.builtin ? (
                <SignalLabel tone="neutral" label={t.builtin} />
              ) : null}
              <span className="text-micro text-ink-3">
                {Object.keys(l.profiles).length > 0
                  ? t.overrideSummary(Object.keys(l.profiles).length)
                  : t.defaultChip}
              </span>
              {!l.active ? (
                pendingDelete === l.id ? (
                  <span className="ml-auto inline-flex min-w-0 flex-wrap items-center justify-end gap-2">
                    <span className="text-micro text-ink-2 min-w-0 break-words">{t.confirmDelete(l.name)}</span>
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => {
                        actions.onDelete(l);
                        setPendingDelete(null);
                      }}
                      className="inline-flex min-h-12 items-center rounded-card border border-live/40 bg-live/10 px-2.5 text-micro text-bronze-hi disabled:opacity-40"
                    >
                      {busy ? "…" : t.confirmYes}
                    </button>
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => setPendingDelete(null)}
                      className="inline-flex min-h-12 items-center rounded-card border border-line px-2.5 text-micro text-ink-2"
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
                    className="ml-auto inline-flex size-12 items-center justify-center rounded-card border border-line text-micro text-ink-3 hover:border-live hover:text-live"
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                )
              ) : null}
            </li>
          ))}
        </ul>
        <div className="mt-3 flex flex-col gap-2 border-t border-line pt-3 sm:flex-row sm:items-center">
          <input
            type="text"
            value={newName}
            aria-label={t.newPresetPlaceholder}
            placeholder={t.newPresetPlaceholder}
            onChange={(e) => setNewName(e.target.value)}
            className="min-h-12 w-full rounded-card border border-line bg-surface-2 px-2 py-1.5 text-body text-ink  sm:w-64"
          />
          <Button
            size="sm"
            ghost
            className="min-h-12"
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
          profiles[row.profile] = row.worker_runtime === "claude-cli"
            ? {
                worker_runtime: "claude-cli",
                provider: null,
                model: row.model ?? "",
                fallback_providers: [],
              }
            : {
                worker_runtime: "hermes",
                provider: row.provider,
                model: row.model ?? "",
                fallback_providers: row.fallbackProviders
                  .filter((fallback) => fallback.provider && fallback.model)
                  .map((fallback) => ({ provider: fallback.provider!, model: fallback.model! })),
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
    onRunAuthSmoke: (laneId) => runLaneAuthSmoke({ laneId, timeoutSeconds: 45 }),
  };

  return (
    <section aria-label={t.title} className="space-y-4">
      {error ? (
        <div className="flex items-center justify-between gap-3 rounded-card border border-status-alert/30 bg-status-alert/10 px-3 py-2 text-sec text-status-alert">
          <span className="flex min-w-0 items-start gap-2"><TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" /><span>{error}</span></span>
            <Button size="sm" ghost className="min-h-12" onClick={() => void reload()} disabled={busy}>
              {t.retry}
            </Button>
        </div>
      ) : null}
      {data === null ? (
        <p className="text-sec text-ink-3">{t.loading}</p>
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
