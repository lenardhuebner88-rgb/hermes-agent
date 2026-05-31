import { useState } from "react";
import { AlertTriangle, ChevronDown, ChevronRight } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { cn } from "@/lib/utils";
import { fmtAge, nowSec } from "../lib/derive";
import { de } from "../i18n/de";
import type { OpenClawCronErrorsResponse } from "../hooks/useControlData";

interface Props {
  data: OpenClawCronErrorsResponse | null;
  error?: string | null;
  now?: number;
}

export function CronErrorsPanel({ data, error = null, now = nowSec() }: Props) {
  const [collapsed, setCollapsed] = useState(true);
  const errors = data?.errors ?? [];
  const hasNotice = Boolean(error || data?.stale);

  if (errors.length === 0 && !hasNotice) return null;

  return (
    <section role="region" aria-label={de.openclaw.cronErrorsTitle(errors.length)} className="hc-card border-amber-500/35 bg-amber-500/[.05] p-4 shadow-[0_0_0_1px_rgba(245,158,11,.10)]">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="flex min-w-0 gap-3">
          <div className={cn(
            "grid h-10 w-10 shrink-0 place-items-center rounded-lg border",
            errors.length > 0 ? "border-red-500/30 bg-red-500/10 text-red-200" : "border-amber-500/30 bg-amber-500/10 text-amber-200",
          )}>
            <AlertTriangle className="h-5 w-5" />
          </div>
          <div className="min-w-0">
            <p className="hc-eyebrow">{de.openclaw.cronErrorsEyebrow}</p>
            <h3 className="mt-1 text-base font-semibold text-white">{de.openclaw.cronErrorsTitle(errors.length)}</h3>
            {data?.stale ? <p className="mt-1 break-words text-sm text-amber-100">{de.openclaw.cronErrorsStale}: {data.stale}</p> : null}
            {error ? <p className="mt-1 break-words text-sm text-red-100">{de.openclaw.cronErrorsFetchError}: {error}</p> : null}
          </div>
        </div>
        {errors.length > 0 ? (
          <Button
            ghost
            size="icon"
            aria-label={collapsed ? de.openclaw.cronErrorsExpand : de.openclaw.cronErrorsCollapse}
            className="self-end sm:self-start"
            onClick={() => setCollapsed((value) => !value)}
          >
            {collapsed ? <ChevronRight className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
          </Button>
        ) : null}
      </div>

      {errors.length > 0 ? (
        <div className={cn("mt-4 grid gap-2", collapsed && "hidden")}>
          {errors.map((job) => (
            <article key={job.id} data-cron-error-row className="rounded-lg border border-red-500/20 bg-red-500/[.06] px-3 py-2">
              <div className="flex flex-col gap-1 sm:flex-row sm:items-start sm:justify-between">
                <h4 className="break-words text-sm font-semibold text-red-100">{job.name}</h4>
                <span className="shrink-0 text-xs hc-soft">
                  {job.lastRunAt > 0 ? de.openclaw.cronErrorsAge(fmtAge(job.lastRunAt, now)) : de.openclaw.cronErrorsNeverRun}
                </span>
              </div>
              <p className="mt-1 whitespace-pre-wrap break-words text-sm text-zinc-200">{job.lastError}</p>
              <p className="mt-2 text-xs text-red-200">{de.openclaw.cronErrorsConsecutive(job.consecutiveErrors)}</p>
            </article>
          ))}
        </div>
      ) : null}
    </section>
  );
}
