import { buildHermesWebSocketUrl } from "@hermes/shared";

// The dashboard can be served either at the root of its host (e.g.
// https://kanban.tilos.com/) or under a URL prefix when reverse-proxied
// (e.g. https://mission-control.tilos.com/hermes/). The Python backend
// injects ``window.__HERMES_BASE_PATH__`` into index.html based on the
// incoming ``X-Forwarded-Prefix`` header so the SPA can address its own
// ``/api/...`` and ``/dashboard-plugins/...`` URLs correctly without a
// rebuild. Empty string means "served at root".
function readBasePath(): string {
  if (typeof window === "undefined") return "";
  const raw = window.__HERMES_BASE_PATH__ ?? "";
  if (!raw) return "";
  // Normalise: ensure leading slash, strip trailing slash.
  const withLead = raw.startsWith("/") ? raw : `/${raw}`;
  return withLead.replace(/\/+$/, "");
}

export const HERMES_BASE_PATH = readBasePath();
const BASE = HERMES_BASE_PATH;

import type { DashboardTheme } from "@/themes/types";

// Ephemeral session token for protected endpoints.
// Injected into index.html by the server — never fetched via API.
declare global {
  interface Window {
    __HERMES_SESSION_TOKEN__?: string;
    __HERMES_BASE_PATH__?: string;
    /** Server-injected flag: ``true`` when the dashboard's OAuth gate is
     * engaged (public bind, no ``--insecure``). Toggles the SPA's
     * WS-upgrade path from legacy ``?token=`` to single-use ``?ticket=``
     * fetched via :func:`getWsTicket`. */
    __HERMES_AUTH_REQUIRED__?: boolean;
  }
}
const SESSION_HEADER = "X-Hermes-Session-Token";

function setSessionHeader(headers: Headers, token: string): void {
  if (!headers.has(SESSION_HEADER)) {
    headers.set(SESSION_HEADER, token);
  }
}

/** Extra knobs for {@link fetchJSON} that aren't part of the Fetch API. */
export interface FetchJSONOptions {
  /**
   * Suppress the loopback stale-token auto-reload for this call. Set it for
   * endpoints whose 401 is an *expected, steady-state* answer rather than a
   * stale token — chiefly ``/api/auth/me``, which returns 401 on every call
   * in non-gated (loopback) mode. Without this, that permanent 401 retriggers
   * the reload on every mount and the SPA reload-loops (it also pins the
   * dashboard process). When set, the 401 bubbles up as a normal ``401:``
   * error so the caller (e.g. AuthWidget) can handle it.
   */
  skipStaleTokenReload?: boolean;
  /**
   * Abort the request after this many milliseconds (0 disables). Defaults
   * to 20s for GETs — polling callers self-heal via the pollingStore
   * backoff, and a request stuck behind a slow link/locked backend must
   * not hang a tile forever. Mutations default to NO timeout: some POST
   * actions (LLM describers, ops runs) legitimately take longer.
   */
  timeoutMs?: number;
}

const GET_TIMEOUT_MS = 20_000;

export interface FetchJSONWithMetaResult<T> {
  data: T | undefined;
  status: number;
  headers: Headers;
}

// ── Global management-profile scope ──────────────────────────────────
// The dashboard is a machine-level management surface: one header switcher
// (ProfileProvider in App.tsx) decides which profile the management pages
// read/write, and fetchJSON transparently appends ?profile=<name> to the
// profile-scoped endpoint families below. "" = the dashboard process's own
// profile (legacy behavior). Calls that already carry an explicit profile
// (e.g. ProfileBuilder writes) are left untouched — explicit beats global.
let _managementProfile = "";

export function setManagementProfile(name: string): void {
  _managementProfile = (name || "").trim();
}

export function getManagementProfile(): string {
  return _managementProfile;
}

// Endpoint families that honor ?profile= on the backend (web_server.py
// _profile_scope or explicit per-profile DB opens). Anything else — ops,
// pairing, cron (which has its own per-job profile params), profiles
// themselves — is machine-global or self-scoped and must NOT be rewritten.
const PROFILE_SCOPED_PREFIXES = [
  "/api/status",
  "/api/gateway",
  "/api/analytics",
  "/api/skills",
  "/api/tools/toolsets",
  "/api/config",
  "/api/env",
  "/api/mcp",
  "/api/messaging/platforms",
  "/api/messaging/telegram/onboarding",
  "/api/messaging/whatsapp/onboarding",
  "/api/model/info",
  "/api/model/set",
  "/api/model/auxiliary",
  "/api/model/moa",
  "/api/model/options",
];

function withManagementProfile(url: string): string {
  if (!_managementProfile) return url;
  if (url.includes("profile=")) return url; // explicit param wins
  const path = url.split("?")[0];
  if (!PROFILE_SCOPED_PREFIXES.some((p) => path.startsWith(p))) return url;
  const sep = url.includes("?") ? "&" : "?";
  return `${url}${sep}profile=${encodeURIComponent(_managementProfile)}`;
}

async function fetchJSONResponse(
  url: string,
  init?: RequestInit,
  opts?: FetchJSONOptions,
  allowNotModified = false,
): Promise<Response> {
  url = withManagementProfile(url);
  // Inject the session token into all /api/ requests.
  const headers = new Headers(init?.headers);
  const token = window.__HERMES_SESSION_TOKEN__;
  if (token) {
    setSessionHeader(headers, token);
  }
  const isGet = !init?.method || init.method.toUpperCase() === "GET";
  const timeoutMs = opts?.timeoutMs ?? (isGet ? GET_TIMEOUT_MS : 0);
  const controller = timeoutMs > 0 ? new AbortController() : null;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let timedOut = false;
  if (controller) {
    // Propagate a caller-supplied abort signal into the timeout controller.
    if (init?.signal?.aborted) controller.abort();
    else init?.signal?.addEventListener("abort", () => controller.abort(), { once: true });
    timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, timeoutMs);
  }
  let res: Response;
  try {
    res = await fetch(`${BASE}${url}`, {
      ...init,
      headers,
      signal: controller ? controller.signal : init?.signal,
      // ``credentials: 'include'`` so the cookie-auth path (gated mode) works
      // for any fetch routed through here. Loopback mode is unaffected — the
      // server doesn't read cookies and the legacy session-token header is
      // already attached above.
      credentials: init?.credentials ?? "include",
    });
  } catch (e) {
    // "network timeout" so pollingStore classifies this like a network
    // failure (stale-while-error + backoff) instead of a contract error.
    if (timedOut) throw new Error(`network timeout after ${timeoutMs}ms: ${url}`);
    throw e;
  } finally {
    if (timer) clearTimeout(timer);
  }
  if (res.status === 401) {
    // Phase 6: the gated middleware emits a structured envelope so the
    // SPA can full-page-navigate to /login on session expiry. Parse it,
    // and only redirect on the known error codes — domain-level 401s
    // (e.g. "you don't have permission to read this monitor") bubble
    // up as regular errors so callers can handle them.
    let body: { error?: string; login_url?: string } = {};
    try {
      body = await res.clone().json();
    } catch {
      /* non-JSON 401 — let it fall through */
    }
    if (
      (body.error === "unauthenticated" || body.error === "session_expired") &&
      body.login_url
    ) {
      // Preserve where the user was so /auth/callback can land them back
      // after re-auth. The gate's login_url already carries a ``next=``
      // built from the request path, but the SPA may be deep inside a
      // SPA route the gate never saw — e.g. a hash route or a client-side
      // /sessions/<id> deep link. Save the current location as a
      // fallback the post-login handler can read.
      try {
        sessionStorage.setItem(
          "hermes.lastLocation",
          window.location.pathname + window.location.search,
        );
      } catch {
        /* SSR / privacy mode — ignore */
      }
      window.location.assign(body.login_url);
      // Never resolve — the page is about to unload.
      return new Promise<Response>(() => {});
    }
    // Loopback mode: ``_SESSION_TOKEN`` rotates on every server restart
    // (``hermes update``, ``hermes gateway restart``, etc.). A tab kept
    // open across the restart holds the OLD token in
    // ``window.__HERMES_SESSION_TOKEN__`` from the previous HTML render,
    // so every fetch returns 401. The HTML is served ``Cache-Control:
    // no-store`` so a reload picks up the freshly-injected token. Trigger
    // that reload once on the first stale-token 401 — gated mode is
    // handled above, so reaching here in gated mode means a real
    // middleware failure that should not reload-loop.
    if (!window.__HERMES_AUTH_REQUIRED__ && !opts?.skipStaleTokenReload) {
      let alreadyReloaded = false;
      try {
        alreadyReloaded =
          sessionStorage.getItem("hermes.tokenReloadAttempted") === "1";
      } catch {
        /* SSR / privacy mode — fall through to throw */
      }
      if (!alreadyReloaded) {
        try {
          sessionStorage.setItem("hermes.tokenReloadAttempted", "1");
        } catch {
          /* SSR / privacy mode — best effort */
        }
        window.location.reload();
        return new Promise<Response>(() => {});
      }
    }
  }
  if (res.ok) {
    // Clear the stale-token reload guard: a successful 2xx proves the
    // current ``window.__HERMES_SESSION_TOKEN__`` is valid, so the next
    // 401 — if any — should be allowed to trigger its own reload cycle.
    try {
      sessionStorage.removeItem("hermes.tokenReloadAttempted");
    } catch {
      /* SSR / privacy mode — ignore */
    }
  }
  if (allowNotModified && res.status === 304) return res;
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new Error(`${res.status}: ${text}`);
  }
  return res;
}

export async function fetchJSON<T>(
  url: string,
  init?: RequestInit,
  opts?: FetchJSONOptions,
): Promise<T> {
  const res = await fetchJSONResponse(url, init, opts);
  return res.json();
}

/** JSON fetch with response metadata, including conditional-GET 304 support. */
export async function fetchJSONWithMeta<T>(
  url: string,
  init?: RequestInit,
  opts?: FetchJSONOptions,
): Promise<FetchJSONWithMetaResult<T>> {
  const res = await fetchJSONResponse(url, init, opts, true);
  return {
    data: res.status === 304 ? undefined : await res.json(),
    status: res.status,
    headers: res.headers,
  };
}

/** Encode a plugin registry key for URL paths (preserves `/` segment separators). */
function pluginPath(name: string): string {
  return name.split("/").map(encodeURIComponent).join("/");
}

/**
 * Fetch a single-use ticket for a WebSocket upgrade in gated mode.
 *
 * The dashboard's gated-mode WS auth (``hermes_cli.web_server._ws_auth_ok``)
 * rejects the legacy ``?token=<_SESSION_TOKEN>`` path and only accepts
 * ``?ticket=<minted>`` consumed against the in-memory ticket store. Browsers
 * can't set ``Authorization`` on a WS upgrade, so this round-trip via the
 * authenticated REST endpoint is the bridge from cookie auth to WS auth.
 *
 * Tickets are single-use and TTL=30s — every WS connect attempt must
 * fetch a fresh ticket.
 */
export async function getWsTicket(): Promise<{ ticket: string; ttl_seconds: number }> {
  const res = await fetch(`${BASE}/api/auth/ws-ticket`, {
    method: "POST",
    credentials: "include",
  });
  if (!res.ok) {
    throw new Error(`/api/auth/ws-ticket: HTTP ${res.status}`);
  }
  return res.json();
}

/**
 * Resolve the auth query-param pair (``[name, value]``) for a WebSocket
 * connect. In gated mode mints a fresh single-use ticket; in loopback mode
 * returns the injected session token — but also falls back to a minted
 * ticket if that token is missing/empty (e.g. a service-worker serving a
 * stale precached ``index.html`` without the server's bootstrap script;
 * incident 2026-07-03), instead of silently sending an empty token that the
 * server would deterministically reject.
 */
export async function buildWsAuthParam(): Promise<[string, string]> {
  const token = window.__HERMES_SESSION_TOKEN__;
  if (window.__HERMES_AUTH_REQUIRED__ || !token) {
    const { ticket } = await getWsTicket();
    return ["ticket", ticket];
  }
  return ["token", token];
}

/**
 * Authenticated ``fetch`` for dashboard ``/api/...`` requests that aren't
 * plain JSON — file uploads (``FormData``), binary downloads (blobs), etc.
 * Mirrors ``fetchJSON``'s auth handling but returns the raw ``Response`` so
 * the caller can read ``.blob()`` / ``.formData()`` / stream it.
 *
 * Auth, in both modes, exactly as ``fetchJSON`` does it:
 *  - loopback / ``--insecure``: attach the ``X-Hermes-Session-Token`` header.
 *  - gated OAuth: no token header (it's absent by design); the
 *    ``hermes_session_at`` cookie rides along via ``credentials: 'include'``.
 *
 * Unlike ``fetchJSON`` this does NOT parse the body, does NOT throw on
 * non-2xx (the caller decides — a 404 on a download is meaningful), and
 * does NOT run the global 401 → /login redirect (binary endpoints aren't
 * navigation targets). Callers that want the redirect behaviour should use
 * ``fetchJSON``.
 */
export async function authedFetch(
  url: string,
  init?: RequestInit,
): Promise<Response> {
  const headers = new Headers(init?.headers);
  const token = window.__HERMES_SESSION_TOKEN__;
  if (token) {
    setSessionHeader(headers, token);
  }
  return fetch(`${BASE}${url}`, {
    ...init,
    headers,
    credentials: init?.credentials ?? "include",
  });
}

/**
 * Open a protected dashboard API file endpoint in a new tab.
 *
 * A plain ``<a href="/api/..."></a>`` cannot attach the loopback dashboard's
 * ``X-Hermes-Session-Token`` header, so protected deliverable links opened in a
 * new tab get ``401 Unauthorized`` even though normal SPA fetches work. Open a
 * placeholder tab synchronously from the click handler so popup blockers allow
 * it, fetch the file through the authenticated API path, then navigate the new
 * tab to a browser-owned blob URL for viewing/downloading.
 */
export async function openAuthedApiFile(url: string, label = "Hermes-Deliverable"): Promise<void> {
  const safeLabel = label.replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "\"": "&quot;",
    "'": "&#39;",
  }[char] ?? char));
  const opened = window.open("about:blank", "_blank");
  if (!opened) {
    throw new Error(`Der Browser hat den ${label}-Tab blockiert.`);
  }
  try {
    opened.opener = null;
  } catch {
    /* best-effort noopener hardening */
  }
  try {
    opened.document?.write(`<p>${safeLabel} wird geladen…</p>`);
  } catch {
    /* some browsers disallow writing to the placeholder; navigation below still works */
  }
  const res = await authedFetch(url);
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    try {
      opened.close();
    } catch {
      /* ignore */
    }
    throw new Error(`${label} konnte nicht geladen werden (${res.status}: ${text})`);
  }
  const blob = await res.blob();
  const blobUrl = URL.createObjectURL(blob);
  opened.location.href = blobUrl;
}

/**
 * Trigger a native download of a protected artifact by its query-token URL,
 * instead of the ``openAuthedApiFile`` blob-in-a-new-tab path.
 *
 * The blob path is wrong for large binaries opened on mobile: navigating a
 * tab to a ``blob:`` URL of an ``application/vnd.android.package-archive``
 * makes Chrome download it under the blob's UUID (losing the real filename)
 * and leaves the ``about:blank`` tab hanging on "wird geladen…". The
 * artifacts endpoint already streams with ``Content-Disposition: attachment;
 * filename=…`` and accepts the loopback session token as a ``?token=`` query
 * param (``_QUERY_TOKEN_API_PREFIXES``), exactly because a plain browser /
 * Android-download-manager request can't set the ``X-Hermes-Session-Token``
 * header. So hand the URL straight to the download manager: correct filename,
 * streamed to disk, no dangling tab. Cookies still ride along for gated mode;
 * the ``?token=`` covers loopback where no cookie is present.
 */
export function downloadAuthedArtifact(url: string, filename: string): void {
  const token = window.__HERMES_SESSION_TOKEN__;
  const sep = url.includes("?") ? "&" : "?";
  const href = token
    ? `${BASE}${url}${sep}token=${encodeURIComponent(token)}`
    : `${BASE}${url}`;
  const anchor = document.createElement("a");
  anchor.href = href;
  anchor.download = filename;
  anchor.rel = "noopener";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
}

/**
 * Build an absolute ``ws(s)://`` URL for a dashboard WebSocket endpoint,
 * with the correct auth query param appended for the active mode (fresh
 * single-use ``ticket`` in gated mode, ``token`` in loopback). Plugins and
 * the SPA should use this instead of hand-assembling a WS URL + reading
 * ``window.__HERMES_SESSION_TOKEN__`` directly, so the gated-mode ticket
 * path can never be forgotten.
 *
 * ``path`` is the dashboard-relative path (e.g.
 * ``"/api/plugins/kanban/events"``); the base-path prefix and host are
 * applied here. Extra query params can be supplied via ``params`` and are
 * merged before the auth param.
 */
export async function buildWsUrl(
  path: string,
  params?: Record<string, string>,
): Promise<string> {
  return buildHermesWebSocketUrl({
    authParam: await buildWsAuthParam(),
    basePath: BASE,
    params,
    path,
  });
}

export type AgentTerminalKind = "hermes" | "claude" | "codex" | "kimi" | "grok" | "qwen";

export interface AgentTerminalAgentState {
  available: boolean;
  binary: string | null;
  reason: string | null;
}

export interface AgentTerminalWorkdirOption {
  key: string;
  label: string;
  path: string;
  group?: "standard" | "projekt" | "worktree";
}

export interface AgentTerminalCapabilityState {
  tmux_available: boolean;
  hermes_tui_available: boolean;
  hermes_binary: string | null;
  reason: string | null;
  agents?: Record<string, AgentTerminalAgentState>;
  workdirs?: AgentTerminalWorkdirOption[];
}

export interface AgentTerminalWindow {
  session: string;
  window: string;
  active: boolean;
  pane_id: string;
  pid: number | null;
  command: string;
  cwd?: string | null;
  dead?: boolean;
  activity?: number | null;
}

export type AgentTerminalOverviewState = "dead" | "frage" | "laeuft" | "wartet" | "idle";

export interface AgentTerminalOverviewWindow extends AgentTerminalWindow {
  tail: string | null;
  state: AgentTerminalOverviewState;
  state_source: "heuristic";
}

export interface AgentTerminalOverviewResponse {
  now: number;
  windows: AgentTerminalOverviewWindow[];
}

export interface AgentTerminalSessionsResponse {
  sessions: string[];
}

export interface AgentTerminalWindowsResponse {
  windows: AgentTerminalWindow[];
}

export interface AgentTerminalWindowResponse {
  window: AgentTerminalWindow;
}

export interface AgentTerminalCaptureResponse {
  content: string;
}

export interface AgentTerminalUploadResponse {
  ok: boolean;
  path: string;
  name: string;
  size: number;
}

export interface ControlOverviewHealthResponse {
  overall?: string;
  subsystems?: Record<string, { status?: string; detail?: string; error?: string | null }>;
}

export interface ControlOverviewVaultSession {
  agent: string;
  started: string;
  task: string;
  path: string;
  stale?: boolean;
}

export interface ControlOverviewVaultReceipt {
  when: string;
  agent: string;
  file: string;
  path: string;
}

export interface ControlOverviewVaultResponse {
  error?: string | null;
  stale_count?: number;
  open_sessions?: ControlOverviewVaultSession[];
  recent_receipts?: ControlOverviewVaultReceipt[];
}

export interface ControlOverviewKanbanTask {
  id: string;
  title: string;
  status: string;
  assignee?: string | null;
}

export interface ControlOverviewKanbanColumn {
  name: string;
  tasks: ControlOverviewKanbanTask[];
}

export interface ControlOverviewKanbanBoardResponse {
  columns?: ControlOverviewKanbanColumn[];
  now?: number;
}

export interface ControlOverviewDecisionQueueResponse {
  count?: number;
  decisions?: Array<{ task_id?: string; task_title?: string; kind?: string }>;
}

/** Single option on an open agent-question (scrape store /api/agent-questions). */
export interface AgentQuestionOption {
  nr: number | string;
  label: string;
  recommended: boolean;
}

/**
 * AI answer suggestion (Feature A Slice 2) as serialized by
 * GET /api/agent-questions: `nr` references an option number, array order is
 * the ranking (first entry = top suggestion).
 */
export interface AgentQuestionSuggestion {
  nr: number;
  rationale: string;
}

/**
 * Flat event dict from GET /api/agent-questions (hermes_cli agent_questions schema).
 * `options[].nr` may be int ("1") or y/n string ("y"/"n"). Newest-first.
 */
export interface AgentQuestionEvent {
  id: number;
  ts: string;
  updated_ts: string | null;
  source: string;
  session: string;
  window: string;
  pane_id: string;
  fingerprint: string;
  kind: string | null;
  cwd: string | null;
  question_text: string;
  options: AgentQuestionOption[];
  class: string | null;
  status: string;
  answered_by: string | null;
  answer: string | null;
  latency_s: number | null;
  /** SQLite INTEGER 0/1 — serialized as number, NOT boolean (POST result `verified` IS boolean). */
  answer_verified: number | null;
  override: number;
  /** Feature A Slice 2 — all six null when no suggestion exists (degradation). */
  suggestions: AgentQuestionSuggestion[] | null;
  suggested_by: string | null;
  suggest_confidence: "high" | "low" | null;
  suggested_ts: string | null;
  suggest_latency_ms: number | null;
  answer_source:
    | "suggested_accepted"
    | "suggested_edited"
    | "operator_free"
    | "terminal"
    | null;
}

/**
 * PA (Jarvis) chat contracts — hermes_cli/pa_chat.py. Single global
 * conversation ("default"); send returns a turn_id, the turn is polled until
 * done|error, GET /api/pa/messages is the bubble source of truth.
 */

/** One bubble from GET /api/pa/messages (chronological page, newest page first
 *  loadable via the `before_id` cursor). `status`/`error` come from the JOINed
 *  turn and are identical on the user and assistant bubble of that turn —
 *  `status === "error"` marks the failed turn (no content heuristics). */
export interface PaChatMessage {
  id: number;
  turn_id: string;
  role: "user" | "assistant";
  content: string;
  engine: string;
  model: string;
  attachments: PaAttachmentRef[];
  /** Unix seconds. */
  ts: number;
  status: "pending" | "running" | "done" | "error";
  error: string | null;
}

/** Turn state from GET /api/pa/turns/{id}. On error the poll stays HTTP 200
 *  and `error`/`reply` carry the same human-readable message. */
export interface PaTurn {
  turn_id: string;
  status: "pending" | "running" | "done" | "error";
  reply: string | null;
  engine: string;
  model: string;
  ts: number;
  error: string | null;
}

/** Attachment reference accepted by POST /api/pa/message (max 1 per turn). */
export interface PaAttachmentRef {
  asset_id: string;
}

/** One page of the PA bubble history (GET /api/pa/messages). Older pages are
 *  fetched with `before_id = next_before_id` and prepended; null = no more. */
export interface PaMessagesPage {
  messages: PaChatMessage[];
  next_before_id: number | null;
}

/** S2.2 engine roster entry (GET /api/pa/engines). */
export interface PaEngineSpec {
  engine: string;
  models: string[];
  default_model: string;
  supports_images: boolean;
}

export interface PaEnginesResponse {
  default_engine: string;
  engines: PaEngineSpec[];
}

/** S2.4 decision inbox (GET /api/pa/inbox). Items are typed; sources that
 *  failed server-side degrade to an `errors` entry instead of failing all. */
export interface PaInboxActionPayload {
  version: number;
  category: string;
  payload: Record<string, string>;
  reason: string | null;
}

interface PaInboxItemBase {
  /** "q<event_id>" for question rows, the card id for kanban rows. */
  id: string;
  title: string;
  block_radius: number;
  /** Unix seconds. */
  ts: number;
}

/** pa_action: gated action waiting for operator confirm. Answered through the
 *  existing POST /api/agent-questions/{question_id}/answer ("1" execute,
 *  "2" reject); 409 = stale/double-tap → refresh the inbox. */
export interface PaInboxActionItem extends PaInboxItemBase {
  type: "pa_action";
  question_id: number;
  kind: string | null;
  category: string | null;
  action_payload: PaInboxActionPayload | null;
  options: AgentQuestionOption[];
}

/** question: classic agent question — answering stays on the classic tab. */
export interface PaInboxQuestionItem extends PaInboxItemBase {
  type: "question";
  question_id: number;
  kind: string | null;
  options: AgentQuestionOption[];
}

/** held_task / freigabe_gate: kanban card waiting — links to the board. */
export interface PaInboxTaskItem extends PaInboxItemBase {
  type: "held_task" | "freigabe_gate";
  card_id: string;
  status: string | null;
  freigabe: string | null;
}

export type PaInboxItem = PaInboxActionItem | PaInboxQuestionItem | PaInboxTaskItem;

export interface PaInboxError {
  source: string;
  error: string;
}

export interface PaInboxResponse {
  generated_at: number;
  items: PaInboxItem[];
  errors: PaInboxError[];
}

/** S3.3-FE PlanSpec-Draft (POST /api/pa/planspec/draft, pa_planspec.py): die
 *  Engine erzeugt genau eine PlanSpec-Markdown-Datei, der PA-Validator liefert
 *  status+findings (INVALID des CLI wird als BLOCK normalisiert). */
export interface PaPlanspecValidation {
  status: "CLEAN" | "WARN" | "BLOCK";
  findings: string[];
}

/** Ein Slice aus taskgraph_hints.subtasks des Drafts. */
export interface PaPlanspecSlice {
  id: string;
  title: string;
  lane: string;
  deps: string[];
}

export interface PaPlanspecDraft {
  draft_id: string;
  planspec_text: string;
  validation: PaPlanspecValidation;
  slices: PaPlanspecSlice[];
}

// S3.6 — Push-to-Talk + Vorlesen (Jarvis-Chat). Payload-Shapes exakt aus den
// Backend-Modellen (hermes_cli/web_server.py: AudioTranscriptionRequest /
// TTSSpeakRequest und ihre Antworten).
/** Antwort von POST /api/audio/transcribe. */
export interface TranscribeResponse {
  ok: boolean;
  transcript: string;
  provider: string;
  polished: boolean;
}

/** Antwort von POST /api/audio/speak — data_url ist direkt an `new Audio()` fütterbar. */
export interface SpeakResponse {
  data_url: string;
}

// S2.7 — Estate-Graph (GET /api/pa/graph, hermes_cli/pa_graph.py). Kontrakt
// „pa-graph/v1": x/y deterministisch in der 1280x820-ViewBox vorberechnet;
// Teilquellen-Ausfälle kommen als errors[] mit (HTTP bleibt 200). href ist
// der kanonische Navigationswert (ref = Mock-Kompatibilitätsalias); vault://
// und memory:// sind semantische Refs, NICHT browser-navigierbar.
export interface PaGraphCluster {
  id: string;
  label: string;
  color: string;
}

export interface PaGraphNode {
  id: string;
  label: string | null;
  cluster: string;
  kind: string;
  /** 0.2–1.0, gradstarke Hubs angehoben. */
  weight: number;
  x: number;
  y: number;
  href?: string;
  ref?: string;
}

export interface PaGraphEdge {
  from: string;
  to: string;
  kind: string;
}

export interface PaGraphResponse {
  schema: string;
  source: string;
  layout: string;
  generated_at: string;
  refresh: { interval_s: number; cache_ttl_s?: number; invalidation?: string; on_error?: string };
  clusters: PaGraphCluster[];
  nodes: PaGraphNode[];
  edges: PaGraphEdge[];
  /** {source, error} pro ausgefallener Teilquelle; dezent anzeigen, nie panel. */
  errors?: PaInboxError[];
}

// S6.4a — PA-Feed (GET /api/pa/feed, hermes_cli/pa_chat.py PAStore.feed_page):
// aufsteigende, bounded Feed-Page für das KI-LAGE-Panel. since_id-Cursor für
// Polling-Clients; das Panel zeigt die letzten ~5 Einträge (Titel + Alter).
export interface PaFeedItem {
  id: number;
  /** Unix-Sekunden. */
  ts: number;
  kind: string;
  severity: string;
  title: string;
  ref: string | null;
  delivered_push: number;
}

export interface PaFeedPage {
  items: PaFeedItem[];
  next_since_id: number;
  has_more: boolean;
}

/** Build a ``?profile=<name>`` query suffix, or "" when unset.
 *
 * Used by the skills/toolsets endpoints so the dashboard can manage a
 * profile other than the one the server process runs under. */
function profileQuery(profile?: string): string {
  return profile ? `?profile=${encodeURIComponent(profile)}` : "";
}

function appendProfileParam(url: string, profile?: string): string {
  if (!profile || url.includes("profile=")) return url;
  return `${url}${url.includes("?") ? "&" : "?"}profile=${encodeURIComponent(profile)}`;
}

export const api = {
  buildWsUrl,
  getStatus: () => fetchJSON<StatusResponse>("/api/status"),
  getAgentTerminalCapabilities: () =>
    fetchJSON<AgentTerminalCapabilityState>("/api/agent-terminals/capabilities"),
  getAgentTerminalSessions: () =>
    fetchJSON<AgentTerminalSessionsResponse>("/api/agent-terminals/sessions"),
  getAgentTerminalWindows: (session?: string) =>
    fetchJSON<AgentTerminalWindowsResponse>(
      `/api/agent-terminals/windows${session ? `?session=${encodeURIComponent(session)}` : ""}`,
    ),
  showAgentTerminalWindow: (session: string, window: string) =>
    fetchJSON<AgentTerminalWindowResponse>("/api/agent-terminals/show", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session, window }),
    }),
  listAgentQuestions: () =>
    fetchJSON<{ questions: AgentQuestionEvent[] }>(
      "/api/agent-questions?status=open&limit=50",
    ),
  answerAgentQuestion: (id: number, answer: string, viaSuggestion?: number) =>
    // For kind=pa_action events the 200 body additionally carries
    // `executed`/`action_result` (S2.3b executor evidence); stale or
    // double-tapped rows fail with 409 → callers refresh their source.
    fetchJSON<{
      ok: boolean;
      verified: boolean;
      latency_s?: number;
      executed?: boolean;
      action_result?: unknown;
    }>(
      `/api/agent-questions/${id}/answer`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          answer,
          answered_by: "operator",
          // Optional additive field (Feature A Slice 2): nr of the accepted
          // stored suggestion; omitted entirely for free/edited answers.
          ...(viaSuggestion !== undefined ? { via_suggestion: viaSuggestion } : {}),
        }),
      },
    ),
  ensureAgentTerminalWindow: (kind: AgentTerminalKind, workdir?: string) =>
    fetchJSON<AgentTerminalWindowResponse>("/api/agent-terminals/ensure", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind, ...(workdir ? { workdir } : {}) }),
    }),
  // PA (Jarvis) chat — pa_chat.py. Bubble history is the source of truth;
  // send returns a turn_id that is polled until done|error.
  listPaMessages: (limit = 30, beforeId?: number) => {
    const params = new URLSearchParams({ limit: String(limit) });
    if (beforeId !== undefined) params.set("before_id", String(beforeId));
    return fetchJSON<PaMessagesPage>(`/api/pa/messages?${params.toString()}`);
  },
  sendPaMessage: (
    text: string,
    attachments?: PaAttachmentRef[],
    options?: { engine?: string; model?: string; projectScope?: string },
  ) =>
    fetchJSON<{ turn_id: string }>("/api/pa/message", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text,
        ...(attachments && attachments.length > 0 ? { attachments } : {}),
        // S2.2: engine/model choice applies to the NEXT turn (optional, the
        // backend default stays sol/gpt-5.6-sol). S2.5: the view state rides
        // along as optional project_scope (omitted when no project is open).
        ...(options?.engine ? { engine: options.engine } : {}),
        ...(options?.model ? { model: options.model } : {}),
        ...(options?.projectScope ? { project_scope: options.projectScope } : {}),
      }),
    }),
  getPaTurn: (turnId: string) =>
    fetchJSON<PaTurn>(`/api/pa/turns/${encodeURIComponent(turnId)}`),
  // S2.2 switcher roster + S2.4 decision inbox + S2.7 estate graph.
  getPaEngines: () => fetchJSON<PaEnginesResponse>("/api/pa/engines"),
  getPaInbox: () => fetchJSON<PaInboxResponse>("/api/pa/inbox"),
  getPaGraph: () => fetchJSON<PaGraphResponse>("/api/pa/graph"),
  // S6.4a: PA-Feed für das KI-LAGE-Panel (aufsteigend, bounded).
  getPaFeed: (limit = 50) =>
    fetchJSON<PaFeedPage>(`/api/pa/feed?limit=${limit}`),
  // S3.3-FE PlanSpec-Draft + Propose (pa_planspec.py). engine/model folgen der
  // S2.2-Switcher-Wahl (weg gelassen → Backend-Default sol); project ist
  // optionaler Kontext. 422 = Engine-Ausgabe ohne PlanSpec-Frontmatter
  // (detail = {error, engine_output}), 400 = unbekannte Engine/Modell.
  draftPlanspec: (
    idea: string,
    options?: { project?: string; engine?: string; model?: string },
  ) =>
    fetchJSON<PaPlanspecDraft>("/api/pa/planspec/draft", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        idea,
        ...(options?.project ? { project: options.project } : {}),
        ...(options?.engine ? { engine: options.engine } : {}),
        ...(options?.model ? { model: options.model } : {}),
      }),
    }),
  // Draft als Approval-Card (planspec.ingest) in die Inbox stellen.
  // 400 = BLOCK/stale Validate-Metadaten, 404 = Draft verschwunden; ein
  // Duplikat liefert idempotent die bereits offene question_id (Dedup im
  // Backend, kein 409).
  proposePlanspec: (draftId: string) =>
    fetchJSON<{ question_id: number }>("/api/pa/planspec/propose", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ draft_id: draftId }),
    }),
  // S3.6 — Push-to-Talk: Audio als base64 data-URL transkribieren (Limit
  // 25 MB, language hint "de" wie im Jarvis-Chat üblich). mime_type weg
  // lassen → Backend liest es aus dem data-URL-Header.
  transcribeAudio: (dataUrl: string, mimeType?: string) =>
    fetchJSON<TranscribeResponse>("/api/audio/transcribe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ data_url: dataUrl, mime_type: mimeType, language: "de" }),
    }),
  // S3.6 — Antworten vorlesen: TTS über die konfigurierte Provider-Kette,
  // Antwort trägt eine direkt abspielbare Audio-data-URL.
  speakText: (text: string) =>
    fetchJSON<SpeakResponse>("/api/audio/speak", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    }),
  /** Authenticated asset URL (cookie/session like every other same-origin
   *  request). 404 = pruned upload → the bubble shows a broken-attachment
   *  state instead of losing the thread. */
  paAssetUrl: (assetId: string) => `/api/pa/asset/${encodeURIComponent(assetId)}`,
  uploadPaImage: (file: File) => {
    // Same raw multipart/form-data pattern as uploadAgentTerminalFile — no
    // Content-Type header, the browser sets the multipart boundary itself.
    // Backend contract: field name "file", images only, max 15 MiB.
    const form = new FormData();
    form.append("file", file, file.name);
    return fetchJSON<{ asset_id: string }>("/api/pa/upload", {
      method: "POST",
      body: form,
    });
  },
  // Live-Screen-Share (S-live): a real, continuous getDisplayMedia session —
  // NOT the image picker. start() opens a session; the client streams the
  // latest frame to /frame (latest wins, no asset pile); attach() materialises
  // the current frame into ONE normal upload asset for the image-turn pipeline;
  // stop() ends it (idempotent server-side).
  startLiveShare: () =>
    fetchJSON<{ session_id: string }>("/api/pa/live-share/start", {
      method: "POST",
    }),
  uploadLiveShareFrame: (sessionId: string, frame: Blob) => {
    const form = new FormData();
    form.append("file", frame, "frame.jpg");
    return fetchJSON<{ ok: boolean }>(
      `/api/pa/live-share/${encodeURIComponent(sessionId)}/frame`,
      { method: "POST", body: form },
    );
  },
  attachLiveShareFrame: (sessionId: string) =>
    fetchJSON<{ asset_id: string }>(
      `/api/pa/live-share/${encodeURIComponent(sessionId)}/attach`,
      { method: "POST" },
    ),
  stopLiveShare: (sessionId: string) =>
    fetchJSON<{ ok: boolean }>(
      `/api/pa/live-share/${encodeURIComponent(sessionId)}/stop`,
      { method: "POST" },
    ),
  createAgentTerminalWindow: (kind: AgentTerminalKind, workdir?: string) =>
    fetchJSON<AgentTerminalWindowResponse>("/api/agent-terminals/create", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind, ...(workdir ? { workdir } : {}) }),
    }),
  respawnAgentTerminalWindow: (session: string, window: string) =>
    fetchJSON<AgentTerminalWindowResponse>("/api/agent-terminals/respawn", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session, window }),
    }),
  killDeadAgentTerminalWindow: (session: string, window: string) =>
    fetchJSON<{ ok: boolean }>("/api/agent-terminals/kill-dead", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session, window }),
    }),
  terminateAgentTerminalWindow: (session: string, window: string, external = false) =>
    fetchJSON<{ ok: boolean }>("/api/agent-terminals/terminate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session, window, external }),
    }),
  captureAgentTerminalWindow: (session: string, window: string, start?: number) =>
    fetchJSON<AgentTerminalCaptureResponse>("/api/agent-terminals/capture", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session, window, ...(start !== undefined ? { start } : {}) }),
    }),
  detachAgentTerminalClient: (clientId: string) =>
    fetchJSON<{ ok: boolean }>("/api/agent-terminals/detach-client", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ client_id: clientId }),
    }),
  renameAgentTerminalWindow: (session: string, window: string, name: string) =>
    fetchJSON<AgentTerminalWindowResponse>("/api/agent-terminals/rename", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session, window, name }),
    }),
  getAgentTerminalOverview: () =>
    fetchJSON<AgentTerminalOverviewResponse>("/api/agent-terminals/overview"),
  sendAgentTerminalKeys: (session: string, window: string, text: string) =>
    fetchJSON<{ ok: boolean }>("/api/agent-terminals/send-keys", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session, window, text }),
    }),
  uploadAgentTerminalFile: (file: File) => {
    // Same raw multipart/form-data pattern as uploadFile above — no
    // Content-Type header, the browser sets the multipart boundary itself.
    const form = new FormData();
    form.append("file", file, file.name);
    return fetchJSON<AgentTerminalUploadResponse>("/api/agent-terminals/upload", {
      method: "POST",
      body: form,
    });
  },
  getControlOverviewHealth: () =>
    fetchJSON<ControlOverviewHealthResponse>("/api/health-status"),
  getControlOverviewVault: () =>
    fetchJSON<ControlOverviewVaultResponse>("/api/vault/provenance"),
  getControlOverviewKanbanBoard: () =>
    fetchJSON<ControlOverviewKanbanBoardResponse>(
      "/api/plugins/kanban/board?card_diagnostics=summary&card_body=none",
    ),
  getControlOverviewDecisionQueue: () =>
    fetchJSON<ControlOverviewDecisionQueueResponse>("/api/plugins/kanban/decision-queue"),
  /**
   * Identity probe for the dashboard auth gate (Phase 7).
   *
   * Returns the verified Session as JSON when gated mode is active and a
   * valid cookie is attached. Loopback mode is unaffected — the endpoint
   * still exists but is never useful there (no Session, no cookie). The
   * AuthWidget component swallows 401s from this call: if the gate isn't
   * engaged, /api/auth/me returns 401 and the widget renders nothing.
   *
   * ``skipStaleTokenReload`` is load-bearing: in loopback mode this endpoint
   * 401s by design, and fetchJSON's default loopback behaviour treats a
   * 401 as a rotated session token and full-page-reloads to pick up a
   * fresh one. Because every *other* dashboard request succeeds (and so
   * clears the one-shot reload guard), that turns this expected 401 into
   * an infinite reload loop. Opting out keeps the 401 a plain throw the
   * widget can catch.
   */
  getAuthMe: () =>
    fetchJSON<AuthMeResponse>("/api/auth/me", undefined, {
      // Loopback mode answers /api/auth/me with 401 on every call (gate not
      // engaged). That is not a stale token, so it must NOT trigger the
      // auto-reload — otherwise the SPA reload-loops. Let the 401 bubble up;
      // AuthWidget swallows it and renders nothing.
      skipStaleTokenReload: true,
    }),
  logout: () =>
    fetch(`${BASE}/auth/logout`, {
      method: "POST",
      credentials: "include",
    }).then((r) => {
      // /auth/logout returns 302 → /login. Follow that with a full-page
      // navigation rather than letting fetch() opaquely consume the
      // redirect — the SPA needs to leave the protected area.
      window.location.assign("/login");
      return r;
    }),
  getSessions: (
    limit = 20,
    offset = 0,
    profile = getManagementProfile(),
    order: "created" | "recent" = "created",
  ) =>
    fetchJSON<PaginatedSessions>(
      appendProfileParam(
        `/api/sessions?limit=${limit}&offset=${offset}&order=${order}`,
        profile,
      ),
    ),
  getSessionMessages: (id: string, profile = getManagementProfile()) =>
    fetchJSON<SessionMessagesResponse>(
      appendProfileParam(`/api/sessions/${encodeURIComponent(id)}/messages`, profile),
    ),
  getSessionDetail: (id: string, profile = getManagementProfile()) =>
    fetchJSON<SessionInfo>(
      appendProfileParam(`/api/sessions/${encodeURIComponent(id)}`, profile),
    ),
  getSessionLatestDescendant: (id: string, profile = getManagementProfile()) =>
    fetchJSON<SessionLatestDescendantResponse>(
      appendProfileParam(
        `/api/sessions/${encodeURIComponent(id)}/latest-descendant`,
        profile,
      ),
    ),
  deleteSession: (id: string, profile = getManagementProfile()) =>
    fetchJSON<{ ok: boolean }>(
      appendProfileParam(`/api/sessions/${encodeURIComponent(id)}`, profile),
      {
        method: "DELETE",
      },
    ),
  getEmptySessionsCount: (profile = getManagementProfile()) =>
    fetchJSON<{ count: number }>(
      appendProfileParam("/api/sessions/empty/count", profile),
    ),
  deleteEmptySessions: (profile = getManagementProfile()) =>
    fetchJSON<{ ok: boolean; deleted: number }>(
      appendProfileParam("/api/sessions/empty", profile),
      {
        method: "DELETE",
      },
    ),
  bulkDeleteSessions: (ids: string[], profile = getManagementProfile()) =>
    fetchJSON<{ ok: boolean; deleted: number }>("/api/sessions/bulk-delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids, profile: profile || undefined }),
    }),
  renameSession: (id: string, title: string, profile = getManagementProfile()) =>
    fetchJSON<{ ok: boolean; title: string }>(
      `/api/sessions/${encodeURIComponent(id)}`,
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title, profile: profile || undefined }),
      },
    ),
  getSessionStats: (profile = getManagementProfile()) =>
    fetchJSON<SessionStoreStats>(appendProfileParam("/api/sessions/stats", profile)),
  exportSessionUrl: (id: string, profile = getManagementProfile()) =>
    appendProfileParam(`/api/sessions/${encodeURIComponent(id)}/export`, profile),
  importSessions: (
    sessions: Array<Record<string, unknown>>,
    profile = getManagementProfile(),
  ) =>
    fetchJSON<SessionImportResponse>("/api/sessions/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sessions, profile: profile || undefined }),
    }),
  pruneSessions: (
    older_than_days: number,
    source?: string,
    profile = getManagementProfile(),
  ) =>
    fetchJSON<{ ok: boolean; removed: number }>("/api/sessions/prune", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ older_than_days, source, profile: profile || undefined }),
    }),
  listFiles: (path?: string) => {
    const query = path ? `?path=${encodeURIComponent(path)}` : "";
    return fetchJSON<ManagedFilesResponse>(`/api/files${query}`);
  },
  readFile: (path: string) =>
    fetchJSON<ManagedFileReadResponse>(
      `/api/files/read?path=${encodeURIComponent(path)}`,
    ),
  uploadFile: (path: string, file: File, overwrite = true) => {
    // Stream the raw bytes as multipart/form-data. Do NOT set Content-Type —
    // the browser adds the multipart boundary automatically. Sending the file
    // as base64 JSON (the old path) inflated the body ~33%, buffered the whole
    // file in memory, and 502'd on large backup archives behind the proxy
    // (NS-501).
    const form = new FormData();
    form.append("path", path);
    form.append("overwrite", String(overwrite));
    form.append("file", file, file.name);
    return fetchJSON<ManagedFileWriteResponse>("/api/files/upload-stream", {
      method: "POST",
      body: form,
    });
  },
  createDirectory: (path: string) =>
    fetchJSON<ManagedFileWriteResponse>("/api/files/mkdir", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    }),
  deleteFile: (path: string, recursive = false) =>
    fetchJSON<{ ok: boolean; path: string }>("/api/files", {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path, recursive }),
    }),
  getLogs: (params: { file?: string; lines?: number; level?: string; component?: string }) => {
    const qs = new URLSearchParams();
    if (params.file) qs.set("file", params.file);
    if (params.lines) qs.set("lines", String(params.lines));
    if (params.level && params.level !== "ALL") qs.set("level", params.level);
    if (params.component && params.component !== "all") qs.set("component", params.component);
    return fetchJSON<LogsResponse>(`/api/logs?${qs.toString()}`);
  },
  getAnalytics: (days: number, profile = getManagementProfile()) =>
    fetchJSON<AnalyticsResponse>(
      appendProfileParam(`/api/analytics/usage?days=${days}`, profile),
    ),
  getModelsAnalytics: (days: number, profile = getManagementProfile()) =>
    fetchJSON<ModelsAnalyticsResponse>(
      appendProfileParam(`/api/analytics/models?days=${days}`, profile),
    ),
  getConfig: (profile = getManagementProfile()) =>
    fetchJSON<Record<string, unknown>>(appendProfileParam("/api/config", profile)),
  getDefaults: () => fetchJSON<Record<string, unknown>>("/api/config/defaults"),
  getSchema: () => fetchJSON<{ fields: Record<string, unknown>; category_order: string[] }>("/api/config/schema"),
  getModelInfo: (profile = getManagementProfile()) =>
    fetchJSON<ModelInfoResponse>(appendProfileParam("/api/model/info", profile)),
  getModelOptions: (
    profileOrOptions?: string | { profile?: string; refresh?: boolean },
  ) => {
    const profile =
      typeof profileOrOptions === "string"
        ? profileOrOptions
        : profileOrOptions?.profile;
    const refresh =
      typeof profileOrOptions === "object" && !!profileOrOptions.refresh;
    const qs = new URLSearchParams();
    if (profile) qs.set("profile", profile);
    if (refresh) qs.set("refresh", "1");
    // Dashboard surfaces (Models page, profile builder, cron) are
    // management/setup UIs: keep the full provider universe with setup
    // affordances. The endpoint now defaults to the configured subset for
    // desktop chat pickers (#56974), so opt in explicitly here.
    qs.set("include_unconfigured", "1");
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    return fetchJSON<ModelOptionsResponse>(`/api/model/options${suffix}`);
  },
  getAuxiliaryModels: (profile = getManagementProfile()) =>
    fetchJSON<AuxiliaryModelsResponse>(
      appendProfileParam("/api/model/auxiliary", profile),
    ),
  getMoaModels: () => fetchJSON<MoaConfigResponse>("/api/model/moa"),
  saveMoaModels: (body: MoaConfigResponse) =>
    fetchJSON<MoaConfigResponse & { ok: boolean }>("/api/model/moa", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  setModelAssignment: (
    body: ModelAssignmentRequest,
    profile = getManagementProfile(),
  ) =>
    fetchJSON<ModelAssignmentResponse>(
      appendProfileParam("/api/model/set", profile),
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    ),
  saveConfig: (config: Record<string, unknown>, profile = getManagementProfile()) =>
    fetchJSON<{ ok: boolean }>(appendProfileParam("/api/config", profile), {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ config }),
    }),
  getConfigRaw: (profile = getManagementProfile()) =>
    fetchJSON<{ yaml: string; path?: string }>(
      appendProfileParam("/api/config/raw", profile),
    ),
  saveConfigRaw: (yaml_text: string, profile = getManagementProfile()) =>
    fetchJSON<{ ok: boolean }>(appendProfileParam("/api/config/raw", profile), {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ yaml_text }),
    }),
  getEnvVars: () => fetchJSON<Record<string, EnvVarInfo>>("/api/env"),
  setEnvVar: (key: string, value: string) =>
    fetchJSON<{ ok: boolean }>("/api/env", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key, value }),
    }),
  deleteEnvVar: (key: string) =>
    fetchJSON<{ ok: boolean }>("/api/env", {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key }),
    }),
  revealEnvVar: (key: string) =>
    fetchJSON<{ key: string; value: string }>("/api/env/reveal", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key }),
    }),

  // Cron jobs
  getCronJobs: (profile = "all") =>
    fetchJSON<CronJob[]>(`/api/cron/jobs?profile=${encodeURIComponent(profile)}`),
  // Single-job detail INCLUDING prompt/script (the bulk list redacts those).
  getCronJob: (id: string, profile?: string) =>
    fetchJSON<CronJob>(
      `/api/cron/jobs/${encodeURIComponent(id)}` +
        (profile ? `?profile=${encodeURIComponent(profile)}` : ""),
    ),
  getCronDeliveryTargets: () =>
    fetchJSON<{ targets: CronDeliveryTarget[] }>("/api/cron/delivery-targets"),
  createCronJob: (job: CronJobMutation, profile = "default") =>
    fetchJSON<CronJob>(`/api/cron/jobs?profile=${encodeURIComponent(profile)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(job),
    }),
  pauseCronJob: (id: string, profile = "default") =>
    fetchJSON<CronJob>(`/api/cron/jobs/${encodeURIComponent(id)}/pause?profile=${encodeURIComponent(profile)}`, { method: "POST" }),
  updateCronJob: (
    id: string,
    updates: CronJobMutation,
    profile = "default",
  ) =>
    fetchJSON<CronJob>(
      `/api/cron/jobs/${encodeURIComponent(id)}?profile=${encodeURIComponent(profile)}`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ updates }),
      },
    ),
  resumeCronJob: (id: string, profile = "default") =>
    fetchJSON<CronJob>(`/api/cron/jobs/${encodeURIComponent(id)}/resume?profile=${encodeURIComponent(profile)}`, { method: "POST" }),
  triggerCronJob: (id: string, profile = "default") =>
    fetchJSON<CronJob>(`/api/cron/jobs/${encodeURIComponent(id)}/trigger?profile=${encodeURIComponent(profile)}`, { method: "POST" }),
  deleteCronJob: (id: string, profile = "default") =>
    fetchJSON<{ ok: boolean }>(`/api/cron/jobs/${encodeURIComponent(id)}?profile=${encodeURIComponent(profile)}`, { method: "DELETE" }),

  // Automation Blueprints — parameterized automation blueprints
  getAutomationBlueprints: () =>
    fetchJSON<{ blueprints: AutomationBlueprint[] }>("/api/cron/blueprints"),
  instantiateAutomationBlueprint: (
    body: { blueprint: string; values: Record<string, string> },
    profile = "default",
  ) =>
    fetchJSON<CronJob>(`/api/cron/blueprints/instantiate?profile=${encodeURIComponent(profile)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),

  // Profiles
  getProfiles: () =>
    fetchJSON<{ profiles: ProfileInfo[] }>("/api/profiles"),
  getActiveProfile: () =>
    fetchJSON<ActiveProfileInfo>("/api/profiles/active"),
  setActiveProfile: (name: string) =>
    fetchJSON<{ ok: boolean; active: string }>("/api/profiles/active", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    }),
  createProfile: (body: {
    name: string;
    clone_from?: string | null;
    clone_from_default?: boolean;
    clone_all?: boolean;
    no_skills?: boolean;
    description?: string;
    provider?: string;
    model?: string;
    mcp_servers?: McpServerCreate[];
    keep_skills?: string[];
    hub_skills?: string[];
  }) =>
    fetchJSON<{
      ok: boolean;
      name: string;
      path: string;
      model_set?: boolean;
      mcp_written?: number;
      skills_disabled?: number;
      hub_installs?: Array<{ identifier: string; pid: number | null }>;
    }>("/api/profiles", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  updateProfileDescription: (name: string, description: string) =>
    fetchJSON<{ ok: boolean; description: string; description_auto: boolean }>(
      `/api/profiles/${encodeURIComponent(name)}/description`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ description }),
      },
    ),
  describeProfileAuto: (name: string, overwrite = true) =>
    fetchJSON<ProfileDescribeAutoResult>(
      `/api/profiles/${encodeURIComponent(name)}/describe-auto`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ overwrite }),
      },
    ),
  setProfileModel: (name: string, provider: string, model: string) =>
    fetchJSON<{ ok: boolean; provider: string; model: string }>(
      `/api/profiles/${encodeURIComponent(name)}/model`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider, model }),
      },
    ),
  renameProfile: (name: string, newName: string) =>
    fetchJSON<{ ok: boolean; name: string; path: string }>(
      `/api/profiles/${encodeURIComponent(name)}`,
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ new_name: newName }),
      },
    ),
  deleteProfile: (name: string) =>
    fetchJSON<{ ok: boolean }>(
      `/api/profiles/${encodeURIComponent(name)}`,
      { method: "DELETE" },
    ),
  getProfileSetupCommand: (name: string) =>
    fetchJSON<{ command: string }>(
      `/api/profiles/${encodeURIComponent(name)}/setup-command`,
    ),
  getProfileSoul: (name: string) =>
    fetchJSON<{ content: string; exists: boolean }>(
      `/api/profiles/${encodeURIComponent(name)}/soul`,
    ),
  updateProfileSoul: (name: string, content: string) =>
    fetchJSON<{ ok: boolean }>(
      `/api/profiles/${encodeURIComponent(name)}/soul`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content }),
      },
    ),

  // Skills & Toolsets
  //
  // All calls accept an optional ``profile`` so the Skills page can manage
  // any profile's skills/toolsets — not just the one the dashboard process
  // runs under. Omitted/empty profile = the dashboard's own profile.
  getSkills: (profile?: string) =>
    fetchJSON<SkillInfo[]>(`/api/skills${profileQuery(profile)}`),
  toggleSkill: (name: string, enabled: boolean, profile?: string) =>
    fetchJSON<{ ok: boolean }>("/api/skills/toggle", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, enabled, profile: profile || undefined }),
    }),
  getSkillContent: (name: string, profile?: string) =>
    fetchJSON<SkillContent>(
      `/api/skills/content?name=${encodeURIComponent(name)}${profile ? `&profile=${encodeURIComponent(profile)}` : ""}`,
    ),
  createSkill: (skill: { name: string; content: string; category?: string }, profile?: string) =>
    fetchJSON<SkillWriteResult>("/api/skills", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...skill, profile: profile || undefined }),
    }),
  updateSkillContent: (name: string, content: string, profile?: string) =>
    fetchJSON<SkillWriteResult>("/api/skills/content", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, content, profile: profile || undefined }),
    }),
  getToolsets: (profile?: string) =>
    fetchJSON<ToolsetInfo[]>(`/api/tools/toolsets${profileQuery(profile)}`),
  toggleToolset: (name: string, enabled: boolean, profile?: string) =>
    fetchJSON<{ ok: boolean; name: string; enabled: boolean }>(
      `/api/tools/toolsets/${encodeURIComponent(name)}`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled, profile: profile || undefined }),
      },
    ),
  getToolsetConfig: (name: string, profile?: string) =>
    fetchJSON<ToolsetConfig>(
      `/api/tools/toolsets/${encodeURIComponent(name)}/config${profileQuery(profile)}`,
    ),
  selectToolsetProvider: (name: string, provider: string, profile?: string) =>
    fetchJSON<{ ok: boolean; name: string; provider: string }>(
      `/api/tools/toolsets/${encodeURIComponent(name)}/provider`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider, profile: profile || undefined }),
      },
    ),
  saveToolsetEnv: (name: string, env: Record<string, string>, profile?: string) =>
    fetchJSON<ToolsetEnvResult>(
      `/api/tools/toolsets/${encodeURIComponent(name)}/env`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ env, profile: profile || undefined }),
      },
    ),
  runToolsetPostSetup: (name: string, key: string, profile?: string) =>
    fetchJSON<ActionResponse & { key: string }>(
      `/api/tools/toolsets/${encodeURIComponent(name)}/post-setup`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key, profile: profile || undefined }),
      },
    ),

  // Session search (FTS5)
  searchSessions: (q: string, profile = getManagementProfile()) =>
    fetchJSON<SessionSearchResponse>(
      appendProfileParam(`/api/sessions/search?q=${encodeURIComponent(q)}`, profile),
    ),

  // OAuth provider management
  getOAuthProviders: () =>
    fetchJSON<OAuthProvidersResponse>("/api/providers/oauth"),
  disconnectOAuthProvider: (providerId: string) =>
    fetchJSON<{ ok: boolean; provider: string }>(
      `/api/providers/oauth/${encodeURIComponent(providerId)}`,
      {
        method: "DELETE",
      },
    ),
  startOAuthLogin: (providerId: string) =>
    fetchJSON<OAuthStartResponse>(
      `/api/providers/oauth/${encodeURIComponent(providerId)}/start`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      },
    ),
  submitOAuthCode: (providerId: string, sessionId: string, code: string) =>
    fetchJSON<OAuthSubmitResponse>(
      `/api/providers/oauth/${encodeURIComponent(providerId)}/submit`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sessionId, code }),
      },
    ),
  pollOAuthSession: (providerId: string, sessionId: string) =>
    fetchJSON<OAuthPollResponse>(
      `/api/providers/oauth/${encodeURIComponent(providerId)}/poll/${encodeURIComponent(sessionId)}`,
    ),
  cancelOAuthSession: (sessionId: string) =>
    fetchJSON<{ ok: boolean }>(
      `/api/providers/oauth/sessions/${encodeURIComponent(sessionId)}`,
      {
        method: "DELETE",
      },
    ),

  // Messaging platforms (gateway channels)
  getMessagingPlatforms: () =>
    fetchJSON<MessagingPlatformsResponse>("/api/messaging/platforms"),
  updateMessagingPlatform: (id: string, body: MessagingPlatformUpdate) =>
    fetchJSON<{ ok: boolean; platform: string }>(
      `/api/messaging/platforms/${encodeURIComponent(id)}`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    ),
  testMessagingPlatform: (id: string) =>
    fetchJSON<MessagingPlatformTestResult>(
      `/api/messaging/platforms/${encodeURIComponent(id)}/test`,
      { method: "POST" },
    ),
  startTelegramOnboarding: (body: { bot_name?: string }) =>
    fetchJSON<TelegramOnboardingStartResponse>(
      "/api/messaging/telegram/onboarding/start",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    ),
  getTelegramOnboardingStatus: (pairingId: string) =>
    fetchJSON<TelegramOnboardingStatusResponse>(
      `/api/messaging/telegram/onboarding/${encodeURIComponent(pairingId)}`,
    ),
  applyTelegramOnboarding: (
    pairingId: string,
    body: { allowed_user_ids: string[]; profile?: string },
  ) =>
    fetchJSON<TelegramOnboardingApplyResponse>(
      `/api/messaging/telegram/onboarding/${encodeURIComponent(pairingId)}/apply`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    ),
  cancelTelegramOnboarding: (pairingId: string) =>
    fetchJSON<{ ok: boolean }>(
      `/api/messaging/telegram/onboarding/${encodeURIComponent(pairingId)}`,
      { method: "DELETE" },
    ),
  startWhatsAppOnboarding: (body: {
    mode?: "bot" | "self-chat";
    allowed_users?: string;
  }) =>
    fetchJSON<WhatsAppOnboardingStartResponse>(
      "/api/messaging/whatsapp/onboarding/start",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    ),
  getWhatsAppOnboardingStatus: (pairingId: string) =>
    fetchJSON<WhatsAppOnboardingStatusResponse>(
      `/api/messaging/whatsapp/onboarding/${encodeURIComponent(pairingId)}`,
    ),
  applyWhatsAppOnboarding: (
    pairingId: string,
    body: { mode?: "bot" | "self-chat"; allowed_users?: string; profile?: string },
  ) =>
    fetchJSON<WhatsAppOnboardingApplyResponse>(
      `/api/messaging/whatsapp/onboarding/${encodeURIComponent(pairingId)}/apply`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    ),
  cancelWhatsAppOnboarding: (pairingId: string) =>
    fetchJSON<{ ok: boolean }>(
      `/api/messaging/whatsapp/onboarding/${encodeURIComponent(pairingId)}`,
      { method: "DELETE" },
    ),

  // Gateway / update actions
  restartGateway: () =>
    fetchJSON<ActionResponse>("/api/gateway/restart", { method: "POST" }),
  updateHermes: () =>
    fetchJSON<ActionResponse>("/api/hermes/update", { method: "POST" }),
  checkHermesUpdate: (force = false) =>
    fetchJSON<UpdateCheckResponse>(
      `/api/hermes/update/check${force ? "?force=true" : ""}`,
    ),
  getActionStatus: (name: string, lines = 200) =>
    fetchJSON<ActionStatusResponse>(
      `/api/actions/${encodeURIComponent(name)}/status?lines=${lines}`,
    ),

  // Dashboard plugins
  getPlugins: () =>
    fetchJSON<PluginManifestResponse[]>("/api/dashboard/plugins"),
  rescanPlugins: () =>
    fetchJSON<{ ok: boolean; count: number }>("/api/dashboard/plugins/rescan"),

  getPluginsHub: () => fetchJSON<PluginsHubResponse>("/api/dashboard/plugins/hub"),

  installAgentPlugin: (body: AgentPluginInstallRequest) =>
    fetchJSON<AgentPluginInstallResponse>("/api/dashboard/agent-plugins/install", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...body }),
    }),

  enableAgentPlugin: (name: string) =>
    fetchJSON<{ ok: boolean; name: string; unchanged?: boolean }>(
      `/api/dashboard/agent-plugins/${pluginPath(name)}/enable`,
      { method: "POST" },
    ),

  disableAgentPlugin: (name: string) =>
    fetchJSON<{ ok: boolean; name: string; unchanged?: boolean }>(
      `/api/dashboard/agent-plugins/${pluginPath(name)}/disable`,
      { method: "POST" },
    ),

  updateAgentPlugin: (name: string) =>
    fetchJSON<AgentPluginUpdateResponse>(
      `/api/dashboard/agent-plugins/${pluginPath(name)}/update`,
      { method: "POST" },
    ),

  removeAgentPlugin: (name: string) =>
    fetchJSON<{ ok: boolean; name: string }>(
      `/api/dashboard/agent-plugins/${pluginPath(name)}`,
      { method: "DELETE" },
    ),

  savePluginProviders: (body: PluginProvidersPutRequest) =>
    fetchJSON<{ ok: boolean }>("/api/dashboard/plugin-providers", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),

  setPluginVisibility: (name: string, hidden: boolean) =>
    fetchJSON<{ ok: boolean; name: string; hidden: boolean }>(
      `/api/dashboard/plugins/${pluginPath(name)}/visibility`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ hidden }),
      },
    ),

  // Dashboard themes
  getThemes: () =>
    fetchJSON<DashboardThemesResponse>("/api/dashboard/themes"),
  setTheme: (name: string) =>
    fetchJSON<{ ok: boolean; theme: string }>("/api/dashboard/theme", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    }),
  getFontPref: () =>
    fetchJSON<DashboardFontResponse>("/api/dashboard/font"),
  setFontPref: (font: string) =>
    fetchJSON<{ ok: boolean; font: string }>("/api/dashboard/font", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ font }),
    }),

  // ── Admin: MCP servers ──────────────────────────────────────────────
  getMcpServers: () => fetchJSON<{ servers: McpServer[] }>("/api/mcp/servers"),
  addMcpServer: (body: McpServerCreate) =>
    fetchJSON<McpServer>("/api/mcp/servers", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  authMcpServer: (name: string) =>
    fetchJSON<McpTestResult>(
      `/api/mcp/servers/${encodeURIComponent(name)}/auth`,
      { method: "POST" },
    ),
  removeMcpServer: (name: string) =>
    fetchJSON<{ ok: boolean }>(`/api/mcp/servers/${encodeURIComponent(name)}`, {
      method: "DELETE",
    }),
  testMcpServer: (name: string) =>
    fetchJSON<McpTestResult>(
      `/api/mcp/servers/${encodeURIComponent(name)}/test`,
      { method: "POST" },
    ),
  setMcpServerEnabled: (name: string, enabled: boolean) =>
    fetchJSON<{ ok: boolean; name: string; enabled: boolean }>(
      `/api/mcp/servers/${encodeURIComponent(name)}/enabled`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled }),
      },
    ),
  getMcpCatalog: () =>
    fetchJSON<{ entries: McpCatalogEntry[]; diagnostics: McpCatalogDiagnostic[] }>(
      "/api/mcp/catalog",
    ),
  installMcpCatalogEntry: (
    name: string,
    env: Record<string, string> = {},
    enable = true,
  ) =>
    fetchJSON<{ ok: boolean; name: string; background: boolean; action?: string }>(
      "/api/mcp/catalog/install",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, env, enable }),
      },
    ),

  // ── Admin: Pairing ──────────────────────────────────────────────────
  getPairing: () => fetchJSON<PairingResponse>("/api/pairing"),
  approvePairing: (platform: string, code: string) =>
    fetchJSON<{ ok: boolean; user: PairingUser }>("/api/pairing/approve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ platform, code }),
    }),
  revokePairing: (platform: string, user_id: string) =>
    fetchJSON<{ ok: boolean }>("/api/pairing/revoke", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ platform, user_id }),
    }),
  clearPendingPairing: () =>
    fetchJSON<{ ok: boolean; cleared: number }>("/api/pairing/clear-pending", {
      method: "POST",
    }),

  // ── Admin: Webhooks ─────────────────────────────────────────────────
  getWebhooks: () => fetchJSON<WebhooksResponse>("/api/webhooks"),
  enableWebhooks: () =>
    fetchJSON<WebhookEnableResponse>("/api/webhooks/enable", { method: "POST" }),
  createWebhook: (body: WebhookCreate) =>
    fetchJSON<WebhookRoute & { secret: string }>("/api/webhooks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  deleteWebhook: (name: string) =>
    fetchJSON<{ ok: boolean }>(`/api/webhooks/${encodeURIComponent(name)}`, {
      method: "DELETE",
    }),
  setWebhookEnabled: (name: string, enabled: boolean) =>
    fetchJSON<{ ok: boolean; name: string; enabled: boolean }>(
      `/api/webhooks/${encodeURIComponent(name)}/enabled`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled }),
      },
    ),

  // ── Admin: Credential pool ──────────────────────────────────────────
  getCredentialPool: () =>
    fetchJSON<{ providers: CredentialPoolProvider[] }>("/api/credentials/pool"),
  addCredentialPoolEntry: (
    provider: string,
    api_key: string,
    label?: string,
  ) =>
    fetchJSON<{ ok: boolean; provider: string; count: number }>(
      "/api/credentials/pool",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider, api_key, label }),
      },
    ),
  removeCredentialPoolEntry: (provider: string, index: number) =>
    fetchJSON<{ ok: boolean; provider: string; count: number }>(
      `/api/credentials/pool/${encodeURIComponent(provider)}/${index}`,
      { method: "DELETE" },
    ),

  // ── Admin: Memory provider ──────────────────────────────────────────
  getMemory: () => fetchJSON<MemoryStatus>("/api/memory"),
  getMemoryProviderConfig: (provider: string) =>
    fetchJSON<MemoryProviderConfig>(
      `/api/memory/providers/${encodeURIComponent(provider)}/config`,
    ),
  updateMemoryProviderConfig: (provider: string, values: Record<string, unknown>) =>
    fetchJSON<{ ok: boolean; active: string }>(
      `/api/memory/providers/${encodeURIComponent(provider)}/config`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ values }),
      },
    ),
  setupMemoryProvider: (provider: string, values: Record<string, unknown> = {}) =>
    fetchJSON<MemoryProviderSetupResponse>(
      `/api/memory/providers/${encodeURIComponent(provider)}/setup`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ values }),
      },
    ),
  setMemoryProvider: (provider: string) =>
    fetchJSON<{ ok: boolean; active: string }>("/api/memory/provider", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider }),
    }),
  resetMemory: (target: "all" | "memory" | "user") =>
    fetchJSON<{ ok: boolean; deleted: string[] }>("/api/memory/reset", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target }),
    }),

  // ── Admin: Gateway lifecycle ────────────────────────────────────────
  startGateway: () =>
    fetchJSON<ActionResponse>("/api/gateway/start", { method: "POST" }),
  stopGateway: () =>
    fetchJSON<ActionResponse>("/api/gateway/stop", { method: "POST" }),

  // ── Admin: Operations ───────────────────────────────────────────────
  runDoctor: () =>
    fetchJSON<ActionResponse>("/api/ops/doctor", { method: "POST" }),
  runSecurityAudit: () =>
    fetchJSON<ActionResponse>("/api/ops/security-audit", { method: "POST" }),
  runBackup: (output?: string) =>
    fetchJSON<ActionResponse>("/api/ops/backup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ output }),
    }),
  downloadBackup: (archive: string) =>
    authedFetch(
      `/api/ops/backup/download?archive=${encodeURIComponent(archive)}`,
    ),
  runImport: (archive: string, force = false) =>
    fetchJSON<ActionResponse>("/api/ops/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ archive, force }),
    }),
  runImportUpload: (file: File, force = false) => {
    const form = new FormData();
    form.append("force", String(force));
    form.append("file", file, file.name);
    return fetchJSON<ActionResponse>("/api/ops/import-upload", {
      method: "POST",
      body: form,
    });
  },
  getHooks: () => fetchJSON<HooksResponse>("/api/ops/hooks"),
  createHook: (body: HookCreate) =>
    fetchJSON<{ ok: boolean; event: string; command: string; approved: boolean }>(
      "/api/ops/hooks",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    ),
  deleteHook: (event: string, command: string) =>
    fetchJSON<{ ok: boolean }>("/api/ops/hooks", {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ event, command }),
    }),
  getSystemStats: () => fetchJSON<SystemStats>("/api/system/stats"),

  // ── Admin: Curator ──────────────────────────────────────────────────
  getCurator: () => fetchJSON<CuratorStatus>("/api/curator"),
  setCuratorPaused: (paused: boolean) =>
    fetchJSON<{ ok: boolean; paused: boolean }>("/api/curator/paused", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paused }),
    }),
  runCurator: () =>
    fetchJSON<ActionResponse>("/api/curator/run", { method: "POST" }),

  // ── Admin: Portal ───────────────────────────────────────────────────
  getPortal: () => fetchJSON<PortalStatus>("/api/portal"),

  // ── Admin: Diagnostics (backgrounded) ───────────────────────────────
  runPromptSize: () =>
    fetchJSON<ActionResponse>("/api/ops/prompt-size", { method: "POST" }),
  runDump: () => fetchJSON<ActionResponse>("/api/ops/dump", { method: "POST" }),
  runConfigMigrate: () =>
    fetchJSON<ActionResponse>("/api/ops/config-migrate", { method: "POST" }),
  runDebugShare: (opts?: { redact?: boolean; lines?: number }) =>
    fetchJSON<DebugShareResponse>("/api/ops/debug-share", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        redact: opts?.redact ?? true,
        lines: opts?.lines ?? 200,
      }),
    }),


  getCheckpoints: () => fetchJSON<CheckpointsResponse>("/api/ops/checkpoints"),
  pruneCheckpoints: () =>
    fetchJSON<ActionResponse>("/api/ops/checkpoints/prune", { method: "POST" }),

  // ── Admin: Skills hub ───────────────────────────────────────────────
  // ``profile`` scopes install/uninstall/update and the installed-state
  // annotations to that profile (omitted = the dashboard's own profile).
  installSkillFromHub: (identifier: string, profile?: string) =>
    fetchJSON<ActionResponse>("/api/skills/hub/install", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ identifier, profile: profile || undefined }),
    }),
  uninstallSkillFromHub: (name: string, profile?: string) =>
    fetchJSON<ActionResponse>("/api/skills/hub/uninstall", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, profile: profile || undefined }),
    }),
  updateSkillsFromHub: (profile?: string) =>
    fetchJSON<ActionResponse>("/api/skills/hub/update", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ profile: profile || undefined }),
    }),
  searchSkillsHub: (q: string, source = "all", limit = 20, profile?: string) =>
    fetchJSON<SkillHubSearchResponse>(
      `/api/skills/hub/search?q=${encodeURIComponent(q)}&source=${encodeURIComponent(source)}&limit=${limit}${profile ? `&profile=${encodeURIComponent(profile)}` : ""}`,
    ),
  getSkillHubSources: (profile?: string) =>
    fetchJSON<SkillHubSourcesResponse>(
      `/api/skills/hub/sources${profileQuery(profile)}`,
    ),
  previewSkillFromHub: (identifier: string) =>
    fetchJSON<SkillHubPreview>(
      `/api/skills/hub/preview?identifier=${encodeURIComponent(identifier)}`,
    ),
  scanSkillFromHub: (identifier: string) =>
    fetchJSON<SkillHubScan>(
      `/api/skills/hub/scan?identifier=${encodeURIComponent(identifier)}`,
    ),
};

/** Identity payload returned by ``GET /api/auth/me`` (Phase 7).
 *
 * Returned by the dashboard's gated middleware when a valid session cookie
 * is attached. ``email`` and ``display_name`` are empty strings under the
 * Nous Portal contract V1 (the access token has no email/name claims —
 * see Contract Anchor C4 in the plan). The AuthWidget surfaces a
 * truncated ``user_id`` instead.
 */
export interface AuthMeResponse {
  user_id: string;
  email: string;
  display_name: string;
  org_id: string;
  provider: string;
  expires_at: number;
}

export interface ActionResponse {
  archive?: string;
  name: string;
  ok: boolean;
  pid: number | null;
  error?: string;
  message?: string;
  uploaded_bytes?: number;
  update_command?: string;
}

export interface DebugShareResponse {
  ok: boolean;
  // label -> paste URL, e.g. { Report: "https://paste.rs/abc", "agent.log": "..." }
  urls: Record<string, string>;
  // "label: error" strings for optional full-log uploads that failed.
  failures: string[];
  redacted: boolean;
  auto_delete_seconds: number;
}

export interface SessionStoreStats {
  total: number;
  active_store: number;
  archived: number;
  messages: number;
  by_source: Record<string, number>;
}

export interface SessionImportResponse {
  ok: boolean;
  imported: number;
  skipped: number;
  detached: number;
  imported_ids: string[];
  skipped_ids: string[];
  errors: Array<Record<string, unknown>>;
}

export interface SkillHubResult {
  name: string;
  description: string;
  source: string;
  identifier: string;
  trust_level: string;
  repo: string | null;
  tags: string[];
}

/** Lock-entry summary for an already-installed hub skill (keyed by identifier). */
export interface SkillHubInstalledEntry {
  name: string | null;
  trust_level: string | null;
  scan_verdict: string | null;
}

export interface SkillHubSearchResponse {
  results: SkillHubResult[];
  /** source_id -> number of results returned by that source. */
  source_counts: Record<string, number>;
  /** source ids that didn't return within the parallel-search timeout. */
  timed_out: string[];
  /** identifier -> installed lock entry (for "already installed" badges). */
  installed: Record<string, SkillHubInstalledEntry>;
}

export interface SkillHubSource {
  id: string;
  label: string;
  /** GitHub only: whether the API is currently rate-limited. */
  rate_limited?: boolean;
  /** hermes-index only: whether the centralized index loaded. */
  available?: boolean;
}

export interface SkillHubSourcesResponse {
  sources: SkillHubSource[];
  index_available: boolean;
  /** Featured/popular skills from the centralized index (zero extra API calls). */
  featured: SkillHubResult[];
  installed: Record<string, SkillHubInstalledEntry>;
}

export interface SkillHubPreview {
  name: string;
  description: string;
  source: string;
  identifier: string;
  trust_level: string;
  repo: string | null;
  tags: string[];
  /** Rendered SKILL.md content (the actual skill text). */
  skill_md: string;
  /** Relative paths of every file in the bundle. */
  files: string[];
}

export interface SkillHubScanFinding {
  severity: string;
  category: string;
  file: string;
  line: number;
  description: string;
}

export interface SkillHubScan {
  name: string;
  identifier: string;
  source: string;
  trust_level: string;
  /** "safe" | "caution" | "dangerous". */
  verdict: string;
  summary: string;
  /** Install-policy decision for this trust+verdict combo. */
  policy: "allow" | "ask" | "block";
  policy_reason: string;
  findings: SkillHubScanFinding[];
  severity_counts: Record<string, number>;
}

// ── Admin types ───────────────────────────────────────────────────────

export interface McpServer {
  name: string;
  transport: "http" | "stdio" | "unknown";
  url: string | null;
  command: string | null;
  args: string[];
  env: Record<string, string>;
  auth: "header" | "oauth" | null;
  enabled: boolean;
  tools: string[] | null;
}

export interface McpCatalogEntry {
  name: string;
  description: string;
  source: string;
  transport: "http" | "stdio";
  auth_type: "api_key" | "oauth" | "none";
  required_env: Array<{ name: string; prompt: string; required: boolean }>;
  // Transport details — what actually connects (http) or runs (stdio).
  command: string | null;
  args: string[];
  url: string | null;
  // Git bootstrap (only set for entries that clone + build locally).
  install_url: string | null;
  install_ref: string | null;
  bootstrap: string[];
  // Default tool pre-selection (null = all tools pre-checked) + guidance text.
  default_enabled: string[] | null;
  post_install: string;
  needs_install: boolean;
  installed: boolean;
  enabled: boolean;
}

export interface McpCatalogDiagnostic {
  name: string;
  kind: string;
  message: string;
}


export type McpHttpAuth = "none" | "header" | "oauth";

export interface McpServerCreate {
  name: string;
  url?: string;
  command?: string;
  args?: string[];
  env?: Record<string, string>;
  auth?: McpHttpAuth;
  bearer_token?: string;
}

export interface McpTestResult {
  ok: boolean;
  error?: string;
  tools: Array<{ name: string; description: string }>;
}

export interface MessagingPlatformEnvVar {
  key: string;
  required: boolean;
  is_set: boolean;
  redacted_value: string | null;
  description: string;
  prompt: string;
  help: string;
  url: string | null;
  is_password: boolean;
  advanced: boolean;
}

export interface MessagingPlatform {
  id: string;
  name: string;
  description: string;
  docs_url: string;
  enabled: boolean;
  configured: boolean;
  gateway_running: boolean;
  /**
   * "connected" | "disabled" | "not_configured" | "pending_restart" |
   * "gateway_stopped" | "startup_failed" | "disconnected" | "fatal" | string
   */
  state: string;
  error_code: string | null;
  error_message: string | null;
  updated_at: string | null;
  home_channel: { platform: string; chat_id: string; name: string; thread_id?: string } | null;
  whatsapp_setup?: {
    mode?: string;
    allowed_users_set?: boolean;
    home_channel_set?: boolean;
  } | null;
  env_vars: MessagingPlatformEnvVar[];
}

export interface MessagingPlatformsResponse {
  env_path: string;
  gateway_start_command: string;
  platforms: MessagingPlatform[];
}

export interface MessagingPlatformUpdate {
  enabled?: boolean;
  env?: Record<string, string>;
  clear_env?: string[];
}

export interface MessagingPlatformTestResult {
  ok: boolean;
  state: string;
  message: string;
}

export interface PairingUser {
  platform: string;
  user_id: string;
  user_name?: string;
  code?: string;
  age_minutes?: number;
}

export interface PairingResponse {
  pending: PairingUser[];
  approved: PairingUser[];
}

export interface WebhookRoute {
  name: string;
  description: string;
  events: string[];
  deliver: string;
  deliver_only: boolean;
  prompt: string;
  skills: string[];
  created_at: string | null;
  url: string;
  secret_set: boolean;
  enabled: boolean;
}

export interface WebhooksResponse {
  enabled: boolean;
  base_url: string;
  subscriptions: WebhookRoute[];
}

export interface WebhookEnableResponse {
  ok: boolean;
  platform: "webhook";
  enabled: true;
  needs_restart: boolean;
  restart_started?: boolean;
  restart_action?: string;
  restart_pid?: number | null;
  restart_error?: string;
}

export interface WebhookCreate {
  name: string;
  description?: string;
  events?: string[];
  prompt?: string;
  skills?: string[];
  deliver?: string;
  deliver_only?: boolean;
  deliver_chat_id?: string;
}

export interface CredentialPoolEntry {
  index: number;
  id: string | null;
  label: string | null;
  auth_type: string | null;
  source: string | null;
  priority: number;
  last_status: string | null;
  request_count: number;
  token_preview: string;
  has_refresh: boolean;
}

export interface CredentialPoolProvider {
  provider: string;
  entries: CredentialPoolEntry[];
}

export interface MemoryProviderInfo {
  name: string;
  description: string;
  available: boolean;
  configured: boolean;
  status: "ready" | "needs_config" | "unavailable" | "missing";
  setup?: MemoryProviderSetupInfo;
}

export interface MemoryStatus {
  active: string;
  providers: MemoryProviderInfo[];
  builtin_files: { memory: number; user: number };
}

export interface MemoryProviderExternalDependency {
  name: string;
  install: string;
  check: string;
}

export interface MemoryProviderSetupInfo {
  pip_dependencies: string[];
  external_dependencies: MemoryProviderExternalDependency[];
  required_env: string[];
  dependencies_installed: boolean;
}

export interface MemoryProviderSetupResult {
  kind: string;
  name: string;
  status: string;
  command: string;
  returncode: number | null;
  stdout: string;
  stderr: string;
}

export interface MemoryProviderSetupResponse {
  ok: boolean;
  provider: string;
  results: MemoryProviderSetupResult[];
  status?: MemoryProviderInfo | null;
}

export interface MemoryProviderFieldOption {
  value: string;
  label: string;
  description?: string;
}

export interface MemoryProviderField {
  key: string;
  label: string;
  kind: "text" | "secret" | "select" | "boolean";
  description: string;
  placeholder: string;
  required: boolean;
  value: string | boolean;
  is_set: boolean;
  options: MemoryProviderFieldOption[];
  url: string;
  when?: Record<string, string | boolean | number> | null;
}

export interface MemoryProviderConfig {
  name: string;
  label: string;
  fields: MemoryProviderField[];
  setup?: MemoryProviderSetupInfo;
}

export interface HookEntry {
  event: string;
  matcher: string | null;
  command: string | null;
  timeout: number | null;
  allowed: boolean;
  approved_at?: string | null;
  executable?: boolean;
}

export interface HooksResponse {
  hooks: HookEntry[];
  valid_events: string[];
}

export interface HookCreate {
  event: string;
  command: string;
  matcher?: string;
  timeout?: number;
  approve?: boolean;
}

export interface UpdateCheckResponse {
  install_method: string;
  current_version: string;
  // commits behind: >=1 known count, 0 up to date, -1 behind by unknown
  // count (nix/pypi), or null when the check could not run.
  behind: number | null;
  update_available: boolean;
  can_apply: boolean;
  update_command: string;
  message: string | null;
}

export interface SystemStats {
  os: string;
  os_release: string;
  os_version: string;
  platform: string;
  arch: string;
  hostname: string;
  python_version: string;
  python_impl: string;
  hermes_version: string;
  cpu_count: number | null;
  psutil: boolean;
  cpu_percent?: number;
  load_avg?: number[];
  uptime_seconds?: number;
  memory?: { total: number; available: number; used: number; percent: number };
  disk?: { total: number; used: number; free: number; percent: number };
  process?: { pid: number; rss: number; create_time: number; num_threads: number };
}

export interface CuratorStatus {
  enabled: boolean;
  paused: boolean;
  interval_hours: number | null;
  last_run_at: string | null;
  min_idle_hours: number | null;
  stale_after_days: number | null;
  archive_after_days: number | null;
}

export interface PortalFeature {
  label: string;
  state: string;
}

export interface PortalStatus {
  logged_in: boolean;
  portal_url: string | null;
  inference_url: string | null;
  provider: string;
  subscription_url: string;
  features: PortalFeature[];
}

export interface CheckpointSession {
  session: string;
  files: number;
  bytes: number;
}

export interface CheckpointsResponse {
  sessions: CheckpointSession[];
  total_bytes: number;
}

export interface ActionStatusResponse {
  exit_code: number | null;
  lines: string[];
  name: string;
  pid: number | null;
  running: boolean;
}

export interface PlatformStatus {
  error_code?: string;
  error_message?: string;
  state: string;
  updated_at: string;
}

export interface StatusResponse {
  active_sessions: number;
  active_sessions_label?: string;
  active_sessions_source?: string;
  active_sessions_updated_at?: number;
  /** Phase 7: ``true`` when the dashboard's OAuth gate is engaged
   * (public bind, no ``--insecure``). Read alongside ``auth_providers``
   * to render a "gated / loopback" badge. */
  auth_required?: boolean;
  /** Phase 7: registered ``DashboardAuthProvider`` names (e.g. ``["nous"]``).
   * Empty in loopback mode; empty + ``auth_required=true`` is a
   * fail-closed state (the dashboard will refuse to bind). */
  auth_providers?: string[];
  /** False when the dashboard is running in a hosted/managed layout where
   * updates are handled by the outer launcher instead of ``hermes update``. */
  can_update_hermes?: boolean;
  config_path: string;
  config_version: number;
  env_path: string;
  gateway_exit_reason: string | null;
  gateway_health_url: string | null;
  gateway_pid: number | null;
  gateway_platforms: Record<string, PlatformStatus>;
  gateway_running: boolean;
  gateway_state: string | null;
  gateway_updated_at: string | null;
  hermes_home: string;
  latest_config_version: number;
  release_date: string;
  version: string;
}

export interface SessionInfo {
  id: string;
  source: string | null;
  model: string | null;
  title: string | null;
  started_at: number;
  ended_at: number | null;
  last_active: number;
  is_active: boolean;
  message_count: number;
  tool_call_count: number;
  input_tokens: number;
  output_tokens: number;
  preview: string | null;
  parent_session_id?: string | null;
}

export interface SessionLatestDescendantResponse {
  requested_session_id: string;
  session_id: string;
  path: string[];
  changed: boolean;
}

export interface PaginatedSessions {
  sessions: SessionInfo[];
  total: number;
  limit: number;
  offset: number;
}

export interface EnvVarInfo {
  is_set: boolean;
  redacted_value: string | null;
  description: string;
  url: string | null;
  category: string;
  is_password: boolean;
  tools: string[];
  advanced: boolean;
  /** True when this var is a messaging-platform credential owned by the Channels page. */
  channel_managed?: boolean;
  /** True when this key is set in .env but not in any catalog (user-added custom key). */
  custom?: boolean;
}

export interface TelegramOnboardingStartResponse {
  pairing_id: string;
  suggested_username: string;
  deep_link: string;
  qr_payload: string;
  expires_at: string;
}

export type TelegramOnboardingStatusResponse =
  | { status: "waiting"; expires_at: string }
  | {
      status: "ready";
      bot_username: string;
      owner_user_id?: string;
      expires_at: string;
    };

export interface TelegramOnboardingApplyResponse {
  ok: boolean;
  platform: "telegram";
  bot_username?: string;
  needs_restart: boolean;
  restart_started?: boolean;
  restart_action?: string;
  restart_pid?: number | null;
  restart_error?: string;
}

export interface WhatsAppOnboardingStartResponse {
  pairing_id: string;
  status:
    | "starting"
    | "installing"
    | "waiting"
    | "connected"
    | "error"
    | "expired"
    | "cancelled";
  qr_payload?: string | null;
  expires_at: string;
  mode: "bot" | "self-chat";
  allowed_users: string;
  account_id?: string | null;
  account_name?: string | null;
  account_phone?: string | null;
  error?: string | null;
}

export type WhatsAppOnboardingStatusResponse = WhatsAppOnboardingStartResponse;

export interface WhatsAppOnboardingApplyResponse {
  ok: boolean;
  platform: "whatsapp";
  needs_restart: boolean;
  restart_started?: boolean;
  restart_action?: string;
  restart_pid?: number | null;
  restart_error?: string;
}

export interface SessionMessage {
  role: "user" | "assistant" | "system" | "tool";
  content: string | null;
  tool_calls?: Array<{
    id: string;
    function: { name: string; arguments: string };
  }>;
  tool_name?: string;
  tool_call_id?: string;
  timestamp?: number;
}

export interface SessionMessagesResponse {
  session_id: string;
  messages: SessionMessage[];
}

export interface LogsResponse {
  file: string;
  lines: string[];
}

export interface ManagedFileEntry {
  name: string;
  path: string;
  is_directory: boolean;
  size: number | null;
  mtime: number;
  mime_type: string | null;
}

export interface ManagedFilesResponse {
  root: string | null;
  path: string;
  parent: string | null;
  locked_root: string | null;
  can_change_path: boolean;
  entries: ManagedFileEntry[];
}

export interface ManagedFileReadResponse {
  name: string;
  path: string;
  size: number;
  mime_type: string;
  data_url: string;
  root: string | null;
  locked_root: string | null;
  can_change_path: boolean;
}

export interface ManagedFileWriteResponse {
  ok: boolean;
  path: string;
  entry: ManagedFileEntry;
  root: string | null;
  locked_root: string | null;
  can_change_path: boolean;
}

export interface AnalyticsDailyEntry {
  day: string;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  reasoning_tokens: number;
  estimated_cost: number;
  actual_cost: number;
  sessions: number;
  api_calls: number;
}

export interface AnalyticsModelEntry {
  model: string;
  input_tokens: number;
  output_tokens: number;
  estimated_cost: number;
  sessions: number;
  api_calls: number;
}

export interface AnalyticsSkillEntry {
  skill: string;
  view_count: number;
  manage_count: number;
  total_count: number;
  percentage: number;
  last_used_at: number | null;
}

export interface AnalyticsSkillsSummary {
  total_skill_loads: number;
  total_skill_edits: number;
  total_skill_actions: number;
  distinct_skills_used: number;
}

export interface AnalyticsResponse {
  daily: AnalyticsDailyEntry[];
  by_model: AnalyticsModelEntry[];
  totals: {
    total_input: number;
    total_output: number;
    total_cache_read: number;
    total_reasoning: number;
    total_estimated_cost: number;
    total_actual_cost: number;
    total_sessions: number;
    total_api_calls: number;
  };
  skills: {
    summary: AnalyticsSkillsSummary;
    top_skills: AnalyticsSkillEntry[];
  };
}

export interface ActiveProfileInfo {
  active: string;
  current: string;
}

export interface ProfileDescribeAutoResult {
  ok: boolean;
  reason: string;
  description: string | null;
  description_auto: boolean;
}

export interface ProfileInfo {
  name: string;
  path: string;
  is_default: boolean;
  model: string | null;
  provider: string | null;
  has_env: boolean;
  skill_count: number;
  gateway_running: boolean;
  description: string;
  description_auto: boolean;
  distribution_name: string | null;
  distribution_version: string | null;
  distribution_source: string | null;
  has_alias: boolean;
}

export interface ModelsAnalyticsModelEntry {
  model: string;
  provider: string;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  reasoning_tokens: number;
  estimated_cost: number;
  actual_cost: number;
  sessions: number;
  api_calls: number;
  tool_calls: number;
  last_used_at: number;
  avg_tokens_per_session: number;
  capabilities: {
    supports_tools?: boolean;
    supports_vision?: boolean;
    supports_reasoning?: boolean;
    context_window?: number;
    max_output_tokens?: number;
    model_family?: string;
  };
}

export interface ModelsAnalyticsResponse {
  models: ModelsAnalyticsModelEntry[];
  totals: {
    distinct_models: number;
    total_input: number;
    total_output: number;
    total_cache_read: number;
    total_reasoning: number;
    total_estimated_cost: number;
    total_actual_cost: number;
    total_sessions: number;
    total_api_calls: number;
  };
  period_days: number;
}

export interface CronJobRepeat {
  times: number | null;
  completed?: number;
}

export interface CronJobMutation {
  name?: string;
  prompt?: string;
  schedule?: string;
  deliver?: string;
  skills?: string[];
  provider?: string | null;
  model?: string | null;
  base_url?: string | null;
  script?: string | null;
  no_agent?: boolean;
  context_from?: string[] | null;
  enabled_toolsets?: string[] | null;
  workdir?: string | null;
}

export interface CronJob {
  id: string;
  profile?: string | null;
  profile_name?: string | null;
  hermes_home?: string | null;
  is_default_profile?: boolean;
  name?: string | null;
  prompt?: string | null;
  script?: string | null;
  skills?: string[] | null;
  schedule?: { kind?: string; expr?: string; run_at?: string; display?: string };
  schedule_display?: string | null;
  repeat?: CronJobRepeat | null;
  enabled: boolean;
  state?: string | null;
  deliver?: string | null;
  model?: string | null;
  provider?: string | null;
  base_url?: string | null;
  no_agent?: boolean | null;
  context_from?: string[] | string | null;
  enabled_toolsets?: string[] | null;
  workdir?: string | null;
  last_run_at?: string | null;
  next_run_at?: string | null;
  last_status?: string | null;
  last_error?: string | null;
  last_delivery_error?: string | null;
}

export interface CronDeliveryTarget {
  id: string;
  name: string;
  home_target_set: boolean;
  home_env_var: string | null;
}

export interface AutomationBlueprintField {
  name: string;
  type: "time" | "enum" | "text" | "weekdays";
  label: string;
  default: string | null;
  options: string[];
  optional: boolean;
  /** When false, options are suggestions — any value is accepted. */
  strict?: boolean;
  help: string;
}

export interface AutomationBlueprint {
  key: string;
  title: string;
  description: string;
  category: string;
  tags: string[];
  fields: AutomationBlueprintField[];
  command: string;
  appUrl: string;
}

export interface SkillInfo {
  name: string;
  description: string;
  category: string;
  enabled: boolean;
}

export interface SkillContent {
  name: string;
  content: string;
  path: string;
}

export interface SkillWriteResult {
  success: boolean;
  message?: string;
  path?: string;
  error?: string;
}

export interface ToolsetInfo {
  name: string;
  label: string;
  description: string;
  enabled: boolean;
  configured: boolean;
  tools: string[];
}

export interface ToolsetProviderEnvVar {
  key: string;
  prompt: string;
  url: string | null;
  default: string | null;
  is_set: boolean;
}

export interface ToolsetProvider {
  name: string;
  badge: string;
  tag: string;
  env_vars: ToolsetProviderEnvVar[];
  post_setup: string | null;
  requires_nous_auth: boolean;
  is_active: boolean;
}

export interface ToolsetConfig {
  name: string;
  has_category: boolean;
  providers: ToolsetProvider[];
  active_provider: string | null;
}

export interface ToolsetEnvResult {
  ok: boolean;
  name: string;
  saved: string[];
  skipped: string[];
  is_set: Record<string, boolean>;
}

export interface SessionSearchResult {
  session_id: string;
  snippet: string;
  role: string | null;
  source: string | null;
  model: string | null;
  session_started: number | null;
}

export interface SessionSearchResponse {
  results: SessionSearchResult[];
}

// ── Model info types ──────────────────────────────────────────────────

export interface ModelInfoResponse {
  model: string;
  provider: string;
  auto_context_length: number;
  config_context_length: number;
  effective_context_length: number;
  capabilities: {
    supports_tools?: boolean;
    supports_vision?: boolean;
    supports_reasoning?: boolean;
    context_window?: number;
    max_output_tokens?: number;
    model_family?: string;
  };
}

// ── Model options / assignment types ──────────────────────────────────

export interface ModelOptionProvider {
  name: string;
  slug: string;
  models?: string[];
  total_models?: number;
  is_current?: boolean;
  is_user_defined?: boolean;
  source?: string;
  warning?: string;
  authenticated?: boolean;
}

export interface ModelOptionsResponse {
  model?: string;
  provider?: string;
  providers?: ModelOptionProvider[];
}

export interface AuxiliaryTaskAssignment {
  task: string;
  provider: string;
  model: string;
  base_url: string;
}

export interface AuxiliaryModelsResponse {
  tasks: AuxiliaryTaskAssignment[];
  main: { provider: string; model: string };
}

export interface MoaModelSlot {
  provider: string;
  model: string;
  /** Optional per-slot reasoning effort — round-tripped, not edited here. */
  reasoning_effort?: string;
}

export interface MoaConfigResponse {
  default_preset: string;
  active_preset: string;
  presets: Record<string, {
    reference_models: MoaModelSlot[];
    aggregator: MoaModelSlot;
    reference_temperature: number;
    aggregator_temperature: number;
    max_tokens: number;
    /** Optional advisor output cap — round-tripped, not edited here. */
    reference_max_tokens?: number | null;
    /** Fan-out cadence (per_iteration | user_turn) — round-tripped. */
    fanout?: string;
    enabled: boolean;
  }>;
  reference_models: MoaModelSlot[];
  aggregator: MoaModelSlot;
  reference_temperature: number;
  aggregator_temperature: number;
  max_tokens: number;
  enabled: boolean;
}

export interface ModelAssignmentRequest {
  confirm_expensive_model?: boolean;
  scope: "main" | "auxiliary";
  provider: string;
  model: string;
  /** Optional OpenAI-compatible endpoint URL for custom/local main providers. */
  base_url?: string;
  /** For auxiliary: task slot name, "" for all, "__reset__" to reset all. */
  task?: string;
}

/** An auxiliary task still pinned to a provider that differs from the
 *  newly-selected main provider after a main-model switch. */
export interface StaleAuxAssignment {
  task: string;
  provider: string;
  model: string;
}

export interface ModelAssignmentResponse {
  confirm_message?: string;
  confirm_required?: boolean;
  ok: boolean;
  scope?: string;
  provider?: string;
  model?: string;
  tasks?: string[];
  reset?: boolean;
  /** Auxiliary slots still pinned to a different provider than the new main.
   *  Switching main never clears aux pins; this lets the UI warn the user
   *  their helper tasks aren't following the switch. Only set on scope:'main'. */
  stale_aux?: StaleAuxAssignment[];
}

// ── OAuth provider types ────────────────────────────────────────────────

export interface OAuthProviderStatus {
  logged_in: boolean;
  source?: string | null;
  source_label?: string | null;
  token_preview?: string | null;
  expires_at?: string | null;
  has_refresh_token?: boolean;
  last_refresh?: string | null;
  error?: string;
}

export interface OAuthProvider {
  id: string;
  name: string;
  /** "pkce" (browser redirect + paste code), "device_code" (show code + URL),
   *  or "external" (delegated to a separate CLI like Claude Code or Qwen). */
  flow: "pkce" | "device_code" | "external";
  cli_command: string;
  docs_url: string;
  status: OAuthProviderStatus;
}

export interface OAuthProvidersResponse {
  providers: OAuthProvider[];
}

/** Discriminated union — the shape of /start depends on the flow. */
export type OAuthStartResponse =
  | {
      session_id: string;
      flow: "pkce";
      auth_url: string;
      expires_in: number;
    }
  | {
      session_id: string;
      flow: "device_code";
      user_code: string;
      verification_url: string;
      expires_in: number;
      poll_interval: number;
    };

export interface OAuthSubmitResponse {
  ok: boolean;
  status: "approved" | "error";
  message?: string;
}

export interface OAuthPollResponse {
  session_id: string;
  status: "pending" | "approved" | "denied" | "expired" | "error";
  error_message?: string | null;
  expires_at?: number | null;
}

// ── Dashboard theme types ──────────────────────────────────────────────

export interface DashboardThemeSummary {
  description: string;
  label: string;
  name: string;
  /** Full theme definition for user themes; undefined for built-ins
   *  (which the frontend already has locally). */
  definition?: DashboardTheme;
}

export interface DashboardThemesResponse {
  active: string;
  themes: DashboardThemeSummary[];
}

export interface DashboardFontResponse {
  /** Active font-override id, or "theme" when no override is set. */
  font: string;
}

// ── Dashboard plugin types ─────────────────────────────────────────────

export interface PluginManifestResponse {
  name: string;
  label: string;
  description: string;
  icon: string;
  version: string;
  tab: {
    path: string;
    position?: string;
    override?: string;
    hidden?: boolean;
  };
  slots?: string[];
  entry: string;
  css?: string | null;
  has_api: boolean;
  loadable?: boolean;
  reason?: string | null;
  source: string;
}

export interface HubAgentPluginRow {
  name: string;
  version: string;
  description: string;
  source: string;
  runtime_status: "disabled" | "enabled" | "inactive";
  has_dashboard_manifest: boolean;
  dashboard_manifest: PluginManifestResponse | null;
  path: string;
  can_remove: boolean;
  can_update_git: boolean;
  auth_required: boolean;
  auth_command: string;
  user_hidden: boolean;
}

export interface PluginsHubProviders {
  memory_provider: string;
  memory_options: MemoryProviderInfo[];
  context_engine: string;
  context_options: Array<{ name: string; description: string }>;
}

export interface PluginsHubResponse {
  plugins: HubAgentPluginRow[];
  orphan_dashboard_plugins: PluginManifestResponse[];
  providers: PluginsHubProviders;
}

export interface AgentPluginInstallRequest {
  identifier: string;
  force?: boolean;
  enable?: boolean;
}

export interface AgentPluginInstallResponse {
  ok: boolean;
  plugin_name?: string;
  warnings?: string[];
  missing_env?: string[];
  after_install_path?: string | null;
  enabled?: boolean;
  error?: string;
}

export interface AgentPluginUpdateResponse {
  ok: boolean;
  name?: string;
  output?: string;
  unchanged?: boolean;
  error?: string;
}

export interface PluginProvidersPutRequest {
  memory_provider?: string;
  context_engine?: string;
}
