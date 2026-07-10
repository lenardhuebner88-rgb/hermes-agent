import type { ToneName } from './types';

/**
 * Legacy-Datenton → Rohfarbe. Bleibt vorerst ausschließlich für die zwei
 * SVG/Data-Viz-Pfade RoleChip und FleetPipeline erhalten; UI-Statusflächen
 * verwenden SignalChip/SignalLabel und die Sheet-A-Status-Tokens.
 */
export const TONE_HEX: Record<ToneName, string> = {
  emerald: '#0a8a60', cyan: '#0a87a8', sky: '#0369a1', indigo: '#4338ca',
  amber: '#b3590a', rose: '#be123c', red: '#d23b4e', zinc: '#79808f', violet: '#2e45d4',
};

/** Task-Status → Ton (für Hermes task_status). */
export const taskStatusLabel: Record<string, string> = {
  triage: 'Triage', todo: 'Offen', scheduled: 'Geplant', ready: 'Startklar',
  running: 'Läuft', blocked: 'Blockiert', review: 'In Prüfung', done: 'Fertig', archived: 'Archiv',
};

/** Profil-Rollen in Klartext. Unbekannte Profile zeigen ihren Rohnamen
 *  (Lookup-Fallback `?? profile` an den Call-Sites). */
export const profileLabel: Record<string, string> = {
  default: 'Standard', admin: 'Admin', coder: 'Coder', devpower: 'DevPower',
  dispatcher: 'Dispatcher', kanbanops: 'Kanban-Ops', planner: 'Planer',
  research: 'Research', critic: 'Kritiker', verifier: 'Verifier', scout: 'Scout',
  // claude-cli-Lanes (Max-Abo) + Review-Lane
  'coder-claude': 'Coder (Claude)', premium: 'Premium', reviewer: 'Reviewer',
};


/** Status-Punkt-Variante (siehe .mc-dot-* in theme.css). */
export type DotKind = 'live' | 'warn' | 'error' | 'ready' | 'idle' | 'offline';

/**
 * Decision-row severity spine (f5): ein Ton wird zu einer pre-attentiven
 * linken Akzent-Leiste (.hc-sev-* in control-tokens.css). So *sieht* der
 * Operator die Schwere einer Entscheidung, bevor er sie liest. Rot/Rose =
 * kritisch, Amber = hoch, Emerald = ruhig, der Rest = Info.
 */
export const severitySpine: Record<ToneName, string> = {
  red: 'hc-sev-critical',
  rose: 'hc-sev-critical',
  amber: 'hc-sev-high',
  emerald: 'hc-sev-calm',
  cyan: 'hc-sev-info',
  sky: 'hc-sev-info',
  indigo: 'hc-sev-info',
  violet: 'hc-sev-info',
  zinc: 'hc-sev-info',
};

/**
 * tone → die Sheet-A-Farbe, die `--hc-hero-accent` speist (Hero-Shell-Gradient +
 * Aurora-Kante, control-tokens.css). Status-Töne mappen auf ihren Farbton;
 * Info/Marke/Neutral fallen auf das ruhige Brand-Grau zurück — nicht auf den
 * interaktiven Bronze-Kanal.
 * Eigenständige Funktion (kein Record), damit der Hero sie teilen kann, ohne
 * dass die Komponentendatei einen Nicht-Komponenten-Export bekommt.
 */
export function heroAccent(tone: ToneName): string {
  switch (tone) {
    case 'red':
    case 'rose':
      return 'var(--color-status-alert)';
    case 'amber':
      return 'var(--color-status-warn)';
    case 'emerald':
      return 'var(--color-status-ok)';
    case 'cyan':
    case 'sky':
    case 'indigo':
      return 'var(--color-brand)';
    default:
      return 'var(--color-brand)';
  }
}
