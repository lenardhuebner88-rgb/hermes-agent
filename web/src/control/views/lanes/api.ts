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

export interface LaneProfileEntry {
  worker_runtime: LaneRuntime | null;
  model: string | null;
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
  description: string;
  /** Optional backend evidence: can this profile actually spawn Kanban workers? */
  kanban_spawn_health?: LaneSpawnHealth | LaneSpawnHealthStatus | null;
}

export interface LaneModelOption {
  /** Technical model id the dispatcher passes through (e.g. "qwen/qwen3.7-max"). */
  id: string;
  /** Operator-facing name (e.g. "Qwen 3.7 Max"). */
  label: string;
  runtime: LaneRuntime;
  /** Dropdown optgroup, e.g. "Claude (Max-Abo)" / "API-Modelle". */
  group: string;
}

export interface LanesResponse {
  lanes: Lane[];
  count: number;
  active_id: string | null;
  profiles: LaneCatalogProfile[];
  /** Curated working-model catalog; absent on older backends. */
  models?: LaneModelOption[];
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
  entry: Pick<LaneProfileEntry, "worker_runtime" | "model">,
): Promise<LaneSpawnCheckResult> {
  return fetchJSON<LaneSpawnCheckResult>(`${BASE}/spawn-check`, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({
      profile,
      worker_runtime: entry.worker_runtime,
      model: entry.model ?? null,
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
  { id: "claude-fable-5", label: "Claude Fable 5 (gesperrt)", runtime: "claude-cli", group: "Claude (Max-Abo)" },
  { id: "claude-opus-4-8", label: "Claude Opus 4.8", runtime: "claude-cli", group: "Claude (Max-Abo)" },
  { id: "gpt-5.5", label: "GPT-5.5", runtime: "hermes", group: "API-Modelle" },
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

export interface EditorRow {
  profile: string;
  description: string;
  /** Label of the profile's config default, for the "Standard (…)" option. */
  defaultLabel: string;
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
  const rows: EditorRow[] = catalog.map((p) => ({
    profile: p.name,
    description: p.description,
    defaultLabel: p.default_model ? modelLabel(p.default_model, models) : "automatisch",
    choice: choiceFromEntry(lane.profiles[p.name]),
  }));
  const extras = Object.keys(lane.profiles)
    .filter((name) => !known.has(name))
    .sort((a, b) => a.localeCompare(b));
  for (const name of extras) {
    rows.push({
      profile: name,
      description: "",
      defaultLabel: "automatisch",
      choice: choiceFromEntry(lane.profiles[name]),
    });
  }
  return rows;
}

/** Editor rows → API payload. Default rows ("") are dropped entirely. */
export function profilesFromEditorRows(
  rows: EditorRow[],
): Record<string, Partial<LaneProfileEntry>> {
  const out: Record<string, Partial<LaneProfileEntry>> = {};
  for (const row of rows) {
    const entry = entryFromChoice(row.choice);
    if (entry !== null) out[row.profile] = entry;
  }
  return out;
}
