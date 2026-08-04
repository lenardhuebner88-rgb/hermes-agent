# Bubble: acht dedizierte Iterationen

Stand: 2026-08-04. Jede Runde lief durch `:app:compileDebugKotlin`,
`:app:testDebugUnitTest` und `:app:connectedDebugAndroidTest`. Die PNGs stammen
vom Android-Emulator; der Debug-Harness rendert das unveränderte
Produktionslayout `overlay_pill.xml` mit deterministischen Zuständen.

| # | Schwerpunkt | Ergebnis | Selbstbewertetes Artefakt |
|---|---|---|---|
| 1 | Zustandsmodell | Status, Transkript, Ton und Aktionen kommen aus einem vollständigen, reinen Präsentationszustand. | [State model](screenshots/iterations/iteration-1-state-model.png): klare Zweizeilen-Hierarchie; Hermes noch zu dominant. |
| 2 | Gesten | Tap, Drag, Long-Press und Cancel werden mit Android-Touch-Slop eindeutig getrennt. | [Gesture arbitration](screenshots/iterations/iteration-2-gesture-arbitration.png): stabiler Processing-Zustand; keine visuelle Sprungstelle. |
| 3 | Platzierung | Systemleisten, Bildschirmränder, gespeicherte Y-Position und Edge-Snap werden geklemmt. | [Inset placement](screenshots/iterations/iteration-3-inset-placement.png): Bubble mit erkennbarem Randabstand, vollständig sichtbar. |
| 4 | Geometrie und Pegel | Responsive Breite, feste 72-dp-Höhe und ein echter proportionaler 0–100-Pegel. | [Responsive geometry](screenshots/iterations/iteration-4-responsive-geometry.png): Pegel entspricht 68 %, Text bleibt zweizeilig. |
| 5 | Fokus-Sicherheit | Ein Commit ist nur in exakt dasselbe, weiterhin aktive Feld erlaubt; Fokusverlust bleibt sichtbar. | [Focus safety](screenshots/iterations/iteration-5-focus-safety.png): Fehler ist ruhig und lesbar; das damalige Retry-Häkchen war noch falsch. |
| 6 | Accessibility und Aktionen | 48-dp-Ziele, polite Live-Status, volle Accessibility-Kopie, korrekte Enable-States sowie Stop-/Retry-/Copy-Symbole. | [Accessibility actions](screenshots/iterations/iteration-6-accessibility-actions.png): Retry ist eindeutig, deaktiviertes Hermes tritt zurück. |
| 7 | Motion | Einmaliger 140-ms-Einstieg, skaliert mit Androids Animator-Einstellung; bei Reduced Motion sofort stabil. | [Reduced motion](screenshots/iterations/iteration-7-reduced-motion.png): ruhiger Erfolg ohne Daueranimation; Hermes war noch zu schwer. |
| 8 | Visueller Feinschliff | Dunkle Idle-Bubble, sekundäre Hermes-Outline, 12-dp-Rand, 384-dp-Maximalbreite, mehr Textfläche und wortsauberes Live-Endsegment. | [Final bubble](screenshots/iterations/iteration-8-final-bubble.png) und [final pill](screenshots/iterations/iteration-8-final-pill.png): klare Priorität Status → Text → Stop; neuestes Wort sichtbar. |

## Visuelles Urteil

- Die Idle-Bubble ist jetzt als Werkzeug präsent, ohne mit einer vollflächig
  violetten Taste über fremden Apps zu dominieren.
- In der Pill bildet die farbige Statuszeile die Orientierung, der weiße Text
  den Inhalt und der untere Pegel ausschließlich die Lautstärke. Farbe ist nie
  der einzige Zustandsträger.
- Abbrechen und Stop bleiben sofort erreichbar. Die optionale Übergabe an
  Hermes ist bewusst kleiner gezeichnet, behält aber ihr 48-dp-Touchziel.
- Der erste finale Langtext-Screenshot verlor trotz korrekter Endsegment-Logik
  das letzte lange Wort. Das sichtbare Fenster wurde von 72 auf 54 Zeichen
  reduziert und erneut gerendert; „Entscheidungen“ ist nun vollständig sichtbar.
- Die statische [Designvorschau](design-preview.html) wurde zusätzlich bei
  390×844 und 1280×900 geprüft. Nach einer gefundenen CSS-Überlagerung wurden
  Status- und Pegelfarbe getrennt; der zweite Lauf meldete keinen verdeckten
  Text und keinen horizontalen Überlauf.

## Prüfergebnis

- Unit: 194 Tests, grün.
- Emulator: 2 Instrumentationstests, grün.
- Der Emulator-Setup-Pfad wurde aus vollständig deaktiviertem Accessibility-
  Zustand geprüft; `device.sh prepare` stellt Dienst, IME und Mikrofon wieder her.
- Bekannte Grenze: Der Emulator-Harness prüft Layout und Zustandslogik
  deterministisch. Akustische Erkennungsqualität wird separat durch den
  deutschen Korpus und die WAV-Injection belegt, nicht durch diese UI-Aufnahmen.
