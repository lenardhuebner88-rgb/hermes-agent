/**
 * flowAttention — pure attention-summary for the FlowView hero band.
 *
 * Aggregates open decision queues so the operator sees at a glance
 * which queues need attention, without scanning all nine sections.
 * Fully pure (no I/O, no React) — tested in flowAttention.test.ts.
 */

// ── Minimal shapes matching the API responses ────────────────────────────────
// These mirror only what the count functions need; the full types live in the
// component files (TriageStrip.tsx and FunnelFreigaben.tsx).

export interface TriageFailureShape {
  run_id: number;
  task_id: string;
}

export interface FailuresResponseShape {
  failures: TriageFailureShape[];
}

export interface FunnelDraftShape {
  id: string;
}

export interface DraftsResponseShape {
  drafts: FunnelDraftShape[];
}

/**
 * Count of actionable triage failures — matches the number of rows the operator
 * sees in TriageStrip (ALL items in the failures array; no client-side filter).
 * Source: TriageStrip.tsx line 141 renders when data.failures.length > 0 and
 * maps every element into a visible list item.
 */
export function countActionableFailures(data: FailuresResponseShape): number {
  return data.failures.length;
}

/**
 * Count of open funnel drafts awaiting operator approval — matches the number of
 * rows the operator sees in FunnelFreigaben (ALL items in the drafts array; no
 * client-side filter).
 * Source: FunnelFreigaben.tsx line 191 renders when data.drafts.length > 0 and
 * maps every element into a visible list item.
 */
export function countOpenFunnelDrafts(data: DraftsResponseShape): number {
  return data.drafts.length;
}

export interface FlowAttentionInput {
  /** Recovery decisions (KanbanDecisionQueue, filtered of hidden kinds). */
  recoveryCount: number;
  /** Triage failures (TriageStrip items). */
  triageCount: number;
  /** Funnel draft items awaiting operator approval. */
  funnelCount: number;
  /** Disposition lifecycle items open for operator decision. */
  dispositionCount: number;
  /** Board tasks currently in "blocked" status. */
  blockedCount: number;
}

export interface FlowAttentionSegment {
  label: string;
  count: number;
  /** DOM id to scroll to (matches the panel section id). */
  anchorId: string;
}

export interface FlowAttentionSummary {
  /** True when every count is zero (nothing needs attention). */
  quiet: boolean;
  /** Ordered list of non-zero queues. */
  segments: FlowAttentionSegment[];
  /** Human-readable one-line summary string. */
  line: string;
}

const QUIET_LINE = "Nichts wartet auf dich.";

/**
 * Derives a scannable one-liner + anchor list from the live queue counts.
 * Returns a stable "quiet" object when everything is zero.
 */
export function summarizeFlowAttention(input: FlowAttentionInput): FlowAttentionSummary {
  const candidates: FlowAttentionSegment[] = [
    { label: "Recovery", count: input.recoveryCount, anchorId: "flow-section-recovery" },
    { label: "Triage", count: input.triageCount, anchorId: "flow-section-triage" },
    { label: "Freigaben", count: input.funnelCount, anchorId: "flow-section-funnel" },
    { label: "Disposition", count: input.dispositionCount, anchorId: "flow-section-disposition" },
    { label: "Blockiert", count: input.blockedCount, anchorId: "flow-section-blocked" },
  ];

  const segments = candidates.filter((s) => s.count > 0);

  if (segments.length === 0) {
    return { quiet: true, segments: [], line: QUIET_LINE };
  }

  const line = segments.map((s) => `${s.count} ${s.label}`).join(" · ");
  return { quiet: false, segments, line };
}
