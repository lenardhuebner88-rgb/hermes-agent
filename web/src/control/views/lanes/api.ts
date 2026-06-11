// Lanes (night-sprint F1) — API client + draft helpers for the Lanes tab.
//
// Deliberately self-contained (own folder, no edits to the shared api.ts /
// i18n/de.ts): a lane is a named profile→(worker_runtime, model) preset; the
// dispatcher hot-reads the ACTIVE lane at every worker spawn, so activation
// needs no gateway restart. Precedence at spawn time:
//   task.model_override > active lane > profile config.yaml default.

import { fetchJSON } from "@/lib/api";

export type LaneRuntime = "hermes" | "claude-cli";

export interface LaneProfileEntry {
  worker_runtime: LaneRuntime | null;
  model: string | null;
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

// --- choice helpers (pure; unit-tested) -------------------------------------
//
// The simple editor shows ONE dropdown per profile. Its value encodes
// runtime + model as `${runtime}|${model}`:
//   ""             → Standard (profile config default, no lane entry)
//   "claude-cli|"  → claude-cli runtime, CLI default model
//   "hermes|gpt-5.5" / "claude-cli|claude-fable-5" → explicit model

/** Fallback catalog when the backend payload carries no `models` yet. */
export const FALLBACK_MODELS: LaneModelOption[] = [
  { id: "claude-fable-5", label: "Claude Fable 5", runtime: "claude-cli", group: "Claude (Max-Abo)" },
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
