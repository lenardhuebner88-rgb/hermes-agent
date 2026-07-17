/**
 * Board-Subtab: kompakte Liste aller Board-Tasks status-gruppiert.
 *
 * Zeigt ALLE Tasks aus useBoard (status-gruppiert nach Spalten), auch
 * Solo-Tasks ohne Kette. Client-Filter nach Text, Status und Assignee.
 * Klick auf eine Zeile öffnet den NodeDetailDrawer aus S3.
 *
 * Bewusst KEINE Task-Erstellung (Anti-Scope).
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { fetchJSON } from "@/lib/api";
import { profileInitial, profileColorClass, premiumLaneMarker, fmtUsd } from "../../lib/fleetHub";
import { BoardArchiveResponseSchema, parseOrThrow } from "../../lib/schemas";
import { inspectEpochSeconds, validateChronology } from "../../lib/derive";
import { taskStatusLabel } from "../../lib/tones";
import type { BoardArchiveResponse, BoardResponse, BoardTask, TaskStatus } from "../../lib/types";
import {
  loadDoneBoardPage,
  type DoneBoardPage,
  type DonePageLoader,
  type PaginatedBoardResponse,
} from "../../hooks/workersBoard";
import { type ChainNode } from "./shared";
import { ExpandableText } from "./HeuteTab";

interface BoardTabProps {
  board: PaginatedBoardResponse | BoardResponse | null;
  boardSlug?: string | null;
  loadArchivePage?: ArchivePageLoader;
  loadDonePage?: DonePageLoader;
  /** Foreign boards are visibility-only in Stufe 3. */
  readOnly?: boolean;
  /** Callback: öffnet den Karten-Detail-Drawer. */
  onOpenNodeDetail: (taskId: string, chainNodes?: ChainNode[]) => void;
  selectedNodeId?: string | null;
  detailControlsId?: string;
}

interface ArchivePageQuery {
  board: string | null;
  q: string;
  assignee: string | null;
  limit: number;
  cursor: string | null;
}

type ArchivePageLoader = (
  query: ArchivePageQuery,
  signal: AbortSignal,
) => Promise<BoardArchiveResponse>;

const fetchArchivePage: ArchivePageLoader = async (query, signal) => {
  const params = new URLSearchParams({ limit: String(query.limit) });
  if (query.board) params.set("board", query.board);
  if (query.q) params.set("q", query.q);
  if (query.assignee) params.set("assignee", query.assignee);
  if (query.cursor) params.set("cursor", query.cursor);
  return parseOrThrow(
    BoardArchiveResponseSchema,
    await fetchJSON<unknown>(`/api/plugins/kanban/board/archive?${params.toString()}`, { signal }),
    "kanban/board/archive",
  );
};

// Reihenfolge der Status-Spalten für die Gruppierung (wie das Board).
const STATUS_ORDER: TaskStatus[] = [
  "triage", "todo", "scheduled", "ready", "running",
  "blocked", "review", "done", "archived",
];

function timestampValue(value: number | null | undefined, now: number): { dateTime: string | null; label: string } | null {
  if (value == null) return null;
  const inspected = inspectEpochSeconds(value, now);
  if (!inspected.valid) return { dateTime: null, label: "Zeit ungültig" };
  const date = new Date(value * 1000);
  return {
    dateTime: date.toISOString(),
    label: date.toLocaleString("de-DE", {
      dateStyle: "medium",
      timeStyle: "medium",
      timeZone: "Europe/Berlin",
    }) + (inspected.relation === "future" ? " · zukünftig" : ""),
  };
}

function TaskInformation({ task, now }: { task: BoardTask; now: number }) {
  const timestamps = [
    ["Erstellt", task.created_at],
    ["Gestartet", task.started_at],
    ["Fertig", task.completed_at],
    ["Archiviert", task.archived_at],
    ["Fällig", task.due_at],
    ["Heartbeat", task.last_heartbeat_at],
  ] as const;
  const chronology = validateChronology({
    createdAt: task.created_at,
    startedAt: task.started_at,
    completedAt: task.completed_at,
  });

  return (
    <details className="fleet-boardtab-disclosure">
      <summary aria-label={`Weitere Informationen zu ${task.title || task.id}`}>Details</summary>
      <dl className="fleet-boardtab-details">
        {task.assignee && <><dt>Assignee</dt><dd>{task.assignee}</dd></>}
        {task.priority !== 0 && <><dt>Priorität</dt><dd>{task.priority}</dd></>}
        {task.comment_count > 0 && <><dt>Kommentare</dt><dd>{task.comment_count}</dd></>}
        {task.link_counts.parents > 0 && <><dt>Vorgänger</dt><dd>{task.link_counts.parents}</dd></>}
        {task.link_counts.children > 0 && <><dt>Nachfolger</dt><dd>{task.link_counts.children}</dd></>}
        {task.progress && task.progress.total > 0 && <><dt>Fortschritt</dt><dd>{task.progress.done}/{task.progress.total}</dd></>}
        {!chronology.valid && <><dt>Zeitfolge</dt><dd>{chronology.reason}</dd></>}
        {timestamps.map(([label, value]) => {
          const formatted = timestampValue(value, now);
          return formatted ? (
            <div className="fleet-boardtab-detail-pair" key={label}>
              <dt>{label}</dt>
              <dd>{formatted.dateTime ? <time dateTime={formatted.dateTime}>{formatted.label}</time> : <span>{formatted.label}</span>}</dd>
            </div>
          ) : null;
        })}
      </dl>
    </details>
  );
}

export function BoardTab({
  board,
  boardSlug = null,
  loadArchivePage = fetchArchivePage,
  loadDonePage = loadDoneBoardPage,
  readOnly = false,
  onOpenNodeDetail,
  selectedNodeId = null,
  detailControlsId,
}: BoardTabProps) {
  const [q, setQ] = useState("");
  const [statusFilter, setStatusFilter] = useState<TaskStatus | "all">("all");
  const [assigneeFilter, setAssigneeFilter] = useState<string>("all");
  const [archiveTasks, setArchiveTasks] = useState<BoardTask[]>([]);
  const [archivePage, setArchivePage] = useState<BoardArchiveResponse | null>(null);
  const [archiveLoading, setArchiveLoading] = useState(false);
  const [archiveError, setArchiveError] = useState<string | null>(null);
  const archiveRequestRef = useRef<{ serial: number; controller: AbortController } | null>(null);
  const archiveSerialRef = useRef(0);
  const isArchive = statusFilter === "archived";
  const [doneTasks, setDoneTasks] = useState<BoardTask[]>([]);
  const [donePage, setDonePage] = useState<DoneBoardPage | null>(null);
  const [doneLoading, setDoneLoading] = useState(true);
  const [doneError, setDoneError] = useState<string | null>(null);
  const doneRequestRef = useRef<{ serial: number; controller: AbortController } | null>(null);
  const doneSerialRef = useRef(0);

  const runDoneLoad = useCallback(async (cursor: string | null, append: boolean) => {
    doneRequestRef.current?.controller.abort();
    const controller = new AbortController();
    const serial = ++doneSerialRef.current;
    doneRequestRef.current = { serial, controller };
    setDoneLoading(true);
    setDoneError(null);
    if (!append) {
      setDonePage(null);
      setDoneTasks([]);
    }
    try {
      const result = await loadDonePage({ board: boardSlug, cursor }, controller.signal);
      if (controller.signal.aborted || doneSerialRef.current !== serial) return;
      const page = result.done_page;
      if (!page) throw new Error("Board-Antwort enthält keine done_page");
      const tasks = result.columns.find((column) => column.name === "done")?.tasks ?? [];
      setDonePage(page);
      setDoneTasks((current) => {
        if (!append) return tasks;
        const byId = new Map(current.map((task) => [task.id, task]));
        for (const task of tasks) byId.set(task.id, task);
        return Array.from(byId.values());
      });
    } catch (error) {
      if (controller.signal.aborted || doneSerialRef.current !== serial) return;
      setDoneError(error instanceof Error ? error.message : String(error));
      if (!append) {
        setDonePage(null);
        setDoneTasks([]);
      }
    } finally {
      if (!controller.signal.aborted && doneSerialRef.current === serial) setDoneLoading(false);
    }
  }, [boardSlug, loadDonePage]);

  useEffect(() => {
    if (!board) {
      setDoneTasks([]);
      setDonePage(null);
      setDoneLoading(false);
      return;
    }
    const timer = window.setTimeout(() => void runDoneLoad(null, false), 0);
    return () => {
      window.clearTimeout(timer);
      doneRequestRef.current?.controller.abort();
    };
  }, [board?.latest_event_id, boardSlug, runDoneLoad]);

  const runArchiveLoad = useCallback(async (cursor: string | null, append: boolean) => {
    archiveRequestRef.current?.controller.abort();
    const controller = new AbortController();
    const serial = ++archiveSerialRef.current;
    archiveRequestRef.current = { serial, controller };
    setArchiveLoading(true);
    setArchiveError(null);
    if (!append) {
      setArchivePage(null);
      setArchiveTasks([]);
    }
    try {
      const result = await loadArchivePage({
        board: boardSlug,
        q: q.trim(),
        assignee: assigneeFilter === "all" ? null : assigneeFilter,
        limit: 50,
        cursor,
      }, controller.signal);
      if (controller.signal.aborted || archiveSerialRef.current !== serial) return;
      setArchivePage(result);
      setArchiveTasks((current) => {
        if (!append) return result.tasks;
        const byId = new Map(current.map((task) => [task.id, task]));
        for (const task of result.tasks) byId.set(task.id, task);
        return Array.from(byId.values());
      });
    } catch (error) {
      if (controller.signal.aborted || archiveSerialRef.current !== serial) return;
      setArchiveError(error instanceof Error ? error.message : String(error));
      if (!append) {
        setArchivePage(null);
        setArchiveTasks([]);
      }
    } finally {
      if (!controller.signal.aborted && archiveSerialRef.current === serial) {
        setArchiveLoading(false);
      }
    }
  }, [assigneeFilter, boardSlug, loadArchivePage, q]);

  useEffect(() => {
    if (!isArchive) {
      archiveRequestRef.current?.controller.abort();
      return;
    }
    const timer = window.setTimeout(() => void runArchiveLoad(null, false), 0);
    return () => {
      window.clearTimeout(timer);
      archiveRequestRef.current?.controller.abort();
    };
  }, [isArchive, runArchiveLoad]);

  // Alle Assignees aus dem Board extrahieren (für Filter-Dropdown).
  const allAssignees = useMemo(() => {
    const set = new Set<string>();
    for (const assignee of isArchive ? (archivePage?.assignees ?? []) : (board?.assignees ?? [])) set.add(assignee);
    return Array.from(set).sort();
  }, [archivePage?.assignees, board?.assignees, isArchive]);

  // Alle Tasks flach, dann filtern.
  const allTasks = useMemo(() => {
    if (isArchive) return archiveTasks;
    const flat: BoardTask[] = (board?.columns ?? []).flatMap((column) =>
      column.name === "done" ? doneTasks : column.tasks
    );
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
  }, [archiveTasks, assigneeFilter, board, doneTasks, isArchive, q, statusFilter]);

  const filtersActive = statusFilter !== "all" || assigneeFilter !== "all" || q.trim() !== "";
  const activeFilterLabels = [
    statusFilter !== "all" ? `Status: ${taskStatusLabel[statusFilter] ?? statusFilter}` : null,
    assigneeFilter !== "all" ? `Assignee: ${assigneeFilter}` : null,
    q.trim() ? `Suche: ${q.trim()}` : null,
  ].filter((label): label is string => label != null);

  const resetFilters = () => {
    setQ("");
    setStatusFilter("all");
    setAssigneeFilter("all");
  };

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

  const totalCount = isArchive
    ? (archivePage?.filtered_count ?? 0)
    : (board?.columns ?? []).reduce(
      (n, column) => n + (column.name === "done" ? (donePage?.total_count ?? 0) : column.tasks.length),
      0,
    );

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

      {filtersActive ? (
        <div className="fleet-boardtab-filter-active" role="status" aria-label="Aktive Board-Filter">
          <span className="fleet-boardtab-filter-active-label">Filter aktiv</span>
          {activeFilterLabels.map((label) => <span key={label} className="fleet-boardtab-filter-value">{label}</span>)}
          <button type="button" onClick={resetFilters} aria-label="Alle Filter zurücksetzen">Zurücksetzen</button>
        </div>
      ) : null}

      {isArchive && archivePage ? (
        <div className="fleet-boardtab-archive-state" role="status" aria-live="polite">
          <span>{archiveTasks.length} von {archivePage.filtered_count} Archivkarten geladen</span>
          {archivePage.filtered_count !== archivePage.total_count ? (
            <span>{archivePage.total_count} insgesamt</span>
          ) : null}
        </div>
      ) : null}
      {isArchive && archiveError ? (
        <div className="fleet-boardtab-archive-error" role="alert">
          <strong>Archiv konnte nicht geladen werden.</strong>
          <span>{archiveError}</span>
        </div>
      ) : null}
      {!isArchive && doneError ? (
        <div className="fleet-boardtab-archive-error" role="alert">
          <strong>Fertige Tasks konnten nicht geladen werden.</strong>
          <span>{doneError}</span>
        </div>
      ) : null}

      {/* Status-Gruppen */}
      {isArchive && archiveLoading && archiveTasks.length === 0 ? (
        <div className="fleet-empty" aria-live="polite">
          <div className="fleet-empty-title">Archiv wird geladen …</div>
          <div className="fleet-empty-sub">Die aktive Board-Abfrage bleibt dabei klein.</div>
        </div>
      ) : !isArchive && doneLoading && grouped.length === 0 ? (
        <div className="fleet-empty" aria-live="polite">
          <div className="fleet-empty-title">Board wird geladen …</div>
        </div>
      ) : grouped.length === 0 ? (
        <div className="fleet-empty">
          <div className="fleet-empty-title">
            {isArchive
              ? (archivePage?.total_count === 0 ? "Archiv ist leer" : "Keine Archivtreffer")
              : (totalCount === 0 ? "Keine Tasks" : "Keine Treffer")}
          </div>
          <div className="fleet-empty-sub">
            {isArchive
              ? (archivePage?.total_count === 0 ? "Es sind keine archivierten Karten vorhanden." : "Suche oder Assignee-Filter anpassen.")
              : (totalCount === 0 ? "Das Board ist leer." : "Filter anpassen.")}
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
            {tasks.map((t) => {
              const metaTitle = [
                t.id.slice(0, 8),
                t.assignee,
                t.priority !== 0 ? `Prio ${t.priority}` : null,
                t.comment_count > 0 ? `${t.comment_count} Kommentare` : null,
                t.link_counts.parents > 0 ? `${t.link_counts.parents} Vorgänger` : null,
                t.link_counts.children > 0 ? `${t.link_counts.children} Nachfolger` : null,
                t.root_id && t.root_id !== t.id && t.link_counts.children === 0 ? `→ ${(t.root_id ?? "").slice(0, 8)}` : null,
                t.cost_effective_usd != null && t.cost_effective_usd > 0 ? fmtUsd(t.cost_effective_usd) : null,
                t.progress && t.progress.total > 0 ? `${t.progress.done}/${t.progress.total}` : null,
              ].filter(Boolean).join(" · ");
              const content = (
                <>
                <span
                  className={`fleet-avatar ${t.assignee ? profileColorClass(t.assignee) : "fleet-avatar-default"}`}
                  {...premiumLaneMarker(t.assignee)}
                  aria-label={t.assignee ? `Assignee ${t.assignee}` : "Kein Assignee"}
                >
                  {t.assignee ? profileInitial(t.assignee) : "?"}
                </span>
                <span className="fleet-boardtab-row-main">
                  <ExpandableText className="fleet-boardtab-title" text={t.title || t.id} />
                  <span className="fleet-boardtab-meta" title={metaTitle}>
                    <span className="fleet-boardtab-id">{t.id.slice(0, 8)}</span>
                    {t.assignee && <span className="fleet-boardtab-assignee">{t.assignee}</span>}
                    {t.priority !== 0 && <span className="fleet-boardtab-priority">Prio {t.priority}</span>}
                    {t.comment_count > 0 && <span className="fleet-boardtab-comments">{t.comment_count} Kommentare</span>}
                    {t.link_counts.parents > 0 && <span className="fleet-boardtab-parents">{t.link_counts.parents} Vorgänger</span>}
                    {t.link_counts.children > 0 && (
                      <span className="fleet-boardtab-chain" title="Teil einer Kette">{t.link_counts.children} Nachfolger</span>
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
                </>
              );
              return (
                <div key={t.id} className="fleet-boardtab-card">
                  {readOnly ? (
                    <div className="fleet-boardtab-row fleet-boardtab-row-readonly" title="Fremd-Board · nur lesen">{content}</div>
                  ) : (
                    <button
                      className={`fleet-boardtab-row${selectedNodeId === t.id ? " fleet-boardtab-row-selected" : ""}`}
                      onClick={() => onOpenNodeDetail(t.id)}
                      aria-expanded={selectedNodeId === t.id}
                      aria-controls={detailControlsId}
                    >
                      {content}
                    </button>
                  )}
                  <TaskInformation task={t} now={board?.now ?? Math.floor(Date.now() / 1000)} />
                </div>
              );
            })}
          </section>
        ))
      )}
      {isArchive && archivePage?.has_more ? (
        <button
          type="button"
          className="fleet-boardtab-load-more"
          disabled={archiveLoading || !archivePage.next_cursor}
          onClick={() => void runArchiveLoad(archivePage.next_cursor, true)}
          aria-label="Weitere Archivkarten laden"
        >
          {archiveLoading ? "Lädt …" : "Mehr laden"}
        </button>
      ) : null}
      {!isArchive && donePage?.has_more && (statusFilter === "all" || statusFilter === "done") ? (
        <button
          type="button"
          className="fleet-boardtab-load-more"
          disabled={doneLoading || !donePage.next_cursor}
          onClick={() => void runDoneLoad(donePage.next_cursor, true)}
          aria-label="Weitere fertige Tasks laden"
        >
          {doneLoading ? "Lädt …" : "Mehr laden"}
        </button>
      ) : null}
    </div>
  );
}
