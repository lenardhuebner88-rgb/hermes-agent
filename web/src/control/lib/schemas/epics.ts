import { z } from "zod";
import {
  nullableEpochSeconds,
} from "./common";

// Epics (Vorhaben-Ebene): GET /epics liefert pro Epic den Task-/Kosten-Rollup.
// cost_usd/tokens sind null, wenn keine Runs Kosten gestempelt haben.
export const EpicSchema = z.object({
  id: z.string(),
  title: z.string().catch(""),
  body: z.string().nullable().catch(null),
  status: z.enum(["open", "closed"]).catch("open"),
  created_at: nullableEpochSeconds,
  closed_at: nullableEpochSeconds,
  task_count: z.coerce.number().catch(0),
  open_tasks: z.coerce.number().catch(0),
  done_tasks: z.coerce.number().catch(0),
  cost_usd: z.coerce.number().nullable().catch(null),
  input_tokens: z.coerce.number().nullable().catch(null),
  output_tokens: z.coerce.number().nullable().catch(null),
});
export type Epic = z.infer<typeof EpicSchema>;

export const EpicsResponseSchema = z.object({
  epics: z.array(EpicSchema).catch([]),
  count: z.coerce.number().catch(0),
});
export type EpicsResponse = z.infer<typeof EpicsResponseSchema>;
