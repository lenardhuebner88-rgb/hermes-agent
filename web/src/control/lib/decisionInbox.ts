// Decision Inbox — "Was braucht mich gerade?" across all surfaces.
//
// Pure aggregation (no React / no fetch): folds the three operator-decision streams
// the dashboard already derives elsewhere — open Autoresearch proposals, the FO
// backlog queue, and the Orchestrator interventions — into ONE priority-sorted list.
// Each row carries a surface badge, a why, a next-action and a navigation target.
// Unit-tested in decisionInbox.test.ts so the cross-surface ranking can't silently drift.

import type { Proposal, ToneName } from "./types";
import type { BacklogItem } from "./schemas";
import { getProposalSeverity, isActionable } from "./autoresearch";
import {
  FO_REASON_LABELS,
  isFoItemStale,
  nextActionForFoItem,
  queueStateForFoItem,
  rankedQueueWithReasons,
  reasonCodesForFoItem,
} from "./foBacklog";
import type { AgentOpsIntervention } from "./agentOps";

export type InboxSurface = "autoresearch" | "family" | "orchestrator";

export interface InboxItem {
  key: string;
  surface: InboxSurface;
  title: string;
  why: string;
  nextAction: string;
  tone: ToneName;
  target: string;
  weight: number;
}

export interface InboxSummary {
  total: number;
  autoresearch: number;
  family: number;
  orchestrator: number;
}

const SEVERITY_WEIGHT: Record<string, number> = { critical: 95, high: 80, medium: 55, low: 40 };
const SURFACE_ORDER: Record<InboxSurface, number> = { autoresearch: 0, family: 1, orchestrator: 2 };

function proposalTone(severity: string): ToneName {
  if (severity === "critical") return "red";
  if (severity === "high") return "amber";
  return "cyan";
}

function interventionWeight(tone: ToneName): number {
  if (tone === "red") return 88;
  if (tone === "amber") return 68;
  return 50;
}

// FO backlog: only items that represent a fresh operator decision earn a row.
// `later`/`done` are not inbox-worthy; a missing owner or a stale claim lifts the floor.
function foWeight(item: BacklogItem): number {
  const state = queueStateForFoItem(item).state;
  let w = 0;
  if (state === "blocked") w = 90;
  else if (state === "now") w = 85;
  else if (state === "next") w = 60;
  else if (state === "in_progress") w = 40;
  if (item.owner === "unassigned" || !item.owner) w = Math.max(w, 70);
  if (w > 0 && isFoItemStale(item)) w += 8;
  return w;
}

export function buildDecisionInbox(input: {
  proposals: Proposal[];
  foItems: BacklogItem[];
  foNowSec: number;
  interventions: AgentOpsIntervention[];
}): InboxItem[] {
  const items: InboxItem[] = [];

  // 1) Autoresearch — every open, actionable proposal is a decision.
  for (const p of input.proposals) {
    if (!isActionable(p)) continue;
    const severity = getProposalSeverity(p);
    items.push({
      key: `ar:${p.id}`,
      surface: "autoresearch",
      title: p.title?.trim() || p.target,
      why: [p.category ?? (p.mode === "code" ? "Code-Änderung" : "Skill"), severity].filter(Boolean).join(" · "),
      nextAction: "Prüfen & entscheiden",
      tone: proposalTone(severity),
      target: "/control/autoresearch",
      weight: SEVERITY_WEIGHT[severity] ?? 40,
    });
  }

  // 2) Family Organizer — blocked / now / next / unowned / stale active items.
  for (const candidate of rankedQueueWithReasons(input.foItems, input.foNowSec)) {
    const w = foWeight(candidate.item);
    if (w <= 0) continue;
    const reasons = reasonCodesForFoItem(candidate.item, input.foNowSec)
      .map((code) => FO_REASON_LABELS[code] ?? code)
      .slice(0, 2)
      .join(" · ");
    items.push({
      key: `fo:${candidate.item.id}`,
      surface: "family",
      title: candidate.item.title,
      why: reasons || (candidate.item.area || "Family Organizer"),
      nextAction: nextActionForFoItem(candidate.item),
      tone: w >= 85 ? "red" : w >= 65 ? "amber" : "cyan",
      target: "/control/backlog",
      weight: w,
    });
  }

  // 3) Orchestrator — interventions are already the "needs an operator" set.
  for (const iv of input.interventions) {
    items.push({
      key: `orch:${iv.id}`,
      surface: "orchestrator",
      title: iv.title,
      why: iv.detail,
      nextAction: "Im Orchestrator entscheiden",
      tone: iv.tone,
      target: iv.target,
      weight: interventionWeight(iv.tone),
    });
  }

  return items.sort(
    (a, b) =>
      b.weight - a.weight ||
      SURFACE_ORDER[a.surface] - SURFACE_ORDER[b.surface] ||
      a.key.localeCompare(b.key),
  );
}

export function inboxSummary(items: InboxItem[]): InboxSummary {
  return {
    total: items.length,
    autoresearch: items.filter((i) => i.surface === "autoresearch").length,
    family: items.filter((i) => i.surface === "family").length,
    orchestrator: items.filter((i) => i.surface === "orchestrator").length,
  };
}
