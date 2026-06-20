# UI/UX-Verbesserungskandidaten — Hermes /control

> Sammlung für Task t_d3bde69f. Keine UI gebaut — nur Vorschläge, geerdet an realen
> Screens/Strings des `/control`-SPA (`web/src/control`). Belege als `Datei:Zeile`.
> Zielnutzer durchweg: **Piet als Operator** (oft Handy/Tablet), der `/control` liest,
> ohne den Code/das System-Vokabular zu kennen.

---

## 1 — Konsistentes Deutsch: Englisch-Strings, GitHub-Jargon & kaputte Umlaute raus

- **Zielnutzer/Situation:** Operator liest eine Review- oder Worker-Karte im Flow-/Inbox-Tab.
- **Schmerzpunkt (sichtbare Zustände):**
  - `components/HermesReviewCard.tsx:51` — der ToneCallout zeigt **komplett Englisch**:
    „Verifier verdict is not available yet; the task is still in review."
  - `i18n/de.ts:199` / `:472` — roher GitHub-Jargon im UI: „REQUEST_CHANGES", „Reopen",
    „NEEDS_REVISION".
  - `i18n/de.ts:470` „Verd**ae**chtige Referenzen" und `:472` „Verifier-L**ae**ufen" —
    **kaputte Umlaute** (ae statt ä/äu) → wirkt unfertig/unprofessionell.
  - `i18n/de.ts:507` „Claim-TTL abgelaufen", `:482`/`:506` „Heartbeat" — interne Begriffe
    ohne Klartext.
- **Vorgeschlagene Änderung:** zentrale `de.ts`-Politur + die eine hartcodierte Englisch-Zeile
  aus `HermesReviewCard` nach `de.ts` ziehen. Glossar-Mapping: REQUEST_CHANGES→„Änderungen
  angefordert", Reopen→„erneut öffnen", Heartbeat→„Lebenszeichen", Claim-TTL→„Reservierung
  abgelaufen", Umlaute reparieren. Für unvermeidbare Fachbegriffe Tooltip/Inline-Erklärung.
- **Erwarteter UX-Gewinn:** Status ohne GitHub-/Systemwissen verständlich; durchgängig
  professionelles Deutsch; keine „kaputt"-Optik.
- **Aufwand:** **S** — i18n-Strings + 1 Komponentenzeile + `de.test.ts`-Assertion; keine Logik.
- **Messbares Erfolgssignal:** `grep -E "REQUEST_CHANGES|Reopen|NEEDS_REVISION|Heartbeat|Claim-TTL|Laeufen|Verdaechtig"`
  über sichtbare UI-Strings (`de.ts` + Komponenten-JSX) liefert **0 Treffer**; Lint/Snapshot grün;
  visuelle Abnahme Review-/Worker-Karte zeigt deutsche Begriffe.

## 2 — Blockierte-Abschluss-Karte: von „Was ist passiert" zu „Was tue ich jetzt"

- **Zielnutzer/Situation:** Operator sieht eine vom Anti-Halluzinations-Gate abgelehnte Karte
  (`components/HermesBlockedCard.tsx`).
- **Schmerzpunkt (sichtbare Zustände):** `i18n/de.ts:478` „Der **Kernel** hat den Abschluss
  abgelehnt: angeblich erstellte Karten existieren nicht." — „Kernel" unerklärt. Phantom-IDs
  als rote Chips (`HermesBlockedCard.tsx:67–77`) und „Was ist zu fixen" (`:60–64`) sind rein
  **beschreibend** — es gibt **keine Aktion auf der Karte**; der Operator muss nach Kanban wechseln.
- **Vorgeschlagene Änderung:** Karten-Footer mit klarer nächster Aktion: Button
  „Erneut versuchen", „An Operator eskalieren", „Verwerfen" (mit Bestätigung). „Kernel"→
  „Abschluss-Prüfung"; Phantom-IDs mit Mikro-Erklärung „diese Karten-IDs wurden behauptet,
  existieren aber nicht".
- **Erwarteter UX-Gewinn:** von Diagnose zu Entscheidung in **1 Klick** statt Tab-Wechsel;
  weniger Reibung beim Aufräumen blockierter Worker.
- **Aufwand:** **M** — UI-Action + Endpoint-Anbindung (reopen/dismiss) + Bestätigungsdialog.
- **Messbares Erfolgssignal:** Anteil blockierter Abschlüsse, die direkt aus `/control` (statt
  via Kanban-CLI/Deep-Link) aufgelöst werden, steigt; Zeit „blockiert sichtbar → aufgelöst"
  sinkt (Run-Status-Übergänge im Event-Log).

## 3 — Dispatch-Vorschau: „Was passiert beim Start?" vor dem Auslösen

- **Zielnutzer/Situation:** Operator dispatcht eine Aufgabe/Kette im Flow-Tab über das
  Capture-Sheet (`i18n/de.ts:243–292`).
- **Schmerzpunkt (sichtbare Zustände):** Es gibt Methode/Gate-Optionen (`de.ts:248–258`) und
  einen „Prüfe…"-Spinner (`dispatchChecking`, `de.ts:286`), aber **vor dem Klick** sieht der
  Operator nicht konkret: Ziel-Lane, Modell, Subtask-Anzahl, Gate-Stufe, grobe Kosten/Laufzeit.
  Unsicherheit „was genau löse ich aus?". Das Muster existiert bereits woanders —
  `components/ProposalCard.tsx:279–296` zeigt „Was passiert beim Klick?" — Dispatch hat es nicht.
- **Vorgeschlagene Änderung:** Dispatch-Bestätigung um eine kompakte Vorschau erweitern (analog
  ProposalCard): Ziel-Lane + Modell + erwartete Subtask-Anzahl + Gate-Tier + grobe Kosten-/
  Laufzeitschätzung, bevor „Kette starten"/„Dispatch" final ist. Payload aus dem vorhandenen
  Dispatch-Guard (`views/FlowView.tsx:287–292`) ableitbar.
- **Erwarteter UX-Gewinn:** weniger Fehlstarts/sofortige Abbrüche, mehr Vertrauen, klare
  Erwartung — direkt auf „weniger Reibung beim Starten/Verstehen von Agenten".
- **Aufwand:** **M** — Vorschau-Payload zusammenstellen + UI-Panel; ProposalCard-Muster
  wiederverwendbar.
- **Messbares Erfolgssignal:** Rate sofort wieder abgebrochener/zurückgenommener Dispatches
  (Run-Cancel in den ersten N Sekunden) sinkt; weniger „falsche Lane/Modell"-Korrekturen nach Start.

## 4 — Worker-Status: eine Klartext-Zustandszeile statt nur Marker dechiffrieren

- **Zielnutzer/Situation:** Operator beobachtet laufende Worker (`components/WorkerCard.tsx:299–404`,
  Zeitachse mit p50/p90/Budget-Markern).
- **Schmerzpunkt (sichtbare Zustände):** Status ist über Farb-Dots + Mini-Bar + Achsen-Marker
  kodiert; Begriffe wie „p50/p90", „Budget %", „Entgleisungsrisiko" (`de.ts:523–526`) brauchen
  Interpretation. Es fehlt eine **Klartext-Zeile auf einen Blick**; die Stuck-Begründung mischt
  Fachbegriffe: `de.ts:506` „Heartbeat ${age} alt oder Claim abgelaufen".
- **Vorgeschlagene Änderung:** pro Worker-Karte eine einzeilige abgeleitete Klartext-Statuszeile,
  z.B. „Läuft normal · ~3 min Rest" / „Ungewöhnlich lange — ggf. entgleist" / „Kein Lebenszeichen
  seit 4 min — Prozess prüfen". p50/p90 als Tooltip („typische/lange Laufzeit"), nicht als rohe Labels.
- **Erwarteter UX-Gewinn:** sofortige Orientierung ohne Marker-Dechiffrieren; klare Statusanzeige
  (Kern-Fokus der Aufgabe).
- **Aufwand:** **S–M** — abgeleiteter Text aus vorhandenen Feldern (Laufzeit, Budget, p50/p90,
  Heartbeat-Alter) + Tooltip; keine neuen Backend-Daten.
- **Messbares Erfolgssignal:** Unit-Snapshot deckt alle 4 Zustände (normal/warn/entgleist/offline)
  mit eindeutiger Klartextzeile ab; qualitativ: Eingriffe auf entgleiste Worker erfolgen früher,
  Fehl-Eingriffe auf normale Worker seltener.

## 5 — Leerzustände mit einem Klick zum nächsten sinnvollen Schritt

- **Zielnutzer/Situation:** Operator öffnet einen noch leeren Tab (frischer Tag, keine Läufe).
- **Schmerzpunkt (sichtbare Zustände):** Leerzustände sind heute reine Text-Hinweise:
  `de.ts:222–223` „Noch keine Läufe" + „Sobald in Kanban Aufgaben angelegt … erscheinen sie hier";
  `de.ts:173–174` „Keine aktiven Worker"; `de.ts:177–178` „Alles freigegeben". Sie erklären das
  *Warum*, bieten aber **keinen Weg vorwärts** — der Operator muss selbst wissen, dass er ins
  Flow-/Capture-Sheet muss.
- **Vorgeschlagene Änderung:** jeden zentralen Leerzustand um eine primäre Aktion ergänzen:
  „Keine aktiven Worker" → Button „Aufgabe starten" (öffnet Capture-Sheet); „Noch keine Läufe" →
  „Neue Kette planen". Konsistentes Muster: Icon + ein Satz + eine Aktion.
- **Erwarteter UX-Gewinn:** leere Tabs werden vom Sackgassen-Text zum Einstiegspunkt — direkt
  „weniger Reibung beim Starten".
- **Aufwand:** **S** — vorhandene Capture/Dispatch-Trigger an Leerzustände hängen; keine neue Logik.
- **Messbares Erfolgssignal:** Klickpfad „leerer Tab → Aufgabe gestartet" von ≥2 Navigations­
  schritten auf **1** reduziert; Anteil Sessions, die aus einem Leerzustand heraus eine Aktion
  starten, steigt.

---

### Priorisierung (Empfehlung)
Quick Wins zuerst: **#1** (reines i18n, sofort sichtbarer Qualitätssprung) und **#5** (Leerzustände,
kleiner Hebel, großer Orientierungsgewinn). Danach #4 (Klartext-Status), dann die M-Kandidaten
#3 (Dispatch-Vorschau) und #2 (blockierte Karte mit Aktion), die Endpoint-/Logik-Arbeit brauchen.
