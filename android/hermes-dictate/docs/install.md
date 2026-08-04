# Hermes Diktat installieren und benutzen

## Herunterladen

Im Tailnet, direkt vom Telefon:

    https://huebners.tail50819a.ts.net/hermes-dictate.apk

Die Datei zeigt immer den zuletzt veröffentlichten Build. Die Prüfsumme liegt daneben:

    https://huebners.tail50819a.ts.net/hermes-dictate.apk.sha256

## Einrichten (einmalig)

1. APK öffnen und Installation aus unbekannter Quelle für den Browser erlauben.
2. App starten — sie führt durch vier Schalter, jeder zeigt seinen Zustand:
   - **Mikrofon** erlauben.
   - **Tastatur aktivieren** und als Eingabemethode auswählen (Fallback-Weg).
   - **Bedienungshilfe aktivieren** — das ist der eigentliche Weg: nur damit kann die Blase
     über anderen Apps schweben und Text ins fokussierte Feld schreiben.
   - **Cloud-Erkennung** ist optional und standardmäßig aus. Ohne sie bleibt jedes Wort auf
     dem Gerät.
3. Optional im **Wörterbuch** eigene Begriffe hinterlegen, eine Regel pro Zeile:

       plan spec => PlanSpec
       kanban board => Kanban Board

## Benutzen

- In ein beliebiges Textfeld tippen — die **Blase** erscheint am Rand.
- **Tippen** startet das Diktat: die Blase wird zur Pill mit drei Elementen —
  **✕ abbrechen · die Welle · ■ stoppen**. Sonst nichts; der erkannte Text landet direkt
  im Feld, nicht auf der Pill.
- **Ziehen** verschiebt die Blase, **langes Drücken** öffnet die Einstellungen.
- Nach dem Einfügen zeigt die Pill kurz **Fertig** mit einem **Rückgängig**-Knopf. Der nimmt
  genau den eingefügten Text wieder heraus — und nur in dem Feld, in das er geschrieben wurde.

## Diktieren auf Deutsch

Gesprochene Satzzeichen und Werte werden umgesetzt:

| Gesagt | Im Feld |
|---|---|
| „Hallo Welt Punkt" | Hallo Welt. |
| „ist der Build grün Fragezeichen" | Ist der Build grün? |
| „erste Zeile neue Zeile zweite Zeile" | zwei Zeilen |
| „am ersten August" | am 1. August |
| „um viertel nach zehn" | um 10:15 Uhr |
| „um halb drei" | um 2:30 Uhr |
| „zweihundertfünfzig Euro" | 250 Euro |
| „wir starten Montag nein Dienstag" | Wir starten Dienstag. |

„Punkt" und „Komma" nach einem Artikel bleiben Substantive — „wir bringen es auf den Punkt"
behält sein letztes Wort.

**Sprachbefehle** (nur wenn sie die ganze Äußerung sind):

- Zurücknehmen: „streich das", „vergiss das", „lösch das", „nein zurück", „das war falsch"
- Letzten Satz löschen: „letzten Satz löschen", „streich den letzten Satz"

Dieselben Wörter mitten im Satz bleiben normaler Text — „streich das Meeting am Montag"
wird geschrieben, nicht ausgeführt.

## Was die App nicht tut

- Kein Diktat in Passwortfeldern und in Banking-Apps.
- Kein Netzwerk ohne ausdrückliches Cloud-Opt-in pro Diktat.
- Kein Protokollieren von Audio oder erkanntem Text.
