# Loop-Runner — pack-basierte autonome Verbesserungs-Loops

Ein Runner, viele Loops: jedes **Pack** (`loops/packs/<name>/`) beschreibt in
`pack.yaml` + Prompt-Dateien, WAS nachts passiert; der Runner liefert das WIE
(Worktree-Isolation, Datei-Queue, Ledger, Retry/Revert/Bounce, Locks, Usage-Limit-Stop,
Discord). Design-SoT (gegrillt 2026-07-02):
`~/vault/03-Agents/Claude-Code/plans/2026-07-02-loop-runner-v1-v2.md`.

## Bedienung

```bash
# CLI (aus dem Repo; venv wird vom shim gesetzt):
venv/bin/python -m loops.runner --pack builder-reviewer --cmd status
venv/bin/python -m loops.runner --pack builder-reviewer --cmd night      # plan + build/verify
venv/bin/python -m loops.runner --pack test-stabiliser  --cmd run        # sweep
venv/bin/python -m loops.runner --pack builder-reviewer --cmd land [--no-push]  # Landungs-Leiter Stufe 1

# systemd (einmalig: loops/systemd/install.sh):
systemctl --user start hermes-loop@<pack>                 # jetzt (detached)
systemctl --user enable --now hermes-loop@<pack>.timer    # nächtlich 23:37
# Pack-eigene Zeit: systemctl --user edit hermes-loop@<pack>.timer (OnCalendar-Drop-in)

# Dashboard: /control → Tab "Loops" — Start mit Engine/Modell/Param-Overrides,
# Stop (STOP-Datei), Timer-Toggle, Landen, Werkstatt (Custom-Packs editieren).
```

**Steuerhebel:** `~/.hermes/loops/<pack>/SEED.md` (optionale Saat für Planner-Packs) ·
`overrides.env` (schreibt das Dashboard; gilt für EINEN Lauf — der Runner benennt sie
beim night/run-Start nach `overrides.consumed.env` um) · `touch ~/.hermes/loops/<pack>/STOP` ·
Ledger + Phasen-Logs unter `~/.hermes/loops/<pack>/`.

## Archetypen & Protokoll

- **pipeline** (plan → Queue → build+verify pro Plan): Fable plant Kontrakt-Pläne
  (done_when/anti_scope/tests), billiges Modell baut, Fable verifiziert adversarial
  (Gates selbst, Tautologie-Check). FAIL → Revert + 1 Retry mit Feedback → bounced.
  Queue-Stufen: `00-planned → 10-building → 20-verified → 30-landed · 90-bounced`.
- **sweep** (eine round-Phase wiederholt): finden + fixen + committen, Status
  `FIXED|DRY|BLOCKED` (Stop nach `dry_rounds` DRY bzw. `fail_streak` BLOCKED).
- `last-status` (eine Zeile) ist das einzige Statussignal der Agenten; Git-HEAD und
  Exit-Codes sind die Wahrheit des Drivers.

## Engines (`loops/engines/`, Katalog `loops/models.yaml`)

| engine | Träger | "model"-Feld |
|---|---|---|
| claude | `claude -p` (Abo) | Modell-Slug (claude-fable-5, claude-sonnet-5, …) |
| kimi | kimi-code CLI (Abo, $0) | kimi-code/kimi-for-coding |
| codex | `codex exec --sandbox danger-full-access` (Abo; workspace-write scheiterte an Worktree-gitdir/STATE_DIR/tmux, 2026-07-05) | gpt-5.5, gpt-5.3-codex |
| hermes | `hermes -p <profil> -z` (+ HERMES_SANDBOX_MODE=1) | **Hermes-PROFIL** (reviewer→NeuralWatt/glm-5.2, coder→Codex-Pool) |
| neuralwatt | `hermes -m <model> --provider neuralwatt -z` (+ HERMES_SANDBOX_MODE=1) | NeuralWatt-Modell-Slug (glm-5.2, kimi-k2.7-code, …) |

Neue Engine = Modul mit `@register("name")` nach dem Contract
`run(model, prompt, cwd, timeout_s) -> EngineResult`.

## Landung (Stufe 1 — bewusster Schritt, nie im Loop)

`--cmd land` automatisiert die Morgen-Mechanik mit Schienen: Abbruch bei UNVERIFIED
(10-building) · Live-Checkout muss auf main + sauber sein · Rollback-Anker-Tag
`loop-land/<pack>/<ts>` · **ff-only**; wenn main konfliktfrei weitergelaufen ist,
rebased der Pack-Worktree automatisch auf main und setzt `loop-rebase/<pack>/<ts>`
als Rollback-Ref · Collection-Sweep + affected-Tests (+ Frontend-Gates wenn web/
berührt) · bei Rot `reset --keep` auf den Anker · Push NUR piet-fork · verifizierte
Pläne → `30-landed` · Pack FRESH von neuem main. Rebase-Konflikt oder dirty
Pack-Worktree bleiben Abbruch mit manuellem Klärbedarf. Das **Urteil** über die
Commits (Ledger + Diffs lesen) bleibt vor dem Aufruf beim Menschen/Hauptagenten.
Auto-Land (Stufe 2) ist bewusst NICHT freigeschaltet.

## Eigene Packs (Werkstatt)

Custom-Packs leben in `~/.hermes/loops/packs-custom/` (nie im Repo; Namens-Kollision
mit Repo-Packs = Fehler). Erstellen: im Dashboard „Duplizieren" oder
`cp -r loops/packs/_blank ~/.hermes/loops/packs-custom/<name>` — Manifest-Schema und
Pflicht-Konventionen (Platzhalter, last-status, Verbote) erzwingt der Server-Lint bzw.
`tests/loops` Pack-Lint. Die Meta-Packs **loop-schmiede** (schmiedet neue Packs aus
eigener Evidenz) und **loop-tuner** (härtet bestehende Prompts evidenzbasiert; Verbote
dürfen nur schärfer werden) pflegen das System selbst.

## Teuer gelernte Fallen (nicht wieder einbauen)

- `git clean` blockt der guard-Hook AUCH in headless Sessions → nur der Driver räumt.
- Neue Dateien sind ohne `git add -A` für `git diff HEAD` (Gate!) unsichtbar.
- Die Claude-CLI meldet Limits auch als „session limit" — Erkennung in `loops/engines`.
- `systemctl start` blockiert bei oneshot bis Prozessende → immer `--no-block`.
- venv heißt `venv/` (ohne Punkt); Worktrees testen mit `PYTHONPATH=<wt>` + Live-venv.
- Kein `grep -q` an `pipefail`-Pipes (SIGPIPE-Race).
- kanban.db ist bewusst NICHT profil-isoliert → hermes-Engine läuft mit
  `HERMES_SANDBOX_MODE=1`.
