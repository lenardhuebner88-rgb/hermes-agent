import { ExternalLink, Quote } from "lucide-react";
import { fmtAge } from "../lib/derive";
import type { KanbanReview, ToneName, VerificationState } from "../lib/types";
import { StatusPill, ToneCallout } from "./atoms";

const stateMeta: Record<VerificationState, { label: string; tone: ToneName; dot: "ready" | "warn" | "error" | "live" }> = {
  approved: { label: "APPROVED", tone: "emerald", dot: "ready" },
  request_changes: { label: "REQUEST_CHANGES", tone: "red", dot: "error" },
  pending: { label: "Verifier pending", tone: "cyan", dot: "live" },
  ungated: { label: "Ungated", tone: "amber", dot: "warn" },
};

export function HermesReviewCard({ review, now }: { review: KanbanReview; now: number }) {
  const state = stateMeta[review.verification_state] ?? stateMeta.pending;
  const verdictLabel = review.verifier_verdict ?? state.label;
  return (
    <article className="hc-card space-y-4 p-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0 space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <StatusPill tone={state.tone} label={verdictLabel} dot={state.dot} />
            {review.reviewer_profile ? <span className="rounded-full border border-white/10 bg-white/[.04] px-2 py-0.5 text-xs text-zinc-200">{review.reviewer_profile}</span> : null}
            <span className="hc-mono text-xs hc-soft">vor {fmtAge(review.submitted_at ?? review.created_at, now)}</span>
          </div>
          <h3 className="line-clamp-2 text-base font-semibold leading-snug text-white">{review.task_title}</h3>
          <p className="hc-mono text-xs hc-dim">{review.task_id}{review.run_id ? ` · Run ${review.run_id}` : ""}</p>
        </div>
        <a className="inline-flex items-center gap-2 rounded-md border border-white/15 px-3 py-1.5 text-sm text-zinc-100 hover:bg-white/10" href="/plugins/kanban">
          <ExternalLink className="h-4 w-4" />
          Details
        </a>
      </div>

      {review.summary_preview ? (
        <div className="whitespace-pre-wrap rounded-lg border border-white/10 bg-black/20 p-3 text-sm leading-6 text-zinc-100">{review.summary_preview}</div>
      ) : null}

      {review.verifier_evidence.length ? (
        <div className="space-y-2">
          <p className="flex items-center gap-1.5 text-xs font-medium uppercase tracking-wide text-cyan-200">
            <Quote className="h-3.5 w-3.5" />
            Verifier evidence
          </p>
          <div className="space-y-2">
            {review.verifier_evidence.map((line) => (
              <blockquote key={line} className="rounded-lg border border-cyan-500/20 bg-cyan-500/10 px-3 py-2 text-sm leading-6 text-cyan-50">{line}</blockquote>
            ))}
          </div>
        </div>
      ) : (
        <ToneCallout tone="cyan">Verifier verdict is not available yet; the task is still in review.</ToneCallout>
      )}
    </article>
  );
}
