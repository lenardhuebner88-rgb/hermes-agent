import { configure } from '@testing-library/react'

// React 19 + Testing Library 16: opt into the act environment so render(),
// fireEvent(), and findBy* queries automatically flush state updates without
// spurious "not wrapped in act(...)" warnings.
;(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true

// Load-flake hardening for the UI project: under full-suite parallel load
// (maxWorkers contention) heavy jsdom render tests miss findBy*'s default 1s
// waitFor poll — surfacing as random "Unable to find role …" failures on
// unrelated files (2026-07-16/17: skills, messaging, toolset-config-panel).
// Raise the async-utility poll ceiling suite-wide; the per-project testTimeout
// (vitest.config.ts) covers the raw-timeout variant of the same contention.
configure({ asyncUtilTimeout: 5000 })
