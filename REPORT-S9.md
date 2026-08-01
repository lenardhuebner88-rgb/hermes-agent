# S9 — Bearbeiter-Marker

- Geändert: `hermes_cli/buzz_bridge.py`, `tests/hermes_cli/test_buzz_bridge.py`, `REPORT-S9.md`.
- Claim/Abschluss: exakte Argumentvektoren für `notes set --name work-marker … --content working:t_x` und `notes rm --name work-marker` getestet.
- Leser: gemessenes `notes ls`-Schema plus `users presence`-Schema → `working:t_x` + `online` ergibt `alive=True`, `stale=False`.
- Kontrollprobe Presence fehlt (`[]`): `alive=False`, `stale=True`; absichtliche Gegenmutation wurde rot: Exit 1, `1 failed`, Assertion `True is False`.
- Kontrollprobe Fremdcontent: `reviewing:t_x` ergibt `[]` und löst keinen Presence-Aufruf aus.
- Fehlende `kanban.work_marker`-Config: Pubkey als Anzeigename, keine Ausnahme.
- Keine Sperre: zwei Agenten auf `t_x` werden beide geliefert; genau ein gemeinsamer Presence-Aufruf mit beiden Pubkeys.
- `scripts/run-affected.sh`: Exit 0; `=== Summary: 1 files, 14 tests passed, 0 failed/errors (100% complete) in 0.9s (6 workers) ===`.
- `/home/piet/.hermes/hermes-agent/.venv/bin/ruff check .`: Exit 0; `All checks passed!`.
- Nicht ausgeführt: Full Suite (laut Auftrag). Offen: nichts im S9-Scope; kein Merge/Push/Deploy/Restart.
