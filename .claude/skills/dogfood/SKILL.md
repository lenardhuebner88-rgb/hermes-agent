---
name: dogfood
description: Use when you need to PROVE — with real Hermes kanban workers and captured evidence, not by assertion — that a rule, capability, tool-access, or behaviour actually holds live; e.g. confirming workers understood a new convention, or can reach a file/script/venv in their isolated workspace, or that a shipped fix took effect. Triggers — "beweise mit Workern", "dogfood", "lass die Worker bestätigen", "können die Worker wirklich X".
---

# Dogfood — prove a capability with real workers + evidence

## Core principle
**Prove, don't assert.** A dogfood makes real kanban workers *demonstrate* a rule/access/behaviour
and return command + exit-code + output as evidence. Three things make it work:
- **Evidence-first.** Every claim needs a command, its exit code, and a real output line. "läuft grün"
  without a captured line is a fail, not a pass.
- **A discovered GAP is a SUCCESS, not a task failure.** The point is to surface truth. Workers report
  a gap (with the exact error) and still `complete` — never block on it.
- **Fan across lanes, then join.** Access varies per lane, so run the same check on each code lane in
  parallel; a `reviewer` join aggregates into one operator table + verdict. The join IS the deliverable.

## The proven pipeline
1. **Author a binding PlanSpec** (see `planspec-template.md`) under `~/vault/03-Agents/Claude-Code/plans/`.
   Fan-out: one self-check subtask per **code lane** + one `reviewer` join that depends on them.
2. **Validate** statically (YAML parses, ids unique, lanes valid, deps acyclic) — see Quick Reference.
3. **Run it** with the helper: `dogfood.sh <planspec.md>` (it ingests → releases → monitors → prints
   evidence). Launch it with `run_in_background: true` — it polls with `sleep`, which is blocked in the
   foreground; the harness re-invokes you when it exits.
4. **Report** the join's operator table + verdict, and surface any GAP findings verbatim.

## Quick Reference (the REAL commands — do not guess)
```bash
REPO=/home/piet/.hermes/hermes-agent ; H=$REPO/venv/bin/hermes       # Haupt-venv (venv/, NICHT .venv/ — konsolidiert 2026-07-02)
$H plan ingest "<spec>.md" --author dogfood --json                   # -> root_task_id + child_ids; held in `scheduled`
$H kanban unblock <child ids…>                                       # release scheduled -> ready (REQUIRED; ingest does not auto-dispatch)
$H kanban show <id> --json   # structure: d['task']['status'|'assignee'|'workspace_path'], d['latest_summary'], d['comments'][-1]['body']
# validate a spec before ingest:
python3 -c "import yaml,sys;d=yaml.safe_load(open(sys.argv[1]).read().split('---\n')[1]);s=d['taskgraph_hints']['subtasks'];ids=[x['id'] for x in s];print('ok' if len(ids)==len(set(ids)) else 'DUP ids')" "<spec>.md"
```
Valid lanes = on-disk profiles only (`ls ~/.hermes/profiles/`). **Code lanes = `coder`, `coder-claude`,
`premium`** (`kanban.worker_gate.code_roles`). Join/aggregate lane = `reviewer`.

## Gotchas (each one bit a fresh agent without this skill)
| Trap | Reality |
|---|---|
| Hand-rolling `kanban create` + `--parent` | Use the **binding PlanSpec + `plan ingest`** — deterministic graph, threads AC into child bodies. |
| Falsches venv/CLI | Kanonisch ist `$REPO/venv/bin/hermes` (`~/.local/bin/hermes` symlinkt darauf, fehlt aber in non-login-PATH). `.venv/` ist seit 2026-07-02 konsumentenlos/deprecated. |
| `--workspace dir:<live-checkout>` | Don't override isolation — code-role workers run in isolated worktrees `.worktrees/kanban/<id>` by default. That isolation IS often what you're proving. |
| `codex` as a lane | No codex kanban lane exists. Cross-family lives in the graph as `critic`/`reviewer`. |
| Claude-Code instruments in AC | Never put `council`/CC-subagent terms in worker AC/titles/bodies — unfulfillable → REQUEST_CHANGES loop. Keep AC to shell + read + `kanban_complete`. |
| Ingested chain never runs | It sits in `scheduled` until you `kanban unblock` the children. |
| Proving a code/dispatcher behaviour after a deploy | The dispatcher holds code in memory — `systemctl --user restart hermes-gateway.service` first, or you test stale code (see `reference_gateway_restart_loads_dispatcher_code`). |

## Worked examples (real, shipped)
`~/vault/03-Agents/Claude-Code/plans/2026-06-18-test-scope-readiness-dogfood.md` (rule-understanding +
access) and `…-venv-symlink-proof-dogfood.md` (behaviour-after-fix proof). Both: 3 code lanes + reviewer
join, all evidence-backed.

## Common mistakes
- Writing AC that fails on a real gap (so workers hide it) — instead: "report the exact error as a GAP and complete."
- Asking for "all tests" — dogfood checks are tiny/targeted; never trigger the full suite. When a spec
  does run pytest in the hermes-agent repo, use `scripts/run-affected.sh` (skips cleanly on an empty
  diff); never the raw `run_tests.sh $(scripts/affected-tests.sh)` (empty output = full suite/timeout).
- Reporting the leaves but not the **join** — the join's consolidated table is the operator-facing answer.
