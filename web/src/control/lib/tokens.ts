/**
 * Design-Tokens für JS-Zugriff (LED-Glows, inline-Styles, Charts).
 * Die kanonische Quelle bleibt tokens.css (CSS-Variablen). Diese Werte spiegeln
 * sie für Fälle, in denen Tailwind-Klassen nicht reichen (z.B. box-shadow-Glow
 * mit currentColor, recharts-Farben).
 */
export const tokens = {
  accent: '#7c3aed',
  accentText: '#c4b5fd',
  accentWash: 'rgba(124,58,237,0.15)',
  accentBorder: 'rgba(124,58,237,0.30)',
  accentGlow: '0 0 12px rgba(124,58,237,0.45)',

  bg: '#0d0d0f', rail: '#0f0f0f', panel: '#111111', panel2: '#141414',
  panelCard: '#161b22', border: '#1e1e1e', borderStrong: '#2a2a2a',
  text: '#f0f0f0', textSoft: '#6b7280', textDim: '#374151',

  status: {
    emerald: '#22c55e', cyan: '#22d3ee', sky: '#38bdf8', indigo: '#818cf8',
    amber: '#f59e0b', rose: '#f43f5e', red: '#ef4444', zinc: '#52525b',
  },
  agent: {
    'main': '#14b8a6', 'sre-expert': '#f97316', 'frontend-guru': '#d946ef',
    'efficiency-auditor': '#eab308', 'james': '#10b981', 'spark': '#ec4899',
    operator: '#3b82f6',
  },

  radius: { sm: 6, md: 8, lg: 10, xl: 14, '2xl': 18, card: 20, pill: 999 },
  hit: { min: 44, tab: 60 },
  dur: { fast: 150, med: 200, slow: 400, pulse: 2000 },
} as const;

/** LED-Glow als box-shadow für einen Status-Hex. */
export const ledGlow = (hex: string) => `0 0 6px ${hex}`;
