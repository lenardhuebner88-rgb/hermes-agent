# Tab "Verbrauch" — Entwurfsentscheidung (D1)

Drei genuinely verschiedene Richtungen, skizziert vor dem ersten Code.
Bewertungsmaßstab (stehende Entscheidung 4): (a) beantwortet die erste
Sekunde „wo geht mein Geld hin", (b) zeigt ehrlich, wie belastbar die Zahl
ist, (c) führt zum nächsten Schritt.

## Richtung A — „KPI-Wand + Chart-Grid" (die sichere Variante)

Oben vier KPI-Kacheln (Äquivalent 7d/30d, Ersparnis, Cache-Rate), darunter
ein großer Tagesbalken-Chart, darunter eine Breakdown-Tabelle, ganz unten
die Hebel-Liste. Leseführung: Zahl → Verlauf → Tabelle → Handlung.

- **Kann gut:** sofort lesbar, wenig Erklärung nötig, nahe an Statistik.
- **Opfert:** die erste Sekunde zeigt Zahlen ohne Richtung; die Hebel — die
  eigentliche Frage des Tabs — stehen als Fußnote unten. Genau das, was D5
  verbietet. Verworfen: wäre „die sicherste Variante, weil sie die sicherste
  ist".

## Richtung B — „Geldfluss"

Ein Sankey-artiger Strom über drei Spalten: Quelle (Origin/Unit) → Modell →
Kosten; Hebel als eingezeichnete Drosseln am jeweiligen Fluss. Die erste
Sekunde zeigt das Geld als Fluss, nicht als Zahl.

- **Kann gut:** beantwortet „wo geht das Geld hin" bildlich am stärksten;
  Flussbreite = Betrag ist intuitiv.
- **Opfert:** die Zeitreihe (Eskalation unsichtbar); auf Tablet-Breite
  unlesbar (drei Spalten mit Labels); Tastatur-Navigation durch ein SVG-
  Flussdiagramm ist Flickwerk; Unsicherheit (Abdeckung) ist in Flussbreiten
  nicht ehrlich darstellbar — ein 70%-gedeckter Fluss müsste schmaler
  wirken, als er ist. Verworfen: scheitert an (b) und an D4 (Tablet,
  Tastatur).

## Richtung C — „Tageslichtung" (gewählt)

Oben die **Hebel-Sektion als Held**: die gerechneten Sparhebel als große
Karten mit Betrag, Gegenrechnung (Ist vs. Gegen-Szenario als Doppelbalken)
und aufklappbarer Annahme samt Plausibilitätsdaten. Darunter **Small
Multiples**: je Origin ein eigener Tages-Chart mit eigener Beschriftung
(Name + Betrag direkt am Chart, keine Legende), Abdeckung als Sättigung
der Balken. Darunter Breakdown- und Fenster-Umschalter, Komponenten-
Gegenüberstellung (Token- vs. Kostenanteil nebeneinander) und die
Top-Läufe mit Sprüngen.

- **Kann gut:** erste Sekunde = „die eine Änderung mit dem größten Effekt"
  (D5 erfüllt); Small Multiples machen den Vergleich der Quellen zur
  eigentlichen Frage, statt einen überladenen Gesamt-Chart zu stapeln;
  Abdeckung ist pro Serie als Sättigung + Zahl ehrlich sichtbar (b);
  jede Karte führt zum nächsten Schritt (Sprung zur Task/Session) (c).
- **Opfert:** kein einziger heroischer Gesamtverlauf; Small Multiples
  brauchen vertikalen Raum (auf Compact kollabieren sie zu einer Liste);
  die Hebel-Karten verlangen dem Backend verlässliche Gegenrechnungen ab —
  ein Hebel ohne Gegenrechnung erscheint gar nicht (vertraglich so
  abgesichert).

**Wahl: C.** Begründung: D5 macht die Hebel-Sektion verbindlich zum Held;
C ist die einzige Richtung, die das strukturell erfüllt statt dekorativ.
B wäre die mutigere Grafik, bricht aber (b) und D4. A erfüllt den Brief
nicht (Hebel unten).

### Regelbrüche, benannt (DESIGN.md „You may break a rule")

- Keine. Kontrast-Boden und Hex-Ratchet bleiben unangetastet; Bronze nur
  interaktiv (Switcher, Links, Retry), Serienfarben ausschließlich
  `--color-data-1..7`, Balken sind statische SVGs (keine Motion, damit
  nichts zu killen).
