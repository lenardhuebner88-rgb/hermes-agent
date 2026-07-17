import { z } from "zod";
import { epochSeconds } from "./common";

const OperatorDigestDecisionSchema = z.object({
  id: z.string().catch(""),
  title: z.string().catch(""),
  action: z.string().catch(""),
  source: z.string().catch(""),
  opened_at: z.string().nullable().catch(null),
  age_days: z.coerce.number().catch(0),
});

const OperatorDigestAlertSchema = z.object({
  id: z.string().catch(""),
  severity: z.enum(["red", "amber"]).catch("red"),
  title: z.string().catch(""),
  detail: z.string().catch(""),
});

export const OperatorDigestResponseSchema = z.object({
  generated_at: epochSeconds,
  decisions: z.array(OperatorDigestDecisionSchema).catch([]),
  alerts: z.array(OperatorDigestAlertSchema).catch([]),
  degraded: z.array(z.string()).catch([]),
});
