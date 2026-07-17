import type { TestProjectConfiguration } from 'vitest/config';
import { defineConfig } from 'vitest/config'

const reactUi: TestProjectConfiguration = {
  extends: './vite.config.ts',
  test: {
    name: 'ui',
    environment: 'jsdom',
    setupFiles: ['./vitest.setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
    globals: true,
    // Load-flake cap: heavy jsdom render tests exceed the 5s default under
    // parallel full-suite load (2026-07-16/17: skills, then messaging).
    testTimeout: 15000
  }
}

const electronNative: TestProjectConfiguration = {
  test: {
    name: 'electron',
    environment: 'node',
    include: ['electron/**/*.test.ts', 'scripts/**.test.{ts,mjs}']
  }
}

export default defineConfig({
  test: {
    projects: [reactUi, electronNative]
  }
})
