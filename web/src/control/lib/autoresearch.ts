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

export function clampLoopIterations(value: number): number {
  if (!Number.isFinite(value)) return 1;
  return Math.max(1, Math.min(5, Math.round(value)));
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

export function rankAutoresearchProposals(proposals: Proposal[], limit = 10): RankedProposalQueue {
  const boundedLimit = Math.max(1, Math.round(limit));
  const ranked = proposals
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
