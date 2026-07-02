import { getProposalPriorityGroup, getProposalSeverity, isActionable, SEVERITY_ORDER, severityTone, type ProposalPriorityGroup } from "./autoresearch";
import type { Proposal, ProposalMode, ProposalSeverity, ToneName } from "./types";

export interface ProposalGroup {
  key: string;
  mode: ProposalMode;
  category: string | null;
  target: string;
  targetLabel: string;
  categoryLabel: string;
  title: string;
  proposals: Proposal[];
  ids: string[];
  count: number;
  severity: ProposalSeverity;
  tone: ToneName;
  priorityGroup: ProposalPriorityGroup;
}

export interface RankedProposalGroupQueue {
  shortlist: ProposalGroup[];
  backlog: ProposalGroup[];
  summary: { total: number; shown: number; remaining: number; proposalCount: number };
}

function normalizeKeyPart(value: string | null | undefined, fallback: string): string {
  const trimmed = value?.trim();
  return trimmed ? trimmed : fallback;
}

function titleCaseToken(value: string): string {
  return value ? `${value.charAt(0).toUpperCase()}${value.slice(1)}` : value;
}

function displayCategory(category: string | null): string {
  const raw = category?.trim();
  if (!raw) return "Vorschlag";
  const words = raw.split(/[_\s-]+/).filter(Boolean);
  return words.length === 0 ? "Vorschlag" : [titleCaseToken(words[0]), ...words.slice(1)].join(" ");
}

function displayTarget(target: string): string {
  const raw = target.trim();
  if (!raw) return "unbekanntes Ziel";
  const parts = raw.split(/[\\/]/).filter(Boolean);
  return parts[parts.length - 1] ?? raw;
}

function proposalGroupKey(proposal: Proposal): string {
  return [
    normalizeKeyPart(proposal.mode, "skill"),
    normalizeKeyPart(proposal.category, "uncategorized"),
    normalizeKeyPart(proposal.target, "unknown-target"),
  ].join("::");
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

function groupSeverity(proposals: readonly Proposal[]): ProposalSeverity {
  return proposals.reduce<ProposalSeverity>((max, proposal) => {
    const severity = getProposalSeverity(proposal);
    return SEVERITY_ORDER[severity] > SEVERITY_ORDER[max] ? severity : max;
  }, "low");
}

function makeProposalGroup(key: string, proposals: Proposal[]): ProposalGroup {
  const first = proposals[0];
  const category = first.category?.trim() || null;
  const target = first.target?.trim() || "";
  const severity = groupSeverity(proposals);
  const categoryLabel = displayCategory(category);
  const targetLabel = displayTarget(target);
  const count = proposals.length;
  return {
    key,
    mode: first.mode,
    category,
    target,
    targetLabel,
    categoryLabel,
    title: `${categoryLabel} in ${targetLabel} - ${count} ${count === 1 ? "Vorschlag" : "Vorschläge"}`,
    proposals,
    ids: proposals.map((proposal) => proposal.id),
    count,
    severity,
    tone: severityTone(severity),
    priorityGroup: getProposalPriorityGroup(first),
  };
}

export function groupAutoresearchProposals(proposals: readonly Proposal[]): ProposalGroup[] {
  const grouped = new Map<string, Proposal[]>();
  for (const proposal of proposals) {
    const key = proposalGroupKey(proposal);
    const bucket = grouped.get(key);
    if (bucket) bucket.push(proposal);
    else grouped.set(key, [proposal]);
  }
  return Array.from(grouped, ([key, bucket]) => makeProposalGroup(key, bucket));
}

export function rankAutoresearchProposalGroups(proposals: readonly Proposal[], limit = 10): RankedProposalGroupQueue {
  const boundedLimit = Math.max(1, Math.round(limit));
  const ranked = proposals
    .filter(isActionable)
    .sort((a, b) => {
      const aRank = proposalRankScore(a);
      const bRank = proposalRankScore(b);
      if (aRank !== null || bRank !== null) return (bRank ?? Number.NEGATIVE_INFINITY) - (aRank ?? Number.NEGATIVE_INFINITY);
      const aGroup = getProposalPriorityGroup(a);
      const bGroup = getProposalPriorityGroup(b);
      if (aGroup.score !== bGroup.score) return aGroup.score - bGroup.score;
      return proposalCreatedAt(b) - proposalCreatedAt(a);
    });
  const groups = groupAutoresearchProposals(ranked);
  const shortlist = groups.slice(0, boundedLimit);
  const backlog = groups.slice(boundedLimit);
  return {
    shortlist,
    backlog,
    summary: {
      total: groups.length,
      shown: shortlist.length,
      remaining: backlog.length,
      proposalCount: ranked.length,
    },
  };
}
