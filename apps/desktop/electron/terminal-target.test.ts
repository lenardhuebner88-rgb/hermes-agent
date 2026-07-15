import { describe, expect, it } from 'vitest'

import {
  resolveTerminalSpawnSpec,
  sanitizeTerminalStartOptions
} from './terminal-target'

describe('desktop controlled terminal target', () => {
  const shellSpec = { command: '/bin/bash', args: ['-il'], name: 'bash' }

  it('uses the interactive shell when no target is requested', () => {
    expect(resolveTerminalSpawnSpec({ payload: {}, shellSpec })).toEqual({
      ...shellSpec,
      target: null
    })
  })

  it('resolves a tmux session/window without shell interpolation', () => {
    expect(
      resolveTerminalSpawnSpec({
        payload: { tmuxTarget: { session: 'coder-42', window: 'agent_1' } },
        shellSpec,
        findOnPath: () => '/usr/bin/tmux'
      })
    ).toEqual({
      command: '/usr/bin/tmux',
      args: ['attach-session', '-t', 'coder-42:agent_1'],
      name: 'tmux',
      target: { session: 'coder-42', window: 'agent_1' }
    })
  })

  it('rejects shell metacharacters and missing tmux', () => {
    expect(() =>
      resolveTerminalSpawnSpec({
        payload: { tmuxTarget: { session: 'coder; rm -rf /', window: '0' } },
        shellSpec,
        findOnPath: () => '/usr/bin/tmux'
      })
    ).toThrow(/Invalid tmux session/)
    expect(() =>
      resolveTerminalSpawnSpec({
        payload: { tmuxTarget: { session: 'coder', window: '0' } },
        shellSpec,
        findOnPath: () => null
      })
    ).toThrow(/tmux is unavailable/)
  })

  it('forwards only the controlled preload contract', () => {
    expect(
      sanitizeTerminalStartOptions({
        cols: 120,
        command: 'bash -lc dangerous',
        cwd: '/tmp/work',
        rows: 40,
        tmuxTarget: { session: 'coder', window: 'agent_1', command: 'ignored' }
      })
    ).toEqual({
      cols: 120,
      cwd: '/tmp/work',
      rows: 40,
      tmuxTarget: { session: 'coder', window: 'agent_1' }
    })
  })
})
