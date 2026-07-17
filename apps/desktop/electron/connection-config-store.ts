import fs from 'node:fs'
import http from 'node:http'
import https from 'node:https'
import path from 'node:path'

import { app, safeStorage } from 'electron'

import {
  authModeFromStatus,
  buildGatewayWsUrl,
  buildGatewayWsUrlWithTicket,
  connectionScopeKey,
  modeIsRemoteLike,
  normalizeRemoteBaseUrl,
  normAuthMode,
  profileRemoteOverride,
  resolveAuthMode,
  tokenPreview
} from './connection-config'
import { DEFAULT_FETCH_TIMEOUT_MS, encryptDesktopSecret as encryptDesktopSecretStrict, resolveTimeoutMs } from './hardening'
import { hasLiveOauthSession, mintGatewayWsTicket } from './hermes-cloud-auth'

// Resolved lazily, NOT at module load: main.ts applies HERMES_DESKTOP_USER_DATA_DIR
// via app.setPath('userData', …) in its own top-level body, which runs AFTER this
// imported module is evaluated. Capturing app.getPath('userData') at import time
// would freeze the pre-override (real user) path and make test:desktop:fresh read
// the real connection.json/active-profile.json instead of its sandbox.
const desktopConnectionConfigPath = () => path.join(app.getPath('userData'), 'connection.json')
// active-profile.json records which Hermes profile the desktop launches its
// local backend as. When set, startHermes() passes `hermes --profile <name>
// dashboard …`, which deterministically pins HERMES_HOME (see
// _apply_profile_override in hermes_cli/main.py) and bypasses the sticky
// ~/.hermes/active_profile file. Unset (null) preserves the legacy behavior:
// no --profile flag, so the backend honors active_profile / default.
const desktopProfileConfigPath = () => path.join(app.getPath('userData'), 'active-profile.json')
// Mirrors hermes_cli.profiles._PROFILE_ID_RE so we never hand the backend a
// value its profile resolver would reject and exit on.
const PROFILE_NAME_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/

let connectionConfigCache = null
let connectionConfigCacheMtime = null

// Atomic file write: temp + rename (atomic on all platforms). Prevents
// partial writes on crash/power loss that corrupt JSON config files.
function writeFileAtomic(targetPath, data, encoding?: BufferEncoding) {
  const tmp = targetPath + '.tmp'
  fs.writeFileSync(tmp, data, encoding)
  fs.renameSync(tmp, targetPath)
}

function fetchPublicJson(url, options: any = {}) {
  // Credential-free JSON GET/POST for public gateway endpoints
  // (``/api/status``, ``/api/auth/providers``). Unlike ``fetchJson`` it sends
  // NO ``X-Hermes-Session-Token`` header — used by the auth-mode probe before
  // any credentials exist, and any time we must not leak a token to an
  // endpoint that doesn't need one.
  return new Promise((resolve, reject) => {
    const body = options.body === undefined ? undefined : Buffer.from(JSON.stringify(options.body))
    let parsed

    try {
      parsed = new URL(url)
    } catch (error) {
      reject(new Error(`Invalid URL: ${error.message}`))

      return
    }

    const client = parsed.protocol === 'https:' ? https : http
    const timeoutMs = resolveTimeoutMs(options.timeoutMs, DEFAULT_FETCH_TIMEOUT_MS)

    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
      reject(new Error(`Unsupported Hermes backend URL protocol: ${parsed.protocol}`))

      return
    }

    const req = client.request(
      parsed,
      {
        method: options.method || 'GET',
        headers: {
          'Content-Type': 'application/json',
          ...(body ? { 'Content-Length': String(body.length) } : {})
        }
      },
      res => {
        const chunks = []
        res.on('data', chunk => chunks.push(chunk))
        res.on('end', () => {
          const text = Buffer.concat(chunks).toString('utf8')

          if ((res.statusCode || 500) >= 400) {
            reject(new Error(`${res.statusCode}: ${text || res.statusMessage}`))

            return
          }

          if (!text) {
            resolve(null)

            return
          }

          const looksHtml = /^\s*<(?:!doctype|html)/i.test(text)
          const contentType = String(res.headers['content-type'] || '')

          if (looksHtml || contentType.includes('text/html')) {
            reject(
              new Error(
                `Expected JSON from ${url} but got HTML (status ${res.statusCode}). ` +
                  'The endpoint is likely missing on the Hermes backend.'
              )
            )

            return
          }

          try {
            resolve(JSON.parse(text))
          } catch {
            reject(new Error(`Invalid JSON from ${url} (status ${res.statusCode}): ${text.slice(0, 200)}`))
          }
        })
      }
    )

    req.on('error', reject)
    req.setTimeout(timeoutMs, () => {
      req.destroy(new Error(`Timed out connecting to Hermes backend after ${timeoutMs}ms`))
    })

    if (body) {
      req.write(body)
    }

    req.end()
  })
}

function encryptDesktopSecret(value) {
  return encryptDesktopSecretStrict(value, safeStorage)
}

function decryptDesktopSecret(secret) {
  if (!secret || typeof secret !== 'object') {
    return ''
  }

  const value = String(secret.value || '')

  if (!value) {
    return ''
  }

  if (secret.encoding === 'safeStorage') {
    try {
      return safeStorage.decryptString(Buffer.from(value, 'base64'))
    } catch {
      return ''
    }
  }

  return value
}

// Validate + normalize the per-profile remote overrides map read from disk.
// Drops malformed names/entries and keeps only the recognized fields so a
// hand-edited or stale connection.json can't inject junk into resolution.
function sanitizeConnectionProfiles(raw: Record<string, any>) {
  if (!raw || typeof raw !== 'object') {
    return {}
  }

  const out = {}

  for (const [name, entry] of Object.entries(raw)) {
    if (!entry || typeof entry !== 'object') {
      continue
    }

    if (name !== 'default' && !PROFILE_NAME_RE.test(name)) {
      continue
    }

    const cleaned: {
      mode: 'remote' | 'local' | 'cloud'
      url?: string
      authMode?: string
      token?: object
      org?: string
    } = {
      mode: modeIsRemoteLike(entry.mode) ? entry.mode : 'local'
    }

    const url = String(entry.url || '').trim()

    if (url) {
      cleaned.url = url
    }

    cleaned.authMode = normAuthMode(entry.authMode)

    if ((entry as any).token && typeof entry.token === 'object') {
      cleaned.token = entry.token
    }

    // Preserve the Hermes Cloud org tag on cloud-mode entries so Settings can
    // reopen into the same org for a per-profile cloud connection.
    if (cleaned.mode === 'cloud') {
      const org = String(entry.org || '').trim()

      if (org) {
        cleaned.org = org
      }
    }

    out[name] = cleaned
  }

  return out
}

function readDesktopConnectionConfig() {
  // Check if file changed on disk since last read (e.g. modified by another
  // process or an external tool).  Our own writes update the cache inline
  // via writeDesktopConnectionConfig, but external changes would be missed.
  let mtime = null

  try {
    mtime = fs.statSync(desktopConnectionConfigPath()).mtimeMs
  } catch {
    mtime = null
  }

  if (connectionConfigCache && connectionConfigCacheMtime === mtime) {
    return connectionConfigCache
  }

  let config = { mode: 'local', remote: {}, profiles: {} }

  try {
    const raw = fs.readFileSync(desktopConnectionConfigPath(), 'utf8')
    const parsed = JSON.parse(raw)

    if (parsed && typeof parsed === 'object') {
      const remote = parsed.remote && typeof parsed.remote === 'object' ? parsed.remote : {}
      // authMode lives on the remote sub-object: 'oauth' (cookie + ws-ticket)
      // or 'token' (legacy static session token). Default to 'token' for
      // backward compatibility with configs written before OAuth support.
      remote.authMode = remote.authMode === 'oauth' ? 'oauth' : 'token'
      config = {
        mode: modeIsRemoteLike(parsed.mode) ? parsed.mode : 'local',
        remote,
        // Per-profile remote overrides: each profile may point at its own
        // backend (local spawn or its own remote URL). Preserved verbatim so
        // profileRemoteOverride() can resolve them; normalized lazily on save.
        profiles: sanitizeConnectionProfiles(parsed.profiles)
      }
    }
  } catch {
    // Missing or malformed connection settings should fall back to local.
  }

  connectionConfigCache = config
  connectionConfigCacheMtime = mtime

  return config
}

function writeDesktopConnectionConfig(config) {
  fs.mkdirSync(path.dirname(desktopConnectionConfigPath()), { recursive: true })
  writeFileAtomic(desktopConnectionConfigPath(), JSON.stringify(config, null, 2))
  connectionConfigCache = config
  connectionConfigCacheMtime = fs.statSync(desktopConnectionConfigPath()).mtimeMs
}

// Returns the desktop's chosen profile name, or null when unset. "default" is
// a valid stored value (pins the root HERMES_HOME explicitly); null means "no
// preference" and preserves the legacy launch (no --profile flag).
function readActiveDesktopProfile() {
  try {
    const raw = fs.readFileSync(desktopProfileConfigPath(), 'utf8')
    const parsed = JSON.parse(raw)
    const name = parsed && typeof parsed.profile === 'string' ? parsed.profile.trim() : ''

    if (name && (name === 'default' || PROFILE_NAME_RE.test(name))) {
      return name
    }
  } catch {
    // Missing or malformed → no preference.
  }

  return null
}

function writeActiveDesktopProfile(name) {
  const value = typeof name === 'string' ? name.trim() : ''

  if (value && value !== 'default' && !PROFILE_NAME_RE.test(value)) {
    throw new Error(`Invalid profile name: ${value}`)
  }

  fs.mkdirSync(path.dirname(desktopProfileConfigPath()), { recursive: true })
  writeFileAtomic(desktopProfileConfigPath(), JSON.stringify({ profile: value || null }, null, 2))

  return value || null
}

// Sanitize a connection config into the renderer-facing shape. With no
// `profile` this describes the global/default connection (the existing
// behavior); with a `profile` it describes that profile's per-profile remote
// override (or an empty "local/inherit" view when the profile has none).
async function sanitizeDesktopConnectionConfig(config = readDesktopConnectionConfig(), profile = null) {
  const key = connectionScopeKey(profile)
  const scoped = key ? config.profiles?.[key] || null : null
  const block = key ? scoped || {} : config.remote || {}

  const envOverride = key ? false : Boolean(process.env.HERMES_DESKTOP_REMOTE_URL)

  const remoteToken = decryptDesktopSecret(block.token)
  const authMode = normAuthMode(block.authMode)
  const remoteUrl = envOverride ? String(process.env.HERMES_DESKTOP_REMOTE_URL || '') : String(block.url || '')
  // The env override forces a plain remote connection. Otherwise reflect the
  // saved mode, preserving 'cloud' (a Hermes Cloud connection — Q6) so the UI
  // reopens into the cloud picker; any non-remote-like value collapses to local.
  const savedMode = key ? scoped?.mode : config.mode
  const mode = envOverride ? 'remote' : modeIsRemoteLike(savedMode) ? savedMode : 'local'

  let remoteOauthConnected = false

  if (authMode === 'oauth' && remoteUrl) {
    try {
      // Display signal: treat a live RT cookie as "connected" even if the AT
      // cookie has lapsed — the gateway refreshes the AT on the next request,
      // so the session is still usable. The authoritative liveness check is
      // the ws-ticket mint in resolveRemoteBackend at actual connect time.
      remoteOauthConnected = await hasLiveOauthSession(remoteUrl)
    } catch {
      remoteOauthConnected = false
    }
  }

  return {
    mode,
    // Echo the scope back so the UI knows which profile (if any) this reflects.
    profile: key,
    remoteAuthMode: authMode,
    remoteOauthConnected,
    remoteUrl,
    // The persisted Hermes Cloud org (slug/id) for a cloud connection, or '' for
    // remote/local. Lets Settings → Gateway reopen into the same org.
    cloudOrg: mode === 'cloud' ? String(block.org || '') : '',
    remoteTokenPreview: tokenPreview(remoteToken),
    remoteTokenSet: Boolean(remoteToken),
    // The env override only forces the global/primary connection; a per-profile
    // scope is never overridden by HERMES_DESKTOP_REMOTE_URL.
    envOverride
  }
}

// Build + validate a `{ url, authMode, token }` remote block. OAuth gateways
// authenticate via the login-window session cookie (verified at connect time in
// resolveRemoteBackend), so only token-auth remotes require a saved token.
// `org` (optional) is the Hermes Cloud org slug/id the instance was discovered
// under — persisted so Settings can reopen into the same org; omitted from the
// block when empty so plain remote connections stay unchanged.
function buildRemoteBlock(remoteUrl, authMode, token, org?: string) {
  if (authMode !== 'oauth' && !decryptDesktopSecret(token)) {
    throw new Error('Remote gateway session token is required.')
  }

  const block: { url: string; authMode: string; token: object; org?: string } = {
    url: normalizeRemoteBaseUrl(remoteUrl),
    authMode,
    token
  }

  const orgValue = typeof org === 'string' ? org.trim() : ''

  if (orgValue) {
    block.org = orgValue
  }

  return block
}

function coerceDesktopConnectionConfig(input: any = {}, existing = readDesktopConnectionConfig(), options: any = {}) {
  const persistToken = options.persistToken !== false
  const key = connectionScopeKey(input.profile)
  // 'cloud' and 'remote' both persist a remote-shaped block; 'cloud' is
  // remembered as its own provenance (Q6) and resolves to remote downstream.
  // Anything else collapses to local.
  const mode = modeIsRemoteLike(input.mode) ? input.mode : 'local'
  const remoteLike = modeIsRemoteLike(mode)

  // The block being edited: a per-profile entry or the global remote block.
  const rawExistingBlock = key ? existing.profiles?.[key] || {} : existing.remote || {}
  // Leaving a CLOUD connection unselects it: a cloud block's url/org/token
  // describe a discovered Hermes Cloud instance, NOT a user-owned remote gateway,
  // so switching to local or remote must NOT inherit them (otherwise the stale
  // cloud URL lingers and re-selecting Cloud looks "already connected"). When the
  // saved block was cloud and the new mode is not cloud, start from an empty
  // block. (remote↔local toggles still preserve a real remote URL as before.)
  const existingMode = key ? existing.profiles?.[key]?.mode : existing.mode
  const leavingCloud = existingMode === 'cloud' && mode !== 'cloud'
  const existingBlock = leavingCloud ? {} : rawExistingBlock
  const remoteUrl = String(input.remoteUrl ?? existingBlock.url ?? '').trim()
  // authMode: explicit input wins; otherwise inherit the saved value, default 'token'.
  const authMode = resolveAuthMode(input.remoteAuthMode, existingBlock.authMode)
  // Cloud org: only meaningful for 'cloud' mode. Explicit input wins; otherwise
  // inherit the saved org. A plain 'remote' connection never carries an org
  // (switching cloud→remote drops it), so it stays unset unless mode is cloud.
  const cloudOrg = mode === 'cloud' ? String(input.cloudOrg ?? existingBlock.org ?? '').trim() : ''
  const incomingToken = typeof input.remoteToken === 'string' ? input.remoteToken.trim() : ''

  const nextToken = incomingToken
    ? persistToken
      ? encryptDesktopSecret(incomingToken)
      : { encoding: 'plain', value: incomingToken }
    : existingBlock.token

  if (key) {
    // Per-profile scope: a remote/cloud entry pins this profile to its own
    // backend; a local entry clears the override so the profile inherits the
    // default. The mode tag (remote vs cloud) is preserved on the entry.
    const profiles = { ...(existing.profiles || {}) }

    if (remoteLike) {
      profiles[key] = { mode, ...buildRemoteBlock(remoteUrl, authMode, nextToken, cloudOrg) }
    } else {
      delete profiles[key]
    }

    return {
      mode: modeIsRemoteLike(existing.mode) ? existing.mode : 'local',
      remote: existing.remote || {},
      profiles
    }
  }

  const nextRemote = remoteLike
    ? buildRemoteBlock(remoteUrl, authMode, nextToken, cloudOrg)
    : { url: remoteUrl ? normalizeRemoteBaseUrl(remoteUrl) : remoteUrl, authMode, token: nextToken }

  // Preserve per-profile overrides when saving the global connection.
  return { mode, remote: nextRemote, profiles: existing.profiles || {} }
}

// Build a remote backend connection descriptor from an already-resolved remote
// config. Handles both auth models (OAuth ws-ticket vs static session token)
// and is shared by the per-profile, env, and global resolution paths. `token`
// is the DECRYPTED static token (or null in OAuth mode). `source` is a label
// for diagnostics ('profile' | 'env' | 'settings').
async function buildRemoteConnection(rawUrl, authMode, token, source) {
  const baseUrl = normalizeRemoteBaseUrl(rawUrl)

  if (authMode === 'oauth') {
    // OAuth gateway: auth comes from the session cookies in the OAuth
    // partition. Liveness is NOT "is the access-token cookie present?" —
    // Portal issues a 24h rotating refresh token (hermes #37247), and the
    // gateway middleware transparently rotates a fresh ~15-min access token
    // from it on the next authenticated request. So a session with an expired
    // AT cookie but a live RT cookie is still perfectly connectable. We
    // early-out only when neither cookie is present, then mint a ws-ticket as
    // the authoritative liveness check.
    if (!(await hasLiveOauthSession(baseUrl))) {
      const err = new Error(
        'Remote Hermes gateway uses OAuth, but you are not signed in. ' +
          'Open Settings → Gateway and click "Sign in", or switch back to Local.'
      ) as any

      err.needsOauthLogin = true
      throw err
    }

    let ticket

    try {
      ticket = await mintGatewayWsTicket(baseUrl)
    } catch (error) {
      const err = new Error(
        'Your remote gateway session has expired. ' + 'Open Settings → Gateway and click "Sign in" again.'
      ) as any

      err.needsOauthLogin = true
      err.cause = error
      throw err
    }

    return {
      baseUrl,
      mode: 'remote',
      source,
      authMode: 'oauth',
      // No static token in OAuth mode; REST is cookie-authed via the partition.
      token: null,
      wsUrl: buildGatewayWsUrlWithTicket(baseUrl, ticket)
    }
  }

  if (!token) {
    throw new Error(
      'Remote Hermes gateway is selected, but no session token is saved. ' +
        'Open Settings → Gateway and save a token, or switch back to Local.'
    )
  }

  return {
    baseUrl,
    mode: 'remote',
    source,
    authMode: 'token',
    token,
    wsUrl: buildGatewayWsUrl(baseUrl, token)
  }
}

// Resolve the remote backend for a given profile, or null when that profile
// should run a LOCAL backend. Precedence:
//   1. explicit per-profile remote override (connection.json `profiles[name]`)
//   2. env override (HERMES_DESKTOP_REMOTE_URL/_TOKEN) — applies app-wide
//   3. global remote (connection.json `mode: 'remote'`)
// A null/empty profile resolves the env/global remote, so legacy callers and
// the connection test (which pass no profile) are unchanged.
async function resolveRemoteBackend(profile) {
  const config = readDesktopConnectionConfig()

  // 1. Per-profile override — "a profile with its own remote host". Wins even
  //    over the env override so an explicitly-configured profile always
  //    reaches its intended backend.
  const override = profileRemoteOverride(config, profile)

  if (override) {
    const token = override.authMode === 'oauth' ? null : decryptDesktopSecret(override.token)

    return buildRemoteConnection(override.url, override.authMode, token, 'profile')
  }

  // 2. Env override (global, token-auth only).
  const rawEnvUrl = process.env.HERMES_DESKTOP_REMOTE_URL
  const rawEnvToken = process.env.HERMES_DESKTOP_REMOTE_TOKEN

  if (rawEnvUrl) {
    if (!rawEnvToken) {
      throw new Error(
        'HERMES_DESKTOP_REMOTE_URL is set but HERMES_DESKTOP_REMOTE_TOKEN is not. ' +
          'Both must be provided to connect to a remote Hermes backend.'
      )
    }

    return buildRemoteConnection(rawEnvUrl, 'token', rawEnvToken, 'env')
  }

  // 3. Global remote (or cloud — cloud resolves to a remote backend, Q6).
  if (!modeIsRemoteLike(config.mode)) {
    return null
  }

  const authMode = normAuthMode(config.remote?.authMode)
  const token = authMode === 'oauth' ? null : decryptDesktopSecret(config.remote?.token)

  return buildRemoteConnection(config.remote?.url, authMode, token, 'settings')
}

async function probeRemoteAuthMode(rawUrl) {
  // Determine how a remote gateway expects callers to authenticate, WITHOUT
  // sending any credentials. ``/api/status`` is public on every Hermes
  // gateway (it backs the portal liveness probe) and reports:
  //   auth_required: true  → OAuth gate is engaged (cookie + ws-ticket auth)
  //   auth_required: false → loopback/--insecure: legacy session-token auth
  // ``/api/auth/providers`` (also public, only meaningful when gated) gives
  // the human-facing provider name(s) for the login button label.
  //
  // The settings UI calls this as the user types a URL so it can render an
  // OAuth login button vs a session-token entry box. Network/parse failures
  // surface as ``reachable: false`` rather than throwing, so a half-typed or
  // unreachable URL degrades to "can't tell yet" instead of a hard error.
  const baseUrl = normalizeRemoteBaseUrl(rawUrl)

  let status

  try {
    status = await fetchPublicJson(`${baseUrl}/api/status`, { timeoutMs: 8_000 })
  } catch (error: any) {
    return {
      baseUrl,
      reachable: false,
      authMode: 'unknown',
      providers: [],
      version: null,
      error: error instanceof Error ? error.message : String(error)
    }
  }

  const authRequired = authModeFromStatus(status) === 'oauth'
  let providers = []

  if (authRequired) {
    // Best-effort: a gated gateway exposes the registered providers so the
    // button can read "Sign in with Nous Research" instead of a generic
    // label, and so a username/password provider can be distinguished from
    // an OAuth-redirect one (``supports_password``). A failure here doesn't
    // change the auth mode, so swallow it.
    try {
      const body = (await fetchPublicJson(`${baseUrl}/api/auth/providers`, { timeoutMs: 8_000 })) as any

      if (Array.isArray(body?.providers)) {
        providers = body.providers
          .filter(p => p && typeof p === 'object')
          .map(p => ({
            name: String(p.name || ''),
            displayName: String(p.display_name || p.name || ''),
            supportsPassword: Boolean(p.supports_password)
          }))
          .filter(p => p.name)
      }
    } catch {
      // Provider listing is optional metadata; the auth mode is already known.
    }
  }

  return {
    baseUrl,
    reachable: true,
    authMode: authRequired ? 'oauth' : 'token',
    providers,
    version: status?.version || null,
    error: null
  }
}

export {
  coerceDesktopConnectionConfig,
  decryptDesktopSecret,
  fetchPublicJson,
  PROFILE_NAME_RE,
  probeRemoteAuthMode,
  readActiveDesktopProfile,
  readDesktopConnectionConfig,
  resolveRemoteBackend,
  sanitizeDesktopConnectionConfig,
  writeActiveDesktopProfile,
  writeDesktopConnectionConfig,
  writeFileAtomic
}
