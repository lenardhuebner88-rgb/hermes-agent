const test = require('node:test')
const assert = require('node:assert/strict')

const { resolveTerminalSpawnSpec, sanitizeTerminalStartOptions } = require('./terminal-target.cjs')

test('uses interactive shell when no controlled terminal target is requested', () => {
  const shellSpec = { command: '/bin/bash', args: ['-il'], name: 'bash' }
  const result = resolveTerminalSpawnSpec({ payload: {}, shellSpec, findOnPath: () => '/usr/bin/tmux' })

  assert.deepEqual(result, {
    command: '/bin/bash',
    args: ['-il'],
    name: 'bash',
    target: null
  })
})

test('resolves a controlled tmux session/window attach target without shell command injection', () => {
  const shellSpec = { command: '/bin/bash', args: ['-il'], name: 'bash' }
  const result = resolveTerminalSpawnSpec({
    payload: { tmuxTarget: { session: 'coder-42', window: 'agent_1' } },
    shellSpec,
    findOnPath: command => (command === 'tmux' ? '/usr/bin/tmux' : null)
  })

  assert.deepEqual(result, {
    command: '/usr/bin/tmux',
    args: ['attach-session', '-t', 'coder-42:agent_1'],
    name: 'tmux',
    target: { session: 'coder-42', window: 'agent_1' }
  })
})

test('rejects tmux targets with shell metacharacters', () => {
  const shellSpec = { command: '/bin/bash', args: ['-il'], name: 'bash' }

  assert.throws(
    () => resolveTerminalSpawnSpec({
      payload: { tmuxTarget: { session: 'coder; rm -rf /', window: '0' } },
      shellSpec,
      findOnPath: () => '/usr/bin/tmux'
    }),
    /Invalid tmux session/
  )
})

test('requires tmux to be installed before accepting an attach target', () => {
  const shellSpec = { command: '/bin/bash', args: ['-il'], name: 'bash' }

  assert.throws(
    () => resolveTerminalSpawnSpec({
      payload: { tmuxTarget: { session: 'coder', window: '0' } },
      shellSpec,
      findOnPath: () => null
    }),
    /tmux is unavailable/
  )
})

test('preload sanitizer forwards only the controlled terminal start contract', () => {
  assert.deepEqual(
    sanitizeTerminalStartOptions({
      cols: 120,
      command: 'bash -lc dangerous',
      cwd: '/tmp/work',
      rows: 40,
      tmuxTarget: { session: 'coder', window: 'agent_1', command: 'ignored' }
    }),
    {
      cols: 120,
      cwd: '/tmp/work',
      rows: 40,
      tmuxTarget: { session: 'coder', window: 'agent_1' }
    }
  )
})
