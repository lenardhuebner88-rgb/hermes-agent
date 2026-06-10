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

export interface LanesResponse {
  lanes: Lane[];
  count: number;
  active_id: string | null;
  profiles: LaneCatalogProfile[];
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

/** Datalist suggestions for the model field — free text stays allowed. */
export const MODEL_SUGGESTIONS = [
  "claude-fable-5",
  "claude-opus-4-8",
  "claude-sonnet-4-6",
  "claude-haiku-4-5",
  "gpt-5.5",
  "gpt-5.4",
  "kimi-for-coding",
  "kimi-k2.6",
  "qwen3.7-max",
];

// --- draft helpers (pure; unit-tested) -------------------------------------

export interface DraftRow {
  profile: string;
  runtime: "" | LaneRuntime;
  model: string;
}

/** Lane → editable rows (sorted for a stable editor layout). */
export function rowsFromLane(lane: Lane): DraftRow[] {
  return Object.entries(lane.profiles)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([profile, entry]) => ({
      profile,
      runtime: entry?.worker_runtime ?? "",
      model: entry?.model ?? "",
    }));
}

/** Editable rows → API payload. Blank profile rows are dropped; blank
 *  runtime/model become null (= profile config default). */
export function profilesFromRows(
  rows: DraftRow[],
): Record<string, Partial<LaneProfileEntry>> {
  const out: Record<string, Partial<LaneProfileEntry>> = {};
  for (const row of rows) {
    const profile = row.profile.trim();
    if (!profile) continue;
    out[profile] = {
      worker_runtime: row.runtime === "" ? null : row.runtime,
      model: row.model.trim() === "" ? null : row.model.trim(),
    };
  }
  return out;
}
