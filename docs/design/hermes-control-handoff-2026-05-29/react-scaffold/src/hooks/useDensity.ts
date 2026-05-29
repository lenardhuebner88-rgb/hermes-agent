/**
 * useDensity — steuert die zentrale „A ↔ B"-Entscheidung.
 *  'airy'    = Richtung A (Bottom-Tabs, luftig)  — Default mobil
 *  'compact' = Richtung B (Rail, Cockpit, dicht) — Default ab lg, oder per Tweak
 *
 * Auflösungsreihenfolge: gespeicherte User-Präferenz > Breakpoint-Default.
 * Persistiert in localStorage (ein Betreiber, keine Server-Settings nötig — falls
 * doch gewünscht, hier gegen einen API-Call tauschen).
 */
import { useCallback, useEffect, useState } from 'react';

export type Density = 'airy' | 'compact';
const KEY = 'hermes.density';
const LG = '(min-width: 1024px)';

function resolveInitial(): Density {
  if (typeof window === 'undefined') return 'airy';
  const saved = window.localStorage.getItem(KEY) as Density | null;
  if (saved === 'airy' || saved === 'compact') return saved;
  return window.matchMedia(LG).matches ? 'compact' : 'airy';
}

export function useDensity() {
  const [density, setDensityState] = useState<Density>('airy');
  const [pinned, setPinned] = useState(false); // hat der Nutzer manuell gewählt?

  useEffect(() => {
    setDensityState(resolveInitial());
    setPinned(!!window.localStorage.getItem(KEY));
  }, []);

  // Breakpoint-Wechsel nur folgen, solange der Nutzer nicht manuell gepinnt hat.
  useEffect(() => {
    if (pinned) return;
    const mq = window.matchMedia(LG);
    const on = () => setDensityState(mq.matches ? 'compact' : 'airy');
    mq.addEventListener('change', on);
    return () => mq.removeEventListener('change', on);
  }, [pinned]);

  const setDensity = useCallback((d: Density) => {
    setDensityState(d);
    setPinned(true);
    window.localStorage.setItem(KEY, d);
  }, []);

  const resetToAuto = useCallback(() => {
    setPinned(false);
    window.localStorage.removeItem(KEY);
    setDensityState(window.matchMedia(LG).matches ? 'compact' : 'airy');
  }, []);

  return { density, setDensity, resetToAuto, pinned };
}
