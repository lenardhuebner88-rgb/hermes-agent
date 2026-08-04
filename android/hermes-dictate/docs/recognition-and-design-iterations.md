# Acht Iterationen: Spracherkennung und Design

Stand: 2026-08-04. Diese Runde folgt auf die acht Bubble-Iterationen aus
[bubble-eight-iterations.md](bubble-eight-iterations.md) und hat einen anderen
Schwerpunkt: die Qualität der deutschen Texterkennung und die Bedienbarkeit des
Overlays, nicht seine Silhouette.

## Der Befund, der die Runde ausgelöst hat

`RecognitionQualityGateTest` meldete WER 0.0000 bei 64/64 exakten Treffern.
Dieser Wert misst keine Erkennungsqualität: Korpus und Pipeline sind zusammen
entstanden, der Test ist eine Regressions­sperre. Ein unabhängiger, nachträglich
geschriebener Korpus zeigt den echten Stand.

| Messung | WER | Exakt |
|---|---:|---:|
| Held-out-Korpus gegen die Pipeline vom 04.08. **vor** dieser Runde | **0.1774** | 43/72 |
| Nach Prosa-Sicherung (It 2) | 0.0948 | 55/72 |
| Nach Zahlen/Ordinalzahlen/Uhrzeiten (It 3) | **0.0062** | 71/72 |

Der Korpus liegt in `app/src/test/kotlin/net/hermes/dictate/RecognitionHoldoutTest.kt`
und trägt eine Ratsche, die nur nach unten bewegt werden darf.

## Die Iterationen

| # | Schwerpunkt | Ergebnis |
|---|---|---|
| 1 | Ehrliche Baseline | 72 unabhängige deutsche Fälle in acht Kategorien; echte WER 0.1774 statt der berichteten 0.0000. Kategorie-WER macht sichtbar, wo es klemmt. |
| 2 | Prosa-Sicherung | Die Pipeline löschte Wörter aus normalem Deutsch. Behoben: Korrektur-Marker, Relativsatz-Guard, Partizip-Guard, "im Grunde genommen", Befehlswort-Wiederholungen, Punkt/Komma als Substantiv. |
| 3 | Deutsche Werte | Ordinalzahlen (auch ersten/dritten/siebten/achten), Zahlen über 99, umgangssprachliche Uhrzeiten, Dezimalkomma, PIN/Port mit Kopula. |
| 4 | Diktierbefehle | Undo und "letzten Satz löschen" akzeptieren jetzt die natürlichen Formulierungen; Kollisionsschutz per Test, damit dieselben Wörter im Satz Text bleiben. |
| 5 | Tokensystem | `values/dimens.xml`; alle dp/sp-Literale aus beiden Overlay-Layouts entfernt; Farbtokenbruch in `bg_chip.xml` geschlossen; Icon-Vektoren auf `@color/icon_on_surface`. |
| 6 | Modus sichtbar | Ein Modus-Punkt vor der Statuszeile zeigt dauerhaft, ob auf dem Gerät oder in der Cloud erkannt wird; das "H" wird zum Icon und fügt sich in die Icon-Sprache ein. |
| 7 | Auffindbarkeit | Undo ist eine sichtbare Aktion im Erfolgszustand statt einer unsichtbaren Sprachphrase; die Bubble nennt den Long-Press für Einstellungen; ein erschöpfter Retry sagt das, statt "keine Aktion". |
| 8 | Abnahme | Volles Gate, Emulator-Belege, Doku, APK. |

## Nachtrag: greenfield-Neuschnitt der Pill

Nach den acht Iterationen kam die Operator-Vorgabe, die Pill wie bei Wispr Flow auf das
Wesentliche zu reduzieren — und keine Hermes-Beschriftung darauf. Ergebnis:

- **Die Welle ersetzt Text.** Während das Mikrofon offen ist, zeigt die Pill nur noch
  Abbrechen · Welle · Stopp. Kein Statuswort, kein mitlaufender Text: der erkannte Text
  gehört ins Zielfeld, nicht auf einen schwebenden Chip. Nur Ergebnis- und Fehlerzustände
  tauschen die Welle gegen eine kurze Textzeile.
- **`Waveform` ist reine, getestete Mathematik**; `OverlayWaveView` zeichnet nur. Zwei
  Designfehler fielen erst am echten Render auf und stehen als Test fest:
  die Verjüngung erstickte anfangs das *neueste* Sample am rechten Rand (jetzt klingt nur
  die Historie nach links aus), und ein Per-Schritt-Decay von 0.86 löschte die gesprochene
  Form binnen zwölf Balken zu einer punktierten Linie (jetzt scrollt die Welle ohne Abfall;
  Stille erzeugt die Ausblendung von selbst, weil Stille Nullen schiebt).
- **Der Hermes-Knopf ist aus der Pill verschwunden** und hat in den Einstellungen ein
  sichtbares Zuhause bekommen („Letztes Diktat an Hermes Voice übergeben"), statt als toter
  Code zurückzubleiben.
- **Bubble und App-Icon** wurden mitgezogen: die Bubble bekommt einen akzentfarbenen Ring, der
  im aktiven Zustand voll durchfärbt; das Launcher-Icon ist jetzt dieselbe Welle, die die App
  beim Zuhören zeichnet.

Beleg: `screenshots/wave/wave-listening.png`, `screenshots/app-icon.png`.

## Bewusste Nicht-Behebung

Ein bares "besser" gilt nicht mehr als Korrekturmarker. Damit bleibt
"wir deployen heute besser morgen" unkorrigiert — der einzige verbleibende Fehler
im Held-out-Korpus und einer im alten Gate-Korpus. Die Gegenprobe wog schwerer:
"das läuft besser als gestern" wurde vorher zu "Das als gestern.". Eine
zerstörte normale Aussage ist der teurere Fehler als eine verpasste Korrektur.
Dieselbe Logik trägt den Determinierer-Guard für "Punkt" und den
Relativsatz-Guard für "ich meine".

## Was diese Runde NICHT belegt

- Keine akustische Erkennungsqualität. Alle Zahlen sind Text-in/Text-out über
  modellierte Recognizer-Ausgaben. Ob Whisper oder Androids Recognizer die Wörter
  akustisch richtig hört, ist damit unverändert offen.
- Kein Realgeräte-Test. `scripts/phone.sh` lief auch in dieser Runde nicht.
- Der Held-out-Korpus modelliert Recognizer-Ausgabe mit korrekter deutscher
  Substantiv-Großschreibung. Liefert ein Recognizer klein geschriebene Substantive,
  korrigiert die Pipeline das nicht — sie kennt keine Wortarten.
