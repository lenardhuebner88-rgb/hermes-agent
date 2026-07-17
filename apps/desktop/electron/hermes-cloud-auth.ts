import { app, BrowserWindow, net as electronNet, session } from 'electron'

import { cookiesHaveLiveSession, cookiesHavePrivySession, cookiesHaveSession, normalizeRemoteBaseUrl } from './connection-config'
import { DEFAULT_FETCH_TIMEOUT_MS, resolveTimeoutMs } from './hardening'
import { serializeJsonBody, setJsonRequestHeaders } from './oauth-net-request'

let oauthSession = null

// ---------------------------------------------------------------------------
// OAuth remote-gateway auth.
//
// Hosted Hermes gateways gate the dashboard behind an OAuth provider (e.g.
// Nous Research) instead of a static session token. The auth model is
// fundamentally different from the token path:
//
//   * REST is authed by HttpOnly session cookies (``hermes_session_at``),
//     established by a browser redirect round-trip (/login → IDP →
//     /auth/callback sets cookies). We cannot read the HttpOnly cookie value
//     in JS — instead we let an Electron BrowserWindow complete the round
//     trip into a PERSISTENT session partition, and thereafter route our REST
//     through Electron's ``net`` bound to that same partition so the cookie
//     jar attaches the cookie automatically.
//   * WebSocket upgrades require a single-use ``?ticket=`` minted at
//     ``POST /api/auth/ws-ticket`` (cookie-authed). The legacy ``?token=``
//     path is unconditionally rejected by gated gateways.
//   * Nous Portal now issues a 24h ROTATING, reuse-detected refresh token
//     alongside the ~15-min access token (Portal NAS #293 / hermes #37247).
//     Both are set as HttpOnly cookies (``hermes_session_at`` ~15 min,
//     ``hermes_session_rt`` 24h). When the AT cookie lapses but the RT cookie
//     is still alive, the gateway middleware transparently rotates a fresh AT
//     on the next authenticated request — so connectivity must NOT be gated on
//     the AT cookie alone. We probe liveness by actually minting a ws-ticket
//     (which triggers that server-side refresh) and treat a real 401 as
//     "needs re-login"; the AT-or-RT cookie presence check is only a cheap
//     "is the user signed in at all?" gate / display signal.
// ---------------------------------------------------------------------------

const OAUTH_SESSION_PARTITION = 'persist:hermes-remote-oauth'

function getOauthSession() {
  if (oauthSession || !app.isReady()) {
    return oauthSession
  }

  oauthSession = session.fromPartition(OAUTH_SESSION_PARTITION)

  return oauthSession
}

// Bare + prefixed variants of the session cookies live in
// connection-config.ts (cookiesHaveSession / cookiesHaveLiveSession). See
// that module for details.

async function hasOauthSessionCookie(baseUrl) {
  const sess = getOauthSession()

  if (!sess) {
    return false
  }

  const parsed = new URL(baseUrl)

  try {
    // Query by URL so the cookie jar applies Domain/Path/Secure scoping for us.
    const cookies = await sess.cookies.get({ url: baseUrl })

    return cookiesHaveSession(cookies)
  } catch {
    // Fall back to a host match if the URL query path errors.
    try {
      const cookies = await sess.cookies.get({ domain: parsed.hostname })

      return cookiesHaveSession(cookies)
    } catch {
      return false
    }
  }
}

// Like hasOauthSessionCookie, but returns true when EITHER a live access-token
// cookie OR a (longer-lived) refresh-token cookie is present. This is the right
// "is the user signed in at all?" check: an expired AT with a live RT is still
// a connectable session because the gateway rotates a fresh AT server-side on
// the next authenticated request. Gating on the AT alone forces a needless full
// re-login every ~15 min. Used for the Settings "connected" indicator and as a
// cheap early-out before attempting a network round-trip in resolveRemoteBackend.
async function hasLiveOauthSession(baseUrl) {
  const sess = getOauthSession()

  if (!sess) {
    return false
  }

  const parsed = new URL(baseUrl)

  try {
    const cookies = await sess.cookies.get({ url: baseUrl })

    return cookiesHaveLiveSession(cookies)
  } catch {
    try {
      const cookies = await sess.cookies.get({ domain: parsed.hostname })

      return cookiesHaveLiveSession(cookies)
    } catch {
      return false
    }
  }
}

async function clearOauthSession(baseUrl) {
  const sess = getOauthSession()

  if (!sess) {
    return
  }

  try {
    const cookies = await sess.cookies.get(baseUrl ? { url: baseUrl } : {})
    await Promise.all(
      cookies.map(c => {
        const scheme = c.secure ? 'https' : 'http'
        const cookieUrl = `${scheme}://${c.domain.replace(/^\./, '')}${c.path || '/'}`

        return sess.cookies.remove(cookieUrl, c.name).catch(() => undefined)
      })
    )
  } catch {
    // Best effort — a stale cookie self-expires anyway.
  }
}

// Open a gateway login window in the OAuth session partition, resolving once
// the access-token cookie appears (login done) or rejecting if the user closes
// the window first. The window navigates through the IDP and back to
// /auth/callback, which sets the session cookies on the partition; we poll the
// cookie jar rather than try to read the HttpOnly value.
//
// `silent` selects the URL the window loads, which decides interactive-vs-silent:
//   - silent=false (default): load ``/login`` — the public interstitial that
//     renders the "Log in with X" provider chooser. This is the interactive
//     remote-gateway login the settings UI drives.
//   - silent=true: load the PROTECTED root ``/`` instead. ``/login`` is a public
//     route, so loading it NEVER triggers the gate's auto-SSO and always shows
//     the chooser. Loading a protected page with no session cookie makes the
//     gate run ``_auto_sso_response``: single registered provider + a live
//     portal session in this partition → a silent 302 through
//     ``/auth/login`` → portal ``/oauth/authorize`` (auto-approves org members)
//     → ``/auth/callback``, which sets the gateway cookie with NO interactive
//     prompt. This is the per-agent cloud cascade (decisions.md Q5).
function openOauthLoginWindow(baseUrl, { silent = false } = {}) {
  return new Promise((resolve, reject) => {
    if (!app.isReady()) {
      reject(new Error('Desktop is not ready to start an OAuth login.'))

      return
    }

    const sess = getOauthSession()

    if (!sess) {
      reject(new Error('OAuth session partition is unavailable.'))

      return
    }

    let settled = false
    let win = null
    let pollTimer = null
    let revealTimer = null

    const finish = err => {
      if (settled) {
        return
      }

      settled = true

      if (pollTimer) {
        clearInterval(pollTimer)
      }

      if (revealTimer) {
        clearTimeout(revealTimer)
      }

      try {
        if (win && !win.isDestroyed()) {
          win.destroy()
        }
      } catch {
        // window already torn down
      }

      if (err) {
        reject(err)
      } else {
        resolve({ baseUrl, ok: true })
      }
    }

    const checkCookie = async () => {
      if (settled) {
        return
      }

      if (await hasOauthSessionCookie(baseUrl)) {
        finish(null)
      }
    }

    try {
      win = new BrowserWindow({
        width: 520,
        height: 720,
        title: silent ? 'Connecting to Hermes Cloud agent…' : 'Sign in to Hermes gateway',
        autoHideMenuBar: true,
        // Silent cascade: start HIDDEN. The auto-SSO 302 chain completes in
        // well under a second, so the window normally never needs to show. We
        // only reveal it as a fallback if the cascade DOESN'T complete quickly
        // (e.g. the portal session lapsed and the gate fell through to the
        // interactive chooser) — see the reveal timer below.
        show: !silent,
        webPreferences: {
          contextIsolation: true,
          nodeIntegration: false,
          sandbox: true,
          session: sess,
          webSecurity: true
        }
      })
    } catch (error) {
      finish(error instanceof Error ? error : new Error(String(error)))

      return
    }

    // Re-check the cookie jar on every successful navigation (the callback
    // redirect is the moment cookies get set) plus a low-frequency poll as a
    // belt-and-braces fallback for IDPs that finish via in-page JS.
    win.webContents.on('did-navigate', () => void checkCookie())
    win.webContents.on('did-redirect-navigation', () => void checkCookie())
    win.webContents.on('did-frame-navigate', () => void checkCookie())
    pollTimer = setInterval(() => void checkCookie(), 750)

    // Silent-mode reveal fallback: if the cascade hasn't settled shortly, the
    // auto-SSO didn't go through silently (no portal session, multi-provider,
    // loop-guard tripped, etc.) and the window is now showing an interactive
    // page. Reveal it so the user can complete sign-in manually rather than
    // staring at nothing. Cleared on finish().
    if (silent && win) {
      revealTimer = setTimeout(() => {
        try {
          if (!settled && win && !win.isDestroyed() && !win.isVisible()) {
            win.show()
          }
        } catch {
          // window torn down
        }
      }, 2500)
    }

    win.on('closed', () => {
      if (!settled) {
        finish(new Error('Login window closed before authentication completed.'))
      }
    })

    // ``next`` is intentionally omitted: the gateway lands on ``/`` after
    // login, which is a valid authenticated page that sets the cookies. We
    // only care that the cookie jar is populated.
    //
    // silent=true loads the protected root so the gate auto-SSOs (no chooser);
    // silent=false loads the public ``/login`` chooser for interactive sign-in.
    const normalizedBase = normalizeRemoteBaseUrl(baseUrl)
    const loginUrl = silent ? `${normalizedBase}/` : `${normalizedBase}/login`
    win.loadURL(loginUrl).catch(error => {
      finish(error instanceof Error ? error : new Error(String(error)))
    })
  })
}

// JSON request routed through the OAuth session partition so the HttpOnly
// session cookie is attached automatically by Electron's net stack. Used for
// authed REST against a gated gateway, including minting WS tickets.
function fetchJsonViaOauthSession(url, options: any = {}) {
  return new Promise((resolve, reject) => {
    const sess = getOauthSession()

    if (!sess) {
      reject(new Error('OAuth session partition is unavailable.'))

      return
    }

    let parsed

    try {
      parsed = new URL(url)
    } catch (error) {
      reject(new Error(`Invalid URL: ${error.message}`))

      return
    }

    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
      reject(new Error(`Unsupported Hermes backend URL protocol: ${parsed.protocol}`))

      return
    }

    const body = serializeJsonBody(options.body)
    const timeoutMs = resolveTimeoutMs(options.timeoutMs, DEFAULT_FETCH_TIMEOUT_MS)

    const request = electronNet.request({
      method: options.method || 'GET',
      url,
      session: sess,
      useSessionCookies: true,
      redirect: 'follow'
    } as any)

    setJsonRequestHeaders(request)

    let timedOut = false

    const timer = setTimeout(() => {
      timedOut = true

      try {
        request.abort()
      } catch {
        // already finished
      }

      reject(new Error(`Timed out connecting to Hermes backend after ${timeoutMs}ms`))
    }, timeoutMs)

    request.on('response', res => {
      const chunks = []
      res.on('data', chunk => chunks.push(Buffer.from(chunk)))
      res.on('end', () => {
        if (timedOut) {
          return
        }

        clearTimeout(timer)
        const text = Buffer.concat(chunks).toString('utf8')
        const statusCode = res.statusCode || 500

        if (statusCode >= 400) {
          const err = new Error(`${statusCode}: ${text || ''}`) as any
          err.statusCode = statusCode
          reject(err)

          return
        }

        if (!text) {
          resolve(null)

          return
        }

        const looksHtml = /^\s*<(?:!doctype|html)/i.test(text)
        const contentType = String(res.headers['content-type'] || res.headers['Content-Type'] || '')

        if (looksHtml || contentType.includes('text/html')) {
          reject(new Error(`Expected JSON from ${url} but got HTML (status ${statusCode}).`))

          return
        }

        try {
          resolve(JSON.parse(text))
        } catch {
          reject(new Error(`Invalid JSON from ${url} (status ${statusCode}): ${text.slice(0, 200)}`))
        }
      })
    })
    request.on('error', error => {
      if (timedOut) {
        return
      }

      clearTimeout(timer)
      reject(error)
    })

    if (body) {
      request.write(body)
    }

    request.end()
  })
}

// Mint a single-use WS ticket for a gated gateway. Returns the ticket string.
// Throws (with statusCode 401) if the session cookie is missing/expired —
// callers treat that as "needs re-login".
async function mintGatewayWsTicket(baseUrl) {
  const body = (await fetchJsonViaOauthSession(`${baseUrl}/api/auth/ws-ticket`, {
    method: 'POST',
    timeoutMs: 8_000
  })) as any

  const ticket = body?.ticket

  if (!ticket || typeof ticket !== 'string') {
    throw new Error('Gateway did not return a WS ticket.')
  }

  return ticket
}

// --- Hermes Cloud discovery + silent per-agent sign-in (cloud-auto-discovery
// Phase 3) ---------------------------------------------------------------
//
// The "cloud" connection mode lets a user sign in to the Nous portal ONCE in
// the OAuth session partition, then (a) discover their hosted agents and (b)
// connect to any of them with no second interactive sign-in. Both ride the one
// portal session cookie living in `persist:hermes-remote-oauth`:
//   - discovery  → GET {portal}/api/agents over the partition-bound net; the
//     portal session cookie authenticates it (NAS Phase 2.5 accepts the cookie).
//   - cascade    → opening an agent's own /login in the same partition hits the
//     portal's silent auto-approve (org member, existing session) and 302s back
//     with that agent's session cookie — no prompt. Each agent still completes
//     its own PKCE exchange; SSO removes the human click, not a security check.

// Canonical Nous portal base URL, overridable for staging/dev. Mirrors the CLI
// convention (hermes_cli/auth.py DEFAULT_NOUS_PORTAL_URL + the same env names)
// so a single override flips every Hermes surface to the same portal.
const DEFAULT_NOUS_PORTAL_URL = 'https://portal.nousresearch.com'

function resolvePortalBaseUrl() {
  const raw = process.env.HERMES_PORTAL_BASE_URL || process.env.NOUS_PORTAL_BASE_URL || DEFAULT_NOUS_PORTAL_URL

  return String(raw).trim().replace(/\/+$/, '')
}

// Whether the OAuth partition currently holds a live Nous portal session — the
// credential that powers both discovery and the silent cascade. The portal
// authenticates via PRIVY, not the Hermes gateway session cookies, so this
// checks for the `privy-token` cookie on the portal host (NOT
// hasLiveOauthSession, which looks for hermes_session_at/rt that the portal
// never sets). See connection-config.ts cookiesHavePrivySession.
async function hasLivePortalSession() {
  const sess = getOauthSession()

  if (!sess) {
    return false
  }

  const portalBaseUrl = resolvePortalBaseUrl()
  const parsed = new URL(portalBaseUrl)

  try {
    const cookies = await sess.cookies.get({ url: portalBaseUrl })

    return cookiesHavePrivySession(cookies)
  } catch {
    try {
      const cookies = await sess.cookies.get({ domain: parsed.hostname })

      return cookiesHavePrivySession(cookies)
    } catch {
      return false
    }
  }
}

// Drive a one-time interactive portal sign-in in the OAuth partition. Unlike
// openOauthLoginWindow (which targets a gateway's /login), this lands on the
// portal itself so the resulting session cookie is portal-scoped — the cookie
// that authenticates discovery AND is reused for every silent per-agent
// cascade. Resolves once the portal session cookie appears.
function openPortalLoginWindow() {
  const portalBaseUrl = resolvePortalBaseUrl()

  return new Promise((resolve, reject) => {
    if (!app.isReady()) {
      reject(new Error('Desktop is not ready to start a Hermes Cloud sign-in.'))

      return
    }

    const sess = getOauthSession()

    if (!sess) {
      reject(new Error('OAuth session partition is unavailable.'))

      return
    }

    let settled = false
    let win = null
    let pollTimer = null

    const finish = err => {
      if (settled) {
        return
      }

      settled = true

      if (pollTimer) {
        clearInterval(pollTimer)
      }

      try {
        if (win && !win.isDestroyed()) {
          win.destroy()
        }
      } catch {
        // window already torn down
      }

      if (err) {
        reject(err)
      } else {
        resolve({ portalBaseUrl, ok: true })
      }
    }

    const checkCookie = async () => {
      if (settled) {
        return
      }

      // A live portal (Privy) session cookie means sign-in completed.
      if (await hasLivePortalSession()) {
        finish(null)
      }
    }

    try {
      win = new BrowserWindow({
        width: 520,
        height: 720,
        title: 'Sign in to Hermes Cloud',
        autoHideMenuBar: true,
        webPreferences: {
          contextIsolation: true,
          nodeIntegration: false,
          sandbox: true,
          session: sess,
          webSecurity: true
        }
      })
    } catch (error) {
      finish(error instanceof Error ? error : new Error(String(error)))

      return
    }

    win.webContents.on('did-navigate', () => void checkCookie())
    win.webContents.on('did-redirect-navigation', () => void checkCookie())
    win.webContents.on('did-frame-navigate', () => void checkCookie())
    pollTimer = setInterval(() => void checkCookie(), 750)

    win.on('closed', () => {
      if (!settled) {
        finish(new Error('Sign-in window closed before authentication completed.'))
      }
    })

    // Land on the portal root; any authenticated portal page sets the session
    // cookie. We only care that the partition cookie jar is populated.
    win.loadURL(portalBaseUrl).catch(error => {
      finish(error instanceof Error ? error : new Error(String(error)))
    })
  })
}

// Discover the hosted (Hermes Cloud) agents the signed-in user can see. Calls
// the NAS trimmed-summary endpoint over the partition-bound net, so the portal
// session cookie is attached automatically (no bearer needed — NAS accepts the
// cookie). Returns { agents } on success, or { needsOrgSelection: true, orgs }
// when the user belongs to multiple orgs and hasn't picked one yet (NAS 409
// org_selection_required). Pass `org` (a slug/id from a prior org list) to
// scope discovery to that org. Throws a needsCloudLogin-tagged error when no
// portal session is present.
async function discoverCloudAgents(org?: string) {
  const portalBaseUrl = resolvePortalBaseUrl()

  if (!(await hasLivePortalSession())) {
    const err = new Error(
      'You are not signed in to Hermes Cloud. Open Settings → Gateway, choose Hermes Cloud, and sign in.'
    ) as any

    err.needsCloudLogin = true
    throw err
  }

  const orgQuery = org ? `?org=${encodeURIComponent(org)}` : ''
  let body

  try {
    body = (await fetchJsonViaOauthSession(`${portalBaseUrl}/api/agents${orgQuery}`, {
      method: 'GET',
      timeoutMs: 15_000
    })) as any
  } catch (error) {
    // A 401 means the portal session lapsed between the liveness check and the
    // call — surface it as a re-login, not a generic failure.
    if (error && error.statusCode === 401) {
      const err = new Error('Your Hermes Cloud session has expired. Open Settings → Gateway and sign in again.') as any
      err.needsCloudLogin = true
      err.cause = error
      throw err
    }

    // A 409 means we're a multi-org user who hasn't picked an org. The body
    // carries the user's org list; surface it so the renderer shows a picker
    // and re-calls discovery with the chosen org. (fetchJsonViaOauthSession
    // throws on >=400 with err.statusCode + err.message "409: <json body>".)
    if (error && error.statusCode === 409) {
      const orgs = parseOrgSelectionError(error)

      if (orgs) {
        return { needsOrgSelection: true, orgs }
      }
    }

    throw error
  }

  return { agents: trimCloudAgents(body), org: trimCloudOrg(body?.org) }
}

// Project a NAS response org ({ id, slug, name, isPersonal }) to the trimmed
// shape the renderer persists, or null when absent/malformed.
function trimCloudOrg(org) {
  if (!org || typeof org !== 'object' || typeof org.id !== 'string') {
    return null
  }

  return {
    id: org.id,
    slug: typeof org.slug === 'string' ? org.slug : null,
    name: typeof org.name === 'string' ? org.name : org.id,
    isPersonal: Boolean(org.isPersonal),
    role: typeof org.role === 'string' ? org.role : 'MEMBER'
  }
}

// Extract the org list from a 409 org_selection_required error body. The error
// message is "409: <raw json>" (see fetchJsonViaOauthSession); parse defensively
// and return null if it isn't the shape we expect (caller then rethrows).
function parseOrgSelectionError(error) {
  const msg = String(error?.message || '')
  const jsonStart = msg.indexOf('{')

  if (jsonStart < 0) {
    return null
  }

  let parsed

  try {
    parsed = JSON.parse(msg.slice(jsonStart))
  } catch {
    return null
  }

  if (parsed?.error !== 'org_selection_required' || !Array.isArray(parsed.orgs)) {
    return null
  }

  return parsed.orgs
    .filter(o => o && typeof o === 'object' && typeof o.id === 'string')
    .map(o => ({
      id: o.id,
      slug: typeof o.slug === 'string' ? o.slug : null,
      name: typeof o.name === 'string' ? o.name : o.id,
      isPersonal: Boolean(o.isPersonal),
      role: typeof o.role === 'string' ? o.role : 'MEMBER'
    }))
}

// Project NAS's agent rows to the trimmed DTO the renderer consumes.
function trimCloudAgents(body) {
  const agents = Array.isArray(body?.agents) ? body.agents : []

  return agents
    .filter(a => a && typeof a === 'object' && typeof a.id === 'string')
    .map(a => ({
      id: a.id,
      name: typeof a.name === 'string' ? a.name : a.id,
      status: typeof a.status === 'string' ? a.status : 'unknown',
      dashboardUrl: typeof a.dashboardUrl === 'string' ? a.dashboardUrl : null,
      dashboardGatewayState: typeof a.dashboardGatewayState === 'string' ? a.dashboardGatewayState : 'unknown'
    }))
}

// Silent per-agent sign-in: open the selected agent dashboard's /login in the
// SAME OAuth partition. Because the user already holds a live portal session
// there, the agent's /oauth/authorize auto-approves (org member) and 302s back,
// setting that agent's gateway session cookie WITHOUT a second interactive
// prompt. Reuses openOauthLoginWindow — the window self-closes the instant the
// agent's session cookie lands (a silent flow finishes in well under a second;
// if the portal session were absent it would fall through to an interactive
// login, which the discovery gate already prevents). Returns once the agent's
// gateway session cookie is present.
async function cloudAgentSilentSignIn(dashboardUrl) {
  const baseUrl = normalizeRemoteBaseUrl(dashboardUrl)

  // Pre-req: a live portal session must exist, or this would surface an
  // interactive prompt rather than a silent cascade. Discovery already gates on
  // this, but a selection can arrive after the session lapsed.
  if (!(await hasLivePortalSession())) {
    const err = new Error('Your Hermes Cloud session has expired. Sign in to Hermes Cloud again.') as any
    err.needsCloudLogin = true
    throw err
  }

  await openOauthLoginWindow(baseUrl, { silent: true })

  return { baseUrl, connected: await hasOauthSessionCookie(baseUrl) }
}

export {
  clearOauthSession,
  cloudAgentSilentSignIn,
  discoverCloudAgents,
  fetchJsonViaOauthSession,
  hasLiveOauthSession,
  hasLivePortalSession,
  hasOauthSessionCookie,
  mintGatewayWsTicket,
  openOauthLoginWindow,
  openPortalLoginWindow,
  resolvePortalBaseUrl
}
