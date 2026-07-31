# Fleet Cockpit: vier Iterationen von 03/10 auf 07/10

Stand: 2026-07-30

Dieses Dokument ist ein Produkt- und Lieferplan, kein neues visuelles Design.
Kimi bearbeitet parallel die Dashboard-UI. Deshalb werden UI-Dateien erst nach
Integration ihres Diffs und mit einem freigegebenen Mockup oder einer präzisen
Visual-Spezifikation geändert. Der neue Backend-Vertrag
`GET /api/plugins/kanban/stats/fleet-metrics` ist dafür die additive Datenbasis.

## Bewertungsmodell

Die Cockpit-Reife wird nicht aus Pixelpolitur allein abgeleitet. Jede Dimension
zählt gleich stark:

| Dimension | 03/10 heute | Ziel 07/10 |
|---|---:|---:|
| Datenvertrauen | Einzelwerte vorhanden, Lücken schwer einzuordnen | Coverage, Freshness und unknown an jeder entscheidenden Kennzahl |
| Preis-Leistung | Kosten und Tokens getrennt sichtbar | Aufgabe, Kette und Modell vergleichbar; Ist-Kosten und API-Äquivalent getrennt |
| Zuverlässigkeit | Ergebnis-/Laufzeitdaten verteilt | Retry, Queue, TTFT, Laufzeit und Sentinel in einer belegbaren Kette |
| Ergebnisqualität | Reviewdaten vorhanden, nicht als Reibung lesbar | Freigaben, Request-Changes und Nacharbeitsrunden pro Aufgabe/Kette |
| Handlungsfähigkeit | Beobachten ohne klare nächste Aktion | priorisierte Alerts mit Ursache, Schwelle und überprüfbarer Auflösung |

Eine Iteration gilt erst als erreicht, wenn Datenvertrag, Darstellung,
Interaktion, Tests und Betriebsnachweis gemeinsam grün sind.

## Iteration 1 — Vertrauensboden, 03/10 → 04/10

Ziel: Jede Kernzahl beantwortet sofort „wie viel wissen wir?“

Backend:

- Token- und API-Äquivalent-Rollups pro exakter Aufgabe, Kette und
  Provider/Modell.
- Task-/Kettenwerte nur bei belegter `task_run_id`; Task-only-Historie bleibt
  als Lücke sichtbar.
- Coverage-Zähler für Tokens, Modell, exakte Run-Zuordnung und Kette.
- Freshness je Bucket sowie explizite unknown-Zähler.
- Separate Abrechnung: tatsächliche metered Kosten, Abo-Grenzkosten und
  API-Vergleichskosten.

UI nach Mockup-Freigabe:

- Eine kompakte Vertrauenszeile oberhalb jeder Tabelle: „bekannt / gesamt“,
  Aktualität und unbekannte Anteile.
- Keine `0`, wenn Preis, Tokenfeld oder Zuordnung fehlt.
- Filter Task / Kette / Modell mit identischer Kennzahlenlogik.

Abnahme:

- Stichprobe aus mindestens einer Hermes-, Claude-, Grok- und Qwen-Quelle.
- Summe der sichtbaren bekannten und unbekannten Anteile entspricht dem
  Denominator.
- Ein Task-only-Historienfall erscheint nicht in einem exakten Taskpreis.
- Mobile 390×844 und Desktop 1280×900 ohne abgeschnittene Coverage-Hinweise.

## Iteration 2 — Wartezeit und Zuverlässigkeit, 04/10 → 05/10

Ziel: Das Cockpit zeigt, wo ein Auftrag Zeit verliert und welche Lücke
technisch behebbar ist.

Backend:

- Provider-/Modell-Coverage mit zwei Denominatoren:
  `all_sources` und `eligible_sources`.
- Maschinenlesbarer Telemetrievertrag je Ursprung. „Quelle liefert TTFT nicht“
  ist `not_applicable`, nicht fehlgeschlagene Messung.
- Queue→Claimed, Claim→Start, Request→First Token und Run→Ende aus exakt
  gespeicherten Lifecycle-Punkten.
- Retry-Quote und Klassen (`auto`, `integration`, `transient`, `operator`).

UI nach Mockup-Freigabe:

- Ein Wartezeit-Funnel mit den vier gemessenen Abschnitten; fehlende Abschnitte
  bleiben offen markiert.
- Providervergleich zeigt Messwert, eligible Coverage und Source-Vertrag
  gemeinsam.
- Retry-Spitzen lassen sich bis Klasse und betroffene Kette aufklappen.

Abnahme:

- Claude CLI senkt `all_sources`-TTFT, aber nicht den
  `eligible_sources`-Denominator.
- Ein Provider mit vollständiger Hermes-Streaming-Telemetrie erreicht bei
  neuen erfolgreichen Läufen 100 Prozent eligible TTFT/Laufzeit.
- Jeder Prozentwert trägt Zähler, Nenner und Zeitraum.
- Kein Latenzwert wird aus Transcript-Zeitstempeln geschätzt.

## Iteration 3 — Frühwarnung und Sentinel, 05/10 → 06/10

Ziel: Aus passiver Statistik wird ein betrieblich nutzbares Kontrollinstrument.

Backend/Betrieb:

- Alerts für Datenalter, Sentinel-Ausfall, Retry-Spitze, Queue-Stau und robuste
  Task-Kosten-Ausreißer.
- Jeder Alert liefert Status, Beobachtung, Denominator, Schwelle und Grund.
- Wöchentlicher content-freier E2E-Sentinel mit ISO-Wochen-Idempotenz,
  Einzelinstanz-Lock und exaktem Langfuse-/Usage-/Lifecycle-/Terminal-Nachweis.
- Timer erst nach expliziter Operatorfreigabe aktivieren.

UI nach Mockup-Freigabe:

- Ein Alertband priorisiert `critical`, `warning`, `unknown`, dann `ok`.
- Jeder Alert führt zu einer gefilterten Detailansicht und zeigt seine
  Berechnungsregel.
- „Unknown“ ist eine eigene Arbeitswarteschlange, kein grauer Erfolgszustand.

Abnahme:

- Ein künstlich veralteter Status, ein Retry-Sample unter Mindestgröße und ein
  echter Schwellenübertritt ergeben drei verschiedene Zustände.
- Der Sentinel ist fail-closed: fehlender exakter Trace oder Terminalbeleg
  ergibt keinen grünen Status.
- Wiederholter Timerlauf derselben ISO-Woche erzeugt keine zweite Karte.
- Betriebshandbuch beschreibt Aktivierung, Diagnose und Rückbau.

## Iteration 4 — Qualität und Preis-Leistung, 06/10 → 07/10

Ziel: Das Cockpit unterstützt die Entscheidung, welches Modell für welche
Arbeit tatsächlich den besten Gegenwert liefert.

Backend:

- Review-Freigaben, Request-Changes, Nacharbeitsrunden und Approval-Rate je
  Aufgabe und Kette.
- Preis-Leistungsansichten kombinieren API-Äquivalent, normalisierte Tokens,
  Laufzeit, Erfolg und Review-Ergebnis—ohne daraus einen undurchsichtigen
  Einheits-Score zu erfinden.
- Mindest-Sample und Coverage-Gates vor jedem Modellvergleich.
- Vergleichszeiträume und Sortierung stehen im Payload; gekürzte Toplisten sind
  als `truncated` markiert.

UI nach Mockup-Freigabe:

- Modellmatrix mit Kosten, Tokens, Erfolg, TTFT/Laufzeit und Nacharbeit.
- Defaultsortierung nach einem explizit gewählten Ziel: günstig, schnell,
  zuverlässig oder wenig Nacharbeit.
- Detailansicht erklärt, warum ein Modell nicht vergleichbar ist.
- Eine klare „nächste Entscheidung“ pro Blick: Route beibehalten, beobachten
  oder mit begrenztem Sample testen.

Abnahme:

- Kein Modell-Ranking bei weniger als dem definierten Mindest-Sample oder
  unvollständiger Preis-/Ergebnis-Coverage.
- Abo-Grenzkosten und API-Äquivalent stehen niemals in derselben Spalte.
- Request-Changes werden als Reibung gezeigt, Freigaben nicht als alleiniger
  Qualitätsbeweis.
- Nutzertest: Ein Operator kann innerhalb von zwei Minuten eine teure,
  langsame oder nacharbeitsintensive Route identifizieren und die zugrunde
  liegenden Runs prüfen.

## Gemeinsamer Lieferstandard

Für jede Iteration:

1. Backendvertrag und Fixture zuerst.
2. Freigegebenes Mockup beziehungsweise präzise Visual-Spezifikation.
3. UI-Implementierung ohne Abweichung vom Datenvertrag.
4. Targeted Backend- und Frontend-Gates.
5. Visuelle Prüfung bei 390×844 und 1280×900.
6. Ein realer, content-freier Sentinel- oder Fixture-Nachweis.

Damit ist 07/10 kein Designurteil, sondern ein überprüfbarer Zustand:
vertrauenswürdige Zahlen, vergleichbare Preis-Leistung, sichtbare Reibung und
konkrete Handlungswege.
