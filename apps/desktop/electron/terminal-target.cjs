const fs = require('node:fs')
const path = require('node:path')

const TMUX_TARGET_PART_RE = /^[A-Za-z0-9_.:@%+=,-]{1,96}$/

function isExecutableFile(candidate) {
  try {
    const stat = fs.statSync(candidate)
    return stat.isFile() && (process.platform === 'win32' || (stat.mode & 0o111) !== 0)
  } catch {
    return false
  }
}

function findOnPath(command) {
  const pathEnv = process.env.PATH || ''
  const extensions = process.platform === 'win32' ? (process.env.PATHEXT || '.EXE;.CMD;.BAT').split(';') : ['']

  for (const dir of pathEnv.split(path.delimiter)) {
    if (!dir) continue

    for (const ext of extensions) {
      const candidate = path.join(dir, command.endsWith(ext) ? command : `${command}${ext}`)
      if (isExecutableFile(candidate)) return candidate
    }
  }

  return null
}

function normalizeTmuxTarget(rawTarget) {
  if (!rawTarget) return null

  if (typeof rawTarget !== 'object' || Array.isArray(rawTarget)) {
    throw new Error('Invalid terminal target: tmuxTarget must be an object')
  }

  const session = String(rawTarget.session || '').trim()
  const windowName = String(rawTarget.window || '').trim()

  if (!session || !TMUX_TARGET_PART_RE.test(session)) {
    throw new Error('Invalid tmux session')
  }

  if (windowName && !TMUX_TARGET_PART_RE.test(windowName)) {
    throw new Error('Invalid tmux window')
  }

  return { session, window: windowName || undefined }
}

function buildTmuxTargetArg(target) {
  return target.window ? `${target.session}:${target.window}` : target.session
}

function resolveTerminalSpawnSpec({ payload = {}, shellSpec, findOnPath: lookup = findOnPath } = {}) {
  const target = normalizeTmuxTarget(payload?.tmuxTarget)

  if (!target) {
    return { ...shellSpec, target: null }
  }

  const tmux = lookup('tmux')
  if (!tmux) {
    throw new Error('tmux is unavailable; cannot attach desktop terminal to an agent session')
  }

  return {
    args: ['attach-session', '-t', buildTmuxTargetArg(target)],
    command: tmux,
    name: 'tmux',
    target
  }
}

function sanitizeTerminalStartOptions(options = {}) {
  const target = options?.tmuxTarget

  return {
    cols: options?.cols,
    cwd: options?.cwd,
    rows: options?.rows,
    tmuxTarget: target
      ? {
          session: target.session,
          window: target.window
        }
      : undefined
  }
}

function normalizeTerminalSize(size = {}) {
  return {
    cols: Math.max(2, Number.parseInt(String(size?.cols || 80), 10) || 80),
    rows: Math.max(2, Number.parseInt(String(size?.rows || 24), 10) || 24)
  }
}

function resizeTerminalSession(sessions, id, size = {}) {
  const sessionInfo = sessions.get(String(id || ''))

  if (!sessionInfo) {
    return false
  }

  const { cols, rows } = normalizeTerminalSize(size)
  sessionInfo.pty.resize(cols, rows)

  return true
}

function disposeTerminalSession(sessions, id) {
  const key = String(id || '')
  const sessionInfo = sessions.get(key)

  if (!sessionInfo) {
    return false
  }

  sessions.delete(key)

  try {
    sessionInfo.pty.kill()
  } catch {
    // Process may already be gone.
  }

  return true
}

module.exports = {
  buildTmuxTargetArg,
  disposeTerminalSession,
  findOnPath,
  normalizeTerminalSize,
  normalizeTmuxTarget,
  resolveTerminalSpawnSpec,
  resizeTerminalSession,
  sanitizeTerminalStartOptions
}
