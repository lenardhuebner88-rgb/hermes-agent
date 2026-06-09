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
import type { ToneName, TaskStatus, WorkerProfile } from "./types";

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
  plan: { id: "plan", label: "Plan", purpose: "Spezifiziert · wartet auf Dispatch", tone: "sky", statuses: ["todo", "scheduled", "ready"] },
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

export type StageActionKey = "plan" | "dispatch" | "ship" | "rework" | "reopen";

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
  dispatch: { key: "dispatch", label: "Dispatch", target: "ready", tone: "amber", intent: "advance", confirm: "Startklar setzen — der Dispatcher übernimmt automatisch?" },
  ship: { key: "ship", label: "Ship", target: "done", tone: "emerald", intent: "advance", confirm: "Review abnehmen und auf Fertig setzen?" },
  rework: { key: "rework", label: "Rework", target: "blocked", tone: "red", intent: "danger", confirm: "Zurück in Nacharbeit (Review → Blockiert)?" },
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
    case "review": return [ACTION.ship, ACTION.rework];
    default: return [];
  }
}

/** Why a stage has no manual button — the honest, explaining guard state. */
export function stageGuard(status: TaskStatus): string | null {
  if (status === "ready") return "Eingereiht — der Dispatcher übernimmt automatisch (~60 s).";
  if (status === "running") return "Worker läuft — schließt selbst ab; das Verifier-Gate routet danach automatisch.";
  if (status === "done") return "Abgenommen — Lauf ist terminal.";
  return null;
}

/** A task is "actionable" when an operator decision (not a worker) moves it on,
 *  or it is blocked (needs rework). These are the rows the Fleet surfaces with
 *  buttons. */
export function isActionableStatus(status: TaskStatus): boolean {
  return status === "triage" || status === "todo" || status === "scheduled" || status === "blocked" || status === "review";
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
  research: { label: "Researcher", short: "R", tone: "emerald" },
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
  /** Tasks waiting on an operator decision, newest-stage-first. */
  actionable: BoardTaskLite[];
  /** Count parked in blocked (needs rework). */
  blockedCount: number;
  /** Total non-archived tasks on the board. */
  total: number;
}

const ACTIONABLE_ORDER: TaskStatus[] = ["review", "blocked", "triage", "todo", "scheduled"];

export function buildPipeline(tasks: BoardTaskLite[]): PipelineModel {
  const buckets: StageBucket[] = FLEET_STAGES.map((stage) => ({
    stage,
    meta: STAGE_META[stage],
    count: 0,
  }));
  const byStage = new Map<FleetStage, StageBucket>(buckets.map((b) => [b.stage, b]));
  let blockedCount = 0;
  let total = 0;
  const actionable: BoardTaskLite[] = [];

  for (const task of tasks) {
    if (task.status === "archived") continue;
    total += 1;
    if (task.status === "blocked") blockedCount += 1;
    const stage = statusToStage(task.status);
    if (stage) byStage.get(stage)!.count += 1;
    if (isActionableStatus(task.status)) actionable.push(task);
  }

  actionable.sort((a, b) => {
    const ra = ACTIONABLE_ORDER.indexOf(a.status);
    const rb = ACTIONABLE_ORDER.indexOf(b.status);
    if (ra !== rb) return ra - rb;
    return (b.priority ?? 0) - (a.priority ?? 0);
  });

  return { buckets, actionable, blockedCount, total };
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
// a new task into the pipeline. The operator picks one of two honest modes in the
// sheet — both create a real Kanban task, neither is a UI illusion:
//   park        → triage + park: lands GEPARKT in `scheduled` (Plan). No worker
//                 starts on its own; the operator clicks Dispatch. Default — the
//                 same safe behaviour as the FO/Orchestrator → Fleet buttons.
//   orchestrate → triage only: lands in `triage` (Capture); the in-gateway
//                 orchestrator triages/decomposes and may dispatch a worker (~60s).
export type CaptureMode = "park" | "orchestrate";

export interface CaptureRequest {
  title: string;
  assignee: string;
  priority: number;
  tenant: string;
  triage: boolean;
  park: boolean;
  notify_home: boolean;
}

export function captureRequest(title: string, mode: CaptureMode): CaptureRequest {
  return {
    title: title.trim(),
    assignee: "coder",
    priority: 0,
    tenant: "flow-capture",
    triage: true,
    park: mode === "park",
    // Only ping the home channel for autonomous (orchestrate) captures — a
    // parked task waits for the operator who is already looking at the board.
    notify_home: mode === "orchestrate",
  };
}
