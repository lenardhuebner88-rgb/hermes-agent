# Finale Kurzliste: `/Lauch my agents use cases`

1. **Titel:** Ops-Sentinel als read-only Local-Agent
   - **Kategorie:** Agent-Use-Case / Operations / Verifikation
   - **Kurzbeschreibung:** Ein geplanter lokaler Agent prüft täglich oder alle paar Stunden Service-Status, stale Coordination-Check-ins, auffällige Worker-Runs und bekannte Betriebsrisiken wie Cron-Bloat oder memsearch-Last. Er schreibt nur einen kompakten Ampel-Report und greift nicht selbst ein.
   - **Nutzen:** Frühwarnung für genau die Systemzustände, die heute manuell entdeckt werden; hoher Nutzen bei begrenztem Scope, weil der Agent read-only bleibt und vorhandene Evidenzquellen nutzt.
   - **Erster Schritt:** Einen minimalen Local-Agent-Ordner mit zwei Checks bauen: `systemctl --user is-active` für Hermes-Dienste und stale Coordination-Check-ins; Ausgabe als lokale Report-Datei.
   - **Aufwand:** S–M für den Pilot, M für sinnvolle Schwellenwerte und Report-Format.
   - **Risiko:** Alarm-Müdigkeit durch schlechte Schwellenwerte; Autonomie-Risiko, falls später Auto-Restart/Auto-Kill ergänzt wird.
   - **Erfolgsmessung:** Anzahl korrekt gemeldeter gelb/rot-Anomalien pro Woche; weniger ungeplante manuelle Diagnose-Runden; keine false-positive-Flut.

2. **Titel:** Intake-Scoping mit Dispatch-Vorschau
   - **Kategorie:** UX / UI / Agent-Use-Case
   - **Kurzbeschreibung:** Vage Ideen werden zuerst in einen freigabefähigen PlanSpec-Draft mit richtigen Lanes, Acceptance Criteria und Anti-Scope verdichtet; vor dem Start zeigt `/control` eine Vorschau auf Lane, Modell, Subtask-Anzahl, Gate-Stufe sowie grobe Kosten/Laufzeit.
   - **Nutzen:** Weniger Fehlstarts und weniger falsche Agent-/Lane-Zuordnung; Piet sieht vor dem Klick, was tatsächlich gestartet wird, statt erst nach Dispatch zu korrigieren.
   - **Erster Schritt:** Bestehendes ProposalCard-Muster „Was passiert beim Klick?“ auf den Dispatch-Flow übertragen und den Intake-Draft gegen reale Profile/Canon-Schema linten.
   - **Aufwand:** M, weil Backend-Payload/Guard und UI-Panel zusammenspielen müssen.
   - **Risiko:** Vorschau wirkt vertrauenswürdig, obwohl Kosten/Laufzeit nur Schätzungen sind; halluzinierte Profile müssen strikt gegen `~/.hermes/profiles/` verhindert werden.
   - **Erfolgsmessung:** Weniger abgebrochene oder sofort korrigierte Dispatches; geringere Rate an Follow-up-Kommentaren wegen falscher Lane, fehlendem Done-Signal oder unklarem Scope.

3. **Titel:** Recon- und Review-Gate für weniger Naht-Bugs
   - **Kategorie:** Codebasis / Agent-Orchestrierung / Review
   - **Kurzbeschreibung:** Für mittlere oder riskante Code-Tasks erstellt ein read-only `scout` vor der Implementierung einen Grounding-Brief mit betroffenen Dateien, Caller-Map, Mustern und Fallen. Nach dem Diff prüft das Review-Gate gezielt geänderte Bestandssymbole per Caller-Grep und macht NEEDS_REVISION-Follow-ups als echte Fix-Tasks sichtbar.
   - **Nutzen:** Schließt die Lücke „grüne Gates, aber falsche Naht“; Coder starten mit besserem Kontext, Reviewer prüfen regressionsorientierter, und Nacharbeiten werden auditable statt nur als blockierter Kommentar sichtbar.
   - **Erster Schritt:** Pilotregel für riskante Tasks definieren: Scout-Brief vor Claim plus Reviewer-Prompt „Caller-Grep für jedes geänderte Bestandssymbol“; NEEDS_REVISION über den vorhandenen Fix-Task-Helper routen.
   - **Aufwand:** M für Prompt-/Routing-Änderungen und Tests; S für einen manuellen Pilot über wenige Tasks.
   - **Risiko:** Zusätzliche Latenz und Reviewer-Nitpicks; nicht für triviale Ein-Zeilen-Änderungen einsetzen.
   - **Erfolgsmessung:** Weniger REQUEST_CHANGES wegen übersehener Caller; kürzere Review-Runden; Anteil NEEDS_REVISION mit klar verlinktem Fix-Task.

**Empfehlung zuerst:** Den Ops-Sentinel zuerst umsetzen, weil er den Kern von „launch local agents“ am direktesten trifft, read-only bleibt und mehrere reale Betriebsrisiken mit kleinem Pilot adressiert. Danach Intake-Scoping mit Dispatch-Vorschau, weil es die Start-UX der Agentenketten verbessert; das Recon-/Review-Gate lohnt sich parallel für größere Codeketten.

THREE_PROPOSALS_FINAL
PROPOSALS_DECISION_READY
FIRST_NEXT_STEP_SELECTED
