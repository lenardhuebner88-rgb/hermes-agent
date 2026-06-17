/**
 * Design-Tokens für JS-Zugriff (LED-Glows, inline-Styles, Charts).
 * Die kanonische Quelle bleibt tokens.css (CSS-Variablen). Diese Werte spiegeln
 * sie für Fälle, in denen Tailwind-Klassen nicht reichen (z.B. box-shadow-Glow
 * mit currentColor, recharts-Farben).
 */
export const tokens = {
  // Daylight ultramarine accent (mirrors --hc-accent* in control-tokens.css).
  accent: '#2e45d4',
  accentStrong: '#1d2fa8',
  accentText: '#2334b8',
  accent2: '#0a8aa6',
  accentWash: 'rgba(46,69,212,0.08)',
  accentBorder: 'rgba(46,69,212,0.32)',
  accentGlow: '0 0 18px rgba(46,69,212,0.22)',
  /** The signal gradient stops, in order (ultramarine → azure → teal). */
  aurora: ['#1d2fa8', '#2e5be8', '#0a8aa6'],

  bg: '#f2f0ea', rail: '#edeae2', panel: '#faf9f5', panel2: '#f5f3ec',
  panelCard: '#fcfbf8', border: '#ddd8cb', borderStrong: '#c7c1b1',
  text: '#1a1d28', textSoft: '#555c6e', textDim: '#8b8f9c',

  /** Elevation shadows (mirror --hc-elev-* in control-tokens.css). */
  elev: {
    1: 'inset 0 1px 0 #fff, 0 1px 2px rgba(26,29,40,.05), 0 12px 28px -20px rgba(26,29,40,.16)',
    2: 'inset 0 1px 0 #fff, 0 2px 6px rgba(26,29,40,.06), 0 28px 60px -30px rgba(26,29,40,.24)',
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
    emerald: '#0a8a60', cyan: '#0a87a8', sky: '#0369a1', indigo: '#4338ca',
    amber: '#b3590a', rose: '#be123c', red: '#d23b4e', zinc: '#79808f',
  },
  agent: {
    'main': '#0f766e', 'sre-expert': '#c2410c', 'frontend-guru': '#a21caf',
    'efficiency-auditor': '#a16207', 'james': '#047857', 'spark': '#be185d',
    operator: '#1d4ed8',
  },

  radius: { sm: 6, md: 8, lg: 10, xl: 14, '2xl': 18, card: 20, pill: 999 },
  hit: { min: 44, tab: 60 },
  dur: { fast: 150, med: 200, slow: 400, pulse: 2000 },
} as const;
