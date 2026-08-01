# REPORT S8 — Buzz Release-Gate Bridge

## Status und geänderte Dateien

Der Slice ist lokal implementiert und in den S8-Zieltests grün. Das vorgeschriebene
Affected-Gate bleibt wegen drei reproduzierten 300-s-Dateitimeouts offen (keine rote
Assertion; Details unten). Kein Merge, Push, Deploy, Dienstneustart oder Live-Config-Write.

- `gateway/kanban_alerts.py` — Alert-Klassifikation; `auto_release_attention` nicht injizierbar,
  fehlendes Feld bleibt standardmäßig injizierbar.
- `gateway/kanban_watchers.py` — **Diff: +1/-0 Zeilen**, nur die zusätzliche Feldprüfung in
  der bestehenden Inject-Bedingung.
- `hermes_cli/buzz_bridge.py` — fork-eigener Notifier/Poller und ausführbares Modul.
- `tests/gateway/test_kanban_alerts.py` — Klassifikation aus echten Alert-Regeln.
- `tests/gateway/test_kanban_watchers_mixin.py` — false blockiert Inject; fehlendes Feld injiziert weiter.
- `tests/hermes_cli/test_buzz_bridge.py` — Nachricht, Event-ID, Allowlist, Emoji und Config-Default.
- `REPORT-S8.md` — dieser Bericht.

Unberührt: `hermes_cli/auto_release.py`, `hermes_cli/kanban_db.py`,
`hermes_cli/web_server.py`, `hermes_cli/kanban.py` und alle weiteren upstream-eigenen Dateien.

## Done-when-Ergebnisse

1. `auto_release_attention` trägt `orchestrator_injectable: false`; der Watcher erzeugt damit
   0 synthetische Turns. Kontrollfall ohne Feld erzeugt weiterhin 1 Turn. Alert-Regel-Kontrolle:
   normale `operator_escalation` trägt `true`, Auto-Release trägt `false`.
2. Die Buzz-Anfrage enthält Task-ID, Kette, Merge-Commit und Halte-Grund; die 64-stellige
   `event_id` aus `buzz messages send` wird in `bridge.event_id` gespeichert und für
   `buzz reactions get --event <id>` verwendet.
3. Konfiguriertes Emoji + allowlisteter Pubkey ruft genau einmal
   `hermes kanban release-gate <task_id>` auf.
4. Pflicht-Kontrollprobe: dasselbe Emoji + nicht allowlisteter Pubkey endet `timed_out`,
   0 Release-Gate-Aufrufe; Loggrund: `reacting pubkey is not in the approver allowlist`.
5. Zweite Kontrollprobe: anderes Emoji + allowlisteter Pubkey endet `timed_out`,
   0 Release-Gate-Aufrufe.
6. Fehlende **und** leere `approvers`-Config ergeben die dokumentierte nichtleere
   Default-Allowlist mit Piets Owner-Pubkey; nie „jeder darf“.

Mutations-Kontrollprobe: Identitäts- und Emoji-Prüfung absichtlich unwirksam gemacht →
`Summary: 1 files, 0 tests passed, 2 failed/errors`, Exit 1; beide Negativtests meldeten
`approved` statt `timed_out`. Danach Guards wiederhergestellt und Zieltests erneut grün.

## Fixture-Provenienz: echtes `buzz reactions get`

Am 2026-08-01 wurde das unveränderte Release-Binary
`/mnt/data/services/buzz/target/release/buzz` mit
`reactions get --event <64-hex>` gegen einen isolierten lokalen Read-only-Protokollstub
ausgeführt. Der Stub lieferte drei rohe kind-7-Reaktionen; nur das echte CLI aggregierte sie.
Private Test-Identität: flüchtig nur im Prozess erzeugt, nie ausgegeben oder gespeichert.
Wörtliches stdout, Exit 0, als Test-Fixture übernommen:

```json
{"reactions":[{"count":2,"emoji":"✅","pubkeys":["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"]},{"count":1,"emoji":"👀","pubkeys":["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]}]}
```

## Gates — wörtliche Ergebnisse

`scripts/run-affected.sh` — **Exit 1**. Erster Pass wörtlich:

```text
=== Summary: 9 files, 39 tests passed, 0 failed/errors (100% complete) in 600.2s (6 workers) ===
```

Danach automatische Reproduktionsrunde wörtlich:

```text
=== Summary: 3 files, 0 tests passed, 0 failed/errors (100% complete) in 600.3s (6 workers) ===
```

Reproduziert: `tests/gateway/test_kanban_alerts.py`,
`tests/gateway/test_kanban_notifier.py` und
`tests/gateway/test_kanban_notifier_watcher_dispatch_gate.py` jeweils
`(300s exceeded; process tree SIGKILL'd)`. Im ersten Pass waren vorher 39 Tests in sechs
anderen Dateien grün; die Alert-Datei zeigte 33 Testpunkte, wurde aber nach SIGKILL vom
Runner korrekt mit 0 gezählten Tests gewertet. Keine Assertion wurde als fehlgeschlagen gemeldet.

`/home/piet/.hermes/hermes-agent/.venv/bin/ruff check .` — **Exit 0**:

```text
All checks passed!
```

Zusätzliche S8-Zieltests auf dem finalen Zustand:

```text
=== Summary: 1 files, 6 tests passed, 0 failed/errors (100% complete) in 0.9s (6 workers) ===
=== Summary: 2 files, 4 tests passed, 0 failed/errors (100% complete) in 1.2s (6 workers) ===
```

Beide Befehle Exit 0. Die volle Testsuite wurde gemäß Auftrag nicht ausgeführt.

## Offen

- Pflicht-Gate `scripts/run-affected.sh` ist wegen der drei reproduzierten Lasttime-outs
  **nicht grün**; Review/Landung darf das nicht als grünes Gate behandeln.
- Live-Werte für `kanban.release_gate.channel_id`, `emoji`, Poll-Intervall und Timeout wurden
  absichtlich nicht geschrieben; ebenso keine Integration, Landung oder Aktivierung im Live-System.
