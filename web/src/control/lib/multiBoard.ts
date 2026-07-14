import type { Worker, WorkersResponse } from "./types";

export const FLEET_BOARD_STORAGE_KEY = "hermes.control.fleet-board";

export interface BoardSummary {
  slug: string;
  name: string;
  archived: boolean;
  is_current: boolean;
  project_bound?: boolean;
  project_name?: string | null;
}

export interface BoardsResponse {
  boards: BoardSummary[];
  current: string;
}

export interface BoardWorkersResponse {
  board: string;
  response: WorkersResponse;
}

export function selectableFleetBoards<
  T extends Pick<BoardSummary, "archived" | "project_bound">,
>(boards: readonly T[]): T[] {
  const active = boards.filter((board) => !board.archived);
  const projectBound = active.filter((board) => board.project_bound === true);
  return projectBound.length > 0 ? projectBound : active;
}

export function withBoardParam(url: string, board: string | null): string {
  if (!board) return url;
  const separator = url.includes("?") ? "&" : "?";
  return `${url}${separator}board=${encodeURIComponent(board)}`;
}

export function mergeBoardWorkers(results: BoardWorkersResponse[], currentBoard: string): WorkersResponse {
  const current = results.find((entry) => entry.board === currentBoard)?.response;
  const workers: Worker[] = results.flatMap(({ board, response }) =>
    response.workers.map((worker) => ({ ...worker, board_slug: board })),
  );
  return {
    workers,
    count: workers.length,
    cap: current?.cap ?? null,
    checked_at: Math.max(0, ...results.map(({ response }) => response.checked_at)),
  };
}

export function readFleetBoard(storage: Pick<Storage, "getItem"> | null): string | null {
  if (!storage) return null;
  try {
    return storage.getItem(FLEET_BOARD_STORAGE_KEY)?.trim() || null;
  } catch {
    return null;
  }
}

export function persistFleetBoard(storage: Pick<Storage, "setItem" | "removeItem"> | null, board: string | null): void {
  if (!storage) return;
  try {
    if (board) storage.setItem(FLEET_BOARD_STORAGE_KEY, board);
    else storage.removeItem(FLEET_BOARD_STORAGE_KEY);
  } catch {
    // Private mode / storage quota: selection still works for this mount.
  }
}

export function validateFleetBoard(board: string | null, catalog: BoardsResponse | null): string | null {
  if (!catalog) return board;
  const availableBoards = selectableFleetBoards(catalog.boards);
  const currentIsAvailable = availableBoards.some((entry) => entry.slug === catalog.current);
  const safeDefault = currentIsAvailable ? null : availableBoards[0]?.slug ?? null;
  if (!board) return safeDefault;
  const available = availableBoards.some((entry) => entry.slug === board && entry.slug !== catalog.current);
  return available ? board : safeDefault;
}

export function boardDataColor(slug: string): string {
  let hash = 0;
  for (const char of slug) hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
  return `var(--color-data-${(hash % 6) + 1})`;
}
