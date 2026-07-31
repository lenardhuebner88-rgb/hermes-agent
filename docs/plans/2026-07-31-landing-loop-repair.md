---
title: "Landing-Loop-Reparatur: Baseline-Match, Recovery-Verdrahtung, Dirty-Toleranz"
type: plan
created: 2026-07-31
status: in-umsetzung
branch: kimi/landing-loop-repair
---

# Landing-Loop-Reparatur (Fix 1–3)

Ausgangslage (Analyse 2026-07-31, Live-Beweis im Sandbox-Klon): Der Landing-Loop
landet korrekt (7 Leerstände bereinigt, 1 Demo-Landung, 1 sauberer Park bei
Merge-Konflikt), aber drei Defekte verhindern Landung/Reparatur des realen
Backlogs (8 `loop/*`-Branches, davon `loop/error-sweep` mit 4 Commits und echten
Inhaltskonflikten in 5 Dateien).

## Fix 1 — BaselineProbe: SHA-Präfix-Match

`BaselineProbe.from_records` verlangt exakte String-Gleichheit
(`head_sha == baseline_sha`). Das Green-Gate-Ledger enthält historisch 9-Zeichen-
Kurz-SHAs (alle 11 bisherigen `pass`-Records) — ein grüner Nachweis für denselben
Commit matcht so nie. Fix: bidirektionaler Präfix-Match (mindestens 7 Zeichen),
fail-closed bei kürzeren/leeren SHAs.

## Fix 2 — Recovery ("reparieren") verdrahten

Heute tot, zweifach:

1. `request_candidate_recovery` leitet die task_id aus dem Branchnamen ab
   (`rsplit("/", 1)[-1]`); Pack-Namen wie `error-sweep` matchen `t_[a-z0-9]+`
   nie → immer `not_applicable`.
2. Recovery feuert nur bei `CANDIDATE_REGRESSION` (Gate rot nach Merge) —
   Merge-Konflikte (der reale Fehler des Backlogs) werden `HELD_ESCALATED` und
   lösen keine Recovery aus.

Fix:

- Neue `FailureClass.MERGE_CONFLICT` (failing_gate `"merge"`), candidate-lokal
  (kein `stop_rest`), recovery-auslösend wie `CANDIDATE_REGRESSION`.
- Task-Auflösung in `request_candidate_recovery`: (a) `t_`-Suffix wie bisher,
  (b) `tasks.branch_name = <branch>`-Lookup, (c) `create_task` mit
  `idempotency_key="landing-recovery:<branch>"`, `branch_name=<branch>`,
  `workspace_kind="worktree"` (Status wird `ready`) — danach wie bisher
  `request_landing_recovery` (Fingerprint-Dedup bleibt).
- Kein Auto-Rebase im Landing-Loop: Merge-Konflikt ⟺ Rebase-Konflikt in der
  Praxis, und `LoopRunner._auto_rebase` läuft bereits nächtlich pro Pack.
  Inhaltskonflikte brauchen einen Worker — genau dafür ist der Recovery-Task.

## Fix 3 — Dirty-Checkout-Toleranz mit Stash-Schutz

Heute parkt `_main_is_ready` jede Landung, sobald der (geteilte) Live-Checkout
irgendeine fremde Änderung enthält — der Normalzustand. Der harte Check
schützt aber das Rollback (`reset --hard`), das fremden Dirt vernichten würde.

Fix in `_land` (nur Live-Pfad):

1. HEAD==base bleibt Pflicht.
2. Dirty-Pfade aus `git status --porcelain -z` (tracked + untracked).
3. Merge-Pfade = `git diff --name-only <merge-base> <branch>`; Schnittmenge
   (präfix-sensitiv für untrackte Verzeichnisse) ≠ ∅ → parken wie bisher.
4. Disjunkt → `git stash push --include-untracked -- <dirty-pfade>`, Merge +
   Gates + ggf. Rollback (jetzt sicher, Baum ist sauber), `git stash pop` im
   `finally`. Pop-Fehler → parken mit klarem Hinweis (Dirt bleibt im Stash,
   nichts verloren).

## Verifikation

- Neue Tests in `tests/hermes_cli/test_landing_loop.py` (Kurz-SHA-Match,
  Merge-Konflikt-Recovery, Stash-Pfad inkl. Pop nach rotem Gate, Schnittmengen-
  Park).
- `scripts/run_tests.sh tests/hermes_cli/test_landing_loop.py -q`
- Review durch Opus (claude CLI) über den Diff — zwei Runden durchlaufen
  (16 Findings → alle geschlossen; Delta N1–N8 → N1–N6 gefixt, N7 hier, N8
  bewusst belassen).
- Endbeweis im Sandbox-Klon mit realem Backlog: Kurz-SHA-Baseline grün,
  Landung trotz fremden Dirts, Dirt nach Rollback intakt, Recovery-Task für
  `loop/error-sweep` materialisiert (temporäres HERMES_HOME), fremder Stash
  und gestagtes `git mv` im Ausgangszustand unangetastet.

## Runbook (N7, bewusst gewähltes Betriebsrisiko)

Während der Gate-Phase (Minuten) liegen fremde untrackte/veränderte Dateien
des Live-Checkouts physisch im Stash — parallele Sessions sehen in diesem
Fenster einen sauberen Baum. Es gibt keinen Datenverlustpfad (jeder
Fehlschlag ist fail-closed, der Stash bleibt erhalten), aber: schlägt die
Rückholung fehl, stoppt die Queue (`NICHT zurückgeholt`) und der Operator
führt den Stash manuell zusammen (`git stash list`, Eintrag
`landing-loop: fremde Basis-Änderungen (autostash)`).
