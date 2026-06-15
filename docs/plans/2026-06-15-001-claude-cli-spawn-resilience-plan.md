---
title: Claude-CLI Coder-Lane Spawn-Resilienz (transiente worktree-provisioning-Timeouts)
status: planned
date: 2026-06-15
type: fix
target_repo: hermes-agent
origin: FO-Orchestrator Smoke 2026-06-15 — Coder-Cloud-Linie lebt, aber flaky (spawn_failed bei git worktree add timeout)
---

# Claude-CLI Coder-Lane Spawn-Resilienz — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ein transienter `git worktree add`-Timeout beim Spawn eines claude-CLI-Workers soll den Task **re-queuen** (zurück auf `ready`) statt ihn dauerhaft zu `blocken`, und keine halb-fertigen, `initializing`-gesperrten Worktrees leaken.

**Architecture:** Drei Schichten, alle im Hermes-Dispatch-Pfad, **ohne DB-Schema-Änderung** (Zähler/Limit laufen über das bestehende `task_events`-Log):
1. **Klassifizieren** — `git`-Timeouts werden in `kanban_worktrees.py` als eigene Exception `WorktreeTimeout(WorktreeError)` erkannt (statt als generischer Fehler durchzuschlagen).
2. **Re-queue statt Block** — `dispatch_once` behandelt `WorktreeTimeout` als *transient*: Task zurück auf `ready`, **ohne** `consecutive_failures` zu erhöhen, gedeckelt durch `SPAWN_RETRY_LIMIT` (gezählt über `spawn_retry`-Events). Erst nach Erschöpfen des Spawn-Budgets greift der bestehende Block-Pfad.
3. **Reap** — bei *jedem* Provisioning-Fehler wird das partielle/locked Worktree force-entfernt + geprunet, damit kein `initializing`-Leak zurückbleibt.

Plus zwei kleine, risikoarme Härtungen: konfigurierbarer Timeout (Env) und ein **opt-in** `gc.auto=0` (Env), um Auto-gc-Stalls als Contention-Quelle zu entschärfen.

**Tech Stack:** Python 3, pytest, sqlite3. Module: `hermes_cli/kanban_worktrees.py`, `hermes_cli/kanban_db.py`. Tests: `tests/hermes_cli/test_kanban_worktrees.py`, `tests/hermes_cli/test_kanban_db.py`.

---

## Hintergrund / Warum (Beleg-geerdet)

Beobachtet am 2026-06-15 (kanban.db `task_runs`):
```
outcome=gave_up
metadata={"failures": 1, "trigger_outcome": "spawn_failed", "effective_limit": 1, "limit_source": "task"}
error: worktree provisioning: Command '[...git worktree add...]' timed out after 120 seconds
```
- Der Timeout selbst ist transient: `git worktree add` (inkl. 108 MB-Checkout) braucht ungestört **~1 s**; der 120-s-Cap wird nur unter git-Ref-Lock-Contention im stark geteilten Checkout `~/.hermes/hermes-agent` gerissen (Auto-Committer + bis zu 4 `claude remote-control`-Bypass-Sessions + Kanban-Worker schreiben gleichzeitig).
- Heutiges Verhalten ist falsch für *Infrastruktur*-Fehler: Ein Spawn-Fehler zählt gegen **dasselbe** `consecutive_failures`-Budget wie ein echter Worker-Fehler (`kanban_db.py:8582-8733`). Mit `max_retries=1` ⇒ erster transienter Timeout = sofort dauerhaft `blocked`.
- Zusätzlich lässt ein getöteter `worktree add` ein dauerhaft `locked (reason=initializing)`-Worktree zurück, das `git worktree prune` nicht reapt → Müll-Akkumulation.

## Scope

- Resilienz **nur** für den Kanban-claude-CLI-Spawn-Pfad (worktree-Provisioning im Dispatcher).
- Keine DB-Migration. Keine Verhaltensänderung für echte Worker-Fehler (die zählen weiter normal gegen `max_retries`).

## Anti-Scope (bewusst NICHT in diesem Plan)

- Reaper für die `bridge-cse_*`-Worktrees aus `claude remote-control` (anderer Code-Pfad; eigener Follow-up-Plan).
- Globale gc-Strategie / scheduled gc als systemd-Timer (nur das opt-in `gc.auto=0`-Flag hier).
- Serialisierung/Locking der konkurrierenden git-Schreiber (Auto-Committer/Bridge) — größere Architektur-Frage.
- Push nach `piet-fork` — der ausführende Agent baut auf einem Branch, gated, und **wartet auf Operator-Go** (Governance + hermes-fork-sync).

## Konstanten / Namens-Vertrag (in allen Tasks konsistent verwenden)

| Name | Ort | Wert | Zweck |
|---|---|---|---|
| `WorktreeTimeout` | `kanban_worktrees.py` | `class WorktreeTimeout(WorktreeError)` | transienter git-Timeout, vom permanenten `WorktreeError` unterscheidbar |
| `HERMES_WORKTREE_GIT_TIMEOUT` | Env, gelesen in `kanban_worktrees.py` | default `120` | git-Timeout tunebar machen |
| `SPAWN_RETRY_LIMIT` | `kanban_db.py` | `int(os.environ.get("HERMES_SPAWN_RETRY_LIMIT", "5"))` | max. transiente Re-queues pro Task |
| `HERMES_WORKTREE_DISABLE_AUTOGC` | Env, gelesen in `kanban_worktrees.py` | default aus | opt-in `gc.auto=0` beim Provisionieren |
| `spawn_retry` | `task_events.kind` | — | Event-Marker, über den `SPAWN_RETRY_LIMIT` gezählt wird |

---

## Task 0: Branch + Baseline

**Files:** keine.

- [ ] **Step 1: Arbeitsbranch anlegen (hermes-fork-sync Reflex 2: Safety-Net)**

```bash
cd /home/piet/.hermes/hermes-agent
TS=$(date -u +%Y%m%dT%H%M%SZ); git branch "backup/before-spawn-resilience-main-$TS" main
git status --short   # Fremd-Dateien anderer Sessions NICHT anfassen
git checkout -b fix/claude-cli-spawn-resilience
```

- [ ] **Step 2: Baseline der betroffenen Tests grün?**

Run: `.venv/bin/python -m pytest -q tests/hermes_cli/test_kanban_worktrees.py tests/hermes_cli/test_kanban_db.py -k "Worktree or worktree or Spawn or spawn or Failure or failure" 2>&1 | tail -15`
Expected: PASS (oder bekannte Vorab-Fails notieren, bevor irgendwas geändert wird).

---

## Task 1: `WorktreeTimeout` — git-Timeout klassifizieren

**Files:**
- Modify: `hermes_cli/kanban_worktrees.py` (Exception-Definitionen oben; `_git` an `:241-258`)
- Test: `tests/hermes_cli/test_kanban_worktrees.py`

- [ ] **Step 1: Failing test schreiben**

```python
# tests/hermes_cli/test_kanban_worktrees.py
import subprocess
from hermes_cli import kanban_worktrees as kwt

def test_git_raises_worktree_timeout_on_subprocess_timeout(monkeypatch, repo):
    def fake_run(*a, **k):
        raise subprocess.TimeoutExpired(cmd=a[0], timeout=k.get("timeout", 120))
    monkeypatch.setattr(kwt.subprocess, "run", fake_run)
    with pytest.raises(kwt.WorktreeTimeout):
        kwt._git(repo, "status")
```

- [ ] **Step 2: Test laufen, Fehlschlag bestätigen**

Run: `.venv/bin/python -m pytest tests/hermes_cli/test_kanban_worktrees.py::test_git_raises_worktree_timeout_on_subprocess_timeout -v`
Expected: FAIL (`AttributeError: ... WorktreeTimeout` bzw. raw `TimeoutExpired` statt `WorktreeTimeout`).

- [ ] **Step 3: Minimal implementieren**

In `kanban_worktrees.py` neben die bestehende `WorktreeError`-Definition:

```python
class WorktreeTimeout(WorktreeError):
    """A git invocation exceeded its timeout (transient lock contention).

    Subclass of WorktreeError so existing ``except WorktreeError`` keeps
    working, but the dispatcher can isinstance-check it to re-queue instead
    of permanently blocking.
    """
```

In `_git` (`:241-258`) den `subprocess.run` umschließen:

```python
    try:
        proc = subprocess.run(  # noqa: S603 -- fixed argv, no shell
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise WorktreeTimeout(
            f"git {' '.join(args[:3])}… timed out after {timeout}s in {repo}"
        ) from exc
```

- [ ] **Step 4: Test laufen, grün bestätigen**

Run: `.venv/bin/python -m pytest tests/hermes_cli/test_kanban_worktrees.py::test_git_raises_worktree_timeout_on_subprocess_timeout -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban_worktrees.py tests/hermes_cli/test_kanban_worktrees.py
git commit -m "fix(kanban): classify git timeouts as WorktreeTimeout"
```

---

## Task 2: git-Timeout über Env tunebar machen (default 120)

**Files:**
- Modify: `hermes_cli/kanban_worktrees.py` (`GIT_TIMEOUT_SECONDS` `:111`, `_git` `:241-258`)
- Test: `tests/hermes_cli/test_kanban_worktrees.py`

- [ ] **Step 1: Failing test schreiben** — der an `subprocess.run` übergebene Timeout muss dem Env folgen.

```python
def test_git_timeout_follows_env_override(monkeypatch, repo):
    seen = {}
    def fake_run(*a, **k):
        seen["timeout"] = k.get("timeout")
        raise subprocess.TimeoutExpired(a[0], k.get("timeout"))
    monkeypatch.setenv("HERMES_WORKTREE_GIT_TIMEOUT", "37")
    monkeypatch.setattr(kwt.subprocess, "run", fake_run)
    with pytest.raises(kwt.WorktreeTimeout):
        kwt._git(repo, "status")
    assert seen["timeout"] == 37
```

- [ ] **Step 2: Test laufen, Fehlschlag bestätigen**

Run: `.venv/bin/python -m pytest tests/hermes_cli/test_kanban_worktrees.py::test_git_timeout_follows_env_override -v`
Expected: FAIL (`assert 120 == 37` — Default-Param wird zur Import-Zeit gebunden).

- [ ] **Step 3: Minimal implementieren** — Timeout zur Aufrufzeit aus Env lesen.

`GIT_TIMEOUT_SECONDS = 120` bei `:111` bleibt als Default-Wert. `_git`-Signatur und -Kopf:

```python
def _git(
    repo: Path | str,
    *args: str,
    check: bool = True,
    timeout: int | None = None,
) -> str:
    if timeout is None:
        timeout = int(os.environ.get("HERMES_WORKTREE_GIT_TIMEOUT", GIT_TIMEOUT_SECONDS))
    ...
```

> Verify: `import os` ist in `kanban_worktrees.py` vorhanden; falls nicht, oben ergänzen. Caller, die `timeout=MERGE_TIMEOUT_SECONDS` explizit übergeben, bleiben unberührt.

- [ ] **Step 4: Test laufen, grün bestätigen**

Run: `.venv/bin/python -m pytest tests/hermes_cli/test_kanban_worktrees.py::test_git_timeout_follows_env_override -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban_worktrees.py tests/hermes_cli/test_kanban_worktrees.py
git commit -m "feat(kanban): make worktree git timeout env-tunable (HERMES_WORKTREE_GIT_TIMEOUT)"
```

---

## Task 3: Partielles Worktree bei Provisioning-Fehler reapen (kein `initializing`-Leak)

**Files:**
- Modify: `hermes_cli/kanban_worktrees.py` (`ensure_worktree` `:319-372`, `_add` `:346-358`)
- Test: `tests/hermes_cli/test_kanban_worktrees.py`

- [ ] **Step 1: Failing test schreiben** — schlägt `worktree add` fehl (Timeout), müssen `worktree remove --force --force` + `worktree prune` laufen.

```python
def test_ensure_worktree_reaps_partial_on_failure(monkeypatch, repo):
    calls = []
    def fake_git(r, *args, check=True, timeout=None):
        calls.append(tuple(args))
        if args[:2] == ("worktree", "add"):
            raise kwt.WorktreeTimeout("simulated add timeout")
        return ""  # branch-exists check / remove / prune
    monkeypatch.setattr(kwt, "_git", fake_git)
    with pytest.raises(kwt.WorktreeTimeout):
        kwt.ensure_worktree(  # >>> Signatur an :319 verifizieren und 1:1 spiegeln
            repo_root=str(repo),
            wt=repo.parent / "wt" / "x",
            branch="kanban/x",
            base_branch="main",
        )
    assert any(c[:4] == ("worktree", "remove", "--force", "--force") for c in calls)
    assert any(c[:2] == ("worktree", "prune") for c in calls)
```

> Verify vor Step 1: exakte Parameternamen von `ensure_worktree` an `kanban_worktrees.py:319` (z. B. `repo_root`/`wt`/`branch`/`base_branch`) und wie `_branch_exists` `_git` nutzt — `fake_git` ggf. so anpassen, dass `_branch_exists` plausibel `False` liefert.

- [ ] **Step 2: Test laufen, Fehlschlag bestätigen**

Run: `.venv/bin/python -m pytest tests/hermes_cli/test_kanban_worktrees.py::test_ensure_worktree_reaps_partial_on_failure -v`
Expected: FAIL (kein `worktree remove`-Call).

- [ ] **Step 3: Minimal implementieren** — Reap-Helfer + Fehlerpfade in `ensure_worktree`.

Neuer Helfer (nahe `_add`):

```python
def _reap_partial(repo_root: str, wt: Path) -> None:
    """A killed/failed ``worktree add`` can leave a registered,
    ``initializing``-locked worktree that ``prune`` won't reap. Force-remove
    it (double --force overrides the lock), then prune bookkeeping. Best-effort.
    """
    _git(repo_root, "worktree", "remove", "--force", "--force", str(wt), check=False)
    _git(repo_root, "worktree", "prune", check=False)
```

Der `try/except`-Block in `ensure_worktree` (`:346-358`) wird zu:

```python
try:
    _add()
except WorktreeTimeout:
    _reap_partial(repo_root, wt)
    raise  # → Dispatcher klassifiziert als transient (Task 4)
except WorktreeError:
    # removed-but-registered worktree blocks re-adding; prune once and retry.
    _git(repo_root, "worktree", "prune", check=False)
    try:
        _add()
    except WorktreeError:
        _reap_partial(repo_root, wt)
        raise
```

> Wichtig: `WorktreeTimeout` zuerst fangen (Subklasse von `WorktreeError`), damit ein Timeout **nicht** in den inline-Retry läuft (der würde den Dispatcher nochmal bis zu `timeout` Sekunden blockieren). Re-queue übernimmt Task 4.

- [ ] **Step 4: Test laufen, grün bestätigen**

Run: `.venv/bin/python -m pytest tests/hermes_cli/test_kanban_worktrees.py::test_ensure_worktree_reaps_partial_on_failure -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban_worktrees.py tests/hermes_cli/test_kanban_worktrees.py
git commit -m "fix(kanban): reap partial/locked worktree on provisioning failure"
```

---

## Task 4: Dispatcher — transiente Provisioning-Timeouts re-queuen statt blocken (Kern)

**Files:**
- Modify: `hermes_cli/kanban_db.py` (`dispatch_once` except-Block `:10413-10426`; neue Helfer nahe `_record_spawn_failure` `:8738`; Konstante nahe `DEFAULT_FAILURE_LIMIT` `:7728`)
- Test: `tests/hermes_cli/test_kanban_db.py` (Harness von `TestClaudeCliWorkerSpawn` `:3653` als Vorlage)

- [ ] **Step 1: Failing tests schreiben** — drei Fälle: transient → re-queue ohne Budget-Verbrauch; nach Limit → block; permanent → block sofort (Regression).

```python
# tests/hermes_cli/test_kanban_db.py  (Setup an TestClaudeCliWorkerSpawn :3653 spiegeln)
from hermes_cli import kanban_worktrees as kwt

def _count_events(conn, tid, kind):
    return conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind=?", (tid, kind)
    ).fetchone()[0]

def test_transient_provisioning_timeout_requeues_without_burning_budget(kanban_home, monkeypatch):
    monkeypatch.setattr(kwt, "isolation_mode", lambda: "worktree")
    def boom(*a, **k):
        raise kwt.WorktreeTimeout("contention")
    monkeypatch.setattr(kwt, "provision_for_task", boom)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="coder-claude",
                             workspace_kind="worktree", max_retries=1)
        kb.dispatch_once(conn, board="default")   # >>> Signatur an dispatch_once verifizieren
        row = conn.execute(
            "SELECT status, consecutive_failures FROM tasks WHERE id=?", (tid,)
        ).fetchone()
        assert row["status"] == "ready"            # re-queued, nicht blocked
        assert row["consecutive_failures"] == 0    # Budget NICHT verbraucht
        assert _count_events(conn, tid, "spawn_retry") == 1

def test_spawn_retry_budget_exhaustion_blocks(kanban_home, monkeypatch):
    monkeypatch.setattr(kwt, "isolation_mode", lambda: "worktree")
    monkeypatch.setattr(kwt, "provision_for_task",
                        lambda *a, **k: (_ for _ in ()).throw(kwt.WorktreeTimeout("x")))
    monkeypatch.setenv("HERMES_SPAWN_RETRY_LIMIT", "2")
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="coder-claude",
                             workspace_kind="worktree", max_retries=1)
        for _ in range(3):
            kb.dispatch_once(conn, board="default")
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
        assert row["status"] == "blocked"          # nach Spawn-Budget → normaler Block

def test_permanent_provisioning_error_blocks_immediately(kanban_home, monkeypatch):
    monkeypatch.setattr(kwt, "isolation_mode", lambda: "worktree")
    monkeypatch.setattr(kwt, "provision_for_task",
                        lambda *a, **k: (_ for _ in ()).throw(kwt.WorktreeError("disk full")))
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="coder-claude",
                             workspace_kind="worktree", max_retries=1)
        kb.dispatch_once(conn, board="default")
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
        assert row["status"] == "blocked"          # permanenter Fehler: unverändertes Verhalten
```

> Verify vor Step 1: exakte Signatur von `kb.dispatch_once` (Args/Kwargs) und von `kb.create_task` (`max_retries`-Kwarg) anhand der bestehenden `TestClaudeCliWorkerSpawn`-Tests an `:3653` — Setup 1:1 übernehmen.

- [ ] **Step 2: Tests laufen, Fehlschlag bestätigen**

Run: `.venv/bin/python -m pytest tests/hermes_cli/test_kanban_db.py -k "transient_provisioning or spawn_retry_budget or permanent_provisioning" -v`
Expected: FAIL (Task wird heute sofort `blocked`, kein `spawn_retry`-Event).

- [ ] **Step 3: Minimal implementieren**

Konstante nahe `DEFAULT_FAILURE_LIMIT` (`:7728`):

```python
SPAWN_RETRY_LIMIT = int(os.environ.get("HERMES_SPAWN_RETRY_LIMIT", "5"))
```

Neue Helfer nahe `_record_spawn_failure` (`:8738`):

```python
def _count_spawn_retries(conn, task_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='spawn_retry'",
        (task_id,),
    ).fetchone()[0]

def _record_spawn_retry(conn, task_id: str, reason: str) -> None:
    """Transient provisioning failure: release the claim, end the run, put the
    task back to ``ready``, log a ``spawn_retry`` event — WITHOUT touching
    consecutive_failures. The next dispatch loop retries once contention clears.
    """
    _record_task_failure(
        conn, task_id, reason,
        outcome="spawn_retry",
        release_claim=True,
        end_run=True,
        count_failure=False,   # >>> neuer Param, siehe unten
    )
    _add_event(conn, task_id, "spawn_retry", {"reason": reason[:500]})  # Helfer-Name an Bestand spiegeln
```

`_record_task_failure` (`:8582-8733`) bekommt einen Schalter `count_failure: bool = True`; im Body wird die Inkrement-/Threshold-Logik (`:8640-8652`) übersprungen, wenn `count_failure` False ist (Claim/Run werden trotzdem sauber geschlossen, Status zurück auf `ready`).

> Verify: exakter Name/Signatur der Event-Schreib-Funktion (im Plan `_add_event`) an bestehenden Aufrufen in `kanban_db.py` ablesen und 1:1 verwenden. `_record_spawn_failure` bleibt unverändert für den permanenten/erschöpften Pfad.

Der except-Block in `dispatch_once` (`:10413-10426`) wird zu:

```python
except Exception as exc:
    transient = isinstance(exc, _kwt.WorktreeTimeout)
    if transient and _count_spawn_retries(conn, claimed.id) < SPAWN_RETRY_LIMIT:
        _record_spawn_retry(
            conn, claimed.id, f"worktree provisioning (transient): {exc}",
        )
    else:
        auto = _record_spawn_failure(
            conn, claimed.id, f"worktree provisioning: {exc}",
            failure_limit=failure_limit,
        )
        if auto:
            result.auto_blocked.append(claimed.id)
    continue
```

> `_kwt` ist das bereits in `dispatch_once` importierte `kanban_worktrees`-Alias (`from hermes_cli import kanban_worktrees as _kwt`, siehe `:10414`).

- [ ] **Step 4: Tests laufen, grün bestätigen**

Run: `.venv/bin/python -m pytest tests/hermes_cli/test_kanban_db.py -k "transient_provisioning or spawn_retry_budget or permanent_provisioning" -v`
Expected: PASS (3 Tests)

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban_db.py tests/hermes_cli/test_kanban_db.py
git commit -m "fix(kanban): re-queue transient worktree-spawn timeouts instead of blocking"
```

---

## Task 5 (optional, opt-in): `gc.auto=0` beim Provisionieren

**Files:**
- Modify: `hermes_cli/kanban_worktrees.py` (`ensure_worktree`, nach erfolgreichem `_add()` `:352-358`)
- Test: `tests/hermes_cli/test_kanban_worktrees.py`

> Nur umsetzen, wenn Auto-gc als Contention-Quelle bestätigt ist. Default-Verhalten bleibt unverändert; das Flag ist opt-in. **Voraussetzung beim Aktivieren:** ein separater, regelmäßiger `git gc`-Lauf (z. B. systemd-Timer), sonst wachsen lose Objekte unbegrenzt.

- [ ] **Step 1: Failing test**

```python
def test_provision_disables_autogc_when_opted_in(monkeypatch, repo):
    calls = []
    real = kwt._git
    def spy(r, *args, **k):
        calls.append(tuple(args))
        return real(r, *args, **k)
    monkeypatch.setenv("HERMES_WORKTREE_DISABLE_AUTOGC", "1")
    monkeypatch.setattr(kwt, "_git", spy)
    kwt.ensure_worktree(repo_root=str(repo), wt=repo.parent/"wt"/"g",
                        branch="kanban/g", base_branch="main")  # Signatur spiegeln
    assert ("config", "gc.auto", "0") in [c[:3] for c in calls]
```

- [ ] **Step 2: Test laufen, Fehlschlag bestätigen**

Run: `.venv/bin/python -m pytest tests/hermes_cli/test_kanban_worktrees.py::test_provision_disables_autogc_when_opted_in -v`
Expected: FAIL

- [ ] **Step 3: Minimal implementieren** — nach erfolgreichem `_add()`:

```python
if os.environ.get("HERMES_WORKTREE_DISABLE_AUTOGC") == "1":
    _git(repo_root, "config", "gc.auto", "0", check=False)
```

- [ ] **Step 4: Test laufen, grün bestätigen** → PASS
- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban_worktrees.py tests/hermes_cli/test_kanban_worktrees.py
git commit -m "feat(kanban): opt-in gc.auto=0 on worktree provisioning (HERMES_WORKTREE_DISABLE_AUTOGC)"
```

---

## Task 6: Gate + Doku + Handoff

- [ ] **Step 1: Collection-Sweep (fängt versehentlich gedroppte Imports)**

Run: `.venv/bin/python -m pytest --co -q tests/hermes_cli/ 2>&1 | tail -3`
Expected: `0 errors`

- [ ] **Step 2: Betroffene Test-Dateien voll laufen**

Run: `.venv/bin/python -m pytest -q tests/hermes_cli/test_kanban_worktrees.py tests/hermes_cli/test_kanban_db.py --timeout=120 --timeout-method=thread 2>&1 | tail -20`
Expected: PASS (Voll-Suite-Hang-Falle vermeiden — nur betroffene Dateien, mit Timeout-Guard; vgl. hermes-fork-sync).

- [ ] **Step 3: Env-Doku ergänzen** — die drei neuen Env-Keys (`HERMES_WORKTREE_GIT_TIMEOUT`, `HERMES_SPAWN_RETRY_LIMIT`, `HERMES_WORKTREE_DISABLE_AUTOGC`) dort eintragen, wo Hermes seine Env-Keys dokumentiert (Verify: `docs/` bzw. config-Referenz im Repo). Diesen Plan auf `status: done` setzen.

- [ ] **Step 4: Handoff (KEIN Push ohne Operator-Go)**

`git fetch piet-fork --prune` + Fast-Forward-Check; dann Operator fragen, bevor `git push piet-fork main:main` (bzw. Branch-Merge). Kein Force-Push. Keine `supabase/migrations/`-artige harte Zone betroffen (reiner Python-Change, keine DB-Migration).

---

## Self-Review (vom Autor durchgeführt)

1. **Spec-Coverage:** beobachtetes Failure-Muster (transient timeout → sofort blocked) → Task 4. `initializing`-Leak → Task 3. „Timeout hochsetzen" → Task 2 (tunebar). Optionale Contention-Quelle Auto-gc → Task 5. ✓
2. **Placeholder-Scan:** keine „TBD/handle edge cases"; alle Code-Schritte mit konkretem Code. Drei explizite „Verify Signatur"-Hinweise sind bewusste Grounding-Checks (Dispatcher-/`ensure_worktree`-Signaturen, Event-Helfer-Name), kein offener Platzhalter im Code.
3. **Typ-/Namens-Konsistenz:** `WorktreeTimeout`, `SPAWN_RETRY_LIMIT`, `spawn_retry` (Event-kind), `_record_spawn_retry`/`_count_spawn_retries`, `count_failure`-Param, `HERMES_WORKTREE_GIT_TIMEOUT`/`HERMES_SPAWN_RETRY_LIMIT`/`HERMES_WORKTREE_DISABLE_AUTOGC` — überall identisch verwendet. ✓

## Risiken

- **Endlos-Re-queue bei chronischer Contention:** durch `SPAWN_RETRY_LIMIT` gedeckelt; danach normaler Block. Permanente Fehler (`WorktreeError`, z. B. Disk voll) blocken weiterhin sofort.
- **`count_failure`-Schalter in `_record_task_failure`:** zentrale Funktion — Caller-Grep (`rg "_record_task_failure\("`) und Default `True` sichern, dass bestehende Aufrufer unverändert zählen.
- **Dispatcher-Latenz:** ein echter Stall blockiert `dispatch_once` weiterhin bis `timeout` (default 120 s) für genau diesen Task; Re-queue verhindert nur die *dauerhafte* Blockade, nicht die einmalige Wartezeit. Kürzerer `HERMES_WORKTREE_GIT_TIMEOUT` = schnelleres Re-queue, mehr Spawn-Versuche — Trade-off, per Env tunebar.
- **Live-Checkout:** Umsetzung läuft im stark geteilten `~/.hermes/hermes-agent` → hermes-fork-sync-Reflexe beachten (Fremd-Dateien nicht anfassen, Backup-Branch, kein Push ohne Go).
