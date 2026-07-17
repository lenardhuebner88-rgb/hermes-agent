import { useMemo } from "react";
import { fetchJSON } from "@/lib/api";
import { DecisionQueueResponseSchema, parseOrThrow } from "../lib/schemas";
import type { DecisionQueueResponse } from "../lib/schemas";
import { buildAgentOpsSnapshot, type AgentOpsSnapshot } from "../lib/agentOps";
import { buildDecisionInbox, inboxSummary, type InboxItem, type InboxSummary } from "../lib/decisionInbox";
import { nowSec } from "../lib/derive";
import type { ToneName } from "../lib/types";
import { usePolling } from "./internal";
import { useProposals } from "./proposalsDeepAudit";
import { useBacklog, useOrchestrationBacklog } from "./backlogOrchestration";
import { useHermesWorkers } from "./workersBoard";
import { useHermesRecentResults } from "./runsDigestRollup";
import { useSystemHealth, useMetricsLite } from "./systemReleaseHealth";

// ── Decision Inbox — the single source for "Was braucht mich?" ─────────────
// Before f5 this exact pipeline (snapshot → buildDecisionInbox → inboxSummary)
// was rebuilt independently in OverviewView, InboxView AND the tab badge, so the
// count could drift between surfaces. One hook now owns it; every consumer reads
// the SAME list and the SAME total. Polls are deduped by pollingStore, so mounting
// this in several places costs no extra requests.
export interface DecisionInboxData {
  items: InboxItem[];
  summary: InboxSummary;
  snapshot: AgentOpsSnapshot;
  /** Worst tone present (drives the hero mood + count colour). */
  worstTone: ToneName;
  loading: boolean;
  /** Per-source load errors, labelled, ready to show as a callout. */
  sourceErrors: string[];
}


const INBOX_TONE_RANK: Record<ToneName, number> = {
  red: 5, rose: 5, amber: 4, cyan: 2, sky: 2, indigo: 2, violet: 2, emerald: 1, zinc: 0,
};


// N-E1/E2: the consolidated kanban decision queue (sticky_blocked, review_rejected,
// role_fit_held, budget_held, decompose_failed, stranded_by_stuck_parent).
// 404/error → the Inbox simply renders without a Kanban section (no crash).
export function useKanbanDecisionQueue() {
  return usePolling<DecisionQueueResponse>(
    "kanban/decision-queue",
    async () => parseOrThrow(
      DecisionQueueResponseSchema,
      await fetchJSON<unknown>("/api/plugins/kanban/decision-queue"),
      "kanban/decision-queue",
    ),
    15000,
  );
}


export function useDecisionInbox(): DecisionInboxData {
  const proposals = useProposals();
  const backlog = useBacklog();
  const workers = useHermesWorkers();
  const results = useHermesRecentResults();
  const health = useSystemHealth();
  const metrics = useMetricsLite();
  const orchestration = useOrchestrationBacklog();
  const kanbanDecisions = useKanbanDecisionQueue();
  const now = nowSec();

  const snapshot = useMemo(
    () =>
      buildAgentOpsSnapshot({
        workers: workers.data?.workers ?? [],
        results: results.data?.results ?? [],
        proposals: proposals.proposals,
        orchestrationItems: orchestration.data?.items ?? [],
        contractHealth: orchestration.data?.contract_health,
        systemHealth: health.data,
        metrics: metrics.data,
        nowSec: orchestration.data?.checked_at ?? now,
      }),
    // `now` is intentionally NOT a dependency: it is only a render-time fallback
    // for a missing payload `checked_at`. Including it forced this 8-source
    // aggregation to recompute on EVERY render — and ControlPage re-renders on
    // the 5s workers/health/metrics poll cadence even when nothing changed.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [workers.data, results.data, proposals.proposals, orchestration.data, health.data, metrics.data],
  );

  const items = useMemo(
    () =>
      buildDecisionInbox({
        proposals: proposals.proposals,
        foItems: backlog.data?.items ?? [],
        foNowSec: backlog.data?.checked_at ?? now,
        interventions: snapshot.interventions,
        kanbanDecisions: kanbanDecisions.data?.decisions ?? [],
      }),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `now` is a render-time fallback only (see snapshot memo above)
    [proposals.proposals, backlog.data, snapshot.interventions, kanbanDecisions.data],
  );

  const summary = useMemo(() => inboxSummary(items), [items]);
  const worstTone = useMemo(
    () => items.reduce<ToneName>((worst, it) => (INBOX_TONE_RANK[it.tone] > INBOX_TONE_RANK[worst] ? it.tone : worst), "emerald"),
    [items],
  );

  const sourceErrors = [
    proposals.error ? `Autoresearch: ${proposals.error}` : "",
    backlog.error ? `Family: ${backlog.error}` : "",
    orchestration.error ? `Orchestrator: ${orchestration.error}` : "",
  ].filter(Boolean);

  const loading = proposals.loading || backlog.loading || orchestration.loading;

  return { items, summary, snapshot, worstTone, loading, sourceErrors };
}

// ---------------------------------------------------------------------------
// Bibliothek-Badge (2026-06-11): Zahl neuer Lesesaal-Einträge seit dem letzten
// Besuch. Der Besuchs-Zeitstempel ist der localStorage-Schlüssel der
// BibliothekView (sie stempelt beim Mount); hier nur lesen. Leichtgewichtiger
// Listen-Poll ohne Bodies über den geteilten pollingStore.
// ---------------------------------------------------------------------------

