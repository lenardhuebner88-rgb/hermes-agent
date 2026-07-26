/**
 * Fleet → Plan: vollständiges, filterbares PlanSpec-Register.
 *
 * Der Tab zeigt Plan-Zustände und Übergabereife. Worker-Telemetrie, Kosten,
 * Kettenverläufe und Risikoerklärungen bleiben in ihren eigenen Fleet-Tabs.
 */
import { useMemo, useState } from "react";
import { FilePlus2, Search } from "lucide-react";

import type { PlanSpecsResponse } from "../../lib/schemas";
import { PlanComposer } from "../../components/fleet/PlanComposer";
import { AutoReleaseTile } from "../../components/fleet/AutoReleaseTile";
import { SignalChip, type SignalTone } from "../../components/leitstand";
import type { PlanSpecRecord } from "./shared";

const PLAN_COMPOSER_EXPANDED_KEY = "fleet-plan-composer-expanded";

export type PlanSegment = "all" | "draft" | "ready" | "attention" | "held" | "board" | "done";
type PlanActionState = NonNullable<PlanSpecRecord["action_state"]>;
type PlanSort = "state" | "findings" | "agent" | "board";

interface PlanTabProps {
  allPlanspecs: PlanSpecRecord[];
  summary?: PlanSpecsResponse["summary"] | null;
  costs?: unknown;
  lanesCatalog?: unknown;
  accountUsage?: unknown;
  onOpenTask?: (taskId: string) => void;
  onApproveSuccess: () => void;
  onShowDetail: (ps: PlanSpecRecord) => void;
  selectedPath?: string | null;
  segment?: PlanSegment;
  query?: string;
  onSegmentChange?: (segment: PlanSegment) => void;
  onQueryChange?: (query: string) => void;
  readOnly?: boolean;
  loading?: boolean;
  error?: string | null;
  onRetry?: () => void | Promise<void>;
}

function planState(item: PlanSpecRecord): PlanActionState {
  if (item.action_state) return item.action_state;
  if (item.kanban_state === "archived") return "archived";
  if (item.kanban_state === "completed" || item.kanban_state === "done") return "completed";
  if (item.kanban_state === "blocked") return "blocked";
  if (item.kanban_state === "running") return "running";
  if (item.kanban_root_task_id) {
    return item.freigabe === "operator"
      && item.kanban_state === "queued"
      && item.kanban_root_status === "scheduled"
      ? "held"
      : "handed_off";
  }
  if (!item.valid) return "draft";
  return item.ingest_would_block ? "blocked" : "ready";
}

const STATE_META: Record<PlanActionState, { label: string; tone: SignalTone }> = {
  draft: { label: "Entwurf", tone: "neutral" },
  ready: { label: "Bereit", tone: "ok" },
  held: { label: "Gehalten", tone: "warn" },
  handed_off: { label: "Übergeben", tone: "neutral" },
  running: { label: "Läuft", tone: "neutral" },
  blocked: { label: "Blockiert", tone: "alert" },
  completed: { label: "Fertig", tone: "ok" },
  archived: { label: "Archiv", tone: "neutral" },
};

function matchesSegment(item: PlanSpecRecord, state: PlanActionState, segment: PlanSegment): boolean {
  if (segment === "all") return true;
  if (segment === "draft" || segment === "ready" || segment === "held") return state === segment;
  if (segment === "attention") return state === "blocked" && !item.kanban_root_task_id;
  if (segment === "board") {
    return ["handed_off", "running"].includes(state)
      || (state === "blocked" && Boolean(item.kanban_root_task_id));
  }
  return state === "completed" || state === "archived";
}

const STATE_SORT_ORDER: Record<PlanActionState, number> = {
  blocked: 0,
  ready: 1,
  held: 2,
  draft: 3,
  running: 4,
  handed_off: 5,
  completed: 6,
  archived: 7,
};

function hasFindings(item: PlanSpecRecord): boolean {
  return (item.finding_count ?? 0) > 0 || item.errors.length > 0 || item.ingest_findings.length > 0;
}

function comparePlanText(left: string | null | undefined, right: string | null | undefined): number {
  const a = left?.trim() ?? "";
  const b = right?.trim() ?? "";
  if (!a && !b) return 0;
  if (!a) return 1;
  if (!b) return -1;
  return a.localeCompare(b, "de", { sensitivity: "base" });
}

function comparePlans(left: PlanSpecRecord, right: PlanSpecRecord, sort: PlanSort): number {
  if (sort === "state") {
    return STATE_SORT_ORDER[planState(left)] - STATE_SORT_ORDER[planState(right)];
  }
  if (sort === "findings") {
    return Number(hasFindings(right)) - Number(hasFindings(left));
  }
  if (sort === "agent") {
    return comparePlanText(left.agent, right.agent);
  }
  return comparePlanText(left.target_board, right.target_board);
}

/**
 * Lade-Skeleton (L3-Spec 3.4, Befund 6): 5 Row-Skeletons analog zum Board,
 * reines CSS-Markup auf .hc-skeleton — ersetzt den Text-Empty-Ladezustand.
 */
function PlanSkeleton() {
  return (
    <div className="fleet-skel" aria-live="polite" aria-busy="true">
      <span className="sr-only">Plan-Register wird geladen …</span>
      {Array.from({ length: 5 }).map((_, i) => (
        <div key={i} className="fleet-skel-row" aria-hidden>
          <div className="fleet-skel-lines">
            <div className="hc-skeleton fleet-skel-bar" />
            <div className="hc-skeleton fleet-skel-bar fleet-skel-bar-short" />
          </div>
        </div>
      ))}
    </div>
  );
}

export function PlanTab({
  allPlanspecs,
  summary,
  onApproveSuccess,
  onShowDetail,
  selectedPath = null,
  segment: controlledSegment,
  query: controlledQuery,
  onSegmentChange,
  onQueryChange,
  readOnly = false,
  loading = false,
  error = null,
  onRetry,
}: PlanTabProps) {
  const [localSegment, setLocalSegment] = useState<PlanSegment>("all");
  const [localQuery, setLocalQuery] = useState("");
  const [sort, setSort] = useState<PlanSort>("state");
  const [visibleLimit, setVisibleLimit] = useState(60);
  const segment = controlledSegment ?? localSegment;
  const query = controlledQuery ?? localQuery;
  const [composerExpanded, setComposerExpanded] = useState(() => {
    if (typeof window === "undefined") return false;
    return window.localStorage.getItem(PLAN_COMPOSER_EXPANDED_KEY) === "true";
  });

  const derivedCounts = useMemo(() => {
    const counts = {
      draft: 0, ready: 0, held: 0, handed_off: 0,
      running: 0, blocked: 0, completed: 0, archived: 0,
    };
    for (const item of allPlanspecs) counts[planState(item)] += 1;
    return counts;
  }, [allPlanspecs]);
  const counts = summary ?? { ...derivedCounts, total_matching: allPlanspecs.length, observed_at: 0 };

  const visible = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase();
    return allPlanspecs
      .map((item, sourceIndex) => ({ item, sourceIndex }))
      .filter(({ item }) => {
        const state = planState(item);
        if (!matchesSegment(item, state, segment)) return false;
        if (!normalizedQuery) return true;
        return `${item.topic} ${item.filename} ${item.agent} ${item.path} ${item.target_board ?? ""}`
          .toLowerCase()
          .includes(normalizedQuery);
      })
      .sort((left, right) => comparePlans(left.item, right.item, sort) || left.sourceIndex - right.sourceIndex)
      .map(({ item }) => item);
  }, [allPlanspecs, query, segment, sort]);
  const rendered = visible.slice(0, visibleLimit);

  const blockedBeforeHandoff = allPlanspecs.filter(
    (item) => planState(item) === "blocked" && !item.kanban_root_task_id,
  ).length;
  const blockedOnBoard = counts.blocked - blockedBeforeHandoff;
  const boardCount = counts.handed_off + counts.running + blockedOnBoard;
  const doneCount = counts.completed + counts.archived;
  const segments: Array<[PlanSegment, string, number]> = [
    ["all", "Alle", counts.total_matching],
    ["draft", "Entwurf", counts.draft],
    ["ready", "Bereit", counts.ready],
    ["attention", "Klärung", blockedBeforeHandoff],
    ["held", "Gehalten", counts.held],
    ["board", "Im Board", boardCount],
    ["done", "Erledigt", doneCount],
  ];

  return (
    <div className="fleet-plantab">
      <div className="fleet-plan-tools">
        {!readOnly ? (
          <button
            type="button"
            className="fleet-plan-new"
            aria-expanded={composerExpanded}
            aria-controls="fleet-plan-composer-body"
            onClick={() => {
              setComposerExpanded((current) => {
                const next = !current;
                window.localStorage.setItem(PLAN_COMPOSER_EXPANDED_KEY, String(next));
                return next;
              });
            }}
          >
            <FilePlus2 aria-hidden size={15} />
            {composerExpanded ? "Composer schließen" : "Neuer Plan"}
          </button>
        ) : <span className="fleet-readonly-note">Fremd-Board · nur lesen</span>}
        {!readOnly ? <AutoReleaseTile compact onOpenTask={undefined} /> : null}
      </div>

      {composerExpanded && !readOnly ? (
        <div id="fleet-plan-composer-body">
          <PlanComposer onIngestSuccess={onApproveSuccess} />
        </div>
      ) : null}

      <div className="fleet-boardtab-register fleet-plan-register" role="tablist" aria-label="Plan-Register">
        {segments.map(([id, label, count]) => (
          <button
            key={id}
            type="button"
            role="tab"
            aria-selected={segment === id}
            className={segment === id ? "is-active" : ""}
            onClick={() => {
              if (controlledSegment === undefined) setLocalSegment(id);
              onSegmentChange?.(id);
              setVisibleLimit(60);
            }}
          >
            {label}<span>{count}</span>
          </button>
        ))}
      </div>

      <div className="fleet-boardtab-filter">
        <label className="fleet-plan-search">
          <Search aria-hidden size={15} />
          <input
            type="search"
            value={query}
            onChange={(event) => {
              if (controlledQuery === undefined) setLocalQuery(event.target.value);
              onQueryChange?.(event.target.value);
              setVisibleLimit(60);
            }}
            placeholder="Plan, Agent oder Board suchen …"
            aria-label="PlanSpecs durchsuchen"
          />
        </label>
        <select
          className="fleet-boardtab-select"
          value={sort}
          onChange={(event) => {
            setSort(event.target.value as PlanSort);
            setVisibleLimit(60);
          }}
          aria-label="Pläne sortieren"
        >
          <option value="state">Zustand</option>
          <option value="findings">Befunde vorhanden</option>
          <option value="agent">Agent</option>
          <option value="board">Board</option>
        </select>
      </div>

      {loading && allPlanspecs.length === 0 ? (
        <PlanSkeleton />
      ) : error && allPlanspecs.length === 0 ? (
        <div className="hc-fleet-empty fleet-empty-error" role="alert">
          <span className="hc-fleet-empty-title">Plan-Register nicht verfügbar</span>
          <span className="hc-fleet-empty-desc">{error}</span>
          {onRetry ? <button type="button" className="fleet-list-expander" onClick={() => void onRetry()}>Erneut laden</button> : null}
        </div>
      ) : visible.length === 0 ? (
        <div className="hc-fleet-empty">
          <span className="hc-fleet-empty-title">
            {allPlanspecs.length === 0 ? "Noch keine PlanSpecs" : "Keine Treffer"}
          </span>
          <span className="hc-fleet-empty-desc">
            {allPlanspecs.length === 0
              ? "Neue Pläne entstehen im Composer oder als bindende Vault-PlanSpec."
              : "Register oder Suche anpassen."}
          </span>
        </div>
      ) : (
        <ul className="fleet-plan-list" aria-label="PlanSpecs">
          {rendered.map((item) => {
            const state = planState(item);
            const meta = STATE_META[state];
            const taskCount = item.kanban_child_total || item.subtask_count;
            return (
              <li key={item.path}>
                <button
                  type="button"
                  className={`fleet-plan-row${selectedPath === item.path ? " is-selected" : ""}`}
                  onClick={() => onShowDetail(item)}
                  aria-current={selectedPath === item.path ? "true" : undefined}
                >
                  <span className="fleet-plan-row-main">
                    <span className="fleet-plan-row-title">{item.topic || item.filename}</span>
                    <span className="fleet-plan-row-meta">
                      <span>{item.agent || "dashboard"}</span>
                      <span>{taskCount} Schritte</span>
                      {(item.dependency_count ?? 0) > 0 ? <span>{item.dependency_count} Abhängigkeiten</span> : null}
                      {(item.finding_count ?? 0) > 0 ? <span>{item.finding_count} Befunde</span> : null}
                      {item.target_board ? <span>→ {item.target_board}</span> : null}
                    </span>
                    {item.action_reason ? <span className="fleet-plan-row-reason">{item.action_reason}</span> : null}
                  </span>
                  <SignalChip tone={meta.tone} label={meta.label} />
                </button>
              </li>
            );
          })}
        </ul>
      )}
      {rendered.length < visible.length ? (
        <button type="button" className="fleet-list-expander" onClick={() => setVisibleLimit((current) => current + 60)}>
          {rendered.length} von {visible.length} · weitere 60 anzeigen
        </button>
      ) : null}
      {allPlanspecs.length < counts.total_matching ? (
        <p className="fleet-plan-hint" role="status">
          {allPlanspecs.length} von {counts.total_matching} Quellen geladen.
        </p>
      ) : null}
    </div>
  );
}
