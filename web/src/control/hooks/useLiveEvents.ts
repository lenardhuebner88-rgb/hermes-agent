import { useEffect, useRef } from "react";
import { buildWsUrl } from "@/lib/api";
import { getSnapshot, refresh, setIntervalScale } from "./pollingStore";
import { boardLoader } from "./useControlData";
import type { BoardResponse } from "../lib/types";

export interface KanbanLiveEvent {
  id: number;
  task_id?: string | null;
  run_id?: string | null;
  kind?: string | null;
  payload?: unknown;
  created_at?: number;
}

export interface KanbanLiveMessage {
  events?: KanbanLiveEvent[];
  cursor?: number;
}

const TASK_EVENT_KEYS = [
  "kanban/board",
  "workers/active",
  "kanban/decision-queue",
] as const;

const RUN_EVENT_KEYS = [
  "tasks/review-verdicts",
  "runs/blocked-completions",
  "runs/recent-results",
] as const;

export function refreshKeysForLiveEvent(event: KanbanLiveEvent): string[] {
  const keys = new Set<string>();
  if (event.task_id) TASK_EVENT_KEYS.forEach((key) => keys.add(key));
  const kind = event.kind ?? "";
  if (event.run_id || /run|claim|complete|review|blocked|hallucination|verifier/i.test(kind)) {
    RUN_EVENT_KEYS.forEach((key) => keys.add(key));
  }
  if (/epic/i.test(kind)) keys.add("kanban/epics");
  return [...keys];
}

function currentCursor(): number {
  return getSnapshot<BoardResponse>("kanban/board")?.data?.latest_event_id ?? 0;
}

// Adaptives Polling: solange der Events-WS verbunden ist, treiben die Events die
// Frische dieser Keys — die Basis-Polls fallen auf 5×-Kadenz als Sicherheitsnetz
// (Board 8s→40s usw.). Bei Disconnect sofort zurück auf Normal-Kadenz.
const LIVE_SCALE = 5;
const LIVE_SCALED_KEYS = [...TASK_EVENT_KEYS, ...RUN_EVENT_KEYS];

export function setLivePollingMode(live: boolean): void {
  for (const key of LIVE_SCALED_KEYS) setIntervalScale(key, live ? LIVE_SCALE : 1);
}

export function useLiveEvents(): void {
  const cursorRef = useRef(currentCursor());
  const backoffRef = useRef(1000);
  const reconnectTimerRef = useRef<number | null>(null);
  const socketRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    let stopped = false;

    const clearReconnect = () => {
      if (reconnectTimerRef.current != null) {
        window.clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
    };
    const closeSocket = () => {
      socketRef.current?.close();
      socketRef.current = null;
    };
    const scheduleReconnect = () => {
      if (stopped || document.hidden || reconnectTimerRef.current != null) return;
      const delay = backoffRef.current;
      backoffRef.current = Math.min(delay * 2, 30000);
      reconnectTimerRef.current = window.setTimeout(() => {
        reconnectTimerRef.current = null;
        void connect();
      }, delay);
    };
    const handleMessage = (raw: string) => {
      let message: KanbanLiveMessage;
      try {
        message = JSON.parse(raw) as KanbanLiveMessage;
      } catch {
        return;
      }
      if (typeof message.cursor === "number") cursorRef.current = message.cursor;
      const keys = new Set<string>();
      for (const event of message.events ?? []) {
        if (typeof event.id === "number") cursorRef.current = Math.max(cursorRef.current, event.id);
        refreshKeysForLiveEvent(event).forEach((key) => keys.add(key));
      }
      keys.forEach((key) => void refresh(key));
    };
    // connect() hat async-Fenster (boardLoader/buildWsUrl), in denen ein
    // zweiter Aufruf (visibilitychange, Reconnect-Timer) den socketRef-Check
    // schon passiert hätte → Doppel-Socket, der erste leakt. Das Flag macht
    // connect() single-flight (Codex-Review-Befund).
    let connecting = false;
    const connect = async () => {
      if (stopped || document.hidden || socketRef.current || connecting) return;
      connecting = true;
      try {
        // since=0 würde die gesamte Event-Historie in 200er-Batches nachspielen
        // und Live-Updates beim ersten Connect deutlich verzögern. Ohne bekannten
        // Cursor daher erst das Board laden: Store-Snapshot, falls eine View ihn
        // schon hält, sonst one-shot Fetch (latest_event_id=0 auf leerem Board
        // ist danach ehrlich).
        if (cursorRef.current === 0) {
          const snapshot = getSnapshot<BoardResponse>("kanban/board")?.data;
          if (snapshot != null) {
            cursorRef.current = snapshot.latest_event_id ?? 0;
          } else {
            try {
              const board = await boardLoader();
              if (stopped || document.hidden || socketRef.current) return;
              cursorRef.current = board.latest_event_id ?? 0;
            } catch {
              scheduleReconnect();
              return;
            }
          }
        } else {
          cursorRef.current = Math.max(cursorRef.current, currentCursor());
        }
        const url = await buildWsUrl("/api/plugins/kanban/events", { since: String(cursorRef.current) });
        if (stopped || document.hidden || socketRef.current) return;
        const ws = new WebSocket(url);
        socketRef.current = ws;
        ws.onopen = () => {
          backoffRef.current = 1000;
          setLivePollingMode(true);
        };
        ws.onmessage = (event) => handleMessage(String(event.data));
        ws.onerror = () => {
          setLivePollingMode(false);
          closeSocket();
          scheduleReconnect();
        };
        ws.onclose = () => {
          setLivePollingMode(false);
          socketRef.current = null;
          scheduleReconnect();
        };
      } catch {
        scheduleReconnect();
      } finally {
        connecting = false;
      }
    };
    const onVisibility = () => {
      if (document.hidden) {
        clearReconnect();
        closeSocket();
      } else {
        cursorRef.current = Math.max(cursorRef.current, currentCursor());
        void connect();
      }
    };

    document.addEventListener("visibilitychange", onVisibility);
    void connect();
    return () => {
      stopped = true;
      document.removeEventListener("visibilitychange", onVisibility);
      clearReconnect();
      closeSocket();
      setLivePollingMode(false);
    };
  }, []);
}
