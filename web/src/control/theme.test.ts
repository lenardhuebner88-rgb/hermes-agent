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
  '--color-surface-0': '#0e100f',
  '--color-surface-1': '#141715',
  '--color-surface-2': '#1a1e1b',
  '--color-surface-3': '#232824',
  '--color-line': '#2a302b',
  '--color-line-soft': '#1e2420',
  '--color-live': '#c9884a',
  '--color-brand': '#8a8577',
  '--color-status-ok': '#86b97e',
  '--color-status-warn': '#d9b23a',
  '--color-status-alert': '#e0604f',
  '--color-ink': '#ebe7de',
  '--color-ink-2': '#a9a59b',
  '--color-ink-3': '#757166',
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
    expect(themeCss).toMatch(/--radius-panel:\s*10px\s*;/);
    expect(themeCss).toMatch(/--radius-card:\s*7px\s*;/);
  });
});
