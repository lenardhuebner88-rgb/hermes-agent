---
title: "PlanSpec — Härter Residue-Scan Fix: Code-/Backtick-Spans ausnehmen"
type: planspec
agent: Claude-Code
created: 2026-06-19
status: ready — awaiting operator ingest (via --force; siehe unten)
approved_by: Piet
operator: Piet
slice: vision-flywheel-haerter-residue-fix
slice_root: /home/piet/.hermes/hermes-agent
risk_class: LOW
contract_version: 1
sequence: "Phase-1-Follow-up — MUSS vor Phase 2 umgesetzt + deployed sein (Stratege generiert Specs, die Marker zitieren können → spurious Blocks würden die Autonomie-Schleife beschädigen)."
topic: "Den deterministischen Härter-Residue-Scan so fixen, dass er Marker (`<…>`/`TODO`/`FIXME`/`TBD`/`...`), die in Backtick-/Code-/Pfad-Spans nur ZITIERT werden, nicht mehr fälschlich blockt — echte unausgefüllte Platzhalter in Prosa werden weiter gefangen. Plus Regressionstest."
freigabe: complete
live_test_depth: smoke
anti_scope:
  - NUR der Residue-Scan (`_residue_tokens` / `_collect_spec_rubric_findings`). Andere Härter-Checks (AC-Presence, Lane, CC-Instrument, Sonnet-Judge) bleiben UNVERÄNDERT.
  - Den Gate NICHT generell schwächen — genuine unausgefüllte Residues in Prosa MÜSSEN weiter gefangen werden.
  - KEIN push/deploy/restart durch Worker (gecaged). Aktivierung = Operator-Schritt nach Merge.
  - NICHT die Vollsuite — targeted via scripts/run-affected.sh.
acceptance_criteria:
  - id: AC-CAGE
    scope_level: child
    statement: "Worker arbeitet nur im provisionierten Worktree (`git rev-parse --show-toplevel` = ein .worktrees/kanban-Pfad). Kein push/deploy/restart."
    verification: "Bericht zeigt pwd + toplevel; keine Aktivierungs-Aktion."
    done_signal: "Worktree-Pfad belegt."
    owner: lane
  - id: AC-GATES
    scope_level: child
    statement: "Vor kanban_complete laufen targeted Gates via `scripts/run-affected.sh` grün (pytest betroffen + ruff). Beleg = Kommando + Exit 0."
    verification: "Kommando + Exit-Code 0 im Bericht."
    done_signal: "run-affected.sh exit 0 belegt."
    owner: lane
  - id: AC-EVIDENCE
    scope_level: child
    statement: "Jede Behauptung mit Kommando + Exit-Code + Output-Zeile; Diff-Summary."
    verification: "Zu jeder Aussage ein Beleg."
    done_signal: "Belegte Exit-Codes."
    owner: lane
  - id: AC-RESIDUE-FIX
    scope_level: child
    statement: "Der Residue-Scan in `hermes_cli/planspecs.py` nimmt Inline-Backtick-Spans, Code-Fences und offensichtliche Pfad-Tokens aus `title`/`body`/AC-`statement` VOR dem Marker-Scan aus; die bare-Ellipse (`...`) wird nur AUSSERHALB von Code geflaggt. Die anderen Checks (`AC-less subtask`, `unknown lane`, CC-Instrument, Judge) bleiben bit-identisch."
    verification: "Tests: (1) eine Spec, die die Marker in Backticks in body UND AC zitiert → `validate_spec_rubric` wirft NICHT (keine findings); (2) eine Spec mit einem ECHTEN unausgefüllten Backtick-freien Winkelklammer-Platzhalter in Prosa-body → blockt weiterhin; (3) bestehende `test_planspec_rubric.py`/`test_planspecs.py` bleiben grün; (4) ein Fixture aus den Vision-Flywheel-Specs (die Marker zitieren) ingestet jetzt."
    done_signal: "Alle vier Test-Fälle grün; Gate nicht geschwächt belegt."
    owner: lane
    applies_to: [F1]
  - id: AC-JOIN
    scope_level: child
    statement: "Adversariale Review: der Fix schwächt den Gate NICHT (genuine Prosa-Residue wird noch gefangen — belegt mit einem Negativ-Test), die zuvor über-geblockten Specs ingesten jetzt, Caller-Grep auf `_residue_tokens`/`_collect_spec_rubric_findings`-Nutzer zeigt keine Regression, targeted Gates grün."
    verification: "Join deckt F1; jedes Hold mit Datei:Zeile."
    done_signal: "go/hold-Verdikt + Beleg dass Gate intakt bleibt."
    owner: reviewer
    applies_to: [J1]
taskgraph_hints:
  binding: true
  subtasks:
    - id: F1
      title: "Härter: Residue-Scan Code-/Backtick-/Pfad-Spans ausnehmen (coder-claude)"
      lane: coder-claude
      deps: []
      body: |
        Worktree, kein push/deploy. Fixe den deterministischen Residue-Scan, der heute Specs blockt,
        die verbotene Marker nur ZITIEREN.

        WO: hermes_cli/planspecs.py — `_residue_tokens` (Marker-Erkennung) und/oder
        `_collect_spec_rubric_findings` (planspecs.py:579-627, scannt title + body + jeden AC-statement).
        Befund-Receipt: vault/03-Agents/Hermes/receipts/2026-06-19-t_e6800638-cleanup-receipt.md.

        FIX (Operator-Entscheidung: Code-/Backtick-Spans ausnehmen):
        1) VOR dem Marker-Scan aus `title`/`body`/jedem AC-`statement` ausnehmen: Inline-Backtick-Spans
           (`` `...` ``), Code-Fences (```` ```...``` ````) und offensichtliche Pfad-Tokens
           (z.B. `a/b/c.py`, `.worktrees/...`). Nur der verbleibende Prosa-Text wird auf Marker geprüft.
        2) Die bare-Ellipse (drei Punkte) nur AUSSERHALB von Code flaggen.
        3) Die Marker selbst (Winkelklammer-Platzhalter, `TODO`/`FIXME`/`TBD`) bleiben Marker — nur ihre
           Erkennung wird code-span-bewusst. Echte unausgefüllte Platzhalter in PROSA werden weiter gefangen.
        4) Alle anderen Checks (AC-Presence, `unknown lane`, CC-Instrument, Judge-Pfad) bit-identisch lassen.

        Tests (tests/hermes_cli/, targeted) — alle vier:
          (1) Spec, die die Marker in Backticks in body UND AC zitiert → `validate_spec_rubric` wirft nicht.
          (2) Spec mit echtem unausgefülltem Backtick-FREIEM Winkelklammer-Platzhalter in Prosa-body → blockt.
          (3) bestehende test_planspec_rubric.py / test_planspecs.py bleiben grün.
          (4) ein reales Fixture aus den Vision-Flywheel-Specs (zitiert Marker) → ingestet/passt die Rubrik.

        Gates: scripts/run-affected.sh grün. kanban_complete: summary + metadata.evidence
        (Kommando+Exit-Code je Behauptung) + Diff-Summary.
    - id: J1
      title: "Reviewer-Join: Residue-Fix abnehmen (reviewer)"
      lane: reviewer
      deps: [F1]
      body: |
        Adversariale Review des Residue-Fix-Diffs. Keine Code-Änderung.
        Prüfe konkret:
          - Gate NICHT geschwächt: ein genuiner unausgefüllter Prosa-Platzhalter wird noch geblockt
            (Negativ-Test vorhanden + belegt).
          - Die zuvor über-geblockten Specs (Vision-Flywheel Phase-1/1.5) ingesten jetzt sauber.
          - Caller-Grep auf `_residue_tokens` / `_collect_spec_rubric_findings`-Nutzer → keine Regression.
          - Andere Härter-Checks unverändert.
          - Targeted Gates grün.
        Liefere via kanban_complete: go/hold + Beleg (Datei:Zeile) dass der Gate intakt bleibt.
---

# Härter Residue-Scan Fix (Code-Span-exempt)

## Contract — Härter-Residue-Fix · 2026-06-19 (Light)
- **Ziel:** Der Residue-Scan blockt zitierte/dokumentierte Marker (in Backtick-/Code-/Pfad-Spans) nicht mehr; echte unausgefüllte Platzhalter in Prosa werden weiter gefangen.
- **IN:** `_residue_tokens` / `_collect_spec_rubric_findings` (planspecs.py) code-span-bewusst machen über title+body+AC; bare Ellipse nur außerhalb Code; Regressionstests (4 Fälle).
- **OUT:** andere Härter-Checks unverändert; keine sonstige Strenge-Senkung.
- **Done when:** zitierte Marker (in Backticks) passieren, genuiner Prosa-Residue blockt weiter, bestehende Tests grün, reviewer-Join GO.
- **Nicht tun / Abbruch bei:** Gate generell schwächen → STOP. Worker gecaged. Im Zweifel (Doku vs Residue) konservativ: nur Backtick-/Code-Spans ausnehmen, sonst flaggen.
- **Sequenz:** Phase-1-Follow-up — MUSS vor Phase 2 deployed sein.

Contract: Piet ✅ (Fix-Ansatz „Code-/Backtick-Spans ausnehmen" gewählt) — Claude ✅

## Warum
Bestätigter Shipped-Bug (siehe Receipt `2026-06-19-t_e6800638-cleanup-receipt.md`): der Residue-Scan läuft über body + jeden AC-statement und blockt Specs, die die Marker nur zitieren (Doku/CLI-Usage/Rubrik-Beschreibung). Live bewiesen (1.5-Dogfood + Verifier `finding_count=25` auf der Phase-1-Spec selbst). Der Härter ist das Tor der ganzen autonomen Pipeline — vor Phase 2 (auto-generierte Strategen-Specs) muss er false-positive-frei sein.

## Ingest
Diese Spec zitiert die Marker zwangsläufig → der AKTUELL noch buggy Härter würde sie blocken. Daher ingest mit `hermes plan ingest <pfad> --force --json` (legitimer Operator-Override für ein bekannt-fehlerhaftes Gate; dogfoodt zugleich `--force`). Nach diesem Fix würde dieselbe Spec auch ohne `--force` durchlaufen (Marker stehen in Backticks). Dann `hermes kanban unblock <child-ids>`.
