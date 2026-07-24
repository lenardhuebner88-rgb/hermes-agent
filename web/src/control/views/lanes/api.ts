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
  /** Current reasoning level for this profile: `agent.reasoning_effort` (hermes)
   *  or `claude_effort` (claude-cli, S1). null = STD (config default). */
  reasoning_effort?: string | null;
  /** Reasoning-effort values the profile-default model can transport (S1); [] = no control. */
  reasoning_support?: string[];
  /** Honest hint shown instead of a Reasoning control when support is [] (no
   *  transport, e.g. grok/qwen/alibaba). claude-cli rows have an ACTIVE 5-level
   *  control since S1 (persists `claude_effort`) and carry no hint. */
  reasoning_hint?: string | null;
}

/** Result of a single model reachability/latency probe (S1). The backend always
 *  fills `profile`/`duration_ms`/`at`; they are typed optional here so cached
 *  probes embedded in older GET /lanes payloads render fail-soft. */
export type LaneProbeStatus = LaneAuthSmokeStatus;

export interface ModelProbeResult {
  provider: string;
  model: string;
  profile?: string;
  status: LaneProbeStatus;
  duration_ms?: number;
  observed_provider?: string | null;
  observed_model?: string | null;
  error_class?: string | null;
  reason?: string | null;
  /** Epoch seconds the probe ran. */
  at?: number;
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
  // --- S1 model-platform metadata (all optional: older payloads omit them) ---
  /** Provider credential present (inventory). Fail-soft when absent. */
  authenticated?: boolean;
  configured?: boolean;
  price_in_per_mtok_usd?: number | null;
  price_out_per_mtok_usd?: number | null;
  context_window?: number | null;
  /** Reasoning-effort values this model can transport; [] = no Reasoning control. */
  reasoning_support?: string[];
  /** Honest hint shown instead of a Reasoning control when support is [] (no
   *  transport, e.g. grok/qwen/alibaba). claude-cli rows have an ACTIVE 5-level
   *  control since S1 (persists `claude_effort`) and carry no hint. */
  reasoning_hint?: string | null;
  /** Last cached probe result, echoed back by GET /lanes. */
  probe?: ModelProbeResult | null;
  /** Backend-curated working-model flag; absent on older payloads. */
  sinnvoll?: boolean;
  used_in_profiles?: boolean;
  admitted?: boolean;
  /** Operator-curated offer exclusion (W1 codex -pro / W2 image models); absent on older payloads. */
  offer_excluded?: boolean;
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
  fallback_providers: Array<{ provider: string; model: string; base_url?: string }>;
  /** S1: omit = leave the profile's reasoning field untouched; a value must be in
   *  the row's reasoning_support (the backend 400s otherwise). For claude-cli rows
   *  it lands in top-level `claude_effort` (→ `--effort`), for hermes in
   *  `agent.reasoning_effort`. */
  reasoning_effort?: string | null;
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

/** S2: probe a single model (reachability + latency). Backend caches the result
 *  and echoes it back on the next GET /lanes as `models[].probe`. */
export function runModelProbe(input: {
  provider: string;
  model: string;
  profile?: string;
  timeoutSeconds?: number;
}): Promise<ModelProbeResult> {
  return fetchJSON<ModelProbeResult>(`${BASE}/model-probe`, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({
      provider: input.provider,
      model: input.model,
      profile: input.profile ?? "coder",
      timeout_seconds: input.timeoutSeconds ?? 45,
    }),
  });
}

/** S2: probe a batch of models sequentially (capped server-side). Returns the
 *  per-model results plus whether the request was truncated to `limit`. */
export function runCatalogProbe(input: {
  models: Array<{ provider: string; model: string }>;
  profile?: string | null;
  timeoutSeconds?: number;
  limit?: number;
}): Promise<{ results: ModelProbeResult[]; truncated: boolean }> {
  return fetchJSON<{ results: ModelProbeResult[]; truncated: boolean }>(`${BASE}/catalog-probe`, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({
      models: input.models,
      profile: input.profile ?? null,
      timeout_seconds: input.timeoutSeconds ?? 45,
      limit: input.limit ?? 8,
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
  removed_profiles?: string[],
): Promise<LanePersistResult> {
  return fetchJSON<LanePersistResult>(`${BASE}/persist`, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({
      profiles,
      ...(removed_profiles ? { removed_profiles } : {}),
    }),
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
  { id: "claude-fable-5", label: "Claude Fable 5", runtime: "claude-cli", group: "Claude (Max-Abo)", provider: null, locked: false },
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

export function providerAwareChoiceFromEntry(
  entry: LaneProfileEntry | undefined,
): string {
  if (!entry || (entry.worker_runtime == null && entry.model == null)) return "";
  const model = entry.model ?? "";
  const runtime =
    entry.worker_runtime ?? (model.startsWith("claude") ? "claude-cli" : "hermes");
  const provider = runtime === "hermes" ? entry.provider ?? "" : "";
  return `${runtime}|${provider}|${model}`;
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

export function entryFromProviderAwareChoice(
  choice: string,
): Partial<LaneProfileEntry> | null {
  if (choice === "") return null;
  const parts = choice.split("|");
  if (parts.length !== 3 || !parts[0]) return entryFromChoice(choice);
  const runtime = parts[0] as LaneRuntime;
  const provider = parts[1] || null;
  const model = parts[2] || null;
  return {
    worker_runtime: runtime,
    provider: runtime === "hermes" ? provider : null,
    model,
  };
}

/** Operator-visible guard for persisted/free-form runtime/model combinations
 *  that contradict the curated model catalog. Unknown models stay fail-soft:
 *  the backend smoke check can still provide a clearer reason. */
export function laneChoiceWarning(choice: string, models: LaneModelOption[]): string | null {
  if (!choice) return null;
  const parts = choice.split("|");
  const runtime = parts[0] as LaneRuntime;
  const provider = parts.length === 3 ? parts[1] : null;
  const model = parts.length === 3 ? parts[2] : parts[1];
  if (!runtime || !model) return null;
  const expected = (models.find((m) =>
    m.id === model && (!provider || !m.provider || m.provider === provider),
  ) ?? models.find((m) => m.id === model))?.runtime ?? null;
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

/** Probe statuses that mean a model is currently NOT reachable for work.
 *  `quota_or_rate_limit` is deliberately excluded — it is transient, not broken. */
export const UNREACHABLE_PROBE_STATUSES: ReadonlySet<LaneProbeStatus> = new Set<LaneProbeStatus>([
  "auth_error",
  "timeout",
  "error",
  "config_error",
]);

/** ModelSelect default filter „sinnvoll & erreichbar": fail-soft on missing
 *  `sinnvoll`/`probe` (older payloads), so an un-flagged model still shows. */
export function isModelReachable(model: LaneModelOption): boolean {
  if (model.sinnvoll === false) return false;
  const probe = model.probe;
  if (probe && UNREACHABLE_PROBE_STATUSES.has(probe.status)) return false;
  return true;
}

/** Stable cache/lookup key for a probe result, shared by GET /lanes
 *  (`models[].probe`), live model/catalog probes, and the compass scoring. */
export function probeKey(provider: string | null | undefined, model: string): string {
  return `${(provider ?? "").trim()}::${model.trim()}`;
}

/** Curated working-model set for batch probes and the compass. Live payloads
 *  carry a backend-computed `sinnvoll`; older payloads (and the captured live
 *  fixture) don't, so fall back to a conservative heuristic (claude-cli
 *  runtime/source, or an authenticated provider) instead of the whole catalog. */
export function filterSinnvoll(models: LaneModelOption[]): LaneModelOption[] {
  if (models.some((m) => m.sinnvoll !== undefined)) {
    return models.filter((m) => m.sinnvoll === true);
  }
  return models.filter(
    (m) => m.runtime === "claude-cli" || m.source === "claude-cli" || m.authenticated === true,
  );
}

function cloneFallbacks(value: LaneFallbackProvider[] | undefined): LaneFallbackProvider[] {
  return (value ?? []).map((entry) => ({
    provider: entry.provider,
    model: entry.model,
    ...(entry.base_url ? { base_url: entry.base_url } : {}),
  }));
}

export interface EditorRow {
  touched?: boolean;
  initialChoice?: string;
  profile: string;
  description: string;
  /** Label of the profile's config default, for the "Standard (…)" option. */
  defaultLabel: string;
  /** Runtime the profile's config default resolves to (catalog `worker_runtime`,
   *  or the same derived runtime as `worker_runtime` for lane-only extras) — the
   *  target the "Standard" choice must revert to, not whatever runtime the row
   *  is currently switched to. */
  defaultRuntime: LaneRuntime;
  defaultProvider: string | null;
  /** Profile-default model id — the persist fallback target for reasoning-only rows. */
  defaultModel?: string | null;
  defaultFallbackProviders: LaneFallbackProvider[];
  worker_runtime: LaneRuntime;
  provider: string | null;
  model: string | null;
  fallbackProviders: LaneFallbackProvider[];
  locked: boolean;
  lockedReason: string | null;
  choice: string;
  // --- S1 reasoning stage (all optional: older payloads/catalogs omit them) ---
  /** Reasoning values the row's EFFECTIVE model can transport; [] = no control. */
  reasoningSupport?: string[];
  /** Honest hint shown instead of a Reasoning control when support is [] (no
   *  transport, e.g. grok/qwen/alibaba). claude-cli rows have an ACTIVE 5-level
   *  control since S1 (persists `claude_effort`) and carry no hint. */
  reasoningHint?: string | null;
  /** Reasoning values of the profile-DEFAULT model — the "Standard" revert target. */
  defaultReasoningSupport?: string[];
  /** Staged reasoning effort; null = "Standard" = leave config untouched on persist. */
  reasoning?: string | null;
  /** The profile's current reasoning level (agent.reasoning_effort or, for
   *  claude-cli, claude_effort), for the "aktuell: X" display. */
  defaultReasoning?: string | null;
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
    // Reasoning stage: the control reflects the EFFECTIVE model's transport
    // support (lane-override model wins over the profile default). The staged
    // value only ever holds a value that is valid for that support, so a later
    // persist can never 400 on a reasoning/model mismatch (S1 contract).
    const effectiveModelId = entry?.model ?? p.default_model;
    const effectiveModel = effectiveModelId
      ? models.find((m) => m.id === effectiveModelId) ?? null
      : null;
    const profileReasoning = p.reasoning_effort ?? null;
    const support = effectiveModel?.reasoning_support ?? p.reasoning_support ?? [];
    const reasoningHint = effectiveModel?.reasoning_hint ?? p.reasoning_hint ?? null;
    const reasoning =
      profileReasoning != null && support.includes(profileReasoning) ? profileReasoning : null;
    return {
      touched: false,
      initialChoice: choiceFromEntry(entry),
      profile: p.name,
      description: p.description,
      defaultLabel: p.default_model ? modelLabel(p.default_model, models) : "automatisch",
      defaultRuntime: p.worker_runtime,
      defaultProvider: p.default_provider ?? null,
      defaultModel: p.default_model,
      defaultFallbackProviders: cloneFallbacks(p.fallback_providers),
      worker_runtime: runtime,
      provider: runtime === "claude-cli" ? null : entry?.provider ?? null,
      model: entry?.model ?? null,
      fallbackProviders: runtime === "claude-cli" ? [] : cloneFallbacks(entry?.fallback_providers ?? p.fallback_providers),
      locked: p.locked === true,
      lockedReason: p.locked_reason ?? null,
      choice: choiceFromEntry(entry),
      reasoningSupport: support,
      reasoningHint,
      defaultReasoningSupport: p.reasoning_support ?? [],
      reasoning,
      defaultReasoning: profileReasoning,
    };
  });
  const extras = Object.keys(lane.profiles)
    .filter((name) => !known.has(name))
    .sort((a, b) => a.localeCompare(b));
  for (const name of extras) {
    const entry = lane.profiles[name];
    const runtime = entry.worker_runtime ?? (entry.model?.startsWith("claude") ? "claude-cli" : "hermes");
    rows.push({
      touched: false,
      initialChoice: choiceFromEntry(entry),
      profile: name,
      description: "",
      defaultLabel: "automatisch",
      defaultRuntime: runtime,
      defaultProvider: null,
      defaultModel: null,
      defaultFallbackProviders: [],
      worker_runtime: runtime,
      provider: entry.provider ?? null,
      model: entry.model ?? null,
      fallbackProviders: cloneFallbacks(entry.fallback_providers),
      locked: runtime === "claude-cli",
      lockedReason: runtime === "claude-cli" ? "Claude-CLI / claude -p excluded from this slice" : null,
      choice: choiceFromEntry(entry),
      reasoningSupport: [],
      // Lane-only extras carry no backend model row, so no hint payload — the
      // ReasoningControl falls back to its generic no-Knopf text (W3).
      reasoningHint: null,
      defaultReasoningSupport: [],
      reasoning: null,
      defaultReasoning: null,
    });
  }
  return rows;
}

function modelOptionValue(option: LaneModelOption): string {
  return `${option.runtime}|${option.provider ?? ""}|${option.id}`;
}

/** Provider-aware choice value for a catalog model (the value `applyChoice`
 *  parses). Exported so the matrix ModelSelect builds choices without
 *  duplicating the `runtime|provider|id` format. */
export function choiceForModel(option: LaneModelOption): string {
  return modelOptionValue(option);
}

/** Applies a Fleet quick-switch dropdown choice to a row. Empty choice ("" =
 *  "Standard") reverts to the profile's catalog default runtime, not whatever
 *  runtime the row is currently switched to — otherwise a lane stays trapped
 *  in a flipped runtime after reverting (live bug, 2026-07-06). */
export function applyChoice(row: EditorRow, choice: string, models: LaneModelOption[]): EditorRow {
  const entry = entryFromProviderAwareChoice(choice);
  if (!entry) {
    const support = row.defaultReasoningSupport ?? row.reasoningSupport ?? [];
    return {
      ...row,
      choice: "",
      worker_runtime: row.defaultRuntime ?? row.worker_runtime,
      provider: null,
      model: null,
      fallbackProviders: row.defaultFallbackProviders,
      reasoningSupport: support,
      reasoning: row.reasoning != null && support.includes(row.reasoning) ? row.reasoning : null,
    };
  }
  const model = models.find((candidate) => modelOptionValue(candidate) === choice);
  const runtime = entry.worker_runtime ?? row.worker_runtime;
  // Switching the model switches the reasoning transport surface with it: the
  // control now offers the chosen model's support, and a staged value that the
  // new model cannot transport is dropped back to "Standard" (persist stays valid).
  const nextSupport = choice === ""
    ? row.defaultReasoningSupport ?? row.reasoningSupport ?? []
    : model?.reasoning_support ?? [];
  const nextReasoning =
    row.reasoning != null && nextSupport.includes(row.reasoning) ? row.reasoning : null;
  return {
    ...row,
    choice,
    worker_runtime: runtime,
    provider: runtime === "hermes" ? model?.provider ?? entry.provider ?? row.defaultProvider ?? null : null,
    model: entry.model ?? null,
    fallbackProviders: runtime === "hermes" ? row.fallbackProviders : [],
    reasoningSupport: nextSupport,
    reasoning: nextReasoning,
  };
}

/** Editor rows → API payload. Default rows ("") are dropped entirely.
 *  Incomplete fallback lines are silently dropped so advisory warnings never
 *  block saving the primary choice. */
export function profilesFromEditorRows(
  rows: EditorRow[],
): Record<string, Partial<LaneProfileEntry> & { reasoning_effort?: string | null }> {
  const out: Record<string, Partial<LaneProfileEntry> & { reasoning_effort?: string | null }> = {};
  for (const row of rows) {
    if (!row.touched) continue;
    const fallbackProviders = cloneFallbacks(row.fallbackProviders).filter(
      (fallback) => fallback.provider && fallback.model,
    );
    const reasoningChanged = (row.reasoning ?? null) !== (row.defaultReasoning ?? null);
    const reasoningEffort = reasoningChanged
      ? (row.reasoning == null || row.reasoning === "" || row.reasoning === "Standard" ? "" : row.reasoning)
      : undefined;
    const hasStructuredOverride =
      row.provider !== null ||
      row.model !== null ||
      fallbackProviders.length > 0 ||
      row.choice !== "" ||
      reasoningChanged;
    if (!hasStructuredOverride) continue;
    if (row.locked || row.worker_runtime === "claude-cli") {
      const entry = entryFromProviderAwareChoice(row.choice);
      const base = entry ?? {
        worker_runtime: "claude-cli" as const,
        model: row.model,
      };
      // F3-1: the locked branch conflated "locked" with "claude-cli" and dropped
      // fallback_providers for BOTH. Only claude-cli legitimately has no fallback
      // transport — a locked HERMES row (e.g. a catalog-locked custom-lane entry)
      // carries a fallback chain that must survive the quick-switch serializer.
      const withFallbacks =
        base.worker_runtime === "hermes"
          ? { ...base, fallback_providers: fallbackProviders }
          : base;
      out[row.profile] = reasoningChanged
        ? { ...withFallbacks, reasoning_effort: reasoningEffort }
        : withFallbacks;
      continue;
    }
    out[row.profile] = {
      worker_runtime: "hermes",
      provider: row.provider,
      model: row.model,
      fallback_providers: fallbackProviders,
      ...(reasoningChanged ? { reasoning_effort: reasoningEffort } : {}),
    };
  }
  return out;
}

export function removedProfilesFromEditorRows(rows: EditorRow[]): string[] {
  return rows
    .filter(
      (row) =>
        row.touched &&
        row.choice === "" &&
        (row.initialChoice ?? "") !== "",
    )
    .map((row) => row.profile);
}

/** Normalizes `profilesFromEditorRows` output into full persist entries.
 *  Reasoning-only rows (model still "Standard") fall back to the profile-default
 *  model so the payload is always a valid `{worker_runtime, model}` the backend
 *  can validate the reasoning value against; entries with neither a model nor a
 *  reasoning value are dropped (nothing to persist). */
export function persistPayloadFromEditorRows(
  rows: EditorRow[],
): Record<string, LanePersistProfileEntry> {
  const structured = profilesFromEditorRows(rows);
  const rowByProfile = new Map(rows.map((row) => [row.profile, row]));
  const out: Record<string, LanePersistProfileEntry> = {};
  for (const [profile, entry] of Object.entries(structured)) {
    const row = rowByProfile.get(profile);
    const fallbackProviders = (entry.fallback_providers ?? [])
      .filter((fallback): fallback is LaneFallbackProvider & { provider: string; model: string } =>
        Boolean(fallback.provider && fallback.model),
      )
      .map((fallback) => ({
        provider: fallback.provider,
        model: fallback.model,
        ...(fallback.base_url ? { base_url: fallback.base_url } : {}),
      }));
    const model = entry.model ?? row?.defaultModel ?? "";
    if (!model && (entry.reasoning_effort == null || entry.reasoning_effort === "")) continue;
    const payload: LanePersistProfileEntry = {
      worker_runtime: entry.worker_runtime ?? row?.defaultRuntime ?? "hermes",
      provider: entry.provider ?? null,
      model,
      fallback_providers: fallbackProviders,
    };
    if (entry.reasoning_effort != null) {
      payload.reasoning_effort = entry.reasoning_effort;
    }
    out[profile] = payload;
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
