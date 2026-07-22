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

- **pipeline** (plan → Queue → build+verify pro Plan): Planner plant Kontrakt-Pläne
  (done_when/anti_scope/tests), Builder implementiert, Verifier prüft adversarial
  (Gates selbst, Tautologie-Check). FAIL → Revert + 1 Retry mit Feedback → bounced.
  Queue-Stufen: `00-planned → 10-building → 20-verified → 30-landed · 90-bounced`.
  Effektive Engine/Model-Routen kommen aus `pack.yaml` + One-Shot/Night-Overrides
  und werden in Prompts als `{{ENGINE}}`/`{{MODEL}}` bzw. Writer-Route
  `{{BUILD_ENGINE}}`/`{{BUILD_MODEL}}` gerendert — keine festen Display-Namen.
- **sweep** (eine round-Phase wiederholt): finden + fixen + committen, Status
  `FIXED|DRY|BLOCKED` (Stop nach `dry_rounds` DRY bzw. `fail_streak` BLOCKED).
- **Eskalations-Konvention** (seit 2026-07-06): ein BLOCKED mit echtem Fund (Bug/
  Risiko außerhalb des Pack-Mandats) schreibt zusätzlich einen strukturierten Block
  nach `~/.hermes/loops/<pack>/ESCALATIONS.md` (Evidenz · Blockiert-weil · Fix-Skizze ·
  Kanal-Vorschlag). Die Morgen-Review liest diese Datei VOR dem Land-Urteil — sonst
  sterben Funde im Ledger (07-03: 40×-Auth-500-Bug blieb unadressiert).
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

## Landung (Stufe 1 — standardmäßig bewusster Schritt)

`--cmd land` automatisiert die Morgen-Mechanik mit Schienen: Abbruch bei UNVERIFIED
(10-building) · Live-Checkout muss auf main + sauber sein · Rollback-Anker-Tag
`loop-land/<pack>/<ts>` · **ff-only**; wenn main konfliktfrei weitergelaufen ist,
rebased der Pack-Worktree automatisch auf main und setzt `loop-rebase/<pack>/<ts>`
als Rollback-Ref · Collection-Sweep + affected-Tests (+ Frontend-Gates wenn web/
berührt) · bei Rot `reset --keep` auf den Anker · Push NUR piet-fork · verifizierte
Pläne → `30-landed` · Pack FRESH von neuem main. Rebase-Konflikt oder dirty
Pack-Worktree bleiben Abbruch mit manuellem Klärbedarf. Das **Urteil** über die
Commits (Ledger + Diffs lesen) bleibt vor dem Aufruf beim Menschen/Hauptagenten.
Auto-Land (Stufe 2) ist für alle übrigen Packs bewusst NICHT freigeschaltet.

**Eng begrenzte Ausnahme (Operator 2026-07-09):** Nur das kuratierte Repo-Pack
`dashboard-experience` steht zusätzlich in der Code-Allowlist des Runners. Die
Freigabe ist an den Repo-Pack-Pfad, das Live-Repo, den Planner→Builder→Verifier-Vertrag
sowie SHA-256 der Manifest- und Prompt-Inhalte gebunden; Custom-Kopien und Drift
brechen hart ab. Der Control-Startdialog darf Engine, Modell und Laufbudgets
one-shot überschreiben; nur Budget-Overrides behalten die Auto-Land-Autorität.
Ein abweichender Phasenvertrag läuft vollständig, bleibt aber zur manuellen Prüfung
und Landung liegen. Dort
plant und verifiziert die Planner-/Verifier-Phase in frischen Sessions, der Builder
baut genau einen Commit (Default-Routen in pack.yaml, Overrides erlaubt), und der
Driver landet ausschließlich bei `1 verified / 1 commit / 0
planned / 0 building`, ausschließlich geänderten Pfaden unter
`web/src/control/**`, exakt passender `PASS <plan-id>`-Quittung und einer
runner-attestierten, commitgebundenen 390/820/1366-Evidenz (Summary + 3 PNG + 3
ARIA) mit unveränderter SHA-256. Er fährt
dieselbe ff-only-/Rebase-/Gate-/Rollback-Leiter,
fordert einen erfolgreichen Push nach `piet-fork` und rollt bei Push-Fehler auch
den lokalen Merge zurück – aber nur, solange `main` exakt auf dem eigenen
Merge-Commit steht; fremde Parallel-Commits erzwingen manuelle Klärung statt
Reset. Eine gesetzte `STOP`-Datei blockiert auch Resume/Push. `autoland: true` in
irgendeinem anderen Manifest ist ein harter Manifest-Fehler; Custom-Packs können
diese Autorität nicht erlangen. Modellphasen laufen mit Worker-Markern: Claudes
globaler Guard sperrt Push/Deploy in den Claude-Phasen; für **alle** Engines setzt
der Runner zusätzlich einen `git`-Push-Deny vor den PATH, neutralisiert bekannte
Push-Remotes/Credentials und stellt die Umgebung nach der Phase wieder her. Die
Ausnahme deployt nicht und erweitert keine Worker-Rechte auf push/merge.

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
