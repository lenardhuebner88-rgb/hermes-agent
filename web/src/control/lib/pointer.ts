/** Grobe Touch-Erkennung: auf Touch-Geräten kein Autofokus in Bottom-Sheets,
 *  sonst schiebt sich die Bildschirmtastatur sofort über das Sheet
 *  (Audit 2026-06-11, F2 — docs/design/control-mobile-audit-2026-06-11/spec.md). */
export function hasFinePointer(): boolean {
  return typeof window !== "undefined" && window.matchMedia("(hover: hover) and (pointer: fine)").matches;
}
