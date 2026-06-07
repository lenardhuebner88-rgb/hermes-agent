/**
 * Design-Tokens für JS-Zugriff (LED-Glows, inline-Styles, Charts).
 * Die kanonische Quelle bleibt tokens.css (CSS-Variablen). Diese Werte spiegeln
 * sie für Fälle, in denen Tailwind-Klassen nicht reichen (z.B. box-shadow-Glow
 * mit currentColor, recharts-Farben).
 */
export const tokens = {
  // Aurora Violet accent (mirrors --hc-accent* in control-tokens.css).
  accent: '#8b5cf6',
  accentStrong: '#a78bfa',
  accentText: '#ddd6fe',
  accent2: '#22d3ee',
  accentWash: 'rgba(139,92,246,0.14)',
  accentBorder: 'rgba(139,92,246,0.34)',
  accentGlow: '0 0 20px rgba(139,92,246,0.28)',
  /** The aurora gradient stops, in order (violet → indigo → cyan). */
  aurora: ['#8b5cf6', '#6366f1', '#22d3ee'],

  bg: '#0d0d0f', rail: '#0f0f0f', panel: '#111111', panel2: '#141414',
  panelCard: '#161b22', border: '#1e1e1e', borderStrong: '#2a2a2a',
  text: '#f0f0f0', textSoft: '#6b7280', textDim: '#374151',

  /** Elevation shadows (mirror --hc-elev-* in control-tokens.css). */
  elev: {
    1: '0 1px 0 rgba(255,255,255,.03) inset, 0 4px 18px rgba(0,0,0,.30)',
    2: '0 1px 0 rgba(255,255,255,.04) inset, 0 12px 40px rgba(0,0,0,.44)',
  },

  /** Named type scale (mirror .hc-type-* / --hc-type-* in control-tokens.css). */
  type: {
    display:  { size: 40, weight: 700, tracking: '-0.03em' },
    title:    { size: 28, weight: 650, tracking: '-0.02em' },
    subtitle: { size: 18, weight: 600, tracking: '-0.01em' },
    body:     { size: 14, weight: 400, tracking: '0' },
    label:    { size: 12, weight: 500, tracking: '0.01em' },
    eyebrow:  { size: 10, weight: 600, tracking: '0.18em' },
  },

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
