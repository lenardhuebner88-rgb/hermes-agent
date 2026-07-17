import { useCallback, useEffect, useRef, useState } from "react";
import {
  subscribe,
  refresh,
  getSnapshot,
  type PollLoader,
  type StoreSnapshot,
  type StructuredError,
} from "./pollingStore";

type LoadState<T> = {
  data: T | null;
  error: string | null;
  loading: boolean;
  /** Epoch seconds of the last SUCCESSFUL load (E1 freshness). null until first ok. */
  lastUpdated: number | null;
  reload: () => Promise<void>;
  updateData: React.Dispatch<React.SetStateAction<T | null>>;
  /** Additive (back-compat): structured error + stale-while-error flag. */
  errorObj?: StructuredError | null;
  isStale?: boolean;
};


// Backed by the shared pollingStore: subscribers on the same `key` dedupe to one
// timer + one request, get 5xx backoff and stale-while-error for free. The
// public LoadState shape is UNCHANGED (errorObj/isStale are additive) so no view
// needs to change. updateData patches the local snapshot for optimistic edits;
// the next poll/reload overwrites it with server truth (same as before).
function emptyPollingSnapshot<T>(): StoreSnapshot<T> {
  return { data: null, error: null, errorObj: null, loading: true, lastUpdated: null, isStale: false };
}


export function usePolling<T>(key: string, loader: PollLoader<T>, intervalMs: number): LoadState<T> {
  const [state, setState] = useState<{ key: string; snap: StoreSnapshot<T> }>(() => ({
    key,
    snap: getSnapshot<T>(key) ?? emptyPollingSnapshot<T>(),
  }));
  // A hook instance survives prop/key changes. Never expose its previous key's
  // snapshot for the transition render: child effects run before this hook's
  // subscription effect and would otherwise issue stale-id requests scoped to
  // the new board. A cache hit for the NEW key remains valid SWR data.
  const snap = state.key === key
    ? state.snap
    : (getSnapshot<T>(key) ?? emptyPollingSnapshot<T>());
  const loaderRef = useRef(loader);
  useEffect(() => {
    loaderRef.current = loader;
  }, [loader]);

  useEffect(() => {
    return subscribe<T>(key, (signal) => loaderRef.current(signal), intervalMs, (next) => {
      setState({ key, snap: next });
    });
  }, [key, intervalMs]);

  const reload = useCallback(() => refresh(key), [key]);
  const updateData = useCallback<React.Dispatch<React.SetStateAction<T | null>>>((action) => {
    setState((current) => {
      const base = current.key === key ? current.snap : (getSnapshot<T>(key) ?? emptyPollingSnapshot<T>());
      return {
        key,
        snap: {
          ...base,
          data: typeof action === "function"
            ? (action as (prev: T | null) => T | null)(base.data)
            : action,
        },
      };
    });
  }, [key]);

  return {
    data: snap.data,
    error: snap.error,
    errorObj: snap.errorObj,
    loading: snap.loading,
    lastUpdated: snap.lastUpdated,
    isStale: snap.isStale,
    reload,
    updateData,
  };
}


// fetchJSON throws `Error("409: {\"detail\":\"…\"}")` — pull out the human detail.
// Exported so views (e.g. LoopsView) can surface the same readable text for
// their own POST mutations without re-parsing fetchJSON's error format.
export function extractDetail(e: unknown): string {
  const msg = e instanceof Error ? e.message : String(e);
  const m = msg.match(/^\d+:\s*(.*)$/s);
  const body = m ? m[1] : msg;
  try {
    const parsed = JSON.parse(body);
    if (parsed && typeof parsed.detail === "string") return parsed.detail;
  } catch {
    /* not JSON — use the raw text */
  }
  return body || msg;
}
