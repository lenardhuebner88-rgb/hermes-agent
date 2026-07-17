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

/**
 * How much to stretch polls while the tab is in the background.
 * A 6x multiplier turns a 5s poll into 30s; a 10s poll is capped at 30s.
 * This bounds timer wake-ups while still keeping data vaguely current.
 */
const BACKGROUND_INTERVAL_SCALE = 6;
const MAX_BACKGROUND_INTERVAL_MS = 30_000;

/** Spacing between per-key refreshes when the tab returns to the foreground.
 * On a phone the (Tailscale-)link is itself just waking up; 12+ simultaneous
 * fetches in that window reliably drop some with "Failed to fetch". */
const FOREGROUND_STAGGER_MS = 150;
const FOREGROUND_STAGGER_CAP_MS = 2_500;

/**
 * Upper bound for a single in-flight poll attempt. Must sit above the GET
 * timeout in lib/api.ts (GET_TIMEOUT_MS = 20_000): a legitimate slow health
 * fetch (~16s) is still legal; a wedged attempt past this deadline is aborted
 * so the next tick can start a fresh request. Backgrounded setTimeout timers
 * freeze on mobile, so the 20s abort may not fire until resume — this is the
 * store-side escape hatch for that wedge.
 */
export const ATTEMPT_DEADLINE_MS = 25_000;

/** Burst of resume events (visibility/pageshow/focus/online) within this
 * window collapses to a single staggered refresh cycle. */
const RESUME_DEDUPE_MS = 1_000;

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

export type PollLoader<T> = (signal?: AbortSignal) => Promise<T>;

type Listener<T> = (snapshot: StoreSnapshot<T>) => void;

interface Entry<T> {
  loader: PollLoader<T>;
  intervalMs: number;
  snapshot: StoreSnapshot<T>;
  lastPayloadJson: string | null;
  listeners: Set<Listener<T>>;
  timer: ReturnType<typeof setTimeout> | null;
  failCount: number;
  nextDelayMs: number;
  inFlight: boolean;
  /** Epoch seconds when the current attempt began; null when idle. Non-notifying. */
  attemptStartedAt: number | null;
  /** Aborts a wedged attempt so a fresh tick can proceed. */
  abortController: AbortController | null;
  /** Monotonic; completions only patch when their captured gen still matches. */
  generation: number;
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

function normalIntervalMs(key: string, entry: Entry<unknown>): number {
  return entry.intervalMs * (getStore().intervalScales.get(key) ?? 1);
}

function backgroundIntervalMs(key: string, entry: Entry<unknown>): number {
  return Math.min(
    normalIntervalMs(key, entry) * BACKGROUND_INTERVAL_SCALE,
    MAX_BACKGROUND_INTERVAL_MS,
  );
}

interface StoreGlobal {
  entries: Map<string, Entry<unknown>>;
  visibilityBound: boolean;
  /** Per-key Intervall-Multiplikator (adaptives Polling, s. setIntervalScale). */
  intervalScales: Map<string, number>;
  /** Epoch ms of the last resume refresh cycle (dedupe visibility/pageshow/…). */
  lastResumeAtMs: number;
}

function getStore(): StoreGlobal {
  const g = globalThis as unknown as { __hermesPollingStore?: StoreGlobal };
  if (!g.__hermesPollingStore) {
    g.__hermesPollingStore = {
      entries: new Map(),
      visibilityBound: false,
      intervalScales: new Map(),
      lastResumeAtMs: 0,
    };
  }
  // HMR-Altbestand vor Einführung der Scales/Resume-Dedupe tolerieren.
  if (!g.__hermesPollingStore.intervalScales) g.__hermesPollingStore.intervalScales = new Map();
  if (typeof g.__hermesPollingStore.lastResumeAtMs !== "number") g.__hermesPollingStore.lastResumeAtMs = 0;
  const store = g.__hermesPollingStore;
  if (!store.visibilityBound && typeof document !== "undefined") {
    store.visibilityBound = true;
    document.addEventListener("visibilitychange", () => {
      const hidden = document.hidden;
      if (hidden) {
        // Backgrounded before the next tick: slow the timer down now, instead
        // of waking up at the (often 1-5s) foreground delay just to reschedule.
        for (const [key, entry] of store.entries) {
          const bgDelay = backgroundIntervalMs(key, entry);
          if (entry.timer && bgDelay > entry.nextDelayMs) {
            clearTimeout(entry.timer);
            entry.timer = null;
            entry.nextDelayMs = bgDelay;
            scheduleNext(key);
          }
        }
        return;
      }
      triggerForegroundResume(store);
    });
    // Same resume path as visibilitychange→visible: pageshow (bfcache), focus
    // (some mobile browsers), online (network back). Deduped so a burst within
    // RESUME_DEDUPE_MS is one stagger cycle, not four.
    // Prefer `window` when present; fall back to globalThis.window so node tests
    // can attach mocks without a real DOM Window binding.
    const resume = () => triggerForegroundResume(store);
    const resumeTarget =
      (typeof window !== "undefined" ? window : null) ??
      (globalThis as { window?: { addEventListener?: (type: string, fn: () => void) => void } }).window;
    if (resumeTarget && typeof resumeTarget.addEventListener === "function") {
      resumeTarget.addEventListener("pageshow", resume);
      resumeTarget.addEventListener("focus", resume);
      resumeTarget.addEventListener("online", resume);
    }
  }
  return store;
}

/**
 * Staggered full refresh of all keys. Coalesces bursts of resume signals
 * (visibility + pageshow + focus + online) into one cycle per ~1s.
 */
function triggerForegroundResume(store: StoreGlobal): void {
  if (typeof document !== "undefined" && document.hidden) return;
  const now = Date.now();
  if (now - store.lastResumeAtMs < RESUME_DEDUPE_MS) return;
  store.lastResumeAtMs = now;

  // Tab refocused / network back: refresh everything and reset backoff so a
  // recovered endpoint snaps back to its normal cadence — staggered rather
  // than all at once (thundering herd, see FOREGROUND_STAGGER_MS).
  let i = 0;
  for (const [key, entry] of store.entries) {
    if (entry.timer) {
      clearTimeout(entry.timer);
      entry.timer = null;
    }
    entry.nextDelayMs = normalIntervalMs(key, entry);
    const delay = Math.min(i * FOREGROUND_STAGGER_MS, FOREGROUND_STAGGER_CAP_MS);
    if (delay === 0) void refresh(key);
    else setTimeout(() => void refresh(key), delay);
    i += 1;
  }
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

/**
 * Non-notifying attempt observability for chrome that re-renders on its own
 * clock (OfflineStaleBanner). Changing attempt state must NEVER call notify().
 */
export function getAttemptState(key: string): {
  refreshing: boolean;
  attemptStartedAt: number | null;
  lastSuccessAt: number | null;
} {
  const entry = getStore().entries.get(key) as Entry<unknown> | undefined;
  if (!entry) {
    return { refreshing: false, attemptStartedAt: null, lastSuccessAt: null };
  }
  return {
    refreshing: entry.inFlight && entry.attemptStartedAt != null,
    attemptStartedAt: entry.attemptStartedAt,
    lastSuccessAt: entry.snapshot.lastUpdated,
  };
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
  if (hidden) {
    // Background tab: don't fetch, but keep a slow heartbeat so we pick up
    // work again quickly when the user comes back. Clear any pending timer
    // first because we may have been called from scheduleNext/refresh.
    entry.nextDelayMs = backgroundIntervalMs(key, entry);
    scheduleNext(key);
    return;
  }

  // Heal a wedged in-flight attempt (mobile background freezes the 20s GET
  // abort timer; until it fires, inFlight blocks every new fetch).
  if (entry.inFlight && entry.attemptStartedAt != null) {
    const ageMs = Date.now() - entry.attemptStartedAt * 1000;
    if (ageMs > ATTEMPT_DEADLINE_MS) {
      entry.abortController?.abort();
      entry.abortController = null;
      entry.inFlight = false;
      entry.attemptStartedAt = null;
      // Fall through to start a fresh attempt. The aborted generation will
      // not patch: we bump generation when the new attempt begins.
    }
  }

  if (!entry.inFlight) {
    entry.inFlight = true;
    entry.generation += 1;
    const generation = entry.generation;
    entry.attemptStartedAt = nowSec();
    const controller = typeof AbortController !== "undefined" ? new AbortController() : null;
    entry.abortController = controller;
    try {
      const data = await entry.loader(controller?.signal);
      // Stale completion: do not patch and do not steal the live generation's timer.
      if (generation !== entry.generation) return;
      entry.failCount = 0;
      entry.nextDelayMs = normalIntervalMs(key, entry);
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
      if (generation !== entry.generation) return;
      // Aborted wedge heal / supersession — do not surface as a user-facing error.
      if (controller?.signal.aborted) {
        // Still the live generation but aborted mid-heal race: reschedule below.
      } else {
        const errObj = parseStructuredError(e);
        entry.failCount += 1;
        entry.nextDelayMs = isServerError(errObj)
          ? Math.min(normalIntervalMs(key, entry) * 2 ** entry.failCount, MAX_BACKOFF_MS)
          : normalIntervalMs(key, entry);
        // stale-while-error: keep the last good `data`, flag it stale.
        patch(entry, { error: errObj.message, errorObj: errObj, loading: false, isStale: entry.snapshot.data != null });
      }
    } finally {
      if (generation === entry.generation) {
        entry.inFlight = false;
        entry.attemptStartedAt = null;
        entry.abortController = null;
      }
    }
    // Only the live generation reschedules after its attempt settles. A stale
    // completion returned early above and must not clear a newer timer.
    if (generation === entry.generation) {
      scheduleNext(key);
    }
    return;
  }

  // Legitimately still in flight (within deadline): keep the cadence so a later
  // tick can deadline-heal without waiting solely on an external resume.
  scheduleNext(key);
}

/** Force an immediate refresh (used by reload()). Returns when the tick settles. */
export function refresh(key: string): Promise<void> {
  return tick(key);
}

/**
 * Adaptives Polling: streckt das Poll-Intervall eines Keys um `scale` (1 = normal).
 * Gedacht für Event-getriebene Frische — solange der Kanban-Events-WebSocket
 * verbunden ist, treiben Events die Refreshes und die Basis-Polls dürfen auf
 * Sicherheitsnetz-Kadenz fallen (useLiveEvents setzt 5× / zurück auf 1×).
 * Wirkt ab dem nächsten Tick; Fehler-Backoff bleibt unangetastet.
 */
export function setIntervalScale(key: string, scale: number): void {
  const store = getStore();
  if (scale <= 1) store.intervalScales.delete(key);
  else store.intervalScales.set(key, scale);
  const entry = store.entries.get(key);
  // Beim Zurückschalten auf 1× sofort die normale Kadenz wiederherstellen,
  // statt einen ggf. minutenlangen gestreckten Timer auslaufen zu lassen.
  if (entry && scale <= 1 && entry.timer && entry.failCount === 0) {
    clearTimeout(entry.timer);
    entry.timer = null;
    entry.nextDelayMs = normalIntervalMs(key, entry);
    scheduleNext(key);
  }
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
  loader: PollLoader<T>,
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
      attemptStartedAt: null,
      abortController: null,
      generation: 0,
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

/** Test helper: drop all entries and timers. Also unbinds the visibility
 * flag so a test can install its own `document` mock and capture the
 * foreground-refresh listener. */
export function _resetPollingStore(): void {
  const store = getStore();
  for (const entry of store.entries.values()) {
    if (entry.timer) clearTimeout(entry.timer);
    entry.abortController?.abort();
  }
  store.entries.clear();
  store.intervalScales.clear();
  store.visibilityBound = false;
  store.lastResumeAtMs = 0;
}
