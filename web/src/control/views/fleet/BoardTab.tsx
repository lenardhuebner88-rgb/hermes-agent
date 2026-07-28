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
import { Copy, Link2, SlidersHorizontal } from "lucide-react";
import { fetchJSON } from "@/lib/api";
import { profileInitial, profileColorClass, premiumLaneMarker, fmtUsd } from "../../lib/fleetHub";
import { BoardArchiveResponseSchema, parseOrThrow } from "../../lib/schemas";
import { taskStatusLabel } from "../../lib/tones";
import type { BoardArchiveResponse, BoardResponse, BoardTask, TaskStatus } from "../../lib/types";
import {
  loadDoneBoardPage,
  type DoneBoardPage,
  type DonePageLoader,
  type PaginatedBoardResponse,
} from "../../hooks/workersBoard";
import { type ChainNode } from "./shared";

interface BoardTabProps {
  board: PaginatedBoardResponse | BoardResponse | null;
  boardSlug?: string | null;
  loadArchivePage?: ArchivePageLoader;
  loadDonePage?: DonePageLoader;
  /** Foreign boards are visibility-only in Stufe 3. */
  readOnly?: boolean;
  /**
   * One-shot deep-link status from FleetView (?status=). Applied once when
   * first provided; later prop changes are ignored so the operator filter
   * stays in control after the deep link lands.
   */
  initialStatusFilter?: TaskStatus | null;
  /** Callback: öffnet den Karten-Detail-Drawer. */
  onOpenNodeDetail: (taskId: string, chainNodes?: ChainNode[]) => void;
  /** KF-5: Klick auf den Ketten-Chip — fuehrt in die Ketten-Ansicht dieser Kette. */
  onOpenChain?: (rootId: string) => void;
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

type BoardRegister = "open" | "done" | "archive";
type BoardQuickView = "all" | "unassigned" | "review" | "standalone" | "result";

function copyTaskId(taskId: string): void {
  if (typeof navigator === "undefined" || !navigator.clipboard) return;
  void navigator.clipboard.writeText(taskId).catch(() => undefined);
}

/**
 * Lade-Skeleton (L3-Spec 3.4, Befund 6): 1 KPI-Skeleton-Zeile + 5 Row-
 * Skeletons (Avatar-Kreis + zwei Text-Bars per .hc-skeleton) statt Text-
 * Empty — kein Layout-Sprung beim Daten-Eintreffen. Reines CSS-Markup,
 * test-/SSR-sicher wie SkeletonCard.
 */
function BoardSkeleton({ label }: { label: string }) {
  return (
    <div className="fleet-skel" aria-live="polite" aria-busy="true">
      <span className="sr-only">{label} …</span>
      <div className="fleet-skel-kpis" aria-hidden>
        {Array.from({ length: 4 }).map((_, i) => (
          <div key={i} className="hc-skeleton fleet-skel-kpi" />
        ))}
      </div>
      {Array.from({ length: 5 }).map((_, i) => (
        <div key={i} className="fleet-skel-row" aria-hidden>
          <div className="hc-skeleton fleet-skel-avatar" />
          <div className="fleet-skel-lines">
            <div className="hc-skeleton fleet-skel-bar" />
            <div className="hc-skeleton fleet-skel-bar fleet-skel-bar-short" />
          </div>
        </div>
      ))}
    </div>
  );
}


export function BoardTab({
  board,
  boardSlug = null,
  loadArchivePage = fetchArchivePage,
  loadDonePage = loadDoneBoardPage,
  readOnly = false,
  initialStatusFilter = null,
  onOpenNodeDetail,
  onOpenChain,
  selectedNodeId = null,
  detailControlsId,
}: BoardTabProps) {
  const [q, setQ] = useState("");
  // KF-5: lesbare Ketten-Kennungen je Root-Id (additiv; aeltere Server liefern
  // den Key nicht). Fallback im Chip ist die Root-Id selbst.
  const chainIdentities = board?.chain_identities;
  const [statusFilter, setStatusFilter] = useState<TaskStatus | "all">(() =>
    initialStatusFilter && STATUS_ORDER.includes(initialStatusFilter) ? initialStatusFilter : "all",
  );
  const [register, setRegister] = useState<BoardRegister>(() =>
    initialStatusFilter === "done" ? "done" : initialStatusFilter === "archived" ? "archive" : "open",
  );
  // One-shot apply if the deep-link status arrives after first mount (e.g. catalog race).
  const initialStatusAppliedRef = useRef(
    initialStatusFilter != null && STATUS_ORDER.includes(initialStatusFilter),
  );
  useEffect(() => {
    if (initialStatusAppliedRef.current) return;
    if (initialStatusFilter == null || !STATUS_ORDER.includes(initialStatusFilter)) return;
    initialStatusAppliedRef.current = true;
    if (initialStatusFilter === "done") setRegister("done");
    if (initialStatusFilter === "archived") setRegister("archive");
    setStatusFilter(initialStatusFilter);
  }, [initialStatusFilter]);
  const [assigneeFilter, setAssigneeFilter] = useState<string>("all");
  const [quickView, setQuickView] = useState<BoardQuickView>("all");
  const [archiveTasks, setArchiveTasks] = useState<BoardTask[]>([]);
  const [archivePage, setArchivePage] = useState<BoardArchiveResponse | null>(null);
  const [archiveLoading, setArchiveLoading] = useState(false);
  const [archiveError, setArchiveError] = useState<string | null>(null);
  const archiveRequestRef = useRef<{ serial: number; controller: AbortController } | null>(null);
  const archiveSerialRef = useRef(0);
  const isArchive = register === "archive";
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
    const flat: BoardTask[] = register === "done"
      ? doneTasks
      : (board?.columns ?? []).flatMap((column) =>
        column.name === "done" || column.name === "archived" ? [] : column.tasks
      );
    const ql = q.trim().toLowerCase();
    return flat.filter((t) => {
      if (statusFilter !== "all" && t.status !== statusFilter) return false;
      if (assigneeFilter !== "all" && (t.assignee ?? "") !== assigneeFilter) return false;
      if (quickView === "unassigned" && t.assignee) return false;
      if (quickView === "review" && t.status !== "review") return false;
      if (quickView === "standalone" && (t.link_counts.parents !== 0 || t.link_counts.children !== 0)) return false;
      if (quickView === "result" && !t.has_result) return false;
      if (ql) {
        const hay = `${t.title} ${t.id} ${t.assignee ?? ""} ${t.block_reason ?? ""}`.toLowerCase();
        if (!hay.includes(ql)) return false;
      }
      return true;
    });
  }, [archiveTasks, assigneeFilter, board, doneTasks, isArchive, q, quickView, register, statusFilter]);

  const filtersActive = statusFilter !== "all" || assigneeFilter !== "all" || q.trim() !== "" || quickView !== "all";
  const activeFilterLabels = [
    statusFilter !== "all" ? `Status: ${taskStatusLabel[statusFilter] ?? statusFilter}` : null,
    assigneeFilter !== "all" ? `Assignee: ${assigneeFilter}` : null,
    q.trim() ? `Suche: ${q.trim()}` : null,
    quickView !== "all" ? `Ansicht: ${
      quickView === "unassigned" ? "Ohne Assignee"
        : quickView === "review" ? "In Review"
          : quickView === "standalone" ? "Standalone"
            : "Mit Ergebnis · alle Status"
    }` : null,
  ].filter((label): label is string => label != null);

  const resetFilters = () => {
    setQ("");
    setStatusFilter("all");
    setAssigneeFilter("all");
    setQuickView("all");
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
    : register === "done"
      ? (donePage?.total_count ?? board?.summary?.status_counts.done ?? 0)
      : (board?.columns ?? []).reduce(
      (n, column) => n + (
        column.name === "done" || column.name === "archived" ? 0 : column.tasks.length
      ),
      0,
    );
  const summary = board?.summary;
  const fallbackStatusCount = (status: string) =>
    (board?.columns ?? []).find((column) => column.name === status)?.tasks.length ?? 0;
  const metricOpen = summary?.open_count ?? (board?.columns ?? [])
    .filter((column) => column.name !== "done" && column.name !== "archived")
    .reduce((count, column) => count + column.tasks.length, 0);
  const metricRunning = summary?.status_counts.running ?? fallbackStatusCount("running");
  const metricBlocked = summary?.status_counts.blocked ?? fallbackStatusCount("blocked");
  const metricDone = summary?.status_counts.done ?? donePage?.total_count ?? fallbackStatusCount("done");

  return (
    <div className="fleet-boardtab">
      <div className="fleet-boardtab-metrics" aria-label="Board-Kennzahlen">
        <div><span>Offen</span><strong>{metricOpen}</strong></div>
        {/* data-tone nur bei Wert > 0 (L3-Spec 3.3): 0 bleibt neutral. */}
        <div data-tone={metricRunning > 0 ? "running" : undefined}><span>Laufend</span><strong>{metricRunning}</strong></div>
        <div data-tone={metricBlocked > 0 ? "blocked" : undefined}><span>Blockiert</span><strong>{metricBlocked}</strong></div>
        <div>
          <span>Fertig</span><strong>{metricDone}</strong>
          {donePage && donePage.loaded_count < metricDone ? <small>{doneTasks.length} geladen</small> : null}
        </div>
      </div>

      <div className="fleet-boardtab-register" role="tablist" aria-label="Board-Register">
        {([
          ["open", "Offen", metricOpen],
          ["done", "Fertig", metricDone],
          ["archive", "Archiv", summary?.status_counts.archived ?? 0],
        ] as const).map(([id, label, count]) => (
          <button
            key={id}
            type="button"
            role="tab"
            aria-selected={register === id}
            className={register === id ? "is-active" : ""}
            onClick={() => {
              setRegister(id);
              setStatusFilter("all");
              setQuickView("all");
            }}
          >
            {label}<span>{count}</span>
          </button>
        ))}
      </div>

      <div className="fleet-boardtab-views" aria-label="Gespeicherte Board-Ansichten">
        {([
          ["all", "Alle", null],
          ["unassigned", "Ohne Assignee", summary?.quick_counts.unassigned_open],
          ["review", "In Review", summary?.quick_counts.review],
          ["standalone", "Standalone", summary?.quick_counts.standalone_open],
          ["result", "Mit Ergebnis", summary?.quick_counts.with_result],
        ] as const).map(([id, label, count]) => (
          <button
            key={id}
            type="button"
            aria-pressed={quickView === id}
            onClick={() => {
              setQuickView(id);
              if (id === "review") setRegister("open");
            }}
            title={id === "result" ? "Alle Status" : undefined}
          >
            {label}{count != null ? <span>{count}</span> : null}
          </button>
        ))}
      </div>

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
        {register === "open" ? <select
          className="fleet-boardtab-select"
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value as TaskStatus | "all")}
          aria-label="Nach Status filtern"
        >
          <option value="all">Alle Status</option>
          {STATUS_ORDER.filter((status) => status !== "done" && status !== "archived").map((s) => (
            <option key={s} value={s}>{taskStatusLabel[s] ?? s}</option>
          ))}
        </select> : null}
        <details className="fleet-boardtab-more">
          <summary aria-label="Weitere Filter"><SlidersHorizontal aria-hidden size={14} /> Filter</summary>
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
        </details>
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
        <BoardSkeleton label="Archiv wird geladen" />
      ) : !isArchive && doneLoading && grouped.length === 0 ? (
        <BoardSkeleton label="Board wird geladen" />
      ) : grouped.length === 0 ? (
        <div className="hc-fleet-empty">
          <span className="hc-fleet-empty-title">
            {isArchive
              ? (archivePage?.total_count === 0 ? "Archiv ist leer" : "Keine Archivtreffer")
              : (totalCount === 0 ? "Keine Tasks" : "Keine Treffer")}
          </span>
          <span className="hc-fleet-empty-desc">
            {isArchive
              ? (archivePage?.total_count === 0 ? "Es sind keine archivierten Karten vorhanden." : "Suche oder Assignee-Filter anpassen.")
              : (totalCount === 0 ? "Das Board ist leer." : "Filter anpassen.")}
          </span>
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
              // KF-5: Ketten-Kontext (nur Kettenmitglieder). Wenn vorhanden,
              // ersetzt der Ketten-Chip den nackten → t_abcd12-Verweis.
              const chain = t.chain ?? null;
              const chainLabel = chain ? (chainIdentities?.[chain.identity_id] ?? chain.identity_id) : null;
              const metaTitle = [
                t.id,
                t.assignee,
                t.priority !== 0 ? `Prio ${t.priority}` : null,
                t.comment_count > 0 ? `${t.comment_count} Kommentare` : null,
                t.link_counts.parents > 0 ? `${t.link_counts.parents} Vorgänger` : null,
                t.link_counts.children > 0 ? `${t.link_counts.children} Nachfolger` : null,
                !chain && t.root_id && t.root_id !== t.id && t.link_counts.children === 0 ? `→ ${(t.root_id ?? "").slice(0, 8)}` : null,
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
                  <span className="fleet-boardtab-title">{t.title || t.id}</span>
                  <span className="fleet-boardtab-meta" title={metaTitle}>
                    <span className="fleet-boardtab-id" title={t.id}>{t.id}</span>
                    {t.assignee && <span className="fleet-boardtab-assignee">{t.assignee}</span>}
                    {t.priority !== 0 && <span className="fleet-boardtab-priority">Prio {t.priority}</span>}
                    {t.comment_count > 0 && <span className="fleet-boardtab-comments">{t.comment_count} Kommentare</span>}
                    {t.link_counts.parents > 0 && <span className="fleet-boardtab-parents">{t.link_counts.parents} Vorgänger</span>}
                    {t.link_counts.children > 0 && (
                      <span className="fleet-boardtab-chain" title="Teil einer Kette">{t.link_counts.children} Nachfolger</span>
                    )}
                    {!chain && t.root_id && t.root_id !== t.id && t.link_counts.children === 0 && (
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
                  <button
                    className={`fleet-boardtab-row${readOnly ? " fleet-boardtab-row-readonly" : ""}${selectedNodeId === t.id ? " fleet-boardtab-row-selected" : ""}`}
                    onClick={() => onOpenNodeDetail(t.id)}
                    aria-expanded={selectedNodeId === t.id}
                    aria-controls={detailControlsId}
                    title={readOnly ? "Fremd-Board · nur lesen" : undefined}
                  >
                    {content}
                  </button>
                  {chain && chainLabel ? (
                    <button
                      type="button"
                      className="fleet-boardtab-chainchip"
                      title={`Kette ${chainLabel} · Glied ${chain.position} von ${chain.total} — Kette öffnen`}
                      onClick={(e) => {
                        e.stopPropagation();
                        onOpenChain?.(chain.identity_id);
                      }}
                    >
                      <Link2 size={12} aria-hidden="true" />
                      <span className="fleet-boardtab-chainchip-label">{chainLabel}</span>
                      <span className="fleet-boardtab-chainchip-pos">
                        {chain.position}/{chain.total}
                      </span>
                    </button>
                  ) : null}
                  <button
                    type="button"
                    className="fleet-boardtab-copy"
                    onClick={() => copyTaskId(t.id)}
                    aria-label={`Task-ID ${t.id} kopieren`}
                    title="Task-ID kopieren"
                  >
                    <Copy aria-hidden size={13} />
                  </button>
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
      {register === "done" && donePage?.has_more ? (
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
