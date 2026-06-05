import type { BacklogContractHealth, BacklogItem, BacklogDetail } from "./schemas";

export type FoSortKey = "risk" | "age" | "status";

// Reason codes explain a candidate's queue rank. Derived ONCE on the client from the
// server-computed per-item facts (status/risk/area/age/owner/quality/readiness) so the
// next-task spotlight and the compare strip can never disagree.
export type FoReasonCode =
  | "now_status"
  | "next_status"
  | "in_progress"
  | "high_risk"
  | "high_impact_area"
  | "aged"
  | "penalty_unowned"
  | "penalty_stale"
  | "missing_acceptance"
  | "missing_next_action"
  | "needs_grooming"
  | "drift";

export const FO_REASON_LABELS: Record<FoReasonCode, string> = {
  now_status: "Status now",
  next_status: "Status next",
  in_progress: "Läuft",
  high_risk: "Hohes Risiko",
  high_impact_area: "Wichtiger Bereich",
  aged: "Lange offen",
  penalty_unowned: "Kein Owner",
  penalty_stale: "Stale",
  missing_acceptance: "Akzeptanz fehlt",
  missing_next_action: "Next Action fehlt",
  needs_grooming: "Grooming nötig",
  drift: "Vertragsdrift",
};

export type FoRankedCandidate = { item: BacklogItem; score: number; reasonCodes: FoReasonCode[] };
export type FoQueueState = "now" | "next" | "in_progress" | "blocked" | "later" | "done";
export type FoQueueStateResult =
  | { state: FoQueueState; reason?: undefined }
  | { state: "drift"; reason: "unknown_status" };

export type FoQualityFlagKind =
  | "weak_title"
  | "missing_acceptance"
  | "unclear_owner"
  | "stale_update"
  | "large_scope"
  | "missing_next_action";

export type FoQualityFlag = { kind: FoQualityFlagKind; label: string; severity: "warn" | "risk" };

export type FoStaleSignal =
  | { state: "fresh"; label: string }
  | { state: "aging"; label: string }
  | { state: "missing_update"; label: string }
  | { state: "stale"; label: string };

const QUALITY_LABELS: Record<FoQualityFlagKind, string> = {
  weak_title: "Titel schwach",
  missing_acceptance: "Akzeptanz fehlt",
  unclear_owner: "Owner unklar",
  stale_update: "Stale Update",
  large_scope: "Scope gross",
  missing_next_action: "Next Action fehlt",
};
const QUALITY_KINDS = new Set<string>(Object.keys(QUALITY_LABELS));

export type FoOwnerLoad = { owner: string; total: number; highRisk: number; stale: number; unready: number };

export type FoHealthStripCounts = {
  now: number;
  nextReady: number;
  blocked: number;
  unowned: number;
  stale: number;
  highRisk: number;
  contractDrift: number;
  missingAcceptance: number;
};

export type FoFilterOptions = {
  owner?: string;
  risk?: string;
  area?: string;
  status?: string;
  stale?: boolean;
};

const RISK_ORDER: Record<string, number> = { high: 0, medium: 1, low: 2 };
const STATUS_ORDER: Record<string, number> = {
  now: 0,
  next: 1,
  in_progress: 2,
  blocked: 3,
  later: 4,
  done: 5,
};
const KNOWN_STATUSES = new Set(Object.keys(STATUS_ORDER));
const KNOWN_OWNERS = new Set(["hermes", "claude", "codex", "piet", "unassigned"]);
const BUSINESS_IMPACT: Record<string, number> = {
  db: 4,
  "hermes-api": 4,
  admin: 3,
  calendar: 3,
  shopping: 3,
  lists: 2,
  kitchen: 1,
  process: 1,
};
const STATE_PULL_WEIGHT: Record<string, number> = {
  now: 50,
  next: 40,
  in_progress: 20,
  blocked: 4,
  later: 0,
  done: -100,
};

export function queueStateForFoItem(item: Pick<BacklogItem, "status">): FoQueueStateResult {
  if (KNOWN_STATUSES.has(item.status)) return { state: item.status as FoQueueState };
  return { state: "drift", reason: "unknown_status" };
}

export function staleSignalForFoItem(
  item: Pick<BacklogItem, "status" | "updated" | "stale" | "freshness" | "age_days">,
  nowSec: number,
): FoStaleSignal {
  // Prefer the server-computed freshness fact (v2); fall back to client heuristics (v1).
  if (item.freshness) {
    const days = typeof item.age_days === "number" ? item.age_days : null;
    switch (item.freshness) {
      case "no_proof":
        return { state: "missing_update", label: "kein Beleg" };
      case "stale":
        return { state: "stale", label: days != null && days > 0 ? `${days} Tage ohne Beleg` : "überfällig" };
      case "aging":
        return { state: "aging", label: days != null ? `${days} Tage` : "altert" };
      default:
        return { state: "fresh", label: days != null ? (days <= 0 ? "heute" : `${days} Tage`) : "frisch" };
    }
  }
  if (!item.updated) return { state: "missing_update", label: "kein Update" };
  if (item.status === "done") return { state: "fresh", label: "abgeschlossen" };
  const updated = Date.parse(`${item.updated.slice(0, 10)}T00:00:00Z`);
  if (Number.isNaN(updated)) return { state: "missing_update", label: "Update unklar" };
  const ageDays = Math.floor((nowSec * 1000 - updated) / 86_400_000);
  const stale = item.stale || (["in_progress", "blocked"].includes(item.status) && ageDays > 7);
  if (stale) return { state: "stale", label: ageDays > 0 ? `${ageDays} Tage ohne Beleg` : "überfällig" };
  return { state: "fresh", label: ageDays <= 0 ? "heute" : `${ageDays} Tage` };
}

function hasAcceptance(detail?: Pick<BacklogDetail, "acceptance_criteria" | "body">): boolean {
  if (!detail) return false;
  if (detail.acceptance_criteria?.length) return true;
  return /akzeptanz|acceptance|criteria/i.test(detail.body);
}

function hasNextAction(detail?: Pick<BacklogDetail, "next_action" | "body">): boolean {
  if (!detail) return false;
  if (detail.next_action?.trim()) return true;
  return /next action|next step|nächster|naechster|vorgehen/i.test(detail.body);
}

function bodyScopeScore(detail?: Pick<BacklogDetail, "body">): number {
  if (!detail?.body) return 0;
  const bullets = detail.body.match(/(^|\n)\s*(?:[-*]|\d+\.)\s+/g)?.length ?? 0;
  return bullets + Math.floor(detail.body.length / 1500);
}

export function qualityFlagsForFoItem(item: BacklogItem, detail?: BacklogDetail): FoQualityFlag[] {
  // Prefer the server-computed taxonomy (v2) — it sees the full body (so `large_scope`
  // and acceptance/next-action depth are available in the list, not only the drawer).
  if (item.quality_issues) {
    return item.quality_issues
      .filter((issue) => QUALITY_KINDS.has(issue.code))
      .map((issue) => ({
        kind: issue.code as FoQualityFlagKind,
        label: QUALITY_LABELS[issue.code as FoQualityFlagKind],
        severity: issue.severity === "risk" ? "risk" : "warn",
      }));
  }
  // Fallback: client heuristics (v1 backend, or detail-derived signals).
  const flags: FoQualityFlag[] = [];
  const title = item.title.trim();
  if (title.length < 12 || ["fix", "bug", "todo", "misc", "cleanup"].includes(title.toLowerCase())) {
    flags.push({ kind: "weak_title", label: "Titel schwach", severity: "warn" });
  }
  if (item.missing_acceptance === true || (detail && !hasAcceptance(detail))) {
    flags.push({ kind: "missing_acceptance", label: "Akzeptanz fehlt", severity: "risk" });
  }
  if (!item.owner || item.owner === "unassigned" || !KNOWN_OWNERS.has(item.owner)) {
    flags.push({ kind: "unclear_owner", label: "Owner unklar", severity: "risk" });
  }
  if (item.stale) {
    flags.push({ kind: "stale_update", label: "Stale Update", severity: "risk" });
  }
  if (bodyScopeScore(detail) >= 10) {
    flags.push({ kind: "large_scope", label: "Scope gross", severity: "warn" });
  }
  if (item.missing_next_action === true || (item.status !== "done" && detail && !hasNextAction(detail))) {
    flags.push({ kind: "missing_next_action", label: "Next Action fehlt", severity: "risk" });
  }
  return flags;
}

export function nextActionForFoItem(item: BacklogItem, detail?: BacklogDetail): string {
  const flags = qualityFlagsForFoItem(item, detail);
  const missingAcceptance = flags.some((flag) => flag.kind === "missing_acceptance");
  const missingNext = flags.some((flag) => flag.kind === "missing_next_action");
  if (missingAcceptance && missingNext) return "Akzeptanzkriterien und konkreten nächsten Schritt klären.";
  if (detail?.next_action?.trim()) return detail.next_action.trim();
  if (item.status === "blocked") return "Blocker prüfen und Entblockungspfad festlegen.";
  if (item.stale) return "Letzten Stand verifizieren und Claim erneuern oder schließen.";
  if (item.owner === "unassigned") return "Owner setzen oder vor Ziehen präzisieren.";
  if (item.status === "now" || item.status === "next") return "Spec vollständig lesen und Umsetzung vorbereiten.";
  if (item.status === "in_progress") return "Aktuellen Beleg prüfen und nächsten Proof erzeugen.";
  if (item.status === "done") return item.result ?? "Ergebnis prüfen.";
  return "Vertragsdrift prüfen, bevor der Task gezogen wird.";
}

export function ownerLoadSummary(items: BacklogItem[]): FoOwnerLoad[] {
  const byOwner = new Map<string, FoOwnerLoad>();
  for (const item of items) {
    if (item.status === "done") continue;
    const owner = item.owner || "(missing)";
    const current = byOwner.get(owner) ?? { owner, total: 0, highRisk: 0, stale: 0, unready: 0 };
    current.total += 1;
    if (item.risk === "high") current.highRisk += 1;
    if (item.stale) current.stale += 1;
    if (!item.owner || item.owner === "unassigned" || !KNOWN_OWNERS.has(item.owner)) current.unready += 1;
    byOwner.set(owner, current);
  }
  return [...byOwner.values()].sort((a, b) => b.total - a.total || a.owner.localeCompare(b.owner));
}

function ageDays(updated: string, nowSec: number): number {
  if (!updated) return 0;
  const parsed = Date.parse(`${updated.slice(0, 10)}T00:00:00Z`);
  if (Number.isNaN(parsed)) return 0;
  return Math.max(0, Math.floor((nowSec * 1000 - parsed) / 86_400_000));
}

// Prefer the server-computed `age_days` fact (deterministic); fall back to client clock.
function itemAgeDays(item: Pick<BacklogItem, "updated" | "age_days">, nowSec: number): number {
  if (typeof item.age_days === "number") return item.age_days;
  return ageDays(item.updated, nowSec);
}

function isStale(item: Pick<BacklogItem, "stale" | "freshness">): boolean {
  return item.stale || item.freshness === "stale";
}

function rankScore(item: BacklogItem, nowSec: number): number {
  const risk = RISK_ORDER[item.risk] === 0 ? 16 : RISK_ORDER[item.risk] === 1 ? 8 : 2;
  const impact = BUSINESS_IMPACT[item.area] ?? 1;
  const state = STATE_PULL_WEIGHT[item.status] ?? -20;
  const age = Math.min(itemAgeDays(item, nowSec), 30) / 2;
  const penalties = (item.owner === "unassigned" ? 8 : 0) + (isStale(item) ? 6 : 0);
  return state + risk + impact * 4 + age - penalties;
}

export function rankFoItems<T extends BacklogItem>(items: T[], nowSec: number): T[] {
  return [...items].sort((a, b) => rankScore(b, nowSec) - rankScore(a, nowSec) || a.id.localeCompare(b.id));
}

// Reason codes for one item — why it ranks where it does. Prefers server facts
// (quality_issues, freshness, readiness) and degrades to the v1 booleans.
export function reasonCodesForFoItem(item: BacklogItem, nowSec: number): FoReasonCode[] {
  const codes: FoReasonCode[] = [];
  const issues = item.quality_issues ?? [];
  const hasIssue = (code: string) => issues.some((q) => q.code === code);

  if (item.status === "now") codes.push("now_status");
  else if (item.status === "next") codes.push("next_status");
  else if (item.status === "in_progress") codes.push("in_progress");

  if (item.risk === "high") codes.push("high_risk");
  if ((BUSINESS_IMPACT[item.area] ?? 1) >= 3) codes.push("high_impact_area");
  if (itemAgeDays(item, nowSec) >= 7) codes.push("aged");
  if (item.owner === "unassigned") codes.push("penalty_unowned");
  if (isStale(item)) codes.push("penalty_stale");
  if (hasIssue("missing_acceptance") || item.missing_acceptance === true) codes.push("missing_acceptance");
  if (hasIssue("missing_next_action") || item.missing_next_action === true) codes.push("missing_next_action");
  if (item.readiness === "needs_grooming") codes.push("needs_grooming");
  if (item.readiness === "drift" || queueStateForFoItem(item).state === "drift") codes.push("drift");
  return codes;
}

// The ranked active queue with per-candidate reason codes — computed ONCE and reused by
// the next-task spotlight and the compare-top-candidates strip so they can't drift.
export function rankedQueueWithReasons(items: BacklogItem[], nowSec: number): FoRankedCandidate[] {
  const active = items.filter((item) => item.status !== "done");
  return rankFoItems(active, nowSec).map((item) => ({
    item,
    score: rankScore(item, nowSec),
    reasonCodes: reasonCodesForFoItem(item, nowSec),
  }));
}

export type FoQuickView = "all" | "ready" | "groom" | "stale" | "unowned";

export function isFoItemStale(item: Pick<BacklogItem, "stale" | "freshness">): boolean {
  return isStale(item);
}

// Server `readiness` fact with a v1 client fallback (graceful degrade if backend is old).
export function readinessForFoItem(item: BacklogItem): string {
  if (item.readiness) return item.readiness;
  if (queueStateForFoItem(item).state === "drift") return "drift";
  if (item.status === "blocked") return "blocked";
  return qualityFlagsForFoItem(item).some((flag) => flag.severity === "risk") ? "needs_grooming" : "ready";
}

export function matchesFoQuickView(item: BacklogItem, view: FoQuickView): boolean {
  switch (view) {
    case "ready":
      return readinessForFoItem(item) === "ready";
    case "groom":
      return readinessForFoItem(item) === "needs_grooming";
    case "stale":
      return isStale(item);
    case "unowned":
      return item.owner === "unassigned" || !item.owner;
    default:
      return true;
  }
}

export function foHealthStripCounts(items: BacklogItem[], contractHealth?: BacklogContractHealth): FoHealthStripCounts {
  const contractDrift =
    (contractHealth?.unknown_statuses ?? []).reduce((sum, entry) => sum + entry.count, 0) +
    (contractHealth?.invalid_risk_count ?? 0) +
    (contractHealth?.invalid_owner_count ?? 0);
  return {
    now: items.filter((item) => queueStateForFoItem(item).state === "now").length,
    nextReady: items.filter((item) => queueStateForFoItem(item).state === "next" && !item.stale).length,
    blocked: items.filter((item) => queueStateForFoItem(item).state === "blocked").length,
    unowned: contractHealth?.unowned_count ?? items.filter((item) => item.owner === "unassigned").length,
    stale: contractHealth?.stale_count ?? items.filter((item) => item.stale).length,
    highRisk: items.filter((item) => item.risk === "high" && item.status !== "done").length,
    contractDrift,
    missingAcceptance: contractHealth?.missing_acceptance_count ?? items.filter((item) => item.missing_acceptance).length,
  };
}

export function computeNextFoTaskId(items: BacklogItem[]): string | null {
  const pick = (status: string) => {
    const candidates = items.filter((it) => queueStateForFoItem(it).state === status && !it.stale);
    if (candidates.length === 0) {
      // fall back to stale items of this status if none non-stale
      const stale = items.filter((it) => queueStateForFoItem(it).state === status);
      if (stale.length === 0) return null;
      stale.sort((a, b) => a.updated.localeCompare(b.updated) || a.id.localeCompare(b.id));
      return stale[0].id;
    }
    candidates.sort((a, b) => a.updated.localeCompare(b.updated) || a.id.localeCompare(b.id));
    return candidates[0].id;
  };
  return pick("now") ?? pick("next") ?? null;
}

export function buildFoCommissionPrompt(detail: BacklogDetail): string {
  return `Du bist eine Orchestrator-Session auf dem Homeserver mit vollem Zugriff. Arbeite GENAU EINEN FO-Backlog-Task ab.
TASK: ${detail.title}   (id: ${detail.id})
SPEC: ~/projects/family-organizer/backlog/items/${detail.id}.md  ← ZUERST vollständig lesen (status, owner, area, risk, Akzeptanzkriterien)
ROOT: ~/projects/family-organizer   GATE: npm run gate:e2e
1) Preflight: cd ~/projects/family-organizer + \`git status\` (FO-Tab liest origin/main → committen, damit Fortschritt sichtbar wird).
2) Task umsetzen (Next.js/Vitest; orchestrate-Skill / Workflow-Harness erlaubt).
3) Gate fahren: npm run gate:e2e — WIRKLICH grün (Mocks = Regressions-Wächter, kein Erstbeweis).
4) NUR bei grün: Item-\`.md\` status→done/in_progress + \`result\`-Zeile aktualisieren; commit + (FO-Repo) push.
5) Discord-Report (nie nur Telegram): Status + Commit + Ergebnis.
ABBRUCH (stop & melde, NICHT loopen/raten): Gate 2–3× rot · DB-Migration/destruktiv · Spec mehrdeutig · etwas außerhalb des Task-Scopes müsste geändert werden.`;
}

export function filterFoItems(
  items: BacklogItem[],
  q: string,
  filters: FoFilterOptions,
): BacklogItem[] {
  let result = items;

  if (q.trim()) {
    const lower = q.toLowerCase();
    result = result.filter(
      (it) =>
        it.title.toLowerCase().includes(lower) ||
        it.id.toLowerCase().includes(lower) ||
        it.area.toLowerCase().includes(lower) ||
        it.owner.toLowerCase().includes(lower) ||
        (it.excerpt ?? "").toLowerCase().includes(lower),
    );
  }

  if (filters.owner) {
    result = result.filter((it) => it.owner === filters.owner);
  }
  if (filters.risk) {
    result = result.filter((it) => it.risk === filters.risk);
  }
  if (filters.area) {
    result = result.filter((it) => it.area === filters.area);
  }
  if (filters.status) {
    result = result.filter((it) => it.status === filters.status);
  }
  if (filters.stale === true) {
    result = result.filter((it) => it.stale);
  }

  return result;
}

export function sortFoItems(items: BacklogItem[], key: FoSortKey): BacklogItem[] {
  const arr = [...items];
  switch (key) {
    case "risk":
      arr.sort(
        (a, b) =>
          (RISK_ORDER[a.risk] ?? 9) - (RISK_ORDER[b.risk] ?? 9) ||
          a.updated.localeCompare(b.updated) ||
          a.id.localeCompare(b.id),
      );
      break;
    case "age":
      arr.sort(
        (a, b) =>
          a.updated.localeCompare(b.updated) ||
          a.id.localeCompare(b.id),
      );
      break;
    case "status":
      arr.sort(
        (a, b) =>
          (STATUS_ORDER[a.status] ?? 9) - (STATUS_ORDER[b.status] ?? 9) ||
          a.updated.localeCompare(b.updated) ||
          a.id.localeCompare(b.id),
      );
      break;
  }
  return arr;
}
