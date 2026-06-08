import { AlertTriangle, ExternalLink, ShieldAlert } from "lucide-react";
import { fmtAge } from "../lib/derive";
import { de } from "../i18n/de";
import type { BlockedCompletion } from "../lib/types";
import { StatusPill, ToneCallout } from "./atoms";

function PhantomChip({ value }: { value: string }) {
  return (
    <span className="hc-mono inline-flex max-w-full items-center gap-1 rounded-full border border-red-500/30 bg-red-500/10 px-2.5 py-1 text-xs text-red-200">
      <span className="truncate">{value}</span>
    </span>
  );
}

export function HermesBlockedCard({ blocked, now }: { blocked: BlockedCompletion; now: number }) {
  const isHardBlock = blocked.kind === "completion_blocked_hallucination";
  const tone = isHardBlock ? "red" : "amber";
  const kindLabel = isHardBlock ? de.hermes.blockedKindBlocked : de.hermes.blockedKindAdvisory;
  return (
    <article className="hc-card space-y-4 p-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0 space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <StatusPill
              tone={tone}
              label={kindLabel}
              dot={isHardBlock ? "error" : "warn"}
            />
            <span className="rounded-full border border-white/10 bg-white/[.04] px-2 py-0.5 text-xs text-zinc-200">{blocked.assignee}</span>
            <span className="hc-mono text-xs hc-soft">vor {fmtAge(blocked.created_at, now)}</span>
          </div>
          <h3 className="line-clamp-2 text-base font-semibold leading-snug text-white">{blocked.task_title}</h3>
          <p className="hc-mono text-xs hc-dim">{blocked.task_id} · {blocked.kind}</p>
        </div>
        <a className="inline-flex items-center gap-2 rounded-md border border-white/15 px-3 py-1.5 text-sm text-zinc-100 hover:bg-white/10" href="/plugins/kanban">
          <ExternalLink className="h-4 w-4" />
          {de.hermes.details}
        </a>
      </div>

      {blocked.summary_preview ? (
        <div className="whitespace-pre-wrap rounded-lg border border-white/10 bg-black/20 p-3 text-sm leading-6 text-zinc-100">{blocked.summary_preview}</div>
      ) : null}

      {blocked.phantom.length ? (
        <div className="space-y-2">
          <p className="flex items-center gap-1.5 text-xs font-medium uppercase tracking-wide text-red-300">
            {isHardBlock ? <ShieldAlert className="h-3.5 w-3.5" /> : <AlertTriangle className="h-3.5 w-3.5" />}
            {de.hermes.blockedPhantomLabel}
          </p>
          <div className="flex flex-wrap gap-2">
            {blocked.phantom.map((id) => <PhantomChip key={id} value={id} />)}
          </div>
        </div>
      ) : null}

      <ToneCallout tone={tone}>{isHardBlock ? de.hermes.blockedHardHint : de.hermes.blockedAdvisoryHint}</ToneCallout>
    </article>
  );
}
