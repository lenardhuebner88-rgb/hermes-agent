/**
 * Fleet-Hub — pure derivation helpers.
 * No React, no side-effects, no fetch — injizierbare `now` für Tests.
 */
import type { Worker, ChainGraphResponse } from "./types";
import type { RunsDailyResponse, RunsDailyPoint } from "./schemas";
import { elapsedSeconds, inspectEpochSeconds } from "./derive";

export type { RunsDailyPoint, RunsDailyResponse };

// ─── Lagezeile ──────────────────────────────────────────────────────────────

export interface LagezeileInput {
  workers: Worker[];
  blockedCount: number;
  pendingApprovals: number;
}

/**
 * buildLagezeile: deutscher Satz, der die aktuelle Flottenlage in einem Atemzug
 * beschreibt. Reine Funktion — kein Zustand, keine Seiteneffekte.
 */
export function buildLagezeile(input: LagezeileInput): string {
  const { workers, blockedCount, pendingApprovals } = input;
  const running = workers.filter((w) => w.run_status === "running").length;

  const parts: string[] = [];

  if (running === 0) {
    parts.push("Keine Worker aktiv");
  } else if (running === 1) {
    parts.push("Ein Worker läuft");
  } else {
    parts.push(`${running} Worker laufen`);
  }

  if (blockedCount === 0) {
    // "nichts blockiert" steht immer im Hauptsatz — auch wenn Pläne warten.
    parts[0] += ", nichts blockiert";
  } else {
    const label = blockedCount === 1 ? "eine Aufgabe blockiert" : `${blockedCount} Aufgaben blockiert`;
    parts.push(label);
  }
  if (pendingApprovals > 0) {
    const label = pendingApprovals === 1 ? "ein Plan wartet auf deine Freigabe" : `${pendingApprovals} Pläne warten auf deine Freigabe`;
    parts.push(label);
  }

  return parts.join(" — ") + ".";
}

// ─── ETA-Fraktion ────────────────────────────────────────────────────────────

/**
 * etaFraction: Fortschritt in [0, 0.95] aus vergangener Zeit vs. p50-ETA.
 * Gibt null zurück wenn eta_p50_seconds fehlt oder 0 ist.
 * Gedeckelt auf 0.95 (nie „fertig" in der UI, solange der Worker noch läuft).
 */
export function etaFraction(startedAt: number, etaP50Seconds: number | null | undefined, now: number): number | null {
  if (!etaP50Seconds || etaP50Seconds <= 0) return null;
  const elapsed = elapsedSeconds(startedAt, now);
  if (elapsed == null) return null;
  return Math.min(0.95, elapsed / etaP50Seconds);
}

// ─── Run-Fortschritt (S2) ───────────────────────────────────────────────────

/**
 * runProgressFraction: 0..1 Fortschritt eines Workers.
 * Bevorzugt das strukturierte Backend-Feld run_progress (elapsed/max_runtime),
 * das eine ehrliche, task-spezifische Ratio liefert — KEINE Cohort-p50-Schätzung.
 * Fällt auf etaFraction zurück, wenn run_progress null/missing (z.B. claude-cli
 * Lanes ohne Runtime-Cap, alte Workers vor S2). Gibt null zurück wenn beides
 * fehlt — die Rail wird dann nicht gerendert (wie bisher).
 */
export function runProgressFraction(
  w: { run_progress?: number | null; started_at: number; eta_p50_seconds?: number | null },
  now: number,
): number | null {
  if (typeof w.run_progress === "number" && w.run_progress >= 0 && w.run_progress <= 1) {
    return w.run_progress;
  }
  return etaFraction(w.started_at, w.eta_p50_seconds, now);
}

// ─── Heartbeat-Alter ─────────────────────────────────────────────────────────

/**
 * heartbeatAge: Sekunden seit dem letzten Heartbeat. null wenn kein Heartbeat.
 */
export function heartbeatAge(lastHeartbeatAt: number | null | undefined, now: number): number | null {
  if (!lastHeartbeatAt) return null;
  return elapsedSeconds(lastHeartbeatAt, now);
}

/** Formatiert Sekunden als kurzes deutsches Label: "9 s", "2 min", "1 h". */
export function fmtSeconds(secs: number): string {
  if (!Number.isFinite(secs) || secs < 0) return "Dauer ungültig";
  if (secs < 60) return `${Math.round(secs)} s`;
  if (secs < 3600) return `${Math.round(secs / 60)} min`;
  return `${Math.round(secs / 3600)} h`;
}

// ─── KPI-Ableitung ───────────────────────────────────────────────────────────

export interface KpiValues {
  /** Laufende Worker. */
  aktiv: number;
  /** Blockierte Tasks auf dem Board. */
  blockiert: number;
  /** Abgeschlossene Tasks in 24h (null wenn Quelle fehlt). */
  fertig24h: number | null;
  /** Kosten in 24h USD (null wenn Quelle fehlt). */
  kosten24h: number | null;
  /** true wenn kosten24h aus cost_usd_equivalent statt realem actual_cost_usd stammt. */
  kosten24hEquiv: boolean;
}

export interface CostDisplayValue {
  value: number | null;
  isEquivalent: boolean;
}

function positiveCost(value: number | null | undefined): number | null {
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? value : null;
}

/** Reale Kosten gewinnen; cost_usd_equivalent nur als sichtbar markierten Fallback nutzen. */
export function costDisplayValue(
  actualCostUsd: number | null | undefined,
  equivalentCostUsd: number | null | undefined,
): CostDisplayValue {
  const actual = positiveCost(actualCostUsd);
  if (actual != null) {
    return { value: actual, isEquivalent: false };
  }
  const equivalent = positiveCost(equivalentCostUsd);
  if (equivalent != null) {
    return { value: equivalent, isEquivalent: true };
  }
  return { value: null, isEquivalent: false };
}

/**
 * deriveKpi: leitet KPI-Werte aus Live-Worker-Daten und dem Board ab.
 * Wenn `actual_cost_usd` fehlt/0 ist, darf `cost_usd_equivalent` nur markiert als Äquivalenzwert erscheinen.
 */
export function deriveKpi(
  workers: Worker[],
  blockedCount: number,
  todayActualCostUsd: number | null | undefined,
  todayRuns: number | null | undefined,
  todayEquivalentCostUsd?: number | null | undefined,
): KpiValues {
  const cost = costDisplayValue(todayActualCostUsd, todayEquivalentCostUsd);
  return {
    aktiv: workers.filter((w) => w.run_status === "running").length,
    blockiert: blockedCount,
    fertig24h: todayRuns ?? null,
    kosten24h: cost.value,
    kosten24hEquiv: cost.isEquivalent,
  };
}

// ─── Profil-Avatar ───────────────────────────────────────────────────────────

/** Erster Buchstabe des Profil-Namens, großgeschrieben. */
export function profileInitial(profile: string): string {
  const clean = profile.replace(/^(coder|premium|reviewer|reviewer|verifier|scout|builder|analyst)-?/i, "").trim() || profile;
  return (clean[0] ?? profile[0] ?? "?").toUpperCase();
}

/** Avatar-Farb-Klasse je Profil (für data-fleet-Scope). */
export function profileColorClass(profile: string): string {
  if (isPremiumLane(profile)) return "fleet-avatar-prem";
  if (/reviewer|review/i.test(profile)) return "fleet-avatar-rev";
  return "fleet-avatar-default";
}

/** Premium-Lane ist zusätzlich zur Farbe immer per Text-Marker benannt. */
export function isPremiumLane(profile: string | null | undefined): boolean {
  return /premium|opus/i.test(profile ?? "");
}

export function premiumLaneMarker(profile: string | null | undefined): {
  title?: "Premium-Lane";
  "aria-label"?: "Premium-Lane";
} {
  return isPremiumLane(profile)
    ? { title: "Premium-Lane", "aria-label": "Premium-Lane" }
    : {};
}

// ─── Token-Formatierung ──────────────────────────────────────────────────────

/** Formatiert Tokenanzahl als kompakten String: "61,4k", "1,2M". */
export function fmtTokens(n: number | null | undefined): string {
  if (n == null) return "—";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1).replace(".", ",")}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1).replace(".", ",")}k`;
  return String(n);
}

/** Formatiert USD-Betrag als "$1,23" oder "—" wenn null. */
export function fmtUsd(usd: number | null | undefined): string {
  if (usd == null) return "—";
  return `$${usd.toFixed(2).replace(".", ",")}`;
}

// ─── PlanSpec-Freigabe-Badge ─────────────────────────────────────────────────

/** true wenn die PlanSpec auf den Operator wartet (freigabe = "operator" + hold). */
export function planSpecWaitsForOperator(freigabe: string | null | undefined, kanbanState: string | null | undefined): boolean {
  const normalizedFreigabe = String(freigabe || "").trim().toLowerCase();
  const normalizedState = String(kanbanState || "").trim().toLowerCase();
  return normalizedFreigabe === "operator" && (normalizedState === "queued" || normalizedState === "not_ingested");
}

export type PlanSpecActionState = {
  freigabe?: string | null;
  kanban_state?: string | null;
  kanban_root_status?: string | null;
  kanban_root_task_id?: string | null;
  kanban_child_total?: number | null;
  kanban_child_done?: number | null;
  kanban_child_running?: number | null;
  kanban_child_blocked?: number | null;
};

/** true wenn eine signierte PlanSpec-Kette noch geparkt ist und gestartet werden kann. */
export function planSpecHasParkedSignedChain(plan: PlanSpecActionState): boolean {
  if (String(plan.freigabe || "").trim().toLowerCase() !== "complete") return false;
  if (!plan.kanban_root_task_id) return false;
  const state = String(plan.kanban_state || "").trim().toLowerCase();
  const rootStatus = String(plan.kanban_root_status || "").trim().toLowerCase();
  if (rootStatus === "scheduled") return true;
  if (state !== "queued") return false;
  const total = Number(plan.kanban_child_total ?? 0);
  if (total <= 0) return false;
  const accounted = Number(plan.kanban_child_done ?? 0)
    + Number(plan.kanban_child_running ?? 0)
    + Number(plan.kanban_child_blocked ?? 0);
  return accounted < total;
}

export function planSpecAwaitsPlanAction(plan: PlanSpecActionState): boolean {
  return planSpecWaitsForOperator(plan.freigabe, plan.kanban_state)
    || planSpecHasParkedSignedChain(plan);
}

/**
 * Fehlermeldung aus einem fehlgeschlagenen PlanSpec-Ingest destillieren.
 *
 * fetchJSON wirft bei !res.ok `new Error(`${status}: ${bodyText}`)`. Der Body ist
 * bei 400 ein FastAPI-Envelope `{"detail":{"findings":[…]}}` (siehe
 * ingest_planspec → PlanSpecBlocked), bei anderen Fehlern häufig `{"detail":"…"}`.
 * Bevorzugt werden response.findings (verschachtelt oder top-level) und
 * response.detail (String); ist der Body kein verwertbares JSON, bleibt die rohe
 * Meldung (bzw. der Fallback, wenn diese leer ist).
 */
export function extractIngestError(e: unknown, fallback: string): string {
  const msg = e instanceof Error ? e.message : String(e);
  const jsonStart = msg.indexOf("{");
  if (jsonStart >= 0) {
    try {
      const parsed = JSON.parse(msg.slice(jsonStart)) as {
        detail?: unknown;
        findings?: unknown;
      };
      const detail = parsed.detail;
      const nestedFindings =
        detail && typeof detail === "object" && !Array.isArray(detail)
          ? (detail as { findings?: unknown }).findings
          : undefined;
      const findings = nestedFindings ?? parsed.findings;
      if (Array.isArray(findings) && findings.length > 0) {
        return findings.map((f) => String(f)).join(" · ");
      }
      if (typeof detail === "string" && detail.trim()) return detail;
    } catch {
      /* Body war kein JSON — rohe Meldung nutzen */
    }
  }
  return msg || fallback;
}

// ─── Plan-Cockpit Hilfsfunktionen ─────────────────────────────────────────────

/**
 * budgetTone: Farb-Ton für ein Token-Budget-Fenster basierend auf Prozentsatz.
 * - < 60 %   → "ok" (grün)
 * - 60–84 %  → "warn" (amber)
 * - ≥ 85 %   → "danger" (rot)
 * Gibt null zurück wenn used_percent nicht vorhanden.
 */
export type BudgetTone = "ok" | "warn" | "danger";

export function budgetTone(usedPercent: number | null | undefined): BudgetTone | null {
  if (usedPercent == null) return null;
  if (usedPercent >= 85) return "danger";
  if (usedPercent >= 60) return "warn";
  return "ok";
}

/**
 * derivePlanLanes: Leitet Lanes aus den PlanSpec-Subtasks ab.
 * Gibt je Lane-Namen einen Eintrag mit Beschreibung und Modell-Vorschlag zurück.
 * Erwartet subtasks aus PlanSpecDetailResponse.
 */
export interface PlanLane {
  lane: string;
  /** Erster Subtask-Titel für die Beschreibung (kurz). */
  description: string;
}

export function derivePlanLanes(
  subtasks: Array<{ lane: string; title: string }>,
): PlanLane[] {
  const seen = new Map<string, string>();
  for (const st of subtasks) {
    if (st.lane && !seen.has(st.lane)) {
      seen.set(st.lane, st.title);
    }
  }
  return Array.from(seen.entries()).map(([lane, description]) => ({ lane, description }));
}

/**
 * buildApproveRequest: Baut den POST-Body für /planspecs/approve.
 * Nur geänderte Lane-Models (Abweichung vom Preset-Default) werden gesendet.
 *
 * @param rootTaskId - Root-Task-ID der freizugebenden Kette
 * @param laneModels - Map lane → gewähltes Modell (lokaler Zustand, alle Lanes)
 * @param presetDefaults - Map lane → Standard-Modell aus dem Lane-Preset-Endpoint
 * @param injectScout - true wenn Scout vorab aktiviert
 */
export interface ApproveRequest {
  root_task_id: string;
  lane_models?: Record<string, string>;
  assignee_overrides?: Record<string, string>;
  inject_scout: boolean;
}

export function buildApproveRequest(
  rootTaskId: string,
  assigneeOverrides: Record<string, string>,
  presetDefaults: Record<string, string>,
  injectScout: boolean,
): ApproveRequest {
  // Nur geänderte Lanes senden (Abweichung vom Preset)
  const changedAssignees: Record<string, string> = {};
  for (const [lane, assignee] of Object.entries(assigneeOverrides)) {
    const normalized = assignee.trim();
    if (normalized && normalized !== (presetDefaults[lane] ?? "")) {
      changedAssignees[lane] = normalized;
    }
  }
  return {
    root_task_id: rootTaskId,
    assignee_overrides: changedAssignees,
    inject_scout: injectScout,
  };
}

/**
 * fmtResetAt: Formatiert das reset_at-ISO-Datum als "So 03:00" (de-DE, kurz).
 * Gibt "—" wenn null/leer.
 */
export function fmtResetAt(resetAt: string | null | undefined): string {
  if (!resetAt) return "—";
  try {
    const d = new Date(resetAt);
    if (isNaN(d.getTime())) return "—";
    return d.toLocaleTimeString("de-DE", {
      weekday: "short",
      hour: "2-digit",
      minute: "2-digit",
      timeZone: "Europe/Berlin",
    });
  } catch {
    return "—";
  }
}

/**
 * normalizeUsageWindowLabel: normalisiert die providerübergreifend uneinheitlichen
 * Upstream-Fenster-Labels ("Current session", "Weekly" …) auf ein knappes deutsches
 * Vokabular fürs Token-Budget — statt sie wörtlich (englisch) durchzureichen.
 */
export function normalizeUsageWindowLabel(label: string, windowKey: string | null): string {
  const key = (windowKey ?? "").toLowerCase();
  if (/sess/i.test(label) || key.includes("session") || key.includes("5h")) return "Sitzung";
  if (/week/i.test(label)) return "Woche";
  if (/month/i.test(label)) return "Monat";
  if (/day|24h/i.test(label)) return "24h";
  return label;
}

// ─── Ketten-Subtab Hilfsfunktionen ───────────────────────────────────────────

/**
 * ChainChipState: Drei-Zustands-Modell eines Ketten-Chips.
 * - 'active'    = mind. 1 Kind running/scheduled/blocked
 * - 'pending'   = unfertig, nichts aktiv (mind. 1 Kind todo/ready/offen)
 * - 'completed' = ALLE Kinder (und Root) sind done/archived
 */
export type ChainChipState = "active" | "pending" | "completed";

/**
 * ChainChipDef: Repräsentation eines Ketten-Chips im Subtab.
 */
export interface ChainChipDef {
  rootId: string;
  label: string;
  /** Fortschritt: fertige Nodes / Gesamt-Nodes (0–1). */
  progress: number;
  done: number;
  total: number;
  /** Drei-Zustands-Modell: 'active' | 'pending' | 'completed'. */
  state: ChainChipState;
  /** Zeitstempel des jüngsten completed_at (für Sortierung der fertigen Ketten). */
  completedAt: number | null;
}

/**
 * buildChainChips: Gruppiert Board-Tasks nach root_id und leitet Chips ab.
 * Reihenfolge: active zuerst (Fortschritts-Ring), dann pending (Uhr-Glyph),
 * dann completed (✓ grün).
 *
 * `boardTasks` = ALLE Tasks aus useBoard() (flat, alle Spalten).
 *
 * Nur echte Ketten (Root + mind. 1 Kind) werden angezeigt. Solo-Tasks ohne
 * Kind-Tasks werden ignoriert.
 *
 * Drei-Zustands-Ableitung (state kommt ausschließlich aus dieser Funktion):
 * - 'active'    = mind. 1 Kind (id !== rootId) ist running/scheduled/blocked
 * - 'completed' = ALLE Mitglieder sind done/archived
 * - 'pending'   = alles andere (unfertig, nichts aktiv)
 */
export function buildChainChips(
  boardTasks: Array<{
    id: string;
    title: string;
    root_id?: string | null;
    status: string;
    completed_at?: number | null;
  }>,
): ChainChipDef[] {
  // Gruppiere nach root_id; Tasks ohne root_id gehören zu sich selbst (= Root).
  const groups = new Map<string, typeof boardTasks>();
  for (const t of boardTasks) {
    const key = t.root_id ?? t.id;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key)!.push(t);
  }

  const chips: ChainChipDef[] = [];
  for (const [rootId, members] of groups) {
    // Kette muss mind. 2 Nodes haben (Root + mind. 1 Kind) um relevant zu sein.
    // Solo-Tasks (nur Root, keine Kinder) werden nicht angezeigt.
    const children = members.filter((t) => t.id !== rootId);
    if (children.length === 0) continue;

    const total = members.length;
    const done = members.filter((t) => t.status === "done" || t.status === "archived").length;
    // Drei-Zustands-Ableitung — ausschließlich hier, nie in JSX
    const isActive = children.some((t) =>
      t.status === "running" || t.status === "scheduled" || t.status === "blocked",
    );
    const isCompleted = done === total;
    const state: ChainChipState = isActive ? "active" : isCompleted ? "completed" : "pending";

    // Root-Titel: der Task, dessen id === rootId (oder erster in der Gruppe).
    const root = members.find((t) => t.id === rootId) ?? members[0];
    const label = root?.title ?? rootId;

    const completedAt = members
      .map((t) => t.completed_at ?? 0)
      .reduce((max, v) => (v > max ? v : max), 0) || null;

    chips.push({ rootId, label, progress: total > 0 ? done / total : 0, done, total, state, completedAt });
  }

  // Sortierung: active zuerst, dann pending, dann completed (nach completedAt desc)
  const stateOrder: Record<ChainChipState, number> = { active: 0, pending: 1, completed: 2 };
  chips.sort((a, b) => {
    const oa = stateOrder[a.state];
    const ob = stateOrder[b.state];
    if (oa !== ob) return oa - ob;
    if (a.state === "active") {
      // Beide aktiv: mehr done = weiter vorn
      return b.done - a.done;
    }
    // Beide pending oder beide completed: neueste zuerst
    return (b.completedAt ?? 0) - (a.completedAt ?? 0);
  });

  return chips;
}

/**
 * SegmentKind: Art eines Segments in der Fortschritts-Leiste.
 * done = grün-matt, active = cyan mit Glow, open = gedimmt.
 */
export type SegmentKind = "done" | "active" | "open";

/**
 * buildSegments: Leitet die Segmente der Fortschritts-Leiste aus den Chain-Graph-Nodes ab.
 * Jedes Node = ein Segment (nach level sortiert, dann nach Ketten-Position).
 * Segmente spiegeln den Mockup: grün=fertig, cyan=läuft, offen=weiß/gedimmt.
 */
export function buildSegments(nodes: ChainGraphResponse["nodes"]): SegmentKind[] {
  const sorted = [...nodes].sort((a, b) => a.level - b.level);
  return sorted.map((n) => {
    if (n.status === "done" || n.status === "archived") return "done";
    if (n.status === "running") return "active";
    return "open";
  });
}

/**
 * pickFocusNode: Wählt den Fokus-Node aus dem Chain-Graph.
 * Priorität: 1) laufender Node, 2) erster scheduled/ready Node, 3) letzter fertiger Node.
 */
export function pickFocusNode(nodes: ChainGraphResponse["nodes"]): ChainGraphResponse["nodes"][number] | null {
  if (nodes.length === 0) return null;

  // 1) Laufender Node
  const running = nodes.find((n: ChainGraphResponse["nodes"][number]) => n.status === "running");
  if (running) return running;

  // 2) Nächster geplanter Node (scheduled/ready/todo)
  const sorted = [...nodes].sort((a: ChainGraphResponse["nodes"][number], b: ChainGraphResponse["nodes"][number]) => a.level - b.level);
  const scheduled = sorted.find((n: ChainGraphResponse["nodes"][number]) => n.status === "scheduled" || n.status === "ready" || n.status === "todo");
  if (scheduled) return scheduled;

  // 3) Letzter fertiger Node (höchster Level)
  const done = sorted.filter((n: ChainGraphResponse["nodes"][number]) => n.status === "done" || n.status === "archived");
  return done[done.length - 1] ?? null;
}

/**
 * chainProgress: Prozentzahl (0–100) aus done/total über alle Nodes.
 */
export function chainProgress(nodes: ChainGraphResponse["nodes"]): { pct: number; done: number; total: number } {
  const total = nodes.length;
  if (total === 0) return { pct: 0, done: 0, total: 0 };
  const done = nodes.filter((n: ChainGraphResponse["nodes"][number]) => n.status === "done" || n.status === "archived").length;
  return { pct: Math.round((done / total) * 100), done, total };
}

/**
 * chainTotalCostUsd: Gesamtkosten der Kette (Summe cost_usd aller Nodes, mit markiertem cost_usd_equivalent-Fallback).
 * Gibt null zurück wenn keine Kosten vorhanden (alle 0).
 */
export function chainTotalCostUsd(nodes: ChainGraphResponse["nodes"]): number | null {
  return chainTotalCostUsdWithSource(nodes).value;
}

export function chainTotalCostUsdWithSource(nodes: ChainGraphResponse["nodes"]): CostDisplayValue {
  let total = 0;
  let hasEquivalent = false;
  for (const n of nodes) {
    const cost = costDisplayValue(n.cost_usd, n.cost_usd_equivalent);
    if (cost.value != null) {
      total += cost.value;
      hasEquivalent = hasEquivalent || cost.isEquivalent;
    }
  }
  return total > 0 ? { value: total, isEquivalent: hasEquivalent } : { value: null, isEquivalent: false };
}

// ─── "Wartet auf dich"-Leiste ────────────────────────────────────────────────

/**
 * PendingItem: Ein einzelner Eintrag der "Wartet auf dich"-Leiste.
 * - kind "approval" → Planet-Subtab: Plan; Tap navigiert zum Plan-Subtab.
 * - kind "blocked"  → Planet-Subtab: Risiko; Tap navigiert zum Risiko-Subtab.
 */
export interface PendingItem {
  kind: "approval" | "blocked";
  topic: string;
  /** Ziel-Subtab für den Tap (Plan oder Risiko). */
  targetSubtab: "plan" | "risiko";
}

/**
 * derivePendingItems: Pure Funktion, die wartende Freigaben und blockierte Tasks
 * zu einer geordneten Liste kombiniert.
 *
 * - Freigaben (freigabe: operator + Wartezustand) → je Item, zuerst
 * - Blockierte Tasks mit backend-bestätigtem ``operator_question`` →
 *   als Operator-Halts ans Ende
 *
 * @param planspecs  Alle PlanSpecs (aus usePlanSpecs)
 * @param blockedTasks Board-Tasks mit Status "blocked"
 */
export function derivePendingItems(
  planspecs: Array<PlanSpecActionState & { topic?: string | null; filename?: string }>,
  blockedTasks: Array<{ id: string; title: string; operator_question?: boolean }>,
): PendingItem[] {
  const items: PendingItem[] = [];

  // Wartende Freigaben (Plan-Subtab)
  for (const ps of planspecs) {
    if (planSpecAwaitsPlanAction(ps)) {
      items.push({
        kind: "approval",
        topic: ps.topic || ps.filename || "Plan",
        targetSubtab: "plan",
      });
    }
  }

  // Operator-Halts (Risiko-Subtab)
  for (const t of blockedTasks) {
    if (t.operator_question === true) {
      items.push({
        kind: "blocked",
        topic: t.title,
        targetSubtab: "risiko",
      });
    }
  }

  return items;
}

/**
 * Zählt die Gesamtzahl der wartenden Einträge (pendingItems).
 * Kurzform für `derivePendingItems(...).length`.
 */
export function pendingCount(items: PendingItem[]): number {
  return items.length;
}

/**
 * Leitet den effektiv selektierten PlanSpec-Pfad ab.
 *
 * Invariante: selectedPath bleibt gültig, solange der Pfad noch in
 * pendingPaths enthalten ist. Fehlt er (nach Approve, Reload oder
 * verspätetem Laden), fällt die Auswahl auf den ersten wartenden Eintrag.
 * Bei leerer Liste → null.
 *
 * Wird als pure Funktion gehalten, damit sie im PlanTab direkt inline
 * nutzbar ist und in Tests ohne DOM laufen kann.
 */
export function deriveEffectivePlanPath(
  selectedPath: string | null,
  pendingPaths: string[],
): string | null {
  if (pendingPaths.length === 0) return null;
  if (selectedPath !== null && pendingPaths.includes(selectedPath)) return selectedPath;
  return pendingPaths[0];
}

// ─── Sparkline (Fertig 24h « 7-Tage-Trend) ──────────────────────────────────

/**
 * Ein Sparkline-Punkt: Datum (ISO-Tag) und die Anzahl erledigter Tasks.
 */
export interface SparklinePoint {
  /** ISO-Datum (YYYY-MM-DD), direkt aus der API-Serie. */
  date: string;
  /** Erledigte Tasks an diesem Tag. */
  value: number;
}

/**
 * deriveSparklinePoints: liefert die letzten 7 Tage der runs/daily-Serie
 * als Sparkline-Punkte (date + done_tasks).
 *
 * Reihe: Die API liefert `series` chronologisch aufsteigend (ältester Tag
 * zuerst, jüngster zuletzt). Der Hook ruft `?days=30` ab; wir nehmen die
 * letzten N Einträge (default 7), damit die Sparkline stets die jüngste
 * Woche zeigt, egal wie viele Tage die API tatsächlich liefert.
 *
 * Rückgabe `null`, wenn weniger als 2 Punkte vorhanden sind — dann wird
 * im UI keine Sparkline gerendert (kein Fake, keine Platzhalter-Kurve).
 *
 * Pure Funktion: keine Seiteneffekte, deterministisch, injizierbar in
 * Tests gegen das echte RunsDailyResponse-Format.
 *
 * @param daily Ergebnis von useHermesRunsDaily (RunsDailyResponse | null)
 * @param maxDays maximale Anzahl Tage (default 7)
 */
export function deriveSparklinePoints(
  daily: RunsDailyResponse | null | undefined,
  maxDays = 7,
): SparklinePoint[] | null {
  if (!daily?.series) return null;

  const series: RunsDailyPoint[] = daily.series;
  if (series.length < 2) return null;

  // Defensive: maxDays >= 2, sonst ist keine Linie möglich.
  const limit = Math.max(2, maxDays);
  const slice = series.slice(-limit);

  return slice.map((p) => ({
    date: p.date,
    value: p.done_tasks,
  }));
}

// ─── Puls-Leitstand (S2): Swimlane-Band-Geometrie ─────────────────────────────

/** Klemmt einen Wert in [0, 1] (NaN/Infinity → 0). */
export function clamp01(v: number): number {
  if (!Number.isFinite(v)) return 0;
  if (v < 0) return 0;
  if (v > 1) return 1;
  return v;
}

export interface BandWorker {
  started_at: number;
  eta_p50_seconds?: number | null;
  eta_p90_seconds?: number | null;
  max_runtime_seconds?: number | null;
  run_progress?: number | null;
  heartbeat_ticks?: number[] | null;
}

export interface BandGeometry {
  /** Elapsed-Anteil am Fenster (0..1) — die gefüllte Bandbreite. */
  fillFraction: number;
  /** Position der p50-Marke im Fenster (0..1) oder null ohne p50-ETA. */
  p50Fraction: number | null;
  /** Positionen der Heartbeat-Ticks im Fenster (0..1). */
  tickFractions: number[];
  /** true wenn das Fenster aus echten Perzentilen/Cap stammt (nicht geschätzt). */
  grounded: boolean;
}

/**
 * bandWindowSeconds: die Zeitachse eines Swimlane-Bands in Sekunden.
 * Bevorzugt das ehrliche p90-Perzentil (Mockup-Achsenlabel „p90-Fenster"),
 * fällt auf den Runtime-Cap, dann p50×1.6, zuletzt elapsed×1.3 zurück — damit
 * auch eine frische Lane ohne Historie ein wachsendes Band zeigt.
 */
export function bandWindowSeconds(w: BandWorker, now: number): { seconds: number; grounded: boolean } {
  if (w.eta_p90_seconds && w.eta_p90_seconds > 0) return { seconds: w.eta_p90_seconds, grounded: true };
  if (w.max_runtime_seconds && w.max_runtime_seconds > 0) return { seconds: w.max_runtime_seconds, grounded: true };
  if (w.eta_p50_seconds && w.eta_p50_seconds > 0) return { seconds: w.eta_p50_seconds * 1.6, grounded: true };
  const elapsed = elapsedSeconds(w.started_at, now);
  if (elapsed == null) return { seconds: 1, grounded: false };
  return { seconds: elapsed * 1.3, grounded: false };
}

/**
 * computeBandGeometry: reine Ableitung der Band-Darstellung eines laufenden
 * Workers gegen sein p90-Fenster — Füllung (elapsed/Fenster), p50-Marke und die
 * Heartbeat-Ticks als Positionen im Fenster. `now` injizierbar für Tests.
 */
export function computeBandGeometry(w: BandWorker, now: number): BandGeometry {
  const elapsed = elapsedSeconds(w.started_at, now);
  if (elapsed == null) {
    return { fillFraction: 0, p50Fraction: null, tickFractions: [], grounded: false };
  }
  const win = bandWindowSeconds(w, now);
  const windowSec = win.seconds > 0 ? win.seconds : 1;

  let fill: number;
  if (win.grounded) {
    fill = clamp01(elapsed / windowSec);
  } else if (typeof w.run_progress === "number") {
    fill = clamp01(w.run_progress);
  } else {
    fill = Math.min(0.95, clamp01(elapsed / windowSec));
  }

  const p50Fraction =
    w.eta_p50_seconds && w.eta_p50_seconds > 0 ? clamp01(w.eta_p50_seconds / windowSec) : null;

  const tickFractions = (w.heartbeat_ticks ?? [])
    .filter((timestamp) => inspectEpochSeconds(timestamp, now).valid && timestamp <= now)
    .map((t) => (t - w.started_at) / windowSec)
    .filter((f) => f >= 0 && f <= 1)
    .map((f) => clamp01(f));

  return { fillFraction: fill, p50Fraction, tickFractions, grounded: win.grounded };
}

/**
 * fmtDurationClock: „6m42s" / „45s" / „1h04m" — die Uhrzeit-Notation des Mockups
 * (im Gegensatz zum groben fmtSeconds „7 min"). Für Band-Meta + ETA-Chips.
 */
export function fmtDurationClock(secs: number | null | undefined): string {
  if (secs == null) return "—";
  if (!Number.isFinite(secs) || secs < 0) return "Dauer ungültig";
  const s = Math.round(secs);
  if (s < 60) return `${s}s`;
  if (s < 3600) {
    const m = Math.floor(s / 60);
    return `${m}m${String(s % 60).padStart(2, "0")}s`;
  }
  const totalMinutes = Math.round(s / 60);
  const h = Math.floor(totalMinutes / 60);
  const m = totalMinutes % 60;
  return `${h}h${String(m).padStart(2, "0")}m`;
}

/** „23:59:02" — Uhrzeit eines Unix-Sekunden-Zeitstempels (Europe/Berlin). */
export function fmtClockTime(epochSec: number | null | undefined): string {
  if (!inspectEpochSeconds(epochSec).valid || epochSec == null) return "Zeit ungültig";
  return new Date(epochSec * 1000).toLocaleTimeString("de-DE", {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    timeZone: "Europe/Berlin",
  });
}

// ─── Puls-Leitstand: Lane-Rolle → Farbton ─────────────────────────────────────

/** Rollen-Tint einer Swimlane, gemappt auf die Fleet-Statustrio-/Live-Tokens. */
export type LaneTint = "coder" | "reviewer" | "verifier" | "neutral";

/**
 * laneTint: Rolle → Farb-Familie (Mockup Variante B): coder=cyan (live/primär),
 * reviewer=amber, verifier=grün, sonst neutral (steel-brand). Der Fleet-Skin
 * nutzt cyan bereits als Default-Avatar-Ton — konsistent mit DESIGN.md.
 */
export function laneTint(profile: string | null | undefined): LaneTint {
  const p = (profile ?? "").toLowerCase();
  if (/verif/.test(p)) return "verifier";
  if (/review|critic/.test(p)) return "reviewer";
  if (/coder|premium|opus|claude|build|scout/.test(p)) return "coder";
  return "neutral";
}

// ─── Puls-Leitstand: Pulse-Strip-Ableitung ────────────────────────────────────

export interface PulseSummary {
  slotsUsed: number;
  slotsCap: number | null;
  queue: number;
  doneToday: number | null;
  blocked: number;
  /** Live-Token-Summe (ein+aus) über alle aktiven Worker. */
  tokenSum: number;
}

/**
 * derivePulse: reine Ableitung der drei Pulse-Kacheln. `queue` = Tasks in ready/
 * scheduled (warten auf einen Slot); `tokenSum` = Σ(input+output) der aktiven
 * Worker; `doneToday`/`blocked`/`cap` kommen aus den bereits geladenen Quellen.
 */
export function derivePulse(input: {
  activeWorkers: Array<{ input_tokens?: number | null; output_tokens?: number | null }>;
  cap: number | null;
  queue: number;
  doneToday: number | null;
  blocked: number;
}): PulseSummary {
  const tokenSum = input.activeWorkers.reduce(
    (sum, w) => sum + (w.input_tokens ?? 0) + (w.output_tokens ?? 0),
    0,
  );
  return {
    slotsUsed: input.activeWorkers.length,
    slotsCap: input.cap,
    queue: input.queue,
    doneToday: input.doneToday,
    blocked: input.blocked,
    tokenSum,
  };
}

// ─── Puls-Leitstand: Live-Ticker-Formatierung + Merge ─────────────────────────

export type LiveEventTone = "ok" | "warn" | "alert" | "none";

export interface FormattedLiveEvent {
  /** Statuszeichen-Präfix (✓ / ◼ …) oder null. */
  mark: string | null;
  text: string;
  tone: LiveEventTone;
}

/**
 * formatLiveEvent: reine Ableitung der Ticker-Zeile aus einem LiveEvent.
 * Heartbeats zeigen ihre Note; Status-Kinds bekommen Präfix + Statuston.
 */
export function formatLiveEvent(e: {
  kind: string;
  note?: string | null;
  task_title?: string | null;
  task_id?: string | null;
}): FormattedLiveEvent {
  const title = (e.task_title || e.task_id || "").trim();
  const note = (e.note || "").trim();
  switch (e.kind) {
    case "heartbeat":
      return { mark: null, text: note || "Heartbeat", tone: "none" };
    case "claimed":
      return { mark: null, text: `Slot geclaimt → ${e.task_id || title}`, tone: "none" };
    case "submitted_for_review":
      return { mark: null, text: `${title} → Review`, tone: "none" };
    case "review_released":
      return { mark: "✓", text: `Review frei · ${title}`, tone: "ok" };
    case "completed":
      return { mark: "✓", text: `done · ${note || title}`, tone: "ok" };
    case "integration_merged":
      return { mark: "✓", text: `gemergt · ${title}`, tone: "ok" };
    case "unblocked":
      return { mark: null, text: `entsperrt · ${title}`, tone: "ok" };
    case "blocked":
      return { mark: "◼", text: `blocked · ${title}${note ? ` — ${note}` : ""}`, tone: "warn" };
    case "auto_retried":
      return { mark: "↻", text: `Auto-Retry · ${title}`, tone: "warn" };
    case "timed_out":
      return { mark: "⏱", text: `Timeout · ${title}`, tone: "alert" };
    case "crashed":
      return { mark: "✗", text: `Crash · ${title}`, tone: "alert" };
    case "gave_up":
      return { mark: "✗", text: `aufgegeben · ${title}`, tone: "alert" };
    default:
      return { mark: null, text: note || `${e.kind} · ${title}`.trim(), tone: "none" };
  }
}

/**
 * mergeLiveEvents: fügt neu gepollte (newest-first) Events in den bestehenden
 * (newest-first) Puffer ein — dedupliziert nach Board + id, sortiert nach
 * Ereigniszeit und deckelt auf `cap`. Event-IDs sind nur innerhalb eines
 * Board-DBs eindeutig; ein reines id-Dedup würde Fremd-Board-Ereignisse
 * verschlucken.
 */
export function mergeLiveEvents<T extends { id: number; at?: number; board_slug?: string | null }>(prev: T[], incoming: T[], cap: number): T[] {
  const byBoardAndId = new Map<string, T>();
  for (const e of prev) byBoardAndId.set(`${e.board_slug ?? "current"}:${e.id}`, e);
  for (const e of incoming) byBoardAndId.set(`${e.board_slug ?? "current"}:${e.id}`, e);
  return [...byBoardAndId.values()]
    .sort((a, b) => (b.at ?? 0) - (a.at ?? 0) || b.id - a.id)
    .slice(0, Math.max(0, cap));
}
