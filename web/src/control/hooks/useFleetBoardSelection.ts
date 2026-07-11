import { useCallback, useState } from "react";
import {
  persistFleetBoard,
  readFleetBoard,
  validateFleetBoard,
  type BoardsResponse,
} from "../lib/multiBoard";

export function useFleetBoardSelection(catalog: BoardsResponse | null) {
  const [storedBoard, setStoredBoard] = useState<string | null>(() =>
    readFleetBoard(typeof window === "undefined" ? null : window.localStorage),
  );
  const selectedBoard = validateFleetBoard(storedBoard, catalog);

  const setSelectedBoard = useCallback((board: string | null) => {
    setStoredBoard(board);
    persistFleetBoard(typeof window === "undefined" ? null : window.localStorage, board);
  }, []);

  return { selectedBoard, setSelectedBoard };
}
