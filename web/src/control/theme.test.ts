import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

// Reads the real theme.css from disk (not a synthetic copy) so this test
// fails if the tokens drift from the values ported from the operator-
// approved mockup (docs/design/leitstand-mockup-terminals.html).
const themeCssPath = path.join(path.dirname(fileURLToPath(import.meta.url)), 'theme.css');
const themeCss = readFileSync(themeCssPath, 'utf-8');

const expectedTokens: Record<string, string> = {
  '--color-surface-0': '#050b14',
  '--color-surface-1': '#081322',
  '--color-surface-2': '#0c1b2e',
  '--color-surface-3': '#102438',
  '--color-line': '#1b3049',
  '--color-line-soft': '#132338',
  '--color-live': '#4fd8eb',
  '--color-brand': '#6f8fb8',
  '--color-status-ok': '#3ddc97',
  '--color-status-warn': '#f2b84b',
  '--color-status-alert': '#ff6b6b',
  '--color-ink': '#e9f2f7',
  '--color-ink-2': '#9db4c4',
  '--color-ink-3': '#64809a',
};

describe('Leitstand theme tokens (theme.css)', () => {
  it('declares an @theme block', () => {
    expect(themeCss).toMatch(/@theme\s*{/);
  });

  it.each(Object.entries(expectedTokens))('defines %s: %s', (name, value) => {
    const re = new RegExp(`${name}:\\s*${value}\\s*;`, 'i');
    expect(themeCss).toMatch(re);
  });

  it('defines radius-panel and radius-card', () => {
    expect(themeCss).toMatch(/--radius-panel:\s*14px\s*;/);
    expect(themeCss).toMatch(/--radius-card:\s*10px\s*;/);
  });
});
