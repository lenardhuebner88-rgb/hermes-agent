import { z } from "zod";
import {
  epochSeconds,
} from "./common";

const CronLatestOutputSchema = z.object({
  filename: z.string().nullable().catch(null),
  mtime: z.coerce.number().nullable().catch(null),
  size_bytes: z.coerce.number().nullable().catch(null),
  run_count: z.coerce.number().catch(0),
});

// next_run_at / last_run_at / paused_at arrive as ISO-8601 strings from the cron
// normalizer (e.g. "2026-06-03T07:30:00+02:00"), but may be epoch numbers in
// other paths — accept both without coercion (coerce.number would turn an ISO
// string into NaN, which slips past .catch). The view normalizes to a Date.
const CronTimestampSchema = z.union([z.number(), z.string()]).nullable().catch(null);

export const CronJobSchema = z.object({
  id: z.coerce.string().catch(""),
  name: z.string().catch(""),
  enabled: z.boolean().catch(false),
  state: z.string().catch(""),
  paused_at: CronTimestampSchema,
  paused_reason: z.string().nullable().catch(null),
  schedule_display: z.string().catch(""),
  // repeat may be a string ("daily") or an object ({times, completed}); only
  // schedule_display is surfaced, so tolerate anything and drop it.
  repeat: z.unknown().nullable().catch(null),
  next_run_at: CronTimestampSchema,
  last_run_at: CronTimestampSchema,
  last_status: z.string().nullable().catch(null),
  last_error: z.string().nullable().catch(null),
  last_delivery_error: z.string().nullable().catch(null),
  deliver: z.string().nullable().catch(null),
  skill: z.string().nullable().catch(null),
  model: z.string().nullable().catch(null),
  profile: z.string().catch("default"),
  is_default_profile: z.boolean().catch(true),
  has_script: z.boolean().catch(false),
  has_prompt: z.boolean().catch(false),
  latest_output: CronLatestOutputSchema.nullable().catch(null),
});

export const CronObservabilityResponseSchema = z.object({
  schema: z.string().catch("hermes-cron-obs-v1"),
  checked_at: epochSeconds,
  gateway: z.object({
    running: z.boolean().catch(false),
    pids: z.array(z.coerce.number()).catch([]),
    error: z.string().nullable().catch(null).optional(),
  }).catch({ running: false, pids: [] }),
  // A single malformed job must never empty the whole list (WorkerSchema lesson).
  jobs: z.array(CronJobSchema).catch([]),
  error: z.string().nullable().catch(null).optional(),
});
export const CronOutputSchema = z.object({
  job_id: z.string().catch(""),
  filename: z.string().nullable().catch(null),
  text: z.string().nullable().catch(null),
  truncated: z.boolean().catch(false),
  mtime: z.coerce.number().nullable().catch(null),
});
