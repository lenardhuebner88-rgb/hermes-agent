/**
 * Tastatur- & A11y-Karte (zentral, damit Shortcuts konsistent bleiben).
 * Aus Richtung C übernommen, gilt aber app-weit als Beschleuniger.
 */
export const KEYMAP = {
  global: {
    openCommandPalette: ['Meta+k', 'Control+k'], // ⌘K / Ctrl+K — toggelt das Overlay
  },
  list: { // in Hermes/OpenClaw-Listen
    next: ['j'],
    prev: ['k'],
    open: ['Enter'],
  },
  autoresearch: { // auf dem obersten offenen Vorschlag
    apply: ['a'],
    skip: ['s'],
  },
  palette: {
    next: ['ArrowDown'],
    prev: ['ArrowUp'],
    confirm: ['Enter'],
    close: ['Escape'],
  },
} as const;

/**
 * A11y-Hinweise für die Umsetzung:
 * - Command-Palette: role="dialog", aria-modal, Fokus beim Öffnen ins Input,
 *   Fokus-Trap, Escape schließt, Fokus zurück auf den Trigger.
 * - Listen-Auswahl: aria-selected am aktiven Row, roving tabindex.
 * - LED-Punkte sind dekorativ → aria-hidden; der Status steht zusätzlich als
 *   Text im StatusPill (nie nur Farbe als Bedeutungsträger).
 * - Alle Animationen über prefers-reduced-motion clampen (tokens.css erledigt das global).
 * - Tap-Targets mobil >= 44px; Bottom-Tab-Zellen >= 60px.
 */
