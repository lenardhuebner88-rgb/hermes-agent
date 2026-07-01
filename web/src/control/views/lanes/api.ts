// Lanes (night-sprint F1) — API client + draft helpers for the Lanes tab.
//
// Deliberately self-contained (own folder, no edits to the shared api.ts /
// i18n/de.ts): a lane is a named profile→(worker_runtime, model) preset; the
// dispatcher hot-reads the ACTIVE lane at every worker spawn, so activation
// needs no gateway restart. Precedence at spawn time:
//   task.model_override > active lane > profile config.yaml default.

import { fetchJSON } from "@/lib/api";

export type LaneRuntime = "hermes" | "claude-cli";
export type LaneSpawnHealthStatus = "healthy" | "unhealthy" | "unknown";

export interface LaneSpawnHealth {
  status: LaneSpawnHealthStatus;
  reason?: string | null;
}

export interface LaneSpawnCheckResult extends LaneSpawnHealth {
  dispatcher_path: LaneRuntime;
  resolved_model: string | null;
}

export type LaneAuthSmokeStatus =
  | "ok"
  | "fallback"
  | "auth_error"
  | "quota_or_rate_limit"
  | "timeout"
  | "config_error"
  | "error"
  | "skipped";

export interface LaneAuthSmokeResult {
  role: string;
  profile: string;
  runtime: LaneRuntime | string;
  requested_provider: string;
  requested_model: string;
  observed_provider?: string | null;
  observed_model?: string | null;
  response_exact?: boolean;
  fallback_activated?: boolean;
  auth_ok?: boolean;
  status: LaneAuthSmokeStatus;
  error_class?: string | null;
  duration_ms?: number;
  session_id?: string | null;
  reason?: string | null;
  observed_response?: string;
  stderr_preview?: string;
}

export interface LaneAuthSmokeScope {
  requested_roles: string[];
  checked_role_count: number;
  total_role_count: number;
  truncated: boolean;
  role_limit: number;
}

export interface LaneAuthSmokeSummary {
  decision: "ready" | "restricted" | "blocked";
  safe_to_activate: boolean;
  ok_count: number;
  blocking_roles: string[];
  fallback_roles: string[];
  skipped_roles: string[];
  checked_role_count: number;
  total_role_count: number;
  truncated: boolean;
  recommended_next_action: string;
}

export interface LaneAuthSmokeResponse {
  ok: boolean;
  lane_id: string;
  source: "lanes-auth-smoke";
  scope?: LaneAuthSmokeScope;
  summary?: LaneAuthSmokeSummary;
  results: LaneAuthSmokeResult[];
}

export type OpenRouterModelImportStatus =
  | "admitted"
  | "already_configured"
  | "failed"
  | "invalid";

export interface OpenRouterModelImportRow {
  id: string;
  status: OpenRouterModelImportStatus;
  reason: string;
}

export interface OpenRouterModelImportResult {
  results: OpenRouterModelImportRow[];
  admitted: string[];
  configured: string[];
}

export interface LaneFallbackProvider {
  provider: string;
  model: string;
  base_url?: string;
}

export interface LaneProfileEntry {
  worker_runtime: LaneRuntime | null;
  provider?: string | null;
  model: string | null;
  fallback_providers?: LaneFallbackProvider[];
  /** Optional backend evidence: can this profile actually spawn Kanban workers? */
  kanban_spawn_health?: LaneSpawnHealth | LaneSpawnHealthStatus | null;
}

export interface Lane {
  id: string;
  name: string;
  profiles: Record<string, LaneProfileEntry>;
  active: boolean;
  builtin: boolean;
  created_at: number | null;
  updated_at: number | null;
}

export interface LaneCatalogProfile {
  name: string;
  worker_runtime: LaneRuntime;
  default_model: string | null;
  default_provider?: string | null;
  fallback_providers?: LaneFallbackProvider[];
  description: string;
  locked?: boolean;
  locked_reason?: string | null;
  /** Optional backend evidence: can this profile actually spawn Kanban workers? */
  kanban_spawn_health?: LaneSpawnHealth | LaneSpawnHealthStatus | null;
}

export interface LaneModelOption {
  /** Technical model id the dispatcher passes through (e.g. "qwen/qwen3.7-max"). */
  id: string;
  /** Operator-facing name (e.g. "Qwen 3.7 Max"). */
  label: string;
  runtime: LaneRuntime;
  provider?: string | null;
  /** Dropdown optgroup, e.g. "Claude (Max-Abo)" / "API-Modelle". */
  group: string;
  locked?: boolean;
  source?: string;
}

export interface LanesResponse {
  lanes: Lane[];
  count: number;
  active_id: string | null;
  profiles: LaneCatalogProfile[];
  /** Curated working-model catalog; absent on older backends. */
  models?: LaneModelOption[];
}

export interface LanePersistProfileEntry {
  worker_runtime: LaneRuntime;
  provider?: string | null;
  model: string;
}

export interface LanePersistResult {
  written: string[];
  failed: { profile: string; error: string }[];
  lanes: Lane[];
  active_id: string;
}

const BASE = "/api/plugins/kanban/lanes";
const JSON_HEADERS = { "Content-Type": "application/json" };

// Triage escalation helpers live beside the lane catalog types instead of in
// the React component so Fast Refresh can keep component files component-only.
const TRIAGE_RETRY_LABEL = "Nochmal";

export const ESCALATION_MODEL = "claude-opus-4-8"; // claude-fable-5 z.Zt. gesperrt
export const ESCALATION_PROFILE = "premium";

interface LaneProfileRuntimeInfo {
  worker_runtime?: string | null;
  kanban_spawn_health?: LaneSpawnHealth | LaneSpawnHealthStatus | null;
}

export interface LanesRuntimeInfo {
  active_id?: string | null;
  lanes?: {
    id: string;
    active?: boolean;
    profiles?: Record<string, LaneProfileRuntimeInfo>;
  }[];
  profiles?: (LaneProfileRuntimeInfo & { name: string })[];
}

export interface EscalationPlan {
  patch: Record<string, unknown> | null;
  hint: string;
  warns: boolean;
  reassigns: boolean;
  disabled: boolean;
}

function activeLane(lanes: LanesRuntimeInfo | null) {
  return lanes?.lanes?.find((l) => l.active || l.id === lanes.active_id) ?? null;
}

function profileRuntimeInfo(profile: string | null, lanes: LanesRuntimeInfo | null): LaneProfileRuntimeInfo | null {
  if (!profile || !lanes) return null;
  const fromProfile = lanes.profiles?.find((p) => p.name === profile) ?? null;
  const fromLane = activeLane(lanes)?.profiles?.[profile] ?? null;
  if (fromLane && fromProfile) {
    return {
      ...fromProfile,
      ...fromLane,
      kanban_spawn_health: fromLane.kanban_spawn_health ?? fromProfile.kanban_spawn_health,
    };
  }
  return fromLane ?? fromProfile;
}

/** Effektive Runtime eines Profils: aktive Lane gewinnt, sonst Profil-Default. */
export function effectiveRuntime(
  profile: string | null,
  lanes: LanesRuntimeInfo | null,
): string | null {
  return profileRuntimeInfo(profile, lanes)?.worker_runtime ?? null;
}

export function normalizeSpawnHealth(value: LaneProfileRuntimeInfo["kanban_spawn_health"]): LaneSpawnHealth | null {
  if (!value) return null;
  if (typeof value === "string") return { status: value };
  return { status: value.status, reason: value.reason ?? null };
}

/** Passive Spawn-Bereitschaft einer Editor-Zeile, ohne Live-Check:
 *  Lane-Eintrag gewinnt, sonst Katalog-Profil, sonst null (keine Evidenz). */
export function laneProfileSpawnHealth(
  profile: string,
  lane: Lane,
  catalog: LaneCatalogProfile[],
): LaneSpawnHealth | null {
  return (
    normalizeSpawnHealth(lane.profiles[profile]?.kanban_spawn_health) ??
    normalizeSpawnHealth(catalog.find((p) => p.name === profile)?.kanban_spawn_health)
  );
}

/** Override-Zustand einer Dropdown-Auswahl als Operator-Label:
 *  null = Standard (kein Lane-Eintrag, Profil-Default greift),
 *  sonst das Label des fest verdrahteten Modells. */
export function choiceOverrideLabel(choice: string, models: LaneModelOption[]): string | null {
  const entry = entryFromChoice(choice);
  if (entry === null) return null;
  if (!entry.model) return "Claude (automatisch)";
  return modelLabel(entry.model, models);
}

function effectiveSpawnHealth(
  profile: string | null,
  lanes: LanesRuntimeInfo | null,
): LaneSpawnHealth | null {
  return normalizeSpawnHealth(profileRuntimeInfo(profile, lanes)?.kanban_spawn_health);
}

function escalationHint(model: string): string {
  return `setzt model_override=${model} und stellt den Task wieder ready`;
}

function escalationReassignHint(profile: string, model: string): string {
  return `hängt den Task auf „premium" um (claude-cli, ${model}) — ` +
    `Spezialwerkzeuge des alten Profils „${profile}" entfallen (z. B. qmd-vault bei research).`;
}

function disabledEscalationHint(profile: string | null, reason: string): string {
  const label = profile ?? "—";
  return `Nochmal stärker ist blockiert: Ziellane „${label}" ist nicht Kanban-spawn-healthy (${reason}). ` +
    `Sicherer Weg: „${TRIAGE_RETRY_LABEL}" auf derselben Lane nutzen oder Operator repariert/reassigned die Lane-Health.`;
}

function validateEscalationTarget(profile: string | null, lanes: LanesRuntimeInfo | null): string | null {
  if (!lanes) return null; // fail-soft: alter Backend-/Fetch-Fehler ohne Health-Katalog.
  const runtime = effectiveRuntime(profile, lanes);
  if (runtime === null) return `Profil fehlt im Lane-Katalog`;
  if (runtime !== "claude-cli") return `Runtime ist ${runtime}, erwartet claude-cli`;
  const health = effectiveSpawnHealth(profile, lanes);
  if (!health) return `keine Kanban-Spawn-Health im Lane-Katalog`;
  if (health.status !== "healthy") return health.reason ? `${health.status}: ${health.reason}` : health.status;
  return null;
}

/** Eskalations-Plan — runtime- und lane-health-ehrlich: auf Nicht-claude-cli-
 * Runtimes wird nur dann aufs premium-Profil umgehängt, wenn premium als
 * Kanban-spawn-healthy belegt ist. Auf gesunden claude-cli-Profilen bleibt es
 * beim reinen model_override. Hint und PATCH-Body kommen aus EINER Quelle,
 * damit Confirm-Text und Wirkung nie auseinanderlaufen. */
export function escalationPlan(
  profile: string | null,
  lanes: LanesRuntimeInfo | null,
): EscalationPlan {
  const runtime = effectiveRuntime(profile, lanes);
  if (lanes && runtime === null) {
    return {
      reassigns: false,
      warns: true,
      disabled: true,
      patch: null,
      hint: disabledEscalationHint(profile, "Profil fehlt im Lane-Katalog"),
    };
  }
  if (runtime !== null && runtime !== "claude-cli") {
    const blockedReason = validateEscalationTarget(ESCALATION_PROFILE, lanes);
    if (blockedReason) {
      return {
        reassigns: false,
        warns: true,
        disabled: true,
        patch: null,
        hint: disabledEscalationHint(ESCALATION_PROFILE, blockedReason),
      };
    }
    return {
      reassigns: true,
      warns: true,
      disabled: false,
      patch: { assignee: ESCALATION_PROFILE, model_override: ESCALATION_MODEL },
      hint: escalationReassignHint(profile ?? "—", ESCALATION_MODEL),
    };
  }

  const blockedReason = runtime === "claude-cli" ? validateEscalationTarget(profile, lanes) : null;
  if (blockedReason) {
    return {
      reassigns: false,
      warns: true,
      disabled: true,
      patch: null,
      hint: disabledEscalationHint(profile, blockedReason),
    };
  }
  return {
    reassigns: false,
    warns: false,
    disabled: false,
    patch: { model_override: ESCALATION_MODEL },
    hint: escalationHint(ESCALATION_MODEL),
  };
}

export function escalationPatchSequence(plan: EscalationPlan): Record<string, unknown>[] {
  return plan.disabled || plan.patch === null ? [] : [plan.patch, { status: "ready" }];
}

export function loadLanes(): Promise<LanesResponse> {
  return fetchJSON<LanesResponse>(BASE);
}

export function activateLane(laneId: string): Promise<{ lane: Lane }> {
  return fetchJSON<{ lane: Lane }>(
    `${BASE}/${encodeURIComponent(laneId)}/activate`,
    { method: "POST" },
  );
}

export function createLane(
  name: string,
  profiles: Record<string, Partial<LaneProfileEntry>>,
): Promise<{ lane: Lane }> {
  return fetchJSON<{ lane: Lane }>(BASE, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({ name, profiles }),
  });
}

export function updateLane(
  laneId: string,
  body: { name?: string; profiles?: Record<string, Partial<LaneProfileEntry>> },
): Promise<{ lane: Lane }> {
  return fetchJSON<{ lane: Lane }>(`${BASE}/${encodeURIComponent(laneId)}`, {
    method: "PUT",
    headers: JSON_HEADERS,
    body: JSON.stringify(body),
  });
}

export function deleteLane(laneId: string): Promise<{ deleted: string }> {
  return fetchJSON<{ deleted: string }>(
    `${BASE}/${encodeURIComponent(laneId)}`,
    { method: "DELETE" },
  );
}

export function smokeCheckLaneConfig(
  profile: string,
  entry: Pick<LaneProfileEntry, "worker_runtime" | "model" | "provider">,
): Promise<LaneSpawnCheckResult> {
  return fetchJSON<LaneSpawnCheckResult>(`${BASE}/spawn-check`, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({
      profile,
      worker_runtime: entry.worker_runtime,
      provider: entry.provider ?? null,
      model: entry.model ?? null,
    }),
  });
}

export function runLaneAuthSmoke(input: {
  laneId: string;
  roles?: string[];
  timeoutSeconds?: number;
}): Promise<LaneAuthSmokeResponse> {
  return fetchJSON<LaneAuthSmokeResponse>(`${BASE}/auth-smoke`, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({
      lane_id: input.laneId,
      roles: input.roles ?? [],
      timeout_seconds: input.timeoutSeconds ?? 45,
    }),
  });
}

export function importOpenRouterModels(rawText: string): Promise<OpenRouterModelImportResult> {
  return fetchJSON<OpenRouterModelImportResult>(`${BASE}/openrouter-models/import`, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({ raw_text: rawText }),
  });
}

export function persistLaneModels(
  profiles: Record<string, LanePersistProfileEntry>,
): Promise<LanePersistResult> {
  return fetchJSON<LanePersistResult>(`${BASE}/persist`, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({ profiles }),
  });
}

// --- choice helpers (pure; unit-tested) -------------------------------------
//
// The simple editor shows ONE dropdown per profile. Its value encodes
// runtime + model as `${runtime}|${model}`:
//   ""             → Standard (profile config default, no lane entry)
//   "claude-cli|"  → claude-cli runtime, CLI default model
//   "hermes|gpt-5.5" / "claude-cli|claude-fable-5" → explicit model

/** Fallback catalog when the backend payload carries no `models` yet. */
export const FALLBACK_MODELS: LaneModelOption[] = [
  { id: "claude-fable-5", label: "Claude Fable 5 (gesperrt)", runtime: "claude-cli", group: "Claude (Max-Abo)", provider: null, locked: true },
  { id: "claude-opus-4-8", label: "Claude Opus 4.8", runtime: "claude-cli", group: "Claude (Max-Abo)", provider: null, locked: false },
  { id: "claude-sonnet-5", label: "Claude Sonnet 5", runtime: "claude-cli", group: "Claude (Max-Abo)", provider: null, locked: false },
  { id: "gpt-5.5", label: "GPT-5.5", runtime: "hermes", group: "OpenAI Codex", provider: "openai-codex" },
  { id: "glm-5.2-fast", label: "GLM 5.2 Fast", runtime: "hermes", group: "Neuralwatt", provider: "neuralwatt" },
];

/** Lane entry → dropdown value. Entries without runtime derive it from the
 *  model id (claude-* → claude-cli), so nothing renders as unrepresentable. */
export function choiceFromEntry(
  entry: LaneProfileEntry | undefined,
): string {
  if (!entry || (entry.worker_runtime == null && entry.model == null)) return "";
  const model = entry.model ?? "";
  const runtime =
    entry.worker_runtime ?? (model.startsWith("claude") ? "claude-cli" : "hermes");
  return `${runtime}|${model}`;
}

/** Dropdown value → lane entry (null = drop the mapping, profile default). */
export function entryFromChoice(
  choice: string,
): Partial<LaneProfileEntry> | null {
  if (choice === "") return null;
  const sep = choice.indexOf("|");
  const runtime = choice.slice(0, sep) as LaneRuntime;
  const model = choice.slice(sep + 1);
  return { worker_runtime: runtime, model: model === "" ? null : model };
}

/** Operator-visible guard for persisted/free-form runtime/model combinations
 *  that contradict the curated model catalog. Unknown models stay fail-soft:
 *  the backend smoke check can still provide a clearer reason. */
export function laneChoiceWarning(choice: string, models: LaneModelOption[]): string | null {
  if (!choice) return null;
  const sep = choice.indexOf("|");
  if (sep <= 0) return null;
  const runtime = choice.slice(0, sep) as LaneRuntime;
  const model = choice.slice(sep + 1);
  if (!model) return null;
  const expected = models.find((m) => m.id === model)?.runtime ?? null;
  if (!expected || expected === runtime) return null;
  return `Worker-/Modell-Kombination passt nicht: ${model} gehört zu ${expected}, ausgewählt ist ${runtime}.`;
}

/** Operator-facing label for a model id (falls back to the raw id). */
export function modelLabel(id: string, models: LaneModelOption[]): string {
  return models.find((m) => m.id === id)?.label ?? id;
}

export function providerLabel(provider: string | null | undefined, models: LaneModelOption[]): string {
  const id = (provider ?? "").trim();
  if (!id) return "auto";
  return models.find((m) => m.provider === id)?.group ?? id;
}

export function providerOptions(models: LaneModelOption[]): { id: string; label: string }[] {
  const seen = new Set<string>();
  const out: { id: string; label: string }[] = [];
  for (const model of models) {
    const provider = (model.provider ?? "").trim();
    if (model.runtime !== "hermes" || !provider || seen.has(provider)) continue;
    seen.add(provider);
    out.push({ id: provider, label: model.group || provider });
  }
  return out;
}

export function modelsForProvider(provider: string | null | undefined, models: LaneModelOption[]): LaneModelOption[] {
  const id = (provider ?? "").trim();
  if (!id) return [];
  return models.filter((m) => m.runtime === "hermes" && m.provider === id);
}

function cloneFallbacks(value: LaneFallbackProvider[] | undefined): LaneFallbackProvider[] {
  return (value ?? []).map((entry) => ({
    provider: entry.provider,
    model: entry.model,
    ...(entry.base_url ? { base_url: entry.base_url } : {}),
  }));
}

export interface EditorRow {
  profile: string;
  description: string;
  /** Label of the profile's config default, for the "Standard (…)" option. */
  defaultLabel: string;
  defaultProvider: string | null;
  defaultFallbackProviders: LaneFallbackProvider[];
  worker_runtime: LaneRuntime;
  provider: string | null;
  model: string | null;
  fallbackProviders: LaneFallbackProvider[];
  locked: boolean;
  lockedReason: string | null;
  choice: string;
}

/** One row per catalog profile (catalog order) plus any extra profiles the
 *  lane maps that the catalog does not know (appended, sorted) — so a lane
 *  never loses entries by being opened in the simple editor. */
export function editorRows(
  lane: Lane,
  catalog: LaneCatalogProfile[],
  models: LaneModelOption[],
): EditorRow[] {
  const known = new Set(catalog.map((p) => p.name));
  const rows: EditorRow[] = catalog.map((p) => {
    const entry = lane.profiles[p.name];
    const runtime = entry?.worker_runtime ?? p.worker_runtime;
    return {
      profile: p.name,
      description: p.description,
      defaultLabel: p.default_model ? modelLabel(p.default_model, models) : "automatisch",
      defaultProvider: p.default_provider ?? null,
      defaultFallbackProviders: cloneFallbacks(p.fallback_providers),
      worker_runtime: runtime,
      provider: runtime === "claude-cli" ? null : entry?.provider ?? null,
      model: entry?.model ?? null,
      fallbackProviders: runtime === "claude-cli" ? [] : cloneFallbacks(entry?.fallback_providers),
      locked: p.locked === true,
      lockedReason: p.locked_reason ?? null,
      choice: choiceFromEntry(entry),
    };
  });
  const extras = Object.keys(lane.profiles)
    .filter((name) => !known.has(name))
    .sort((a, b) => a.localeCompare(b));
  for (const name of extras) {
    const entry = lane.profiles[name];
    const runtime = entry.worker_runtime ?? (entry.model?.startsWith("claude") ? "claude-cli" : "hermes");
    rows.push({
      profile: name,
      description: "",
      defaultLabel: "automatisch",
      defaultProvider: null,
      defaultFallbackProviders: [],
      worker_runtime: runtime,
      provider: entry.provider ?? null,
      model: entry.model ?? null,
      fallbackProviders: cloneFallbacks(entry.fallback_providers),
      locked: runtime === "claude-cli",
      lockedReason: runtime === "claude-cli" ? "Claude-CLI / claude -p excluded from this slice" : null,
      choice: choiceFromEntry(entry),
    });
  }
  return rows;
}

/** Editor rows → API payload. Default rows ("") are dropped entirely.
 *  Incomplete fallback lines are silently dropped so advisory warnings never
 *  block saving the primary choice. */
export function profilesFromEditorRows(
  rows: EditorRow[],
): Record<string, Partial<LaneProfileEntry>> {
  const out: Record<string, Partial<LaneProfileEntry>> = {};
  for (const row of rows) {
    const fallbackProviders = cloneFallbacks(row.fallbackProviders).filter(
      (fallback) => fallback.provider && fallback.model,
    );
    const hasStructuredOverride =
      row.provider !== null ||
      row.model !== null ||
      fallbackProviders.length > 0 ||
      row.choice !== "";
    if (!hasStructuredOverride) continue;
    if (row.locked || row.worker_runtime === "claude-cli") {
      const entry = entryFromChoice(row.choice);
      out[row.profile] = entry ?? {
        worker_runtime: "claude-cli",
        model: row.model,
      };
      continue;
    }
    out[row.profile] = {
      worker_runtime: "hermes",
      provider: row.provider,
      model: row.model,
      fallback_providers: fallbackProviders,
    };
  }
  return out;
}

export function laneEntryWarnings(row: EditorRow): string[] {
  const warnings: string[] = [];
  if (row.locked || row.worker_runtime === "claude-cli") {
    if (row.fallbackProviders.length > 0 || row.provider) {
      warnings.push("Claude-CLI fallback editing is not supported.");
    }
    return warnings;
  }
  const primaryProvider = row.provider ?? row.defaultProvider;
  const primaryModel = row.model;
  if (row.fallbackProviders.some((fallback) => !fallback.provider || !fallback.model)) {
    warnings.push("Each fallback requires provider and model.");
  }
  for (const fallback of row.fallbackProviders) {
    if (primaryProvider && primaryModel && fallback.provider === primaryProvider && fallback.model === primaryModel) {
      warnings.push("Primary and fallback are identical.");
      break;
    }
  }
  if (
    primaryProvider === "openrouter" &&
    row.fallbackProviders.length > 0 &&
    row.fallbackProviders.every((fallback) => fallback.provider === "openrouter")
  ) {
    warnings.push("OpenRouter primary has only OpenRouter fallbacks.");
  }
  const experimentalPrimary =
    primaryProvider === "openrouter" ||
    (primaryModel ?? "").includes("qwen") ||
    (primaryModel ?? "").includes("kimi") ||
    (primaryModel ?? "").includes("moonshot");
  if (experimentalPrimary && row.fallbackProviders.length === 0) {
    warnings.push("Fallback fehlt.");
  }
  return warnings;
}
