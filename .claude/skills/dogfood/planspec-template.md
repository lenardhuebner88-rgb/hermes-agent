---
title: "PlanSpec — <WAS soll bewiesen werden> Dogfood"
type: planspec
agent: Claude-Code
created: <YYYY-MM-DD>
status: ready — awaiting operator ingest
approved_by: Piet
operator: Piet
slice: <kurz-slug>-dogfood
slice_root: /home/piet/.hermes/hermes-agent
risk_class: LOW
contract_version: 1
topic: "<Ein Satz: welche Lanes beweisen WAS, mit welchem belegten Output.>"
freigabe: complete
live_test_depth: smoke
anti_scope:
  - Keine Code-Änderung, kein git/push/deploy, kein Service-Restart — reiner Lese-/Beleg-Lauf.
  - NICHT die komplette Suite — nur winzige/targeted Checks.
  - Nicht blocken, wenn ein Check fehlschlägt — exakten Fehler als GAP melden und trotzdem completen.
acceptance_criteria:
  - id: AC-EVIDENCE
    scope_level: child
    statement: "Jede Behauptung mit Kommando + Exit-Code + echter Output-Zeile; Ergebnis in kanban_complete (summary + metadata.evidence)."
    verification: "Verifier liest den Bericht: zu jedem Check existiert Kommando + Output."
    done_signal: "Echte Exit-Codes im Bericht, keine unbelegte Behauptung."
    owner: lane
  - id: AC-PROOF
    scope_level: child
    statement: "<Die konkrete zu beweisende Aussage, belegt mit den genauen Kommandos. Fehlt sie → GAP mit exaktem Output.>"
    verification: "<Wie der Verifier es prüft.>"
    done_signal: "<Beleg ODER belegtes GAP.>"
    owner: lane
    applies_to: [S1, S2, S3]
  - id: AC-AGGREGATE
    scope_level: child
    statement: "Der Join liefert EINE Operator-Tabelle (Lane × {<Spalten>}) + Verdikt 'ERFÜLLT' oder exakte Gap-Liste pro Lane; Erfolg-ohne-Beleg wird geflaggt."
    verification: "Tabelle deckt alle Lanes; jedes GAP benannt."
    done_signal: "Konsolidierte Tabelle + Verdikt."
    owner: reviewer
    applies_to: [S4]
taskgraph_hints:
  binding: true
  subtasks:
    - id: S1
      title: "<Check> (coder)"
      lane: coder
      deps: []
      body: |
        Read-only Beweis-Check. Du änderst KEINEN Code, kein git/push/deploy, NICHT die Vollsuite.
        Deliverable = Beleg-Bericht via kanban_complete; jede Aussage mit Kommando + Exit-Code + Output.

        1) WO BIN ICH: `pwd`; `git rev-parse --show-toplevel` — provisionierter Worktree, nicht Live-Checkout.
        2) <DER EIGENTLICHE CHECK — exakte Kommandos, deren Output beweist, was zu beweisen ist.>
           Fehlt/scheitert er → GAP mit exaktem Output, NICHT faken.
        3) Restatement in 1 Satz.

        kanban_complete: summary ("READY: …" / "GAP: <was>") + metadata.evidence (je Kommando: Exit-Code + Output-Zeile).
        Nicht blocken; GAP melden und completen.
    - id: S2
      title: "<Check> (coder-claude)"
      lane: coder-claude
      deps: []
      body: |
        Identisch zu S1, aber aus der coder-claude-Lane (claude-cli-Runtime). <selber Check-Block wie S1.>
    - id: S3
      title: "<Check> (premium)"
      lane: premium
      deps: []
      body: |
        Identisch zu S1, aber aus der premium-Lane. <selber Check-Block wie S1.>
    - id: S4
      title: "Beweis aggregieren (Operator-Bestätigung)"
      lane: reviewer
      deps: [S1, S2, S3]
      body: |
        Fasse die drei Beweis-Checks (coder, coder-claude, premium) zu EINER Operator-Bestätigung zusammen.
        Lies je Kind summary + metadata.evidence. Liefere via kanban_complete:
          - Tabelle: Lane × {<Spalten passend zum Check>}.
          - Verdikt: "ERFÜLLT — …" ODER exakte Gap-Liste pro Lane.
          - Flagge Erfolg-ohne-Beleg explizit.
        Keine Code-Änderung. Deliverable = Tabelle + Verdikt.
---

# <Titel> Dogfood

## Warum
<1–3 Sätze: welcher Befund/Zweifel, was soll der Beweis zeigen.>

## Ingest
`dogfood.sh "<dieser-pfad>.md"` (ingest → unblock → monitor → Evidenz), mit run_in_background.
