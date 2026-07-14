import type { AutoresearchRun, AutoresearchStatus, Proposal, ProposalSeverity, ToneName } from "./types";

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
  if (typeof proposal.operator_action_required === "boolean") return proposal.operator_action_required;
  return proposal.status === "proposed" && proposal.last_outcome !== "reverted_no_improvement";
}

export function isRevertedNoImprovement(proposal: Proposal): boolean {
  return proposal.status === "proposed" && proposal.last_outcome === "reverted_no_improvement";
}

export function splitAutoresearchProposals(proposals: Proposal[]) {
  const actionable = proposals.filter(isActionable);
  const delivery = proposals.filter((p) => !isActionable(p) && p.delivery_state !== "integrated" && (
    ["queued", "running", "review", "failed"].includes(p.delivery_state ?? "")
    || p.decision_owner === "kanban"
    || (p.delivery_state == null && ["testing", "routed_to_kanban", "pooled", "escalated"].includes(p.status))
  ));
  const integrated = proposals.filter((p) => p.delivery_state === "integrated" || p.status === "applied");
  const history = proposals.filter((p) => (
    !isActionable(p)
    && !delivery.includes(p)
    && !integrated.includes(p)
  ));
  return {
    actionable,
    reverted: proposals.filter(isRevertedNoImprovement),
    testing: proposals.filter((p) => p.status === "testing"),
    applied: proposals.filter((p) => p.status === "applied"),
    skipped: proposals.filter((p) => p.status === "skipped"),
    done: proposals.filter((p) => p.status === "applied" || p.status === "skipped"),
    delivery,
    integrated,
    history,
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

// ---------------------------------------------------------------------------
// Severity (critical|high|medium|low) — model-assigned with category fallback.
// Display + grouping/collapse dimension only; never drops a proposal. Mirrors
// the backend fallbacks in hermes_cli/{autoresearch_proposals,capability_researcher}.
// ---------------------------------------------------------------------------
export const SEVERITY_ORDER: Record<ProposalSeverity, number> = {
  critical: 4,
  high: 3,
  medium: 2,
  low: 1,
};

const CATEGORY_SEVERITY_FALLBACK: Record<string, ProposalSeverity> = {
  // code lane
  bug_risk: "high",
  dead_logic: "medium",
  error_handling: "medium",
  // skill lane
  contradiction: "critical",
  stale: "high",
  missing_trigger: "high",
  unclear_trigger: "medium",
  incomplete_steps: "medium",
  missing_section: "low",
};

const SEVERITY_TONE: Record<ProposalSeverity, ToneName> = {
  critical: "red",
  high: "amber",
  medium: "sky",
  low: "zinc",
};

export function getProposalSeverity(proposal: Proposal): ProposalSeverity {
  const raw = proposal.severity;
  if (raw && raw in SEVERITY_ORDER) return raw;
  const category = proposal.category?.trim();
  if (category && category in CATEGORY_SEVERITY_FALLBACK) return CATEGORY_SEVERITY_FALLBACK[category];
  return "medium";
}

export function severityRank(proposal: Proposal): number {
  return SEVERITY_ORDER[getProposalSeverity(proposal)];
}

export function severityTone(severity: ProposalSeverity): ToneName {
  return SEVERITY_TONE[severity];
}

/** critical+high stay open; medium+low are collapsed behind a "weitere" disclosure.
 *  Input order is preserved within each bucket (caller ranks first). */
export function partitionBySeverity(proposals: Proposal[]): { open: Proposal[]; collapsed: Proposal[] } {
  const open: Proposal[] = [];
  const collapsed: Proposal[] = [];
  for (const p of proposals) {
    (severityRank(p) >= SEVERITY_ORDER.high ? open : collapsed).push(p);
  }
  return { open, collapsed };
}

/** Keep only proposals at or above a severity threshold (for the "nur hoch+" chip). */
export function filterBySeverityThreshold(proposals: Proposal[], threshold: ProposalSeverity): Proposal[] {
  const min = SEVERITY_ORDER[threshold];
  return proposals.filter((p) => severityRank(p) >= min);
}

export interface SeverityDistribution {
  bySeverity: Record<ProposalSeverity, number>;
  byCategory: Record<string, number>;
  total: number;
}

/** Count proposals per severity tier and per category — computed from the
 *  already-polled list, no extra persistence. */
export function severityDistribution(proposals: Proposal[]): SeverityDistribution {
  const bySeverity: Record<ProposalSeverity, number> = { critical: 0, high: 0, medium: 0, low: 0 };
  const byCategory: Record<string, number> = {};
  for (const p of proposals) {
    bySeverity[getProposalSeverity(p)] += 1;
    const category = p.category?.trim();
    if (category) byCategory[category] = (byCategory[category] ?? 0) + 1;
  }
  return { bySeverity, byCategory, total: proposals.length };
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

// ---------------------------------------------------------------------------
// f-autoresearch-tab-driver: on-demand driver helpers
// Pure + colocated-tested. `last_run` is schema-typed as `unknown` (free-form
// summary blob), so the four research counters are read defensively here.
// ---------------------------------------------------------------------------

/** Observability + token counters carried (un-validated) on `status.last_run`. */
export interface LastRunCounters {
  skillsResearched: number | null;
  researchErrors: number | null;
  skillsWithFindings: number | null;
  researchTokens: number | null;
}

function coerceCounter(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/**
 * Pull the four research counters out of the free-form `last_run` blob. Returns
 * null per field when absent/non-numeric so the UI can tell "0 (real, converged)"
 * apart from "missing" — the whole point of the observability surface.
 */
export function readLastRunCounters(lastRun: unknown): LastRunCounters {
  const obj = lastRun && typeof lastRun === "object" ? (lastRun as Record<string, unknown>) : null;
  return {
    skillsResearched: coerceCounter(obj?.skills_researched),
    researchErrors: coerceCounter(obj?.research_errors),
    skillsWithFindings: coerceCounter(obj?.skills_with_findings),
    researchTokens: coerceCounter(obj?.research_tokens),
  };
}

/** True when any research counter is present (so the UI can show the row at all). */
export function hasResearchCounters(counters: LastRunCounters): boolean {
  return counters.skillsResearched !== null || counters.researchErrors !== null || counters.skillsWithFindings !== null;
}

/** Error badge shows only when the loop reported >=1 failed model call. */
export function shouldShowResearchErrorBadge(researchErrors: number | null | undefined): boolean {
  return typeof researchErrors === "number" && researchErrors > 0;
}

/** Token tile: real count, thousands-separated; 0/missing → "n/v" (never guess). */
export function formatResearchTokens(tokens: number | null | undefined): string {
  if (typeof tokens !== "number" || !Number.isFinite(tokens) || tokens <= 0) return "n/v";
  return Math.round(tokens).toLocaleString("de-DE");
}

/**
 * Parse the optional min_use_count override from the trigger input. Empty/invalid
 * → null (don't send; backend keeps its default of 5). Only a finite value > 0
 * passes through, mirroring the backend guard in start_runner.
 */
export function parseMinUseCount(input: string): number | null {
  const trimmed = input.trim();
  if (!trimmed) return null;
  const value = Number(trimmed);
  if (!Number.isFinite(value) || value <= 0) return null;
  return value;
}

// ---------------------------------------------------------------------------
// f-autoresearch-intensify (P2): run-history / ROI panel helpers (pure, tested).
// ---------------------------------------------------------------------------

/** Sum of MiniMax tokens across the shown runs (for the aggregate ROI line). */
export function sumRunTokens(runs: readonly AutoresearchRun[]): number {
  return runs.reduce((acc, r) => acc + (Number.isFinite(r.tokens) ? r.tokens : 0), 0);
}

const RUN_LANE_LABEL: Record<AutoresearchRun["lane"], string> = {
  code: "Code",
  skill: "Skills",
  "deep-audit": "Deep-Audit",
  test: "Test-Foundry",
};

const RUN_LANE_TONE: Record<AutoresearchRun["lane"], ToneName> = {
  code: "violet",
  skill: "cyan",
  "deep-audit": "amber",
  test: "emerald",
};

/** Human label + tone for a run lane. */
export function runLaneLabel(lane: AutoresearchRun["lane"]): string {
  return RUN_LANE_LABEL[lane] ?? "Skills";
}

export function runLaneTone(lane: AutoresearchRun["lane"]): ToneName {
  return RUN_LANE_TONE[lane] ?? "cyan";
}

export function runVetoedCount(run: Pick<AutoresearchRun, "vetoed">): number {
  return typeof run.vetoed === "number" && Number.isFinite(run.vetoed) ? Math.max(0, Math.round(run.vetoed)) : 0;
}

export function runModelLabel(run: Pick<AutoresearchRun, "model">): string | null {
  const model = run.model?.trim();
  return model ? model : null;
}

/**
 * Format a run timestamp for the panel. Accepts the ISO string the backend
 * writes; returns a short local date-time, or "—" for an empty/invalid value
 * (never throws on garbage).
 */
export function formatRunTime(at: string | null | undefined): string {
  if (!at) return "—";
  const ms = Date.parse(at);
  if (!Number.isFinite(ms)) return "—";
  return new Date(ms).toLocaleString("de-DE", { dateStyle: "short", timeStyle: "short" });
}

/**
 * The closed set of valid `area` values the Autoresearch trigger accepts
 * (mirrors AREA_ROOTS in scripts/autoresearch_request.py — any other value is
 * rejected with a 400). `scope` is the plain-German description of what each
 * area researches, shown in the dropdown + the targeting preview.
 */
export const AUTORESEARCH_AREAS = [
  { value: "all", scope: "alle Skills" },
  { value: "software-development", scope: "skills/software-development" },
  { value: "devops", scope: "skills/devops" },
  { value: "github", scope: "skills/github" },
  { value: "hermes-kanban", scope: "alle Kanban-Skills" },
  { value: "research", scope: "skills/research" },
  { value: "productivity", scope: "skills/productivity" },
  { value: "mlops", scope: "skills/mlops" },
  { value: "creative", scope: "skills/creative" },
  { value: "firecrawl", scope: "skills/firecrawl" },
  { value: "dashboard", scope: "Dashboard-Code (scripts + tests)" },
] as const;

/** Plain-German scope for an area value; falls back to the raw value if unknown. */
export function describeArea(value: string): string {
  return AUTORESEARCH_AREAS.find((a) => a.value === value)?.scope ?? value;
}

export type CodeWeaknessScope = "incremental" | "full" | "deep";

/**
 * Busy-key for the consolidated code-scan button. MUST match the key that
 * `generateCodeWeaknesses(variant)` sets in useControlData.ts, so the spinner
 * shows on the button only for the variant the operator just launched.
 */
export function codeWeaknessBusyKey(scope: CodeWeaknessScope): string {
  return scope === "full" ? "generate-code-full" : scope === "deep" ? "generate-code-deep" : "generate-code";
}

export function describeAutoresearchBusy(busy: string | null | undefined): string | null {
  if (!busy) return null;
  if (busy === "generate") return "Vorschläge werden erzeugt. Andere Autoresearch-Aktionen warten, bis die neuen Karten sichtbar sind.";
  if (busy === "generate-code") return "Geänderte Dateien werden auf Code-Risiken geprüft. Das kann kurz dauern.";
  if (busy === "generate-code-full") return "Vollscan läuft. Bitte warte, bis die neuen Code-Funde als Karten sichtbar sind.";
  if (busy === "generate-code-deep") return "Deep-Scan läuft. Das ist der längere Code-Scan; andere Aktionen bleiben pausiert.";
  if (busy === "confirm-batch") return "Auswahl wird übernommen. Einzelkarten bleiben gesperrt, bis alle markierten Vorschläge verarbeitet sind.";
  return "Eine Karte wird verarbeitet. Warte auf das Ergebnis, bevor du die nächste Entscheidung triffst.";
}

export interface RecentRunsSummary {
  runs: number;
  tokens: number;
  proposed: number;
  scanned: number;
}

export interface ProposalRoiSummary {
  applied: number;
  skipped: number;
  reverted: number;
  decided: number;
  acceptanceRate: number | null;
  tokensPerApplied: number | null;
}

export function proposalAgeDays(proposal: Proposal, now: number = Date.now()): number | null {
  const created = proposalCreatedAt(proposal);
  if (!Number.isFinite(created) || created <= 0) return null;
  const createdMs = created < 1_000_000_000_000 ? created * 1000 : created;
  const age = Math.floor((now - createdMs) / (24 * 60 * 60 * 1000));
  return Number.isFinite(age) && age >= 0 ? age : null;
}

export function summarizeProposalRoi(proposals: Proposal[], tokens: number): ProposalRoiSummary {
  const split = splitAutoresearchProposals(proposals);
  const applied = split.applied.length;
  const skipped = split.skipped.length;
  const reverted = split.reverted.length;
  const decided = applied + skipped + reverted;
  const safeTokens = Number.isFinite(tokens) && tokens > 0 ? tokens : 0;
  return {
    applied,
    skipped,
    reverted,
    decided,
    acceptanceRate: decided > 0 ? applied / decided : null,
    tokensPerApplied: applied > 0 ? safeTokens / applied : null,
  };
}

/**
 * Aggregate the runs whose `at` falls within the last `days` (default 7),
 * computed client-side from the already-polled runs[]. `now` is injectable for
 * deterministic tests. Runs with an unparseable/empty `at` are excluded; the
 * same finite guards as sumRunTokens apply per field. Proposal acceptance is
 * summarized separately from the current proposal store, not per run.
 */
export function summarizeRecentRuns(runs: readonly AutoresearchRun[], days = 7, now: number = Date.now()): RecentRunsSummary {
  const cutoff = now - days * 24 * 60 * 60 * 1000;
  const acc: RecentRunsSummary = { runs: 0, tokens: 0, proposed: 0, scanned: 0 };
  for (const r of runs) {
    const ms = Date.parse(r.at);
    if (!Number.isFinite(ms) || ms < cutoff) continue;
    acc.runs += 1;
    acc.tokens += Number.isFinite(r.tokens) ? r.tokens : 0;
    acc.proposed += Number.isFinite(r.proposed) ? r.proposed : 0;
    acc.scanned += Number.isFinite(r.scanned) ? r.scanned : 0;
  }
  return acc;
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
