// Ausgelagert aus CronView.tsx: Komponentendateien exportieren nur
// Komponenten (react-refresh/only-export-components) — Helper, die Tests
// oder Nachbarn brauchen, leben in Sibling-Modulen.
import { de } from "../i18n/de";
import type { CronJob, ToneName } from "../lib/types";
import type { DotKind } from "../lib/tones";

const t = de.crons;

export type JobTone = { tone: ToneName; dot: DotKind; label: string };

export function jobTone(job: CronJob): JobTone {
  if (job.last_delivery_error) return { tone: "red", dot: "error", label: t.deliveryError };
  if (job.last_error) return { tone: "red", dot: "error", label: t.runError };
  if (!job.enabled) return { tone: "amber", dot: "warn", label: t.disabled };
  if (job.state === "paused" || job.paused_at) return { tone: "amber", dot: "warn", label: t.paused };
  return { tone: "emerald", dot: "live", label: job.last_status || t.scheduled };
}
