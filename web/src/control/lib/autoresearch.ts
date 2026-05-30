import type { AutoresearchStatus, Proposal, ToneName } from "./types";

export interface ProposalPriorityGroup {
  key: "safety" | "quick-win" | "code-gate" | "other";
  label: string;
  tone: ToneName;
  score: number;
}

export interface RankedProposal {
  proposal: Proposal;
  group: ProposalPriorityGroup;
}

export interface RankedProposalQueue {
  shortlist: RankedProposal[];
  backlog: RankedProposal[];
  summary: { total: number; shown: number; remaining: number };
}

// AR3 sweeps one skill per iteration; the ceiling mirrors the backend
// MAX_ITERATIONS so a manual run can cover the whole used-skill set.
export const MAX_LOOP_ITERATIONS = 50;
export function clampLoopIterations(value: number): number {
  if (!Number.isFinite(value)) return 1;
  return Math.max(1, Math.min(MAX_LOOP_ITERATIONS, Math.round(value)));
}

export function describeLoopStatus(status: AutoresearchStatus | null) {
  const running = status?.state === "running" || status?.state === "stopping";
  const iteration = status?.iteration ?? 0;
  const max = status?.max ?? 0;
  const progressPercent = running && max > 0 ? Math.max(0, Math.min(100, (iteration / max) * 100)) : 0;
  const routeStatus = status?.route_status || "unbekannt";
  const routeOk = routeStatus === "configured";

  return {
    running,
    iterationLabel: running && max > 0 ? `${iteration} / ${max}` : "kein Lauf aktiv",
    progressPercent,
    stepLabel: status?.last_step || "-",
    evalLabel: status?.last_eval || "-",
    heartbeatLabel: status?.heartbeat_age_s == null ? "-" : `${status.heartbeat_age_s}s ${status.heartbeat_fresh ? "frisch" : "stale"}`,
    routeTone: (routeOk ? "emerald" : "amber") as ToneName,
    routeHint: routeOk ? null : "Modell-Route nicht bestätigt",
  };
}


export function isActionable(proposal: Proposal): boolean {
  return proposal.status === "proposed" && proposal.last_outcome !== "reverted_no_improvement";
}

export function isRevertedNoImprovement(proposal: Proposal): boolean {
  return proposal.status === "proposed" && proposal.last_outcome === "reverted_no_improvement";
}

export function splitAutoresearchProposals(proposals: Proposal[]) {
  return {
    actionable: proposals.filter(isActionable),
    reverted: proposals.filter(isRevertedNoImprovement),
    testing: proposals.filter((p) => p.status === "testing"),
    applied: proposals.filter((p) => p.status === "applied"),
    skipped: proposals.filter((p) => p.status === "skipped"),
    done: proposals.filter((p) => p.status === "applied" || p.status === "skipped"),
  };
}

const SAFETY_TERMS = ["safety", "security", "secret", "token", "credential", "warn", "risk"];
const QUICK_WIN_SECTIONS = new Set(["output", "procedure", "when to use", "examples", "safety"]);

function proposalHaystack(proposal: Proposal): string {
  return [proposal.target, proposal.section, proposal.title, proposal.rationale_plain, proposal.new_text]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
}

export function getProposalPriorityGroup(proposal: Proposal): ProposalPriorityGroup {
  const haystack = proposalHaystack(proposal);
  if (SAFETY_TERMS.some((term) => haystack.includes(term))) {
    return { key: "safety", label: "Safety-Lücke", tone: "amber", score: 0 };
  }
  if (proposal.mode === "code") {
    return { key: "code-gate", label: "Code-Gate", tone: "violet", score: 2 };
  }
  const section = proposal.section?.trim().toLowerCase();
  if (proposal.mode === "skill" && section && QUICK_WIN_SECTIONS.has(section)) {
    return { key: "quick-win", label: "Quick Win", tone: "emerald", score: 1 };
  }
  return { key: "other", label: "Weitere", tone: "zinc", score: 3 };
}

function proposalCreatedAt(proposal: Proposal): number {
  const value = proposal.created_at;
  if (typeof value === "number") return value;
  if (typeof value === "string") {
    const parsed = Date.parse(value);
    return Number.isFinite(parsed) ? parsed : 0;
  }
  return 0;
}

function proposalRankScore(proposal: Proposal): number | null {
  const value = proposal.rank_score;
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

export function rankAutoresearchProposals(proposals: Proposal[], limit = 10): RankedProposalQueue {
  const boundedLimit = Math.max(1, Math.round(limit));
  const ranked = proposals
    .filter(isActionable)
    .map((proposal) => ({ proposal, group: getProposalPriorityGroup(proposal) }))
    .sort((a, b) => {
      if (a.group.score !== b.group.score) return a.group.score - b.group.score;
      return proposalCreatedAt(b.proposal) - proposalCreatedAt(a.proposal);
    });
  const shortlist = ranked.slice(0, boundedLimit);
  const backlog = ranked.slice(boundedLimit);
  return {
    shortlist,
    backlog,
    summary: { total: ranked.length, shown: shortlist.length, remaining: backlog.length },
  };
}

/**
 * Pure selection helpers for the batch-confirm review queue.
 *
 * These were extracted out of AutoresearchView so the selection reduction is
 * unit-testable. The BLOCKER fix lives here: "Sichtbare auswählen" must only
 * select proposals the operator can actually see (the shortlist), never the
 * backlog proposals hidden behind a collapsed <details>. Passing only the
 * visible ids to `selectVisibleProposals` enforces that contract.
 */

/** Toggle a single proposal id in/out of the current selection (immutable). */
export function toggleProposalSelection(current: ReadonlySet<string>, proposalId: string, selected: boolean): Set<string> {
  const next = new Set(current);
  if (selected) next.add(proposalId);
  else next.delete(proposalId);
  return next;
}

/**
 * Select-all: returns a selection containing exactly the *visible* ids.
 * BLOCKER FIX — callers must pass only the visible (shortlist) ids, never the
 * full shortlist+backlog list, so collapsed backlog proposals can never be
 * batch-confirmed without the operator opening them.
 */
export function selectVisibleProposals(visibleIds: readonly string[]): Set<string> {
  return new Set(visibleIds);
}

/** Clear the whole selection. */
export function clearProposalSelection(): Set<string> {
  return new Set<string>();
}

/**
 * Keep only ids that still exist in the queue (prunes stale selections after a
 * reload). Order follows `validIds` so downstream batch calls stay stable.
 */
export function pruneProposalSelection(current: ReadonlySet<string>, validIds: readonly string[]): string[] {
  return validIds.filter((id) => current.has(id));
}

export function rankAutoresearchReviewQueue(proposals: Proposal[], limit = 10): RankedProposalQueue {
  const boundedLimit = Math.max(1, Math.round(limit));
  const ranked = proposals
    .filter(isActionable)
    .map((proposal) => ({ proposal, group: getProposalPriorityGroup(proposal) }))
    .sort((a, b) => {
      const aRank = proposalRankScore(a.proposal);
      const bRank = proposalRankScore(b.proposal);
      if (aRank !== null || bRank !== null) return (bRank ?? Number.NEGATIVE_INFINITY) - (aRank ?? Number.NEGATIVE_INFINITY);
      if (a.group.score !== b.group.score) return a.group.score - b.group.score;
      return proposalCreatedAt(b.proposal) - proposalCreatedAt(a.proposal);
    });
  const shortlist = ranked.slice(0, boundedLimit);
  const backlog = ranked.slice(boundedLimit);
  return {
    shortlist,
    backlog,
    summary: { total: ranked.length, shown: shortlist.length, remaining: backlog.length },
  };
}
