import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const css = readFileSync(new URL("../jarvis.css", import.meta.url), "utf8");

/**
 * Repro-Guard für den abgeschnittenen Senden-Button auf Mobile.
 *
 * Ursache (Research-Handoff t_842e4dcc): bei 360–430 CSS px lief die einzeilige
 * Frag-Leiste rechts über — die fünf nicht schrumpfbaren Controls (4×44px +
 * 52px Senden) plus ein Attachment-Chip passten nicht in ~312px, der Senden-
 * Button lag außerhalb. jsdom rendert kein Layout, daher ist der belastbare
 * automatisierte Nachweis, dass der Composer im Mobile-Media-Block auf ein
 * zweizeiliges Layout umgestellt ist: Icon-Leiste (inkl. Senden) als eigene
 * volle Zeile → kein horizontaler Overflow. Die echte Pixel-/PWA-Abnahme bei
 * 390px bleibt der visuellen Operator-Verifikation überlassen.
 */
describe("Mobile Composer: kein abgeschnittener Senden-Button (Zweizeilen-Layout)", () => {
  it("stellt die Frag-Leiste mobil auf Umbruch (zweizeilig statt Overflow)", () => {
    // Diese jv-ask-Regel mit flex-wrap existiert nur im Mobile-Media-Block —
    // der Desktop-Composer bleibt die einzeilige Pille.
    expect(css).toMatch(/\.jv-ask\s*\{[^}]*flex-wrap:\s*wrap/);
  });

  it("legt die volle Icon-Leiste (inkl. Senden) auf eine eigene Zeile", () => {
    // flex-basis 100% zwingt .jv-icons in eine eigene, volle Zeile — der 52px
    // Senden-Button bleibt damit vollständig sichtbar und tappbar.
    expect(css).toMatch(/\.jv-ask\s+\.jv-icons\s*\{[^}]*flex:\s*1\s*0\s*100%/);
    expect(css).toMatch(/\.jv-ask\s+\.jv-icons\s*\{[^}]*justify-content:\s*space-between/);
  });
});
