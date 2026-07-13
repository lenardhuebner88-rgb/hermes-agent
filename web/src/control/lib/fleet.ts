/**
 * Fleet stage model — the operator-facing pipeline over the real Kanban
 * status lifecycle. This is the SINGLE source of truth that maps the eight
 * kanban statuses (triage/todo/scheduled/ready/running/blocked/review/done)
 * onto the five operator stages (Capture → Plan → Execute → Verify → Ship)
 * and decides which transition each Fleet button triggers via
 * ``PATCH /api/plugins/kanban/tasks/{id}``.
 *
 * Pure + framework-neutral so the stage maths is unit-tested without React.
 * Nothing here calls the API — it only describes WHAT a button should do.
 */
import type { ToneName, TaskStatus, WorkerProfile, ReviewTier, ActiveReviewStage } from "./types";

export type FleetStage = "capture" | "plan" | "execute" | "verify" | "ship";

export const FLEET_STAGES: FleetStage[] = ["capture", "plan", "execute", "verify", "ship"];

export interface StageMeta {
  id: FleetStage;
  /** Pipeline label — kept as the canonical English stage name (matches the design). */
  label: string;
  /** One-line German purpose shown under the stage in the rail. */
  purpose: string;
  tone: ToneName;
  /** Kanban statuses that live in this stage. */
  statuses: TaskStatus[];
}

export const STAGE_META: Record<FleetStage, StageMeta> = {
  capture: { id: "capture", label: "Capture", purpose: "Eingang · Aufgabe erfasst", tone: "zinc", statuses: ["triage"] },
  plan: { id: "plan", label: "Flow-Kette", purpose: "Gehalten · wartet auf Operator-Freigabe", tone: "sky", statuses: ["todo", "scheduled", "ready"] },
  execute: { id: "execute", label: "Execute", purpose: "Worker läuft · Tool-Calls", tone: "amber", statuses: ["running"] },
  verify: { id: "verify", label: "Verify", purpose: "Verifier-Gate · echte Tests", tone: "cyan", statuses: ["review"] },
  ship: { id: "ship", label: "Ship", purpose: "Abgenommen · geliefert", tone: "emerald", statuses: ["done"] },
};

/** Map a raw kanban status to its operator stage. ``blocked`` has no stage of
 *  its own — it is a rework signal surfaced separately, but for placement we
 *  treat it as "execute" (it fell out of an in-flight run). */
export function statusToStage(status: TaskStatus): FleetStage | null {
  if (status === "archived") return null;
  if (status === "blocked") return "execute";
  for (const stage of FLEET_STAGES) {
    if (STAGE_META[stage].statuses.includes(status)) return stage;
  }
  return null;
}

// ── Stage actions ──────────────────────────────────────────────────────────
// Every actionable transition an operator can drive from the Fleet, wired to a
// real PATCH target status. `intent` colours the button; `guard` (below) covers
// the stages where the action is deliberately worker/gate-driven, not manual.

export type StageActionKey = "plan" | "dispatch" | "reopen";

export interface StageAction {
  key: StageActionKey;
  label: string;
  /** PATCH /tasks/{id} body status this button writes. */
  target: TaskStatus;
  tone: ToneName;
  intent: "advance" | "danger";
  /** Short German confirmation line shown before the write fires. */
  confirm: string;
}

const ACTION: Record<StageActionKey, StageAction> = {
  plan: { key: "plan", label: "Plan", target: "todo", tone: "sky", intent: "advance", confirm: "Aufgabe spezifizieren (Triage → Plan)?" },
  dispatch: { key: "dispatch", label: "Starten", target: "ready", tone: "amber", intent: "advance", confirm: "Operator-Freigabe setzen — danach übernimmt der Dispatcher automatisch?" },
  reopen: { key: "reopen", label: "Reopen", target: "ready", tone: "sky", intent: "advance", confirm: "Blockade lösen und neu einreihen?" },
};

/** The operator action(s) available for a task in a given status. Empty when
 *  the next move is worker/gate-driven (see {@link stageGuard}). */
export function stageActions(status: TaskStatus): StageAction[] {
  switch (status) {
    case "triage": return [ACTION.plan];
    case "todo":
    case "scheduled": return [ACTION.dispatch];
    case "blocked": return [ACTION.reopen];
    default: return [];
  }
}

/** Why a stage has no manual button — the honest, explaining guard state. */
export function stageGuard(status: TaskStatus): string | null {
  if (status === "ready") return "Freigegeben — der Dispatcher übernimmt automatisch (~60 s).";
  if (status === "running") return "Worker läuft — schließt selbst ab; das Verifier-Gate routet danach automatisch.";
  if (status === "review") return "Verifier-Gate läuft — nur ein maschinenlesbares Verifier-Urteil darf den Status ändern.";
  if (status === "done") return "Abgenommen — Lauf ist terminal.";
  return null;
}

/** A task is "actionable" when an operator decision (not a worker) moves it on,
 *  or it is blocked (needs rework). These are the rows the Fleet surfaces with
 *  buttons. */
export function isActionableStatus(status: TaskStatus): boolean {
  return status === "triage" || status === "todo" || status === "scheduled" || status === "blocked";
}

// ── Management actions (S3) ──────────────────────────────────────────────────
// The operator actions the pure stage model can't express because they are not
// simple status writes: Retry (unblock + a dispatcher tick, useFixRedispatch),
// Cancel (archive a single task) and Cancel-chain (POST /tasks/{root}/cancel-
// chain). Rendered on TOP of {@link stageActions} so the Fleet node drawer and
// the Risiko blocked-rows carry a full control surface. Terminal tasks
// (done/archived) and a live `running` worker expose no single-task destructive
// button here (the worker is torn down via the chain/worker controls).

export type ManageActionKey = "retry" | "cancel" | "cancelChain";

export function manageActions(status: TaskStatus, opts: { hasChain: boolean }): ManageActionKey[] {
  if (status === "done" || status === "archived") return [];
  const out: ManageActionKey[] = [];
  // Retry only makes sense once a run has fallen out (blocked): re-open + tick.
  if (status === "blocked") out.push("retry");
  // Cancel a single task (archive) for every non-terminal, non-running task —
  // a live worker is stopped through the chain/worker path, not by archiving.
  if (status !== "running") out.push("cancel");
  // Cancel the whole chain only when the task belongs to a multi-node chain.
  if (opts.hasChain) out.push("cancelChain");
  return out;
}

// ── Operator-question classification (S6) ────────────────────────────────────
// Backend-owned truth: the dispatcher includes verdict + retry history in this
// decision.  Inferring from prose here made a verifier's innocent "why?" look
// like a human escalation even though the same block was auto-retryable.
export function isOperatorQuestion(operatorQuestion?: boolean | null): boolean {
  return operatorQuestion === true;
}

// ── Role chips ───────────────────────────────────────────────────────────────
// The screenshot's coloured run-role chip (Verifier = azure, Coder = gold,
// Researcher = emerald). Keyed by worker profile; results override to "Verifier"
// when the run_role is a verification run.

export interface RoleChip {
  label: string;
  short: string;
  tone: ToneName;
}

const ROLE_BY_PROFILE: Record<string, RoleChip> = {
  verifier: { label: "Verifier", short: "V", tone: "sky" },
  coder: { label: "Coder", short: "C", tone: "amber" },
  premium: { label: "Coder", short: "C", tone: "amber" },
  devpower: { label: "DevPower", short: "D", tone: "amber" },
  research: { label: "Research", short: "R", tone: "emerald" },
  planner: { label: "Planner", short: "P", tone: "emerald" },
  critic: { label: "Critic", short: "K", tone: "rose" },
  admin: { label: "Admin", short: "A", tone: "violet" },
  dispatcher: { label: "Dispatcher", short: "D", tone: "violet" },
  kanbanops: { label: "Kanban-Ops", short: "O", tone: "violet" },
  default: { label: "Worker", short: "W", tone: "zinc" },
};

export function roleChip(profile: WorkerProfile | string | null | undefined, runRole?: string | null): RoleChip {
  if (runRole === "verification") return ROLE_BY_PROFILE.verifier;
  if (!profile) return ROLE_BY_PROFILE.default;
  return (
    ROLE_BY_PROFILE[profile] ?? {
      label: profile,
      short: profile.slice(0, 1).toUpperCase(),
      tone: "zinc",
    }
  );
}

// ── Pipeline aggregation ─────────────────────────────────────────────────────

export interface BoardTaskLite {
  id: string;
  title: string;
  status: TaskStatus;
  assignee?: string | null;
  priority?: number;
  age?: { created_age_seconds: number | null } | null;
  /** Epoch seconds the task reached `done` — used to sort Ship newest-first. */
  completed_at?: number | null;
  latest_summary?: string | null;
}

export interface StageBucket {
  stage: FleetStage;
  meta: StageMeta;
  count: number;
}

export interface PipelineModel {
  /** Per-stage counts for the rail (Capture..Ship). */
  buckets: StageBucket[];
  /** Every active, non-terminal task the operator may need to track. */
  active: BoardTaskLite[];
  /** Tasks waiting on an operator decision, newest-stage-first. */
  actionable: BoardTaskLite[];
  /** Count parked in blocked (needs rework). */
  blockedCount: number;
  /** Total non-archived tasks on the board. */
  total: number;
}

const ACTIONABLE_ORDER: TaskStatus[] = ["blocked", "triage", "todo", "scheduled"];
const ACTIVE_ORDER: TaskStatus[] = ["blocked", "running", "review", "ready", "scheduled", "todo", "triage"];

export function buildPipeline(tasks: BoardTaskLite[]): PipelineModel {
  const buckets: StageBucket[] = FLEET_STAGES.map((stage) => ({
    stage,
    meta: STAGE_META[stage],
    count: 0,
  }));
  const byStage = new Map<FleetStage, StageBucket>(buckets.map((b) => [b.stage, b]));
  let blockedCount = 0;
  let total = 0;
  const active: BoardTaskLite[] = [];
  const actionable: BoardTaskLite[] = [];

  for (const task of tasks) {
    if (task.status === "archived") continue;
    total += 1;
    if (task.status === "blocked") blockedCount += 1;
    const stage = statusToStage(task.status);
    if (stage) byStage.get(stage)!.count += 1;
    if (task.status !== "done") active.push(task);
    if (isActionableStatus(task.status)) actionable.push(task);
  }

  active.sort((a, b) => {
    const ra = ACTIVE_ORDER.indexOf(a.status);
    const rb = ACTIVE_ORDER.indexOf(b.status);
    if (ra !== rb) return ra - rb;
    return (b.priority ?? 0) - (a.priority ?? 0);
  });

  actionable.sort((a, b) => {
    const ra = ACTIONABLE_ORDER.indexOf(a.status);
    const rb = ACTIONABLE_ORDER.indexOf(b.status);
    if (ra !== rb) return ra - rb;
    return (b.priority ?? 0) - (a.priority ?? 0);
  });

  return { buckets, active, actionable, blockedCount, total };
}

/** Group board tasks into the five Flow stage columns (Capture..Ship). Archived
 *  is dropped; blocked sits in Execute as a rework guard. Each column is sorted
 *  priority-desc then oldest-first. The live backbone of the Flow board. */
export function groupByStage<T extends BoardTaskLite>(tasks: T[]): Record<FleetStage, T[]> {
  const out: Record<FleetStage, T[]> = { capture: [], plan: [], execute: [], verify: [], ship: [] };
  for (const task of tasks) {
    if (task.status === "archived") continue;
    const stage = statusToStage(task.status);
    if (stage) out[stage].push(task);
  }
  for (const stage of FLEET_STAGES) {
    if (stage === "ship") {
      // Ship = "what just shipped": newest completion first, so a freshly
      // shipped task is always at the top instead of buried under hundreds of
      // old high-priority done tasks. Falls back to creation recency
      // (smaller created age = newer) when completed_at is missing.
      out[stage].sort((a, b) =>
        (b.completed_at ?? 0) - (a.completed_at ?? 0) ||
        (a.age?.created_age_seconds ?? 0) - (b.age?.created_age_seconds ?? 0));
    } else {
      out[stage].sort((a, b) => (b.priority ?? 0) - (a.priority ?? 0) || (b.age?.created_age_seconds ?? 0) - (a.age?.created_age_seconds ?? 0));
    }
  }
  return out;
}

// ── Flow counts ──────────────────────────────────────────────────────────────
// The header metric pods. blocked (Nacharbeit) is surfaced as its own number so
// a rework signal is visible at a glance instead of hidden as a red card inside
// Execute. wip = every non-terminal task on the board.
export interface FlowCounts {
  running: number;
  plan: number;
  review: number;
  blocked: number;
  wip: number;
}

export function flowCounts(tasks: BoardTaskLite[]): FlowCounts {
  let running = 0, plan = 0, review = 0, blocked = 0, wip = 0;
  for (const task of tasks) {
    if (task.status === "archived" || task.status === "done") continue;
    wip += 1;
    switch (task.status) {
      case "running": running += 1; break;
      case "blocked": blocked += 1; break;
      case "review": review += 1; break;
      case "todo":
      case "scheduled":
      case "ready": plan += 1; break;
    }
  }
  return { running, plan, review, blocked, wip };
}

// ── Quick capture ────────────────────────────────────────────────────────────
// The "+ Aufgabe" capture button (header on desktop, sticky FAB on mobile) drops
// a new task into the pipeline. The operator picks a PLAN METHOD and (for the two
// decomposing methods) a GATE/AUTO switch — all create a real Kanban task, none
// is a UI illusion:
//   park     → triage + park: lands GEPARKT in `scheduled` (Plan). No worker
//              starts on its own; the operator clicks Dispatch. The deliberate
//              "I'll dispatch it myself" escape hatch. (No gate switch — it is
//              already operator-held.)
//   lean     → the in-gateway decomposer triages the raw prompt into a subtask
//              DAG / routes a single one (~60s). DEFAULT. No durable spec.
//   document → backend-driven rich decompose that ALSO renders a durable Vault
//              plan-spec (narrative + subtask table) from the same object it
//              creates the subtasks from (one truth, no drift).
//
// GATE (lean + document only): hold the subtasks in `scheduled` until the
// operator clicks "Go ausführen" in the Plan stage. AUTO (default) dispatches
// them as soon as they are ready — the Stufe-A behaviour, backward-compatible.
//
// Routing: park and lean+AUTO use the plain POST /tasks (Stufe-A, unchanged).
// document* and lean+GATE need the backend-driven POST /tasks/flow-capture
// (planning + optional spec + gate-hold). See usesFlowCaptureEndpoint().
//
// IMPORTANT: assignee is left unset (null) for the POST /tasks path. Hardcoding
// "coder" here used to defeat the whole point of triage routing: the decomposer's
// no-fanout path only re-routes a task whose assignee is empty
// (kanban_decompose.py), so a pre-set "coder" sent every raw prompt straight to
// the coder, never to the orchestrator. With a null assignee the decomposer
// routes; for the parked path the dispatcher applies `kanban.default_assignee` at
// manual Dispatch (same end result as before).
export type CaptureMethod = "park" | "lean" | "document";

// Phase-C levers carried straight from the capture sheet (mirror of the
// "Kette starten"-Panel): the chain-wide review tier and an optional read-only
// scout recon pre-step. Both optional — a lever-less capture stays byte-identical
// to the Stufe-A behaviour, so old payload assertions/tests don't churn.
export interface CaptureLevers {
  /** Chain-wide staged-review tier; "" / undefined = silent default (not sent). */
  reviewTier?: ReviewTier | "";
  /** Prepend a read-only scout recon task before the entry children. Gated
   *  chains only (a single parked task has no build-children to precede). */
  injectScout?: boolean;
  /** Optional short description: stored as the root body so the creation-time
   *  risk heuristic has substance to auto-classify the tier (not just the title).
   *  Only carried on the flow-capture endpoint; "" / undefined = not sent. */
  description?: string;
}

export interface CaptureRequest {
  title: string;
  assignee: string | null;
  priority: number;
  tenant: string;
  triage: boolean;
  park: boolean;
  notify_home: boolean;
  /** Only present for a parked capture that picked an explicit review tier. */
  review_tier?: ReviewTier;
}

/** POST /tasks body for the park / lean+AUTO captures (the Stufe-A path). */
export function captureRequest(title: string, method: CaptureMethod, levers?: CaptureLevers): CaptureRequest {
  const req: CaptureRequest = {
    title: title.trim(),
    assignee: null,
    priority: 0,
    tenant: "flow-capture",
    triage: true,
    park: method === "park",
    // Only ping the home channel for autonomous (lean) captures — a parked task
    // waits for the operator who is already looking at the board.
    notify_home: method !== "park",
  };
  // Park: the parked single task carries the chosen review tier so the staged-
  // review resolver governs it when the operator later dispatches it. Attached
  // only when a real tier was picked → a tier-less capture is byte-identical.
  if (levers?.reviewTier) req.review_tier = levers.reviewTier;
  return req;
}

export interface FlowCaptureRequest {
  title: string;
  method: "lean" | "document";
  gate: boolean;
  tenant: string;
  priority: number;
  notify_home: boolean;
  /** Stamped on the root (children inherit at release). Only present when set. */
  review_tier?: ReviewTier;
  /** Persisted as a root intent the release path honours. Only present when on. */
  inject_scout?: boolean;
  /** Stored as the root body so the risk heuristic can auto-classify. Only set. */
  description?: string;
}

/** POST /tasks/flow-capture body for the backend-driven captures (document*,
 *  lean+GATE). The backend parks the root, plans it, optionally writes the
 *  Vault spec, and holds the subtasks when gated. The Phase-C levers ride along
 *  so the operator's tier/scout choice at capture reaches the gated chain. */
export function flowCaptureRequest(title: string, method: CaptureMethod, gate: boolean, levers?: CaptureLevers): FlowCaptureRequest {
  const req: FlowCaptureRequest = {
    title: title.trim(),
    method: method === "document" ? "document" : "lean",
    gate,
    tenant: "flow-capture",
    priority: 0,
    notify_home: true,
  };
  if (levers?.reviewTier) req.review_tier = levers.reviewTier;
  if (levers?.injectScout) req.inject_scout = true;
  if (levers?.description?.trim()) req.description = levers.description.trim();
  return req;
}

/** Which backend a (method, gate) capture routes to. park and lean+AUTO use the
 *  plain POST /tasks (Stufe-A, backward-compatible); everything else needs the
 *  backend-driven /tasks/flow-capture (planning + spec + gate-hold). */
export function usesFlowCaptureEndpoint(method: CaptureMethod, gate: boolean): boolean {
  return method === "document" || (method === "lean" && gate);
}

// ── Projekt-Achse (tenant) ──────────────────────────────────────────────────
// Der Operator-Vertrag 2026-06-10: tenant = Projekt, Epics = Vorhaben darunter.
// Altlasten ohne tenant laufen als "Unsortiert" — kein Backfill-Zwang.

export const UNSORTED_PROJECT = "__none__";

const PROJECT_LABELS: Record<string, string> = {
  "family-organizer": "Family Organizer",
  orchestrator: "Orchestrierung",
  "flow-capture": "Flow",
  hermes: "Hermes",
};

/** Anzeige-Name eines Projekts (tenant). null/leer → "Unsortiert". */
export function projectLabel(tenant: string | null | undefined): string {
  if (!tenant) return "Unsortiert";
  return PROJECT_LABELS[tenant] ?? tenant;
}

/** Filter-Schlüssel eines Tasks für den Projekt-Filter. */
export function projectKey(tenant: string | null | undefined): string {
  return tenant || UNSORTED_PROJECT;
}

export interface ProjectOption {
  key: string;
  label: string;
  count: number;
}

/** Projekt-Filter-Optionen aus den Board-Tasks (nur nicht-archivierte),
 *  sortiert nach Größe; "Unsortiert" immer zuletzt. */
export function projectOptions<T extends { tenant?: string | null; status: TaskStatus }>(tasks: T[]): ProjectOption[] {
  const counts = new Map<string, number>();
  for (const t of tasks) {
    if (t.status === "archived") continue;
    const key = projectKey(t.tenant);
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  const opts = [...counts.entries()].map(([key, count]) => ({
    key,
    label: projectLabel(key === UNSORTED_PROJECT ? null : key),
    count,
  }));
  opts.sort((a, b) => {
    if (a.key === UNSORTED_PROJECT) return 1;
    if (b.key === UNSORTED_PROJECT) return -1;
    return b.count - a.count;
  });
  return opts;
}

// ── Ketten-Modell (root_id-Gruppen) ─────────────────────────────────────────
// Die primäre Einheit des Flow-Boards: eine Root-Kette = der Tree-Sink
// (board `root_id`) + alle Tasks, die in ihn rollen. Einzeltasks (Gruppe der
// Größe 1) laufen separat als "singles".

export interface ChainTaskLite extends BoardTaskLite {
  root_id?: string | null;
  tenant?: string | null;
  epic_id?: string | null;
  progress?: { done: number; total: number } | null;
  review_tier?: ReviewTier | null;
  active_review_stage?: ActiveReviewStage | null;
}

const REVIEW_TIER_RANK: Record<ReviewTier, number> = { standard: 0, review: 1, critical: 2 };

/** Highest staged-review tier across a chain's members, or null when the chain
 *  carries no elevated tier (every member standard/unset → no Review pill).
 *  Mirrors the backend ``_TIER_ORDER`` ranking; ``standard`` is the silent
 *  baseline and never surfaces a pill. */
export function chainReviewTier(members: Array<{ review_tier?: ReviewTier | null }>): ReviewTier | null {
  let best: ReviewTier | null = null;
  for (const m of members) {
    const tier = m.review_tier;
    if (!tier || tier === "standard") continue;
    if (best === null || REVIEW_TIER_RANK[tier] > REVIEW_TIER_RANK[best]) best = tier;
  }
  return best;
}

/** The review stage actually RUNNING for a chain right now — the
 *  `active_review_stage` of whichever member currently sits in `review`
 *  status. Unlike {@link chainReviewTier} (the configured ceiling) this is the
 *  live verifier→reviewer→critic step. At most one member reviews at a time, so
 *  no ranking is needed; returns null when nothing is in review (or the review
 *  member carries no stage yet). */
export function chainActiveReviewStage(
  members: Array<{ status?: TaskStatus | string; active_review_stage?: ActiveReviewStage | null }>,
): ActiveReviewStage | null {
  return members.find((m) => m.status === "review")?.active_review_stage ?? null;
}

export interface ChainModel<T extends ChainTaskLite> {
  rootId: string;
  /** Der Root-Task selbst, wenn er im Board-Snapshot liegt. */
  root: T | null;
  /** Alle Mitglieder inkl. Root, Stage-sortiert (Capture→Ship-Reihenfolge). */
  members: T[];
  total: number;
  doneCount: number;
  stageCounts: Record<FleetStage, number>;
  blockedCount: number;
  runningCount: number;
  reviewCount: number;
  /** true, wenn jede Karte der Kette done/archived ist. */
  isDone: boolean;
  /** Jüngstes completed_at der Kette (Sortierung der Geliefert-Liste). */
  latestCompletedAt: number | null;
  tenant: string | null;
  epicId: string | null;
}

export interface ChainBoard<T extends ChainTaskLite> {
  /** Ketten mit aktiver Arbeit, dringendste zuerst. */
  active: ChainModel<T>[];
  /** Komplett gelieferte Ketten, jüngste zuerst. */
  done: ChainModel<T>[];
  /** Einzeltasks (keine Kette) mit aktiver Arbeit, Stage-sortiert. */
  singles: T[];
  /** Gelieferte Einzeltasks, jüngste zuerst. */
  doneSingles: T[];
}

const STAGE_ORDER: Record<FleetStage, number> = { execute: 0, verify: 1, plan: 2, capture: 3, ship: 4 };

/** Dringlichkeits-Rang einer Kette: running > review > blocked > plan/capture. */
function chainUrgency<T extends ChainTaskLite>(c: ChainModel<T>): number {
  if (c.runningCount > 0) return 4;
  if (c.reviewCount > 0) return 3;
  if (c.blockedCount > 0) return 2;
  return 1;
}

/** Gruppiert Board-Tasks in Root-Ketten + Einzeltasks. Archivierte fallen raus. */
export function buildChains<T extends ChainTaskLite>(tasks: T[]): ChainBoard<T> {
  const groups = new Map<string, T[]>();
  for (const t of tasks) {
    if (t.status === "archived") continue;
    const key = (t as { root_id?: string | null }).root_id || t.id;
    const list = groups.get(key);
    if (list) list.push(t);
    else groups.set(key, [t]);
  }

  const active: ChainModel<T>[] = [];
  const done: ChainModel<T>[] = [];
  const singles: T[] = [];
  const doneSingles: T[] = [];

  for (const [rootId, members] of groups) {
    if (members.length === 1 && members[0].id === rootId) {
      const t = members[0];
      (t.status === "done" ? doneSingles : singles).push(t);
      continue;
    }
    const stageCounts: Record<FleetStage, number> = { capture: 0, plan: 0, execute: 0, verify: 0, ship: 0 };
    let blockedCount = 0, runningCount = 0, reviewCount = 0, doneCount = 0;
    let latestCompletedAt: number | null = null;
    for (const m of members) {
      const stage = statusToStage(m.status);
      if (stage) stageCounts[stage] += 1;
      if (m.status === "blocked") blockedCount += 1;
      if (m.status === "running") runningCount += 1;
      if (m.status === "review") reviewCount += 1;
      if (m.status === "done") {
        doneCount += 1;
        if (m.completed_at && (latestCompletedAt == null || m.completed_at > latestCompletedAt)) {
          latestCompletedAt = m.completed_at;
        }
      }
    }
    members.sort((a, b) => {
      const sa = STAGE_ORDER[statusToStage(a.status) ?? "ship"];
      const sb = STAGE_ORDER[statusToStage(b.status) ?? "ship"];
      if (sa !== sb) return sa - sb;
      return (b.priority ?? 0) - (a.priority ?? 0);
    });
    const root = members.find((m) => m.id === rootId) ?? null;
    const chain: ChainModel<T> = {
      rootId,
      root,
      members,
      total: members.length,
      doneCount,
      stageCounts,
      blockedCount,
      runningCount,
      reviewCount,
      isDone: doneCount === members.length,
      latestCompletedAt,
      tenant: root?.tenant ?? members.find((m) => m.tenant)?.tenant ?? null,
      epicId: root?.epic_id ?? members.find((m) => m.epic_id)?.epic_id ?? null,
    };
    (chain.isDone ? done : active).push(chain);
  }

  active.sort((a, b) => {
    const ua = chainUrgency(a), ub = chainUrgency(b);
    if (ua !== ub) return ub - ua;
    return (b.root?.priority ?? 0) - (a.root?.priority ?? 0);
  });
  done.sort((a, b) => (b.latestCompletedAt ?? 0) - (a.latestCompletedAt ?? 0));
  singles.sort((a, b) => {
    const sa = STAGE_ORDER[statusToStage(a.status) ?? "ship"];
    const sb = STAGE_ORDER[statusToStage(b.status) ?? "ship"];
    if (sa !== sb) return sa - sb;
    return (b.priority ?? 0) - (a.priority ?? 0);
  });
  doneSingles.sort((a, b) => (b.completed_at ?? 0) - (a.completed_at ?? 0));

  return { active, done, singles, doneSingles };
}

// ── Epic-Gruppierung (Toggle im Flow-Board) ─────────────────────────────────

export interface EpicChainGroup<T extends ChainTaskLite> {
  /** null = "Ohne Epic"-Gruppe. */
  epicId: string | null;
  chains: ChainModel<T>[];
}

/**
 * Gruppiert Ketten nach Epic. Die Gruppen-Reihenfolge folgt der ersten
 * (= dringendsten) Kette je Epic; "Ohne Epic" steht immer zuletzt.
 */
export function groupChainsByEpic<T extends ChainTaskLite>(
  chains: ChainModel<T>[],
): EpicChainGroup<T>[] {
  const byEpic = new Map<string | null, ChainModel<T>[]>();
  for (const chain of chains) {
    const key = chain.epicId ?? null;
    const list = byEpic.get(key);
    if (list) list.push(chain);
    else byEpic.set(key, [chain]);
  }
  const groups: EpicChainGroup<T>[] = [];
  for (const [epicId, list] of byEpic) {
    if (epicId !== null) groups.push({ epicId, chains: list });
  }
  const none = byEpic.get(null);
  if (none) groups.push({ epicId: null, chains: none });
  return groups;
}
