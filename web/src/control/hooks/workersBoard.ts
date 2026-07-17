import { useCallback, useEffect, useRef, useState } from "react";
import { fetchJSON } from "@/lib/api";
import {
  WorkersResponseSchema,
  BoardsResponseSchema,
  LiveEventsResponseSchema,
  WorkerActivityResponseSchema,
  BoardResponseSchema,
  parseOrThrow,
} from "../lib/schemas";
import type { WorkerActivityResponse } from "../lib/schemas";
import { nowSec } from "../lib/derive";
import { mergeLiveEvents } from "../lib/fleetHub";
import { mergeBoardWorkers, withBoardParam, type BoardsResponse } from "../lib/multiBoard";
import type { LiveEvent } from "../lib/types";
import type { BoardResponse, WorkersResponse } from "../lib/types";
import { usePolling } from "./internal";

export const DONE_PAGE_LIMIT = 30;

export interface DoneBoardPage {
  total_count: number;
  loaded_count: number;
  limit: number;
  has_more: boolean;
  next_cursor: string | null;
}

export type PaginatedBoardResponse = BoardResponse & { done_page?: DoneBoardPage };

function parsePaginatedBoardResponse(raw: unknown, source: string): PaginatedBoardResponse {
  const board = parseOrThrow(BoardResponseSchema, raw, source);
  if (typeof raw !== "object" || raw === null || !("done_page" in raw)) return board;
  const page = (raw as { done_page?: unknown }).done_page;
  if (typeof page !== "object" || page === null) throw new Error(`${source}: invalid done_page`);
  const value = page as Record<string, unknown>;
  const nextCursor = value.next_cursor;
  if (
    !Number.isInteger(value.total_count)
    || !Number.isInteger(value.loaded_count)
    || !Number.isInteger(value.limit)
    || typeof value.has_more !== "boolean"
    || !(nextCursor === null || typeof nextCursor === "string")
  ) {
    throw new Error(`${source}: invalid done_page`);
  }
  return { ...board, done_page: page as DoneBoardPage };
}

export interface DonePageQuery {
  board: string | null;
  cursor: string | null;
}

export type DonePageLoader = (
  query: DonePageQuery,
  signal: AbortSignal,
) => Promise<PaginatedBoardResponse>;

export const loadDoneBoardPage: DonePageLoader = async ({ board, cursor }, signal) => {
  const params = new URLSearchParams({
    card_diagnostics: "summary",
    card_body: "none",
    done_limit: String(DONE_PAGE_LIMIT),
  });
  if (cursor) params.set("done_cursor", cursor);
  const url = withBoardParam(`/api/plugins/kanban/board?${params.toString()}`, board);
  return parsePaginatedBoardResponse(
    await fetchJSON<unknown>(url, { signal }),
    board ? `kanban/board:${board}:done` : "kanban/board:done",
  );
};

export function useHermesWorkers() {
  return usePolling<WorkersResponse>(
    "workers/active",
    async () => parseOrThrow(WorkersResponseSchema, await fetchJSON<unknown>("/api/plugins/kanban/workers/active"), "workers/active"),
    // chrome-badge cadence, 15s staleness accepted (perf plan 2026-07-17)
    15000,
  );
}


export function useBoardCatalog() {
  return usePolling<BoardsResponse>(
    "kanban/boards",
    async () => parseOrThrow(
      BoardsResponseSchema,
      await fetchJSON<unknown>("/api/plugins/kanban/boards"),
      "kanban/boards",
    ),
    60000,
  );
}

/** Fleet-only aggregation. Existing single-board consumers keep useHermesWorkers(). */

export function useAllBoardWorkers() {
  return usePolling<WorkersResponse>(
    "workers/active:all-boards",
    async () => {
      const catalog = parseOrThrow(
        BoardsResponseSchema,
        await fetchJSON<unknown>("/api/plugins/kanban/boards"),
        "kanban/boards",
      );
      const activeBoards = catalog.boards.filter((board) => !board.archived);
      if (activeBoards.length === 0) throw new Error("Keine aktiven Kanban-Boards gemeldet");
      const responses = await Promise.all(activeBoards.map(async ({ slug }) => ({
        board: slug,
        response: parseOrThrow(
          WorkersResponseSchema,
          await fetchJSON<unknown>(withBoardParam("/api/plugins/kanban/workers/active", slug)),
          `workers/active:${slug}`,
        ),
      })));
      return mergeBoardWorkers(responses, catalog.current);
    },
    5000,
  );
}


export interface RunLiveEventsState {
  events: LiveEvent[];
  loading: boolean;
  error: string | null;
  lastUpdated: number | null;
  isStale: boolean;
}

/**
 * useRunLiveEvents — Puls-Leitstand-Ticker (S2). Pollt GET
 * /runs/live-events alle 4000ms und akkumuliert die Events inkrementell. Fleet
 * kann mehrere Board-DBs gleichzeitig lesen; deshalb besitzt jedes Board einen
 * eigenen since_id-Cursor und jedes Event wird mit board_slug angereichert.
 *
 * Bewusst KEIN usePolling: der Store ersetzt seinen Snapshot pro Poll, hier
 * brauchen wir das Merge über since_id. Pausiert bei document.hidden (wie
 * usePolling) und stoppt sauber beim Unmount — nur montiert (Worker-Subtab
 * sichtbar) läuft der Poll, sonst schläft der Ticker.
 */

export function useRunLiveEvents(
  enabled = true,
  cap = 40,
  boardSlugs: readonly string[] = [],
): RunLiveEventsState {
  const [events, setEvents] = useState<LiveEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdated, setLastUpdated] = useState<number | null>(null);
  const sinceIdByBoardRef = useRef<Record<string, number>>({});
  const boardKey = [...new Set(boardSlugs.map((slug) => slug.trim()).filter(Boolean))]
    .sort()
    .join("\u0000");

  useEffect(() => {
    if (!enabled) return;
    let stopped = false;
    let timer: number | null = null;
    const boards = boardKey ? boardKey.split("\u0000") : [null];
    const boardSet = new Set(boards);

    const schedule = () => {
      if (stopped) return;
      timer = window.setTimeout(() => void tick(), 4000);
    };

    const tick = async () => {
      // document.hidden: nicht fetchen, aber weiter takten (billiger No-op),
      // damit der Ticker sofort weiterläuft, sobald der Tab wieder sichtbar ist.
      if (typeof document !== "undefined" && document.hidden) {
        schedule();
        return;
      }
      try {
        const results = await Promise.allSettled(boards.map(async (board) => {
          const cursorKey = board ?? "current";
          const since = sinceIdByBoardRef.current[cursorKey];
          const baseUrl = since != null
            ? `/api/plugins/kanban/runs/live-events?since_id=${since}`
            : "/api/plugins/kanban/runs/live-events";
          const url = withBoardParam(baseUrl, board);
          const parsed = parseOrThrow(
            LiveEventsResponseSchema,
            await fetchJSON<unknown>(url),
            `runs/live-events:${cursorKey}`,
          );
          return { board, cursorKey, parsed };
        }));
        if (stopped) return;
        const fulfilled = results.flatMap((result) => result.status === "fulfilled" ? [result.value] : []);
        const failed = results.length - fulfilled.length;
        if (fulfilled.length === 0) {
          const firstFailure = results.find((result) => result.status === "rejected");
          throw firstFailure && firstFailure.status === "rejected"
            ? firstFailure.reason
            : new Error("Keine Board-Ereignisse erreichbar");
        }
        setError(failed > 0 ? `${failed} Board-Ereignisquelle${failed === 1 ? "" : "n"} nicht erreichbar` : null);
        setLoading(false);
        setLastUpdated(nowSec());
        const incoming = fulfilled.flatMap(({ board, parsed }) =>
          parsed.events.map((event) => ({ ...event, board_slug: board })),
        );
        if (incoming.length > 0) {
          setEvents((prev) => mergeLiveEvents(
            prev.filter((event) => boardSet.has(event.board_slug ?? null)),
            incoming,
            cap,
          ));
        }
        for (const { cursorKey, parsed } of fulfilled) {
          if (parsed.latest_id != null) {
            sinceIdByBoardRef.current[cursorKey] = Math.max(
              sinceIdByBoardRef.current[cursorKey] ?? 0,
              parsed.latest_id,
            );
          }
        }
      } catch (e) {
        if (!stopped) {
          setLoading(false);
          setError(e instanceof Error ? e.message : String(e));
        }
      } finally {
        schedule();
      }
    };

    void tick();
    return () => {
      stopped = true;
      if (timer != null) window.clearTimeout(timer);
    };
  }, [enabled, cap, boardKey]);

  const visibleBoards = new Set(boardKey ? boardKey.split("\u0000") : [null]);
  const visibleEvents = events.filter((event) => visibleBoards.has(event.board_slug ?? null));
  return {
    events: visibleEvents,
    loading,
    error,
    lastUpdated,
    isStale: error != null && visibleEvents.length > 0,
  };
}


// F1: Aktivitäts-Timeline — pollt Task-Events nur wenn Cockpit expandiert (taskId != null).
// Interval ~8000ms; pausiert automatisch wenn taskId null.
export function useWorkerActivity(taskId: string | null, board: string | null = null) {
  const key = taskId ? `worker-activity/${taskId}:${board ?? "current"}` : "worker-activity/__none__";
  const loader = useCallback(async (): Promise<WorkerActivityResponse> => {
    if (!taskId) return { task_id: "", events: [] };
    const url = withBoardParam(
      `/api/plugins/kanban/tasks/${encodeURIComponent(taskId)}/activity?limit=12`,
      board,
    );
    return parseOrThrow(
      WorkerActivityResponseSchema,
      await fetchJSON<unknown>(url),
      `worker-activity/${taskId}`,
    );
  }, [taskId, board]);
  // Null-taskId → Intervall auf sehr groß setzen + leeren Snapshot zurückgeben
  // (usePolling pausiert nicht von sich aus; Loader gibt sofort leeres Objekt zurück)
  const result = usePolling<WorkerActivityResponse>(key, loader, taskId ? 8000 : 600_000);
  if (!taskId) return { ...result, data: { task_id: "", events: [] } as WorkerActivityResponse };
  return result;
}


// Loader auch für Nicht-Hook-Subscriber exportiert (CommandPalette abonniert
// Board/Crons/Epics on-demand, solange die Palette offen ist — sonst bleibt
// die globale Suche leer, bis die jeweilige View einmal besucht wurde).
// card_diagnostics=summary drops the per-card structured diagnostics list,
// card_body=none drops body+result (BoardTaskSchema strips both anyway —
// together they dominate the 8 s payload on real boards); the drawer
// fetches detail via /tasks/:id. The kanban plugin dashboard keeps the
// defaults (full). The server also sends an ETag, so an unchanged board
// revalidates as a 304 instead of re-transferring.
// First arg is the board name when called from useBoard. pollingStore may also
// invoke shared loaders with an AbortSignal — only accept real board strings.
export const boardLoader = async (board?: string | null | AbortSignal) => {
  const boardName = typeof board === "string" ? board : null;
  return parseOrThrow(
    BoardResponseSchema,
    await fetchJSON<unknown>(withBoardParam("/api/plugins/kanban/board?card_diagnostics=summary&card_body=none", boardName)),
    boardName ? `kanban/board:${boardName}` : "kanban/board",
  );
};


// Full kanban board grouped by status column — the Fleet pipeline (stage
// counts + actionable rows) reads this. 8s keeps the operator's stage view
// fresh without churning the DB; usePolling pauses it when the tab is hidden.
export function useBoard(board: string | null = null) {
  const key = board ? `kanban/board:${board}` : "kanban/board";
  return usePolling<BoardResponse>(key, () => boardLoader(board), 8000);
}
