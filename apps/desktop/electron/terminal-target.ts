import fs from 'node:fs'
import path from 'node:path'

const TMUX_TARGET_PART_RE = /^[A-Za-z0-9_.:@%+=,-]{1,96}$/

export interface TerminalTmuxTarget {
  session: string
  window?: string
}

export interface TerminalShellSpec {
  args: string[]
  command: string
  name: string
}

function isExecutableFile(candidate: string): boolean {
  try {
    const stat = fs.statSync(candidate)

    return stat.isFile() && (process.platform === 'win32' || (stat.mode & 0o111) !== 0)
  } catch {
    return false
  }
}

export function findOnPath(command: string): string | null {
  const extensions = process.platform === 'win32' ? (process.env.PATHEXT || '.EXE;.CMD;.BAT').split(';') : ['']

  for (const dir of (process.env.PATH || '').split(path.delimiter)) {
    if (!dir) {
      continue
    }

    for (const ext of extensions) {
      const candidate = path.join(dir, command.endsWith(ext) ? command : `${command}${ext}`)

      if (isExecutableFile(candidate)) {
        return candidate
      }
    }
  }

  return null
}

export function normalizeTmuxTarget(rawTarget: unknown): TerminalTmuxTarget | null {
  if (!rawTarget) {
    return null
  }

  if (typeof rawTarget !== 'object' || Array.isArray(rawTarget)) {
    throw new Error('Invalid terminal target: tmuxTarget must be an object')
  }

  const raw = rawTarget as Record<string, unknown>
  const session = String(raw.session || '').trim()
  const windowName = String(raw.window || '').trim()

  if (!session || !TMUX_TARGET_PART_RE.test(session)) {
    throw new Error('Invalid tmux session')
  }

  if (windowName && !TMUX_TARGET_PART_RE.test(windowName)) {
    throw new Error('Invalid tmux window')
  }

  return { session, window: windowName || undefined }
}

export function buildTmuxTargetArg(target: TerminalTmuxTarget): string {
  return target.window ? `${target.session}:${target.window}` : target.session
}

export function resolveTerminalSpawnSpec({
  payload = {},
  shellSpec,
  findOnPath: lookup = findOnPath
}: {
  payload?: { tmuxTarget?: unknown }
  shellSpec: TerminalShellSpec
  findOnPath?: (command: string) => string | null
}): TerminalShellSpec & { target: TerminalTmuxTarget | null } {
  const target = normalizeTmuxTarget(payload.tmuxTarget)

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

export function sanitizeTerminalStartOptions(options: Record<string, unknown> = {}) {
  const target = options.tmuxTarget as Record<string, unknown> | undefined

  return {
    cols: options.cols,
    cwd: options.cwd,
    rows: options.rows,
    tmuxTarget: target ? { session: target.session, window: target.window } : undefined
  }
}
