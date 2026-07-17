import { z } from "zod";
import {
  epochSeconds,
  nullableNumber,
} from "./common";

const DictateApkSchema = z.object({
  name: z.string(),
  url: z.string(),
  size: z.coerce.number().catch(0),
  mtime: z.coerce.number().catch(0),
});

// Diktat Stufe 11 — one finalized day's metric snapshot (history entry or the
// live `today` delta). `.catch()` per field so a single corrupt day degrades
// field-by-field instead of dropping the whole document.
const DictateHistoryDaySchema = z.object({
  date: z.string().catch(""),
  dictations: z.coerce.number().catch(0),
  failures: z.coerce.number().catch(0),
  retries: z.coerce.number().catch(0),
  busy: z.coerce.number().catch(0),
  success_rate_percent: z.coerce.number().nullable().catch(null),
  latency_p50_ms: z.coerce.number().nullable().catch(null),
  latency_p95_ms: z.coerce.number().nullable().catch(null),
});
export type DictateHistoryDay = z.infer<typeof DictateHistoryDaySchema>;

export const DictateStatusResponseSchema = z.object({
  schema: z.literal("hermes-dictate-status-v1"),
  connected: z.boolean().catch(false),
  last_contact_at: z.coerce.number().nullable().catch(null),
  app_version: z.string().nullable().catch(null),
  engine: z.enum(["on_device", "cloud"]).nullable().catch(null),
  language: z.enum(["system", "german", "english", "auto"]).nullable().catch(null),
  style: z.enum(["auto", "formal", "casual", "concise", "neutral"]).nullable().catch(null),
  surface: z.enum(["overlay", "ime"]).nullable().catch(null),
  microphone_permission: z.boolean().nullable().catch(null),
  service_enabled: z.boolean().nullable().catch(null),
  last_error: z.string().nullable().catch(null),
  dictations: z.coerce.number().catch(0),
  failures: z.coerce.number().catch(0),
  retries: z.coerce.number().catch(0),
  busy: z.coerce.number().catch(0),
  success_rate_percent: z.coerce.number().nullable().catch(null),
  latency_ms: z.coerce.number().nullable().catch(null),
  latency_p50_ms: z.coerce.number().nullable().catch(null),
  latency_p95_ms: z.coerce.number().nullable().catch(null),
  apk: DictateApkSchema.nullable().catch(null),
  // Stufe 11, additive: `.optional()` keeps these backward-compatible with
  // object literals written against the pre-Stufe-11 shape (e.g. other
  // components' test fixtures) — undefined and "present but malformed" both
  // normalize to []/null at the point of use (DictateTrend), same effective
  // fallback a plain `.catch()` would give a genuinely-missing key.
  history: z.array(DictateHistoryDaySchema).optional().catch([]),
  today: DictateHistoryDaySchema.nullable().optional().catch(null),
});
export type DictateStatusResponse = z.infer<typeof DictateStatusResponseSchema>;

// Diktat Stufe 9 — shared dictionary/snippet rules, synced between the
// /control/diktat editor and the app. `schema` pins the envelope shape (same
// versioning idiom as DictateStatusResponseSchema above); `revision`/
// `updated_at`/`updated_by` are null/0 when no document has been saved yet
// (`exists:false`, mirroring GET's "missing file" response).
export const DictatePersonalizationSchema = z.object({
  schema: z.literal("hermes-dictate-personalization-v1"),
  exists: z.boolean().catch(false),
  dictionary_rules: z.string().catch(""),
  snippet_rules: z.string().catch(""),
  revision: z.coerce.number().catch(0),
  updated_at: z.string().nullable().catch(null),
  updated_by: z.string().nullable().catch(null),
});
export type DictatePersonalizationResponse = z.infer<typeof DictatePersonalizationSchema>;
// GET /release-status (plugin_api.py get_release_status) — the auto-release
// kill-switch state + the last 10 auto_release timeline events + the last 5
// pre-deploy git anchors. Feeds the Risiko-Tab Hero cockpit + Aktivität rail.
// ⚠️ `pause_on_red_streak` is resolved backend-side (auto_release._release_config)
// but NOT yet added to this endpoint's response — optional/nullable here so the
// UI degrades gracefully until a backend change adds it (tracked as a read seam,
// not invented on the frontend).
const ReleaseStatusEventSchema = z.object({
  task_id: z.string().catch(""),
  created_at: epochSeconds,
  payload: z.record(z.string(), z.unknown()).catch({}),
});
export const ReleaseStatusResponseSchema = z.object({
  autonomous: z.boolean(),
  max_tier_autonomous: z.string().catch("review"),
  pause_on_red_streak: z.coerce.number().nullable().catch(null).optional(),
  recent: z.array(ReleaseStatusEventSchema).catch([]),
  anchors: z.array(z.string()).catch([]),
});
export type ReleaseStatusEvent = z.infer<typeof ReleaseStatusEventSchema>;
export type ReleaseStatusResponse = z.infer<typeof ReleaseStatusResponseSchema>;

// GET/POST /release-mode (plugin_api.py get_release_mode_endpoint /
// set_release_mode_endpoint, AD-S4 + follow-up) — the WRITE-backed twin of
// release-status: same autonomous/max_tier_autonomous/pause_on_red_streak,
// plus red_streak (current consecutive-red-nights count, the "x" in the
// Risiko-Tab safety line) and max_in_progress (kanban.max_in_progress).
// max_in_progress_per_profile/max_concurrent_per_repo/serialize_by_repo feed
// the "Parallele Worker pro Profil" coupled lever (2026-07-08); per_profile
// is nullable — its real config default is unlimited, never fake it as 1.
// Feeds the Hero cockpit; POST /release-mode + POST /release-concurrency
// write it back.
export const ReleaseModeResponseSchema = z.object({
  autonomous: z.boolean(),
  max_tier_autonomous: z.enum(["standard", "review", "critical"]).catch("review"),
  pause_on_red_streak: z.coerce.number().nullable().catch(null),
  red_streak: z.coerce.number().catch(0),
  max_in_progress: z.coerce.number().catch(3),
  max_in_progress_per_profile: nullableNumber,
  max_concurrent_per_repo: z.coerce.number().catch(1),
  serialize_by_repo: z.boolean().catch(true),
});
export type ReleaseModeResponse = z.infer<typeof ReleaseModeResponseSchema>;
export type ReleaseTier = ReleaseModeResponse["max_tier_autonomous"];
const HealthStatusSchema = z.enum(["healthy", "degraded", "offline"]);
const SubsystemHealthSchema = z.object({
  status: HealthStatusSchema.catch("offline"),
  detail: z.string().catch(""),
  error: z.string().nullable().catch(null),
  latency_ms: z.coerce.number().catch(0).optional(),
  heartbeat_age_s: z.coerce.number().nullable().catch(null).optional(),
});
const defaultSubsystemHealth = { status: "offline" as const, detail: "", error: null };

export const SystemHealthResponseSchema = z.object({
  schema: z.string().catch("hermes-health-v1"),
  checked_at: epochSeconds,
  overall: HealthStatusSchema.catch("offline"),
  subsystems: z.object({
    gateway: SubsystemHealthSchema.catch(defaultSubsystemHealth),
    autoresearch: SubsystemHealthSchema.catch(defaultSubsystemHealth),
    kanban_db: SubsystemHealthSchema.catch(defaultSubsystemHealth),
    kanban_dispatcher: SubsystemHealthSchema.catch(defaultSubsystemHealth),
  }),
});

export const VaultProvenanceResponseSchema = z.object({
  schema: z.string().catch("hermes-vault-provenance-v1"),
  error: z.string().nullable().catch(null),
  stale_count: z.coerce.number().catch(0),
  open_sessions: z.array(z.object({
    agent: z.string().catch("?"),
    started: z.string().catch("?"),
    task: z.string().catch(""),
    path: z.string().catch(""),
    age_hours: z.number().nullable().catch(null),
    stale: z.boolean().catch(false),
  })).catch([]),
  recent_receipts: z.array(z.object({
    when: z.string().catch(""),
    agent: z.string().catch("?"),
    file: z.string().catch(""),
    path: z.string().catch(""),
  })).catch([]),
});
const MetricsGroupSchema = z.object({
  count: z.coerce.number().catch(0),
  error_count: z.coerce.number().catch(0),
  error_rate: z.coerce.number().catch(0),
  p50_ms: z.coerce.number().catch(0),
  p95_ms: z.coerce.number().catch(0),
});

export const MetricsLiteResponseSchema = z.object({
  schema: z.string().catch("hermes-metrics-lite-v1"),
  checked_at: epochSeconds,
  uptime_seconds: z.coerce.number().catch(0),
  // A malformed group degrades to defaults rather than emptying the record.
  groups: z.record(z.string(), MetricsGroupSchema).catch({}),
  error: z.string().nullable().catch(null).optional(),
});

const PressureOverallSchema = z.enum(["ok", "busy", "saturated", "unknown"]);
const TailnetPressureStateSchema = z.enum(["direct", "relay", "inactive", "unknown"]);

const PressureHostSchema = z.object({
  cpu_percent: z.coerce.number().nullable().catch(null),
  load_avg: z.array(z.coerce.number()).catch([]),
  cpu_count: z.coerce.number().catch(1),
  memory_percent: z.coerce.number().nullable().catch(null),
}).catch({
  cpu_percent: null,
  load_avg: [],
  cpu_count: 1,
  memory_percent: null,
});

const PressureDashboardProcessSchema = z.object({
  pid: z.coerce.number().nullable().catch(null),
  rss_mb: z.coerce.number().nullable().catch(null),
  cpu_percent: z.coerce.number().nullable().catch(null),
  cpu_weight: z.coerce.number().nullable().catch(null),
  cpu_quota: z.string().catch("unknown"),
  tasks_current: z.coerce.number().nullable().catch(null),
  num_threads: z.coerce.number().nullable().catch(null).optional(),
}).catch({
  pid: null,
  rss_mb: null,
  cpu_percent: null,
  cpu_weight: null,
  cpu_quota: "unknown",
  tasks_current: null,
});
const PressureSourceSchema = z.object({
  kind: z.string().catch("unknown"),
  label: z.string().catch("unknown"),
  count: z.coerce.number().catch(0),
  cpu_percent: z.coerce.number().catch(0),
  rss_mb: z.coerce.number().catch(0),
  scope: z.string().catch("unknown"),
  scope_kind: z.string().catch("unknown"),
  throttled: z.boolean().catch(false),
});

const PressureAccessSchema = z.object({
  tailnet: TailnetPressureStateSchema.catch("unknown"),
  api_latency_ms: z.coerce.number().nullable().catch(null),
  detail: z.string().nullable().catch(null),
}).catch({
  tailnet: "unknown" as const,
  api_latency_ms: null,
  detail: null,
});

const PressureRecommendationSchema = z.object({
  label: z.string().catch("Kein Hebel"),
  detail: z.string().catch("Keine auffaellige Last erkannt."),
  tone: z.enum(["emerald", "cyan", "sky", "indigo", "amber", "rose", "red", "zinc", "violet"]).catch("zinc"),
}).catch({
  label: "Kein Hebel",
  detail: "Keine auffaellige Last erkannt.",
  tone: "zinc" as const,
});

export const PressureStatusResponseSchema = z.object({
  schema: z.literal("hermes-pressure-v1"),
  checked_at: epochSeconds,
  overall: PressureOverallSchema.catch("unknown"),
  cause: z.string().catch("Pressure unbekannt"),
  recommendation: PressureRecommendationSchema,
  host: PressureHostSchema,
  dashboard: PressureDashboardProcessSchema,
  pressure_sources: z.array(PressureSourceSchema).catch([]),
  access: PressureAccessSchema,
  token_pressure: z.object({
    class: z.string().catch("unknown"),
    pct: z.coerce.number().nullable().catch(null),
    updated_at: z.union([z.string(), z.number()]).nullable().catch(null).optional(),
  }).catch({ class: "unknown", pct: null }),
  errors: z.array(z.string()).catch([]),
});
