/**
 * Fleet-Hub — pure derivation helpers.
 * No React, no side-effects, no fetch — injizierbare `now` für Tests.
 */
import type { Worker, ChainGraphResponse } from "./types";

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
  const elapsed = Math.max(0, now - startedAt);
  return Math.min(0.95, elapsed / etaP50Seconds);
}

// ─── Heartbeat-Alter ─────────────────────────────────────────────────────────

/**
 * heartbeatAge: Sekunden seit dem letzten Heartbeat. null wenn kein Heartbeat.
 */
export function heartbeatAge(lastHeartbeatAt: number | null | undefined, now: number): number | null {
  if (!lastHeartbeatAt) return null;
  return Math.max(0, now - lastHeartbeatAt);
}

/** Formatiert Sekunden als kurzes deutsches Label: "9 s", "2 min", "1 h". */
export function fmtSeconds(secs: number): string {
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
  /** Kosten in 24h USD (null wenn Quelle fehlt oder 0 = Subscription). */
  kosten24h: number | null;
}

/**
 * deriveKpi: leitet KPI-Werte aus Live-Worker-Daten und dem Board ab.
 * Nur echte Quellen — kein Fake. Wenn `actual_cost_usd` fehlt → null.
 */
export function deriveKpi(
  workers: Worker[],
  blockedCount: number,
  todayActualCostUsd: number | null | undefined,
  todayRuns: number | null | undefined,
): KpiValues {
  return {
    aktiv: workers.filter((w) => w.run_status === "running").length,
    blockiert: blockedCount,
    fertig24h: todayRuns ?? null,
    kosten24h: todayActualCostUsd ?? null,
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
  if (/premium|opus/i.test(profile)) return "fleet-avatar-prem";
  if (/reviewer|review/i.test(profile)) return "fleet-avatar-rev";
  return "fleet-avatar-default";
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
export function planSpecWaitsForOperator(freigabe: string, kanbanState: string): boolean {
  return freigabe === "operator" && (kanbanState === "queued" || kanbanState === "not_ingested");
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
  lane_models: Record<string, string>;
  inject_scout: boolean;
}

export function buildApproveRequest(
  rootTaskId: string,
  laneModels: Record<string, string>,
  presetDefaults: Record<string, string>,
  injectScout: boolean,
): ApproveRequest {
  // Nur geänderte Lanes senden (Abweichung vom Preset)
  const changedLanes: Record<string, string> = {};
  for (const [lane, model] of Object.entries(laneModels)) {
    if (model && model !== (presetDefaults[lane] ?? "")) {
      changedLanes[lane] = model;
    }
  }
  return {
    root_task_id: rootTaskId,
    lane_models: changedLanes,
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
 * chainTotalCostUsd: Gesamtkosten der Kette (Summe cost_usd aller Nodes).
 * Gibt null zurück wenn keine Kosten vorhanden (alle 0).
 */
export function chainTotalCostUsd(nodes: ChainGraphResponse["nodes"]): number | null {
  const total = nodes.reduce((sum: number, n: ChainGraphResponse["nodes"][number]) => sum + (n.cost_usd ?? 0), 0);
  return total > 0 ? total : null;
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
 * - Blockierte Tasks (status "blocked", block_reason enthält "operator hold") →
 *   als Operator-Halts ans Ende
 *
 * @param planspecs  Alle PlanSpecs (aus usePlanSpecs)
 * @param blockedTasks Board-Tasks mit Status "blocked"
 */
export function derivePendingItems(
  planspecs: Array<{ freigabe: string; kanban_state: string; topic?: string | null; filename?: string }>,
  blockedTasks: Array<{ id: string; title: string; block_reason?: string | null }>,
): PendingItem[] {
  const items: PendingItem[] = [];

  // Wartende Freigaben (Plan-Subtab)
  for (const ps of planspecs) {
    if (planSpecWaitsForOperator(ps.freigabe, ps.kanban_state)) {
      items.push({
        kind: "approval",
        topic: ps.topic || ps.filename || "Plan",
        targetSubtab: "plan",
      });
    }
  }

  // Operator-Halts (Risiko-Subtab)
  for (const t of blockedTasks) {
    const reason = t.block_reason ?? "";
    const isOperatorHold =
      reason.toLowerCase().includes("operator") ||
      reason.toLowerCase().includes("operator hold");
    if (isOperatorHold) {
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
