/**
 * pollingStore — a deliberately small, framework-free polling coordinator that
 * sits under usePolling. It provides three things the per-hook setInterval loop
 * could not, without pulling in react-query/swr:
 *
 *  1. Request dedup — N components polling the same key (e.g. workers/active is
 *     subscribed by Overview, HermesFleet and ControlPage) share ONE timer and
 *     ONE in-flight request per tick.
 *  2. Exponential backoff on 5xx — a failing endpoint backs off up to 60s
 *     instead of hammering every interval.
 *  3. stale-while-error — the last good value is kept and flagged `isStale`
 *     rather than blanking the UI on a transient failure.
 *
 * Errors are surfaced both as the legacy `error: string` (back-compat) and a
 * structured `errorObj` so callers can branch on an HTTP-ish code.
 *
 * One module-global singleton (parked on globalThis so Vite HMR doesn't leak a
 * second copy). setTimeout-reschedule (not setInterval) so the delay can vary
 * with backoff. The document.hidden gate is honoured exactly like the old loop.
 */

const MAX_BACKOFF_MS = 60_000;

export interface StructuredError {
  /** HTTP status ("500"), or "network" / "contract" for non-HTTP failures. */
  code: string;
  message: string;
  detail?: string;
}

export interface StoreSnapshot<T> {
  data: T | null;
  error: string | null;
  errorObj: StructuredError | null;
  loading: boolean;
  /** Epoch seconds of the last SUCCESSFUL load. null until the first ok. */
  lastUpdated: number | null;
  /** True when the shown data is a retained last-good value after a failure. */
  isStale: boolean;
}

type Listener<T> = (snapshot: StoreSnapshot<T>) => void;

interface Entry<T> {
  loader: () => Promise<T>;
  intervalMs: number;
  snapshot: StoreSnapshot<T>;
  lastPayloadJson: string | null;
  listeners: Set<Listener<T>>;
  timer: ReturnType<typeof setTimeout> | null;
  failCount: number;
  nextDelayMs: number;
  inFlight: boolean;
}

const nowSec = () => Math.floor(Date.now() / 1000);

function initialSnapshot<T>(): StoreSnapshot<T> {
  return { data: null, error: null, errorObj: null, loading: true, lastUpdated: null, isStale: false };
}

export function parseStructuredError(e: unknown): StructuredError {
  const message = e instanceof Error ? e.message : String(e);
  // fetchJSON throws `${status}: ${body}` for HTTP errors (see lib/api.ts).
  const httpMatch = message.match(/^(\d{3}):\s*([\s\S]*)$/);
  if (httpMatch) {
    return { code: httpMatch[1], message, detail: httpMatch[2] };
  }
  const isNetwork = /network|failed to fetch|load failed/i.test(message);
  return { code: isNetwork ? "network" : "contract", message };
}

function isServerError(err: StructuredError): boolean {
  return /^5\d\d$/.test(err.code) || err.code === "network";
}

interface StoreGlobal {
  entries: Map<string, Entry<unknown>>;
  visibilityBound: boolean;
}

function getStore(): StoreGlobal {
  const g = globalThis as unknown as { __hermesPollingStore?: StoreGlobal };
  if (!g.__hermesPollingStore) {
    g.__hermesPollingStore = { entries: new Map(), visibilityBound: false };
  }
  const store = g.__hermesPollingStore;
  if (!store.visibilityBound && typeof document !== "undefined") {
    store.visibilityBound = true;
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) return;
      // Tab refocused: refresh everything immediately and reset backoff so a
      // recovered endpoint snaps back to its normal cadence.
      for (const key of store.entries.keys()) {
        const entry = store.entries.get(key);
        if (entry) entry.nextDelayMs = entry.intervalMs;
        void refresh(key);
      }
    });
  }
  return store;
}

function notify<T>(entry: Entry<T>): void {
  for (const listener of entry.listeners) listener(entry.snapshot);
}

function patch<T>(entry: Entry<T>, partial: Partial<StoreSnapshot<T>>): void {
  entry.snapshot = { ...entry.snapshot, ...partial };
  notify(entry);
}

function payloadJson(data: unknown): string | null {
  try {
    return JSON.stringify(data);
  } catch {
    return null;
  }
}

function scheduleNext(key: string): void {
  const entry = getStore().entries.get(key) as Entry<unknown> | undefined;
  if (!entry || entry.listeners.size === 0) return;
  entry.timer = setTimeout(() => void tick(key), entry.nextDelayMs);
}

async function tick(key: string): Promise<void> {
  const entry = getStore().entries.get(key) as Entry<unknown> | undefined;
  if (!entry) return;
  if (entry.timer) {
    clearTimeout(entry.timer);
    entry.timer = null;
  }
  if (entry.listeners.size === 0) return; // stopped — no reschedule

  const hidden = typeof document !== "undefined" && document.hidden;
  if (!hidden && !entry.inFlight) {
    entry.inFlight = true;
    try {
      const data = await entry.loader();
      entry.failCount = 0;
      entry.nextDelayMs = entry.intervalMs;
      const nextPayloadJson = payloadJson(data);
      const unchangedPayload =
        nextPayloadJson != null &&
        nextPayloadJson === entry.lastPayloadJson &&
        entry.snapshot.error == null &&
        entry.snapshot.errorObj == null &&
        !entry.snapshot.loading &&
        !entry.snapshot.isStale;
      if (!unchangedPayload) {
        entry.lastPayloadJson = nextPayloadJson;
        patch(entry, { data, error: null, errorObj: null, loading: false, lastUpdated: nowSec(), isStale: false });
      }
    } catch (e) {
      const errObj = parseStructuredError(e);
      entry.failCount += 1;
      entry.nextDelayMs = isServerError(errObj)
        ? Math.min(entry.intervalMs * 2 ** entry.failCount, MAX_BACKOFF_MS)
        : entry.intervalMs;
      // stale-while-error: keep the last good `data`, flag it stale.
      patch(entry, { error: errObj.message, errorObj: errObj, loading: false, isStale: entry.snapshot.data != null });
    } finally {
      entry.inFlight = false;
    }
  }
  scheduleNext(key);
}

/** Force an immediate refresh (used by reload()). Returns when the tick settles. */
export function refresh(key: string): Promise<void> {
  return tick(key);
}

export function getSnapshot<T>(key: string): StoreSnapshot<T> | null {
  const entry = getStore().entries.get(key) as Entry<T> | undefined;
  return entry ? entry.snapshot : null;
}

/**
 * Subscribe to a polled key. The first subscriber starts the timer; the last to
 * unsubscribe stops it (ref-counted, so no leaked timers).
 */
export function subscribe<T>(
  key: string,
  loader: () => Promise<T>,
  intervalMs: number,
  listener: Listener<T>,
): () => void {
  const store = getStore();
  let entry = store.entries.get(key) as Entry<T> | undefined;
  if (!entry) {
    entry = {
      loader,
      intervalMs,
      snapshot: initialSnapshot<T>(),
      lastPayloadJson: null,
      listeners: new Set(),
      timer: null,
      failCount: 0,
      nextDelayMs: intervalMs,
      inFlight: false,
    };
    store.entries.set(key, entry as Entry<unknown>);
  } else {
    // Keep the latest loader/interval (closures change across renders).
    entry.loader = loader;
    entry.intervalMs = intervalMs;
  }

  entry.listeners.add(listener);
  listener(entry.snapshot); // hand over the current snapshot synchronously

  if (entry.timer == null && !entry.inFlight) {
    void tick(key); // first subscriber (or restarted) → tick now
  }

  return () => {
    const current = store.entries.get(key) as Entry<T> | undefined;
    if (!current) return;
    current.listeners.delete(listener);
    if (current.listeners.size === 0 && current.timer) {
      clearTimeout(current.timer);
      current.timer = null;
    }
  };
}

/** Test helper: drop all entries and timers. */
export function _resetPollingStore(): void {
  const store = getStore();
  for (const entry of store.entries.values()) {
    if (entry.timer) clearTimeout(entry.timer);
  }
  store.entries.clear();
}
