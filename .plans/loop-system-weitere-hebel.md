---
title: "Loop-System: Weitere Hebel für Autonomie, Verlässlichkeit und Selbstkorrektur"
freigabe: draft
live_test_depth: contract
acceptance_criteria:
  - applies_to: [AGENT-TIMEOUT]
    text: "Neuer Turn-Wall-Clock-Timeout ist konfigurierbar, default-off/konservativ, und bricht Runaway-Turns kontrolliert ab."
  - applies_to: [STUCK-DETECTOR]
    text: "Wiederholte identische Fehler/Tool-Calls werden erkannt und mit einem Recovery-Nudge behandelt."
  - applies_to: [CI-GATES]
    text: "Format-Gate und HOME-Hartkodierung-Check laufen in CI und blocken bei Verstößen."
  - applies_to: [DB-HEALTH]
    text: "SessionDB führt beim Öffnen einen proaktiven Gesundheitscheck durch und repariert/reported bei Bedarf."
taskgraph_hints:
  binding: true
  subtasks:
    - id: AGENT-TIMEOUT
      title: "Per-Turn-Wall-Clock-Timeout im Agent-Loop"
      lane: coder
      review_tier: review
      deps: []
      acceptance_criteria:
        - "Neuer Config-Key (z. B. agent.max_turn_wall_seconds) mit konservativem Default (z. B. 1800s) oder 0=off."
        - "Deadline wird in agent/conversation_loop.py:run_conversation() beim Turn-Start gesetzt."
        - "Schleifenkopf und Retry-Wartezeiten prüfen die Deadline und brechen mit _turn_exit_reason='turn_wall_clock_timeout' ab."
        - "Session wird vor dem Abbruch persistiert; final_response enthält einen Hinweis."
        - "Tests: Runaway-Turn-Simulation, Timeout bei Retry-Wartezeit, Config-Default."
      body: |
        Ort: agent/conversation_loop.py:run_conversation() (ca. Zeile 624/686).
        Pattern: time.monotonic() Deadline, keine Blockierung durch sleep/backoff.
        Sicher: Default sehr hoch oder off, damit bestehendes Verhalten erhalten bleibt.

    - id: STUCK-DETECTOR
      title: "Stuck-Loop-Detektor für wiederholte Fehler/Tool-Calls"
      lane: coder
      review_tier: review
      deps: []
      acceptance_criteria:
        - "Ringpuffer der letzten N Tool-Calls (Name + normalisierte Args + Fehler-Hash) wird geführt."
        - "Derselbe Fehler >2× oder derselbe Tool-Call >3× hintereinander triggert einen stuck_loop-Nudge."
        - "Der Nudge wird als Tool-Result-Anhang an das letzte Tool-Ergebnis gehängt (prompt-caching-kompatibel), NICHT als synthetische User-Nachricht mid-loop injiziert — genau EIN Nudge pro erkannter Stuck-Sequenz, danach Reset."
        - "Tests: Wiederholter fehlgeschlagener patch, wiederholter read_file, Reset nach erfolgreicher Änderung."
      body: |
        Ort: agent/conversation_loop.py nach Tool-Ausführung (ca. Zeile 4642/4738) oder agent/tool_executor.py.
        Pattern: Stateful Turn-Tracker, Hash über tool_name + sortierten args + Fehlerkurztext.
        Sicher: Keine harten Abbrüche, nur ein strategischer Nudge.

    - id: CI-GATES
      title: "Format-Gate + systematischer Check gegen HOME-Hartkodierung"
      lane: coder
      review_tier: standard
      deps: []
      acceptance_criteria:
        - "Neues Skript scripts/gate-format.sh führt ruff format --check NUR über fork-eigene Pfade aus (analog lint:control), NICHT über den ganzen Baum — ein globales Reformat kollidiert bei jedem Upstream-Sync mit NousResearch."
        - "KEIN einmaliges ruff format . über die gesamte Codebase (Merge-Konflikt-Bombe gegen origin/main); nur die fork-eigenen Zielpfade werden initial formatiert."
        - "Neues Skript scripts/check-hermes-home-usage.py findet Path.home() / '.hermes', os.path.expanduser('~/.hermes') und ähnliche Konstrukte, ebenfalls auf fork-eigene Pfade begrenzt."
        - "Check blockt in CI; Ausnahmen nur mit explizitem Kommentar/Allowlist-Eintrag."
        - "Tests: Beispiel-Verstöße werden erkannt; erlaubte Fälle (Doku, Test-Fixtures) werden nicht falsch-positiv."
      body: |
        Ort: pyproject.toml [tool.ruff], .github/workflows/lint.yml, scripts/.
        Pattern: Lint-Gate erweitern, dedizierter AST/Text-Check für verbotene Hermes-Home-Konstrukte.
        Sicher: Reine CI-/Skript-Änderungen, keine Laufzeitverhaltensänderung.

    - id: DB-HEALTH
      title: "Proaktiver DB-Gesundheitscheck bei SessionDB-Init"
      lane: coder
      review_tier: review
      deps: []
      acceptance_criteria:
        - "SessionDB.__init__ ruft einen neuen health_check()-Hook auf."
        - "Prüfung umfasst journal_mode, integrity_check, Sessions-Read, FTS-Schreibprobe (existierende _db_opens_cleanly-Logik wiederverwenden)."
        - "Bei Fehler wird repair_state_db_schema() getriggert oder zumindest ein strukturierter WARNING-Log-Eintrag geschrieben."
        - "Bestehendes reaktives Verhalten bei malformed database schema bleibt erhalten."
        - "Tests: Korrupte DB wird erkannt/repariert, gesunde DB startet ohne Warnung, NFS-WAL-Fallback bleibt erhalten."
      body: |
        Ort: hermes_state.py:SessionDB.__init__ und _db_opens_cleanly.
        Pattern: Proaktiver Check vor dem ersten Schreiben, keine Duplikation der Reparaturlogik.
        Sicher: Fehler werden geloggt, nicht still behoben; Reparatur nur über bestehende Pfade.
---

# Hintergrund

Diese PlanSpecs erfassen die größten, in diesem Durchgang bewusst zurückgestellten Hebel, die das Hermes-/Claude-Code-/Loop-System autonomer, verlässlicher und selbstkorrigierender machen. Sie basieren auf der Erkundung des Worktrees `kimi-isoliert-20260706T182237Z` (Basis `main @ 6ba0ac47a`).

## Begründung für Reihenfolge

1. **AGENT-TIMEOUT** stoppt die teuerste Fehlerklasse: Runaway-Turns, die Stunden an API-Kosten verbrauchen.
2. **STUCK-DETECTOR** eliminiert die häufigste sinnlose Wiederholung: identische fehlgeschlagene Tool-Calls.
3. **CI-GATES** ist der kostengünstigste Hebel für langfristige Codequalität und verhindert Isolationsbrüche.
4. **DB-HEALTH** macht das Zustandsmanagement proaktiv statt reaktiv.

## Abgrenzung

- Keine Änderungen an Secrets/Credentials.
- Keine Deploys, Service-Restarts oder destruktiven DB-Aktionen.
- Keine großen Rewrites (z. B. keine Aufteilung von run_agent.py).
- Neue Verhaltenseinstellungen sollen über config.yaml gesteuert werden, nicht über neue HERMES_* Env-Vars.
