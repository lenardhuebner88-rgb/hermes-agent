/**
 * Board-Subtab: kompakte Liste aller Board-Tasks status-gruppiert.
 *
 * Zeigt ALLE Tasks aus useBoard (status-gruppiert nach Spalten), auch
 * Solo-Tasks ohne Kette. Client-Filter nach Text, Status und Assignee.
 * Klick auf eine Zeile öffnet den NodeDetailDrawer aus S3.
 *
 * Bewusst KEINE Task-Erstellung (Anti-Scope).
 */
import { useState, useMemo } from "react";
import { profileInitial, profileColorClass, premiumLaneMarker, fmtUsd } from "../../lib/fleetHub";
import { taskStatusLabel } from "../../lib/tones";
import type { BoardResponse, BoardTask, TaskStatus } from "../../lib/types";
import { type ChainNode } from "./shared";

interface BoardTabProps {
  board: BoardResponse | null;
  /** Callback: öffnet den Karten-Detail-Drawer. */
  onOpenNodeDetail: (taskId: string, chainNodes?: ChainNode[]) => void;
  selectedNodeId?: string | null;
  detailControlsId?: string;
}

// Reihenfolge der Status-Spalten für die Gruppierung (wie das Board).
const STATUS_ORDER: TaskStatus[] = [
  "triage", "todo", "scheduled", "ready", "running",
  "blocked", "review", "done", "archived",
];

export function BoardTab({ board, onOpenNodeDetail, selectedNodeId = null, detailControlsId }: BoardTabProps) {
  const [q, setQ] = useState("");
  const [statusFilter, setStatusFilter] = useState<TaskStatus | "all">("all");
  const [assigneeFilter, setAssigneeFilter] = useState<string>("all");

  // Alle Assignees aus dem Board extrahieren (für Filter-Dropdown).
  const allAssignees = useMemo(() => {
    const set = new Set<string>();
    for (const t of board?.assignees ?? []) set.add(t);
    return Array.from(set).sort();
  }, [board]);

  // Alle Tasks flach, dann filtern.
  const allTasks = useMemo(() => {
    const flat: BoardTask[] = (board?.columns ?? []).flatMap((c) => c.tasks);
    const ql = q.trim().toLowerCase();
    return flat.filter((t) => {
      if (statusFilter !== "all" && t.status !== statusFilter) return false;
      if (assigneeFilter !== "all" && (t.assignee ?? "") !== assigneeFilter) return false;
      if (ql) {
        const hay = `${t.title} ${t.id} ${t.assignee ?? ""} ${t.block_reason ?? ""}`.toLowerCase();
        if (!hay.includes(ql)) return false;
      }
      return true;
    });
  }, [board, q, statusFilter, assigneeFilter]);

  // Filtergebnis nach Status gruppieren (nur Status mit sichtbaren Tasks).
  const grouped = useMemo(() => {
    const map = new Map<TaskStatus, BoardTask[]>();
    for (const t of allTasks) {
      const arr = map.get(t.status) ?? [];
      arr.push(t);
      map.set(t.status, arr);
    }
    return STATUS_ORDER
      .filter((s) => map.has(s))
      .map((s) => ({ status: s, tasks: map.get(s)! }));
  }, [allTasks]);

  const totalCount = (board?.columns ?? []).reduce((n, c) => n + c.tasks.length, 0);

  return (
    <div className="fleet-boardtab">
      {/* Filter-Leiste */}
      <div className="fleet-boardtab-filter">
        <input
          className="fleet-boardtab-suche"
          type="text"
          placeholder="Suchen …"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          aria-label="Tasks durchsuchen"
        />
        <select
          className="fleet-boardtab-select"
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value as TaskStatus | "all")}
          aria-label="Nach Status filtern"
        >
          <option value="all">Alle Status</option>
          {STATUS_ORDER.map((s) => (
            <option key={s} value={s}>{taskStatusLabel[s] ?? s}</option>
          ))}
        </select>
        <select
          className="fleet-boardtab-select"
          value={assigneeFilter}
          onChange={(e) => setAssigneeFilter(e.target.value)}
          aria-label="Nach Assignee filtern"
        >
          <option value="all">Alle Assignees</option>
          {allAssignees.map((a) => (
            <option key={a} value={a}>{a}</option>
          ))}
        </select>
      </div>

      {/* Status-Gruppen */}
      {grouped.length === 0 ? (
        <div className="fleet-empty">
          <div className="fleet-empty-title">{totalCount === 0 ? "Keine Tasks" : "Keine Treffer"}</div>
          <div className="fleet-empty-sub">
            {totalCount === 0 ? "Das Board ist leer." : "Filter anpassen."}
          </div>
        </div>
      ) : (
        grouped.map(({ status, tasks }) => (
          <section key={status} className="fleet-boardtab-group">
            <header className="fleet-boardtab-group-header">
              <span className={`fleet-boardtab-status fleet-status-${status}`}>
                {taskStatusLabel[status] ?? status}
              </span>
              <span className="fleet-boardtab-count">{tasks.length}</span>
            </header>
            {tasks.map((t) => (
              <button
                key={t.id}
                className={`fleet-boardtab-row${selectedNodeId === t.id ? " fleet-boardtab-row-selected" : ""}`}
                onClick={() => onOpenNodeDetail(t.id)}
                aria-expanded={selectedNodeId === t.id}
                aria-controls={detailControlsId}
              >
                <span
                  className={`fleet-avatar ${t.assignee ? profileColorClass(t.assignee) : "fleet-avatar-default"}`}
                  {...premiumLaneMarker(t.assignee)}
                >
                  {t.assignee ? profileInitial(t.assignee) : "?"}
                </span>
                <span className="fleet-boardtab-row-main">
                  <span className="fleet-boardtab-title">{t.title || t.id}</span>
                  <span className="fleet-boardtab-meta">
                    <span className="fleet-boardtab-id">{t.id.slice(0, 8)}</span>
                    {t.link_counts.children > 0 && (
                      <span className="fleet-boardtab-chain" title="Teil einer Kette">⛓ {t.link_counts.children}</span>
                    )}
                    {t.root_id && t.root_id !== t.id && t.link_counts.children === 0 && (
                      <span className="fleet-boardtab-inchain" title="Gehört zu einer Kette">→ {(t.root_id ?? "").slice(0, 8)}</span>
                    )}
                    {t.cost_effective_usd != null && t.cost_effective_usd > 0 && (
                      <span className="fleet-boardtab-cost">{fmtUsd(t.cost_effective_usd)}</span>
                    )}
                    {t.progress && t.progress.total > 0 && (
                      <span className="fleet-boardtab-prog" title="Fortschritt">{t.progress.done}/{t.progress.total}</span>
                    )}
                  </span>
                </span>
              </button>
            ))}
          </section>
        ))
      )}
    </div>
  );
}
