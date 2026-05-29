/**
 * Diff-Verarbeitung.
 * Im Prototyp ist `diff_before_after` bereits als Zeilen-Array modelliert
 * ({ type:'ctx'|'add'|'del', text }). Liefert die echte API stattdessen einen
 * rohen unified-diff-String, wandelt `parseUnifiedDiff` ihn in dasselbe Modell —
 * so rendert die DiffView-Komponente in beiden Fällen identisch.
 */
import type { DiffLine } from './types';

/**
 * Parst einen unified-diff (git/diff -u). Header-Zeilen (--- / +++ / @@ / diff)
 * werden verworfen; nur Inhaltszeilen werden klassifiziert.
 */
export function parseUnifiedDiff(raw: string): DiffLine[] {
  const out: DiffLine[] = [];
  for (const line of raw.split('\n')) {
    if (
      line.startsWith('+++') || line.startsWith('---') ||
      line.startsWith('@@') || line.startsWith('diff ') ||
      line.startsWith('index ')
    ) continue;
    if (line.startsWith('+')) out.push({ type: 'add', text: line.slice(1) });
    else if (line.startsWith('-')) out.push({ type: 'del', text: line.slice(1) });
    else out.push({ type: 'ctx', text: line.startsWith(' ') ? line.slice(1) : line });
  }
  // Führende/abschließende Leer-Kontextzeilen trimmen
  while (out.length && out[0].type === 'ctx' && out[0].text === '') out.shift();
  while (out.length && out[out.length - 1].type === 'ctx' && out[out.length - 1].text === '') out.pop();
  return out;
}

/** Robust: nimmt entweder das fertige Array oder einen rohen String. */
export function toDiffLines(input: DiffLine[] | string): DiffLine[] {
  return typeof input === 'string' ? parseUnifiedDiff(input) : input;
}

/** Zeilennummer-Gutter (für die Cockpit-Variante B). Nur add/ctx werden gezählt. */
export function withLineNumbers(lines: DiffLine[]): Array<DiffLine & { ln: number | null }> {
  let n = 0;
  return lines.map((l) => {
    if (l.type !== 'del') { n += 1; return { ...l, ln: n }; }
    return { ...l, ln: null };
  });
}

export interface DiffStats { added: number; removed: number; }
export function diffStats(lines: DiffLine[]): DiffStats {
  return {
    added: lines.filter((l) => l.type === 'add').length,
    removed: lines.filter((l) => l.type === 'del').length,
  };
}
