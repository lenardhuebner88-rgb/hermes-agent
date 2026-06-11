/**
 * Ton-System — die Signatur des Designs: Hairline-Border + transluzente Wäsche.
 * Jeder Status-Ton rendert als  border-<tone>/20  bg-<tone>/10  text-<tone>-200|300.
 *
 * Diese Klassen funktionieren mit den Status-Farben aus tokens.css / theme.css.
 * `toneClasses(tone)` ist der einzige Ort, der dieses Muster kodiert — nutze es
 * für Pills, Callouts und getönte Flächen, statt die Klassen überall zu wiederholen.
 */
import type { ToneName, Priority } from './types';

/** Tailwind-Klassen für eine getönte Fläche (Pill/Callout/Chip). */
export const toneClasses = (tone: ToneName): string => TONE_CLASS[tone];

const TONE_CLASS: Record<ToneName, string> = {
  emerald: 'border-emerald-500/20 bg-emerald-500/10 text-emerald-200',
  cyan:    'border-cyan-500/20 bg-cyan-500/10 text-cyan-200',
  sky:     'border-sky-500/20 bg-sky-500/10 text-sky-200',
  indigo:  'border-indigo-400/20 bg-indigo-400/10 text-indigo-200',
  amber:   'border-amber-500/20 bg-amber-500/10 text-amber-200',
  rose:    'border-rose-500/20 bg-rose-500/10 text-rose-200',
  red:     'border-red-500/20 bg-red-500/10 text-red-200',
  zinc:    'border-zinc-600/20 bg-zinc-600/10 text-zinc-200',
  // Violett = Marke. Nur für Akzent/Interaktion, nie als Status-Warnung.
  // Fix: vorher nicht-präfixierte --accent-* Vars (existieren nicht → unsichtbar);
  // jetzt die kanonischen --hc-accent-* aus control-tokens.css.
  violet:  'border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] text-[var(--hc-accent-text)]',
};

/** Roher Hex-Wert eines Tons (für inline-Style, LED-Glow, Border-Akzent). */
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
  research: 'Research', critic: 'Kritiker', verifier: 'Verifier',
  // claude-cli-Lanes (Max-Abo) + Review-Lane
  'coder-claude': 'Coder (Claude)', premium: 'Premium', reviewer: 'Reviewer',
};

export const priorityLabel: Record<Priority, string> = { high: 'Hoch', med: 'Mittel', low: 'Niedrig' };
export const priorityTone: Record<Priority, ToneName> = { high: 'rose', med: 'amber', low: 'zinc' };

/** CSS-Variable für die Agenten-Farbe (siehe theme.css --agent-*). */
export const agentColorVar: Record<string, string> = {
  main: '--agent-atlas', 'sre-expert': '--agent-forge', 'frontend-guru': '--agent-pixel',
  'efficiency-auditor': '--agent-lens', james: '--agent-james', spark: '--agent-spark',
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
 * tone → die CSS-Farbe, die `--hc-hero-accent` speist (Hero-Shell-Gradient +
 * Aurora-Kante, control-tokens.css). Status-Töne mappen auf ihren Farbton;
 * Marke/Neutral (violet/zinc/rose-as-red etc.) fallen auf den Akzent zurück.
 * Eigenständige Funktion (kein Record), damit der Hero sie teilen kann, ohne
 * dass die Komponentendatei einen Nicht-Komponenten-Export bekommt.
 */
export function heroAccent(tone: ToneName): string {
  switch (tone) {
    case 'red':
    case 'rose':
      return 'var(--hc-red)';
    case 'amber':
      return 'var(--hc-amber)';
    case 'emerald':
      return 'var(--hc-emerald)';
    case 'cyan':
    case 'sky':
    case 'indigo':
      return 'var(--hc-cyan)';
    default:
      return 'var(--hc-accent)';
  }
}
