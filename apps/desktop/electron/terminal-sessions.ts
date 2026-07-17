import { execFile } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'

import { app } from 'electron'

// Platform flags mirrored from main (process.platform — not main anchor state).
const IS_WINDOWS = process.platform === 'win32'

const terminalSessions = new Map()

type TerminalSessionsDeps = {
  findOnPath: (command: string) => string | null
}

let findOnPath: (command: string) => string | null = () => null

/** One-time DI: main's findOnPath (WSL-aware PATH resolution). */
export function initTerminalSessions(deps: TerminalSessionsDeps) {
  findOnPath = deps.findOnPath
}

export function getTerminalSession(id) {
  return terminalSessions.get(id)
}

export function setTerminalSession(id, info) {
  terminalSessions.set(id, info)
}

export function deleteTerminalSession(id) {
  return terminalSessions.delete(id)
}

function isExecutableFile(filePath) {
  if (!filePath || !path.isAbsolute(filePath)) {
    return false
  }

  try {
    fs.accessSync(filePath, fs.constants.X_OK)

    return true
  } catch {
    return false
  }
}

function posixShellSpec(shellPath) {
  const shellName = path.basename(shellPath)
  const interactiveArgs = shellName.includes('zsh') || shellName.includes('bash') ? ['-il'] : ['-i']

  return { args: interactiveArgs, command: shellPath, name: shellName }
}

// Windows PowerShell 5.1 ships at a fixed System32 path on every Windows box;
// prefer it only after PowerShell 7+ (`pwsh`).
function windowsPowerShellPath() {
  const systemRoot = process.env.SystemRoot || process.env.windir || 'C:\\Windows'
  const builtin = path.join(systemRoot, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')

  return isExecutableFile(builtin) ? builtin : findOnPath('powershell.exe')
}

// Map a resolved shell path to its spawn spec, picking interactive flags by
// family: PowerShell drops its logo banner (so the prompt sits flush like the
// POSIX shells), cmd needs nothing, and everything else (zsh/bash/fish/sh…)
// gets POSIX interactive-login flags.
function shellSpecFor(shellPath) {
  const name = path.basename(shellPath).toLowerCase()

  if (name.startsWith('pwsh') || name.startsWith('powershell')) {
    return { args: ['-NoLogo'], command: shellPath, name }
  }

  if (name.startsWith('cmd')) {
    return { args: [], command: shellPath, name }
  }

  return posixShellSpec(shellPath)
}

// Best installed Windows shell: PowerShell 7+ (`pwsh`), then Windows PowerShell
// 5.1, then comspec/cmd.exe as the universal fallback.
function windowsShellSpec() {
  const command =
    findOnPath('pwsh.exe') || findOnPath('pwsh') || windowsPowerShellPath() || process.env.COMSPEC || 'cmd.exe'

  return shellSpecFor(command)
}

// Resolve the interactive shell for the embedded terminal: an explicit user
// override wins, otherwise auto-detect the best one installed for the platform.
export function terminalShellCommand() {
  // HERMES_DESKTOP_SHELL is the cross-platform escape hatch (a path or a bare
  // name on PATH); $SHELL is honored on POSIX, where it's the user's canonical
  // choice, but ignored on Windows, where it's usually a stray MSYS/Git path
  // node-pty can't spawn natively.
  const override = (process.env.HERMES_DESKTOP_SHELL || (IS_WINDOWS ? '' : process.env.SHELL) || '').trim()

  if (override) {
    const resolved = isExecutableFile(override) ? override : findOnPath(override)

    if (resolved) {
      return shellSpecFor(resolved)
    }
  }

  if (IS_WINDOWS) {
    return windowsShellSpec()
  }

  const shellPath = ['/bin/zsh', '/bin/bash', '/bin/sh'].find(candidate => isExecutableFile(candidate))

  return posixShellSpec(shellPath || '/bin/sh')
}

export function safeTerminalCwd(cwd) {
  const candidate = path.resolve(String(cwd || app.getPath('home')))

  try {
    const stat = fs.statSync(candidate)

    return stat.isDirectory() ? candidate : path.dirname(candidate)
  } catch {
    return app.getPath('home')
  }
}

export function terminalShellEnv() {
  const env = { ...process.env }

  // Electron is commonly launched through `npm run dev`; do not leak npm's
  // managed prefix into a user's interactive shell (nvm/proto warn loudly).
  for (const key of Object.keys(env)) {
    if (key === 'npm_config_prefix' || key.startsWith('npm_config_') || key.startsWith('npm_package_')) {
      delete env[key]
    }
  }

  // Strip color/theme-detection vars that ride along when Electron is launched
  // from a non-tty agent shell (Cursor's runner sets NO_COLOR/FORCE_COLOR=0
  // /TERM=dumb; some terminals set COLORFGBG which would flip Hermes' TUI into
  // light-mode). Our PTY is a real xterm-compat terminal — force truecolor.
  delete env.NO_COLOR
  delete env.FORCE_COLOR
  delete env.COLORFGBG

  env.COLORTERM = 'truecolor'
  env.LC_CTYPE = env.LC_CTYPE || 'UTF-8'
  env.TERM = 'xterm-256color'
  env.TERM_PROGRAM = 'Hermes'
  env.TERM_PROGRAM_VERSION = app.getVersion()

  // Let a hermes/--tui launched in this pane know it's embedded in the desktop
  // GUI (build_environment_hints surfaces this). Distinct from HERMES_DESKTOP,
  // which marks the agent *backend* and gates cron/gateway behavior.
  env.HERMES_DESKTOP_TERMINAL = '1'

  return env
}

export function terminalChannel(id, suffix) {
  return `hermes:terminal:${id}:${suffix}`
}

// Best-effort read of a live PTY child's current working directory so a
// reopened tab can restart the shell where the user last `cd`'d, instead of the
// tab's original launch dir. Shell-agnostic (no prompt/OSC config needed) on
// POSIX; Windows has no cheap per-process cwd query without a native module, so
// it returns null and the caller falls back to the launch cwd.
export function readProcessCwd(pid) {
  return new Promise(resolve => {
    if (!Number.isInteger(pid) || pid <= 0) {
      resolve(null)

      return
    }

    if (process.platform === 'linux') {
      fs.promises
        .readlink(`/proc/${pid}/cwd`)
        .then(target => resolve(target || null))
        .catch(() => resolve(null))

      return
    }

    if (process.platform === 'darwin') {
      // lsof ships with macOS; -Fn emits the cwd fd's path on an `n<path>` line.
      execFile('lsof', ['-a', '-p', String(pid), '-d', 'cwd', '-Fn'], { timeout: 2000 }, (err, stdout) => {
        if (err) {
          resolve(null)

          return
        }

        const line = String(stdout || '')
          .split('\n')
          .find(entry => entry.startsWith('n'))

        resolve(line ? line.slice(1) : null)
      })

      return
    }

    resolve(null)
  })
}

export function disposeTerminalSession(id) {
  const sessionInfo = terminalSessions.get(id)

  if (!sessionInfo) {
    return false
  }

  terminalSessions.delete(id)

  try {
    sessionInfo.pty.kill()
  } catch {
    // Process may already be gone.
  }

  return true
}

/** App-quit cleanup: kill every open PTY (node-pty#904 race avoidance). */
export function disposeAllTerminalSessions() {
  for (const id of [...terminalSessions.keys()]) {
    disposeTerminalSession(id)
  }
}
