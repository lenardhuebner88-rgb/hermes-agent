// Decision Inbox — "Was braucht mich gerade?" across all surfaces.
//
// Pure aggregation (no React / no fetch): folds the three operator-decision streams
// the dashboard already derives elsewhere — open Autoresearch proposals, the FO
// backlog queue, and the Orchestrator interventions — into ONE priority-sorted list.
// Each row carries a surface badge, a why, a next-action and a navigation target.
// Unit-tested in decisionInbox.test.ts so the cross-surface ranking can't silently drift.

import type { Proposal, ToneName } from "./types";
import type { BacklogItem, KanbanDecision, KanbanDecisionKind } from "./schemas";
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

export type InboxSurface = "autoresearch" | "family" | "orchestrator" | "kanban";

export interface InboxItem {
  key: string;
  surface: InboxSurface;
  title: string;
  why: string;
  nextAction: string;
  tone: ToneName;
  target: string;
  weight: number;
  /** Verstrichene Sekunden, seit die Entscheidung anstand — speist die
   *  "vor Xm"-Anzeige auf TopDecision/Queue. Nur Quellen mit echtem
   *  Sekunden-Zeitstempel füllen es (Kanban `age_seconds`); fehlt es,
   *  bleibt das Alter leer statt erfunden zu werden. */
  ageSeconds?: number;
  /** K3: nur für `review_rejected` gesetzt — schaltet den Inline-Resolve
   *  ("Fix-Lauf starten": unblock → ready + Dispatcher-Tick) am CommandHome
   *  frei. Alle anderen Kinds lösen weiter über den Deep-Link auf. */
  fixTaskId?: string;
  /** R1: nur für `deliverable_posted_not_completed` gesetzt — schaltet den
   *  Inline-Repair ("Repair starten": POST /tasks/<id>/repair → blocked→done)
   *  am CommandHome frei. */
  repairTaskId?: string;
}

export interface InboxSummary {
  total: number;
  autoresearch: number;
  family: number;
  orchestrator: number;
  kanban: number;
}

const SEVERITY_WEIGHT: Record<string, number> = { critical: 95, high: 80, medium: 55, low: 40 };
const SURFACE_ORDER: Record<InboxSurface, number> = { autoresearch: 0, family: 1, orchestrator: 2, kanban: 3 };

// Kanban decision kinds → inbox weight + tone. A verifier rejection or a hard
// block needs an operator now (red/amber, high weight); a held/advisory state
// is lower-urgency (cyan). Unknown kinds (coerced by the schema) land mid-low.
const KANBAN_KIND_META: Record<KanbanDecisionKind, { weight: number; tone: ToneName }> = {
  review_rejected: { weight: 86, tone: "red" },
  budget_held: { weight: 78, tone: "amber" },
  operator_escalation: { weight: 92, tone: "red" },
  integration_parked: { weight: 82, tone: "amber" },
  rate_limited_loop: { weight: 80, tone: "amber" },
  release_gate_parked: { weight: 76, tone: "cyan" },
  tree_root_woke: { weight: 58, tone: "emerald" },
  sticky_blocked: { weight: 75, tone: "amber" },
  role_fit_held: { weight: 55, tone: "cyan" },
  decompose_failed: { weight: 52, tone: "cyan" },
  stranded_by_stuck_parent: { weight: 50, tone: "cyan" },
  deliverable_posted_not_completed: { weight: 84, tone: "amber" },
};

const KANBAN_KIND_LABELS: Record<KanbanDecisionKind, string> = {
  review_rejected: "Verifier: Änderungen gefordert",
  budget_held: "Budget-Limit erreicht",
  operator_escalation: "Operator-Eskalation",
  integration_parked: "Integration geparkt",
  rate_limited_loop: "Rate-Limit-Schleife",
  release_gate_parked: "Release-Gate geparkt",
  tree_root_woke: "Root bereit zur Finalisierung",
  sticky_blocked: "Blockiert — Unblock nötig",
  role_fit_held: "Rolle passt nicht",
  decompose_failed: "Decompose fehlgeschlagen",
  stranded_by_stuck_parent: "Wartet auf blockierten Vorgänger",
  deliverable_posted_not_completed: "Deliverable da — Repair nötig",
};

// Interventions that merely SUMMARIZE a surface already enumerated per-item above
// would double-count the inbox total: `open-proposals` is one row saying "5 offen"
// on TOP of the 5 actionable-proposal rows from section 1, so 5 decisions read as 6
// and the headline never matches reality. We drop such summaries HERE (in the inbox
// fold) only — they remain valid in the AgentOps summary view, which is not per-item.
const REDUNDANT_INTERVENTION_IDS = new Set<string>(["open-proposals"]);

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
  kanbanDecisions?: KanbanDecision[];
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
      // Deep-link to the exact proposal card, not the whole tab — AutoresearchView
      // reads ?focus and scrolls/focuses `autoresearch-proposal-${id}`.
      target: `/control/autoresearch?focus=${encodeURIComponent(p.id)}`,
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
      // Deep-link to the exact backlog item — BacklogView reads ?focus and opens
      // that item's detail drawer.
      target: `/control/backlog?focus=${encodeURIComponent(candidate.item.id)}`,
      weight: w,
    });
  }

  // 3) Orchestrator — interventions are already the "needs an operator" set.
  //    Skip summaries that duplicate a per-item surface (see REDUNDANT_INTERVENTION_IDS).
  for (const iv of input.interventions) {
    if (REDUNDANT_INTERVENTION_IDS.has(iv.id)) continue;
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

  // 4) Kanban — consolidated decision queue from the gateway (E1). Each row is
  //    already one operator decision; map kind → weight/tone and deep-link to
  //    the task in the backlog tab. suggested_command rides along as a copy-hint.
  for (const d of input.kanbanDecisions ?? []) {
    if (!d.task_id) continue;
    const meta = KANBAN_KIND_META[d.kind] ?? { weight: 50, tone: "cyan" as ToneName };
    const label = KANBAN_KIND_LABELS[d.kind] ?? d.kind;
    items.push({
      key: `kanban:${d.kind}:${d.task_id}`,
      surface: "kanban",
      title: d.title || d.task_id,
      why: [label, d.reason].filter(Boolean).join(" · "),
      nextAction: d.suggested_command || "Im Board entscheiden",
      tone: meta.tone,
      // Deep-link to the task in the Board/backlog tab (reads ?focus).
      target: `/control/backlog?focus=${encodeURIComponent(d.task_id)}`,
      weight: meta.weight,
      // Gateway liefert age_seconds bereits mit (kanban_db._age) — durchreichen,
      // damit die TopDecision "vor Xm" zeigen kann. null → kein Alter.
      ...(d.age_seconds != null ? { ageSeconds: d.age_seconds } : {}),
      // K3: a verifier rejection has ONE dominant resolution (fix run by the
      // owning coder — the retry sees the verifier feedback in its
      // worker_context), so it earns the inline action.
      ...(d.kind === "review_rejected" ? { fixTaskId: d.task_id } : {}),
      // R1: a posted-but-uncompleted deliverable has ONE dominant resolution —
      // close the missing kanban_complete step — so it earns its inline repair.
      ...(d.kind === "deliverable_posted_not_completed" ? { repairTaskId: d.task_id } : {}),
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
    kanban: items.filter((i) => i.surface === "kanban").length,
  };
}
