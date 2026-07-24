# Giant-Module Modularization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Re-aimed 2026-07-24** by `docs/refactor/2026-07-24-modularization-delta-merge-capability.md` (operator decision, grill session `work:5`). The overriding goal is now **staying merge-capable against upstream**; CodeGraph visibility is kept only where it costs nothing. Scope reduced from five files to one. The tooling (Tasks 1–4) is unchanged and still correct.

**Goal:** Extract the fork-owned portion of `hermes_cli/kanban_db.py` into a fork-owned package so the file stops being a 1.59 MB battleground against upstream — leaving a module file that is close to upstream's own copy, mergeable, and (as a side effect) back under CodeGraph's 1 MiB limit.

**Architecture:** `hermes_cli/kanban_db.py` **stays a plain module file**, not a package — upstream's future diffs are addressed to that literal path and would fail to apply against a directory. It keeps only symbols upstream also carries. The 733 fork-only symbols move to a new fork-owned package `hermes_cli/kanban_ext/`, split into submodules by the same deterministic AST tooling. `kanban_db.py` ends with one explicit re-export block from that package, so all ~275 importers and the tests that monkeypatch private symbols are untouched. That trailing block is the only fork-owned hunk left in an upstream-owned file.

**Tech Stack:** Python 3 `ast` (stdlib only, no new dependencies), PyYAML for boundary maps, pytest, `scripts/run-affected.sh`, `git merge-tree`, `codegraph`.

## Global Constraints

- **Scope is `hermes_cli/kanban_db.py` alone.** `gateway/run.py`, `hermes_cli/web_server.py`, `cli.py` and `hermes_cli/main.py` are explicitly out — see "Why the other four are out" below.
- **`hermes_cli/kanban_db.py` must remain a file, never a directory.** This is the load-bearing constraint of the re-aim. A package at that path makes every future upstream diff unapplyable.
- **Pure-move discipline.** The extraction commit contains moves only. No renames, no reformatting, no bug fixes, no docstring corrections — explicitly including the five defects under Follow-ups. Mixing fixes in invalidates the equivalence gate.
- **Zero production call-site edits.** No file outside `hermes_cli/` changes its imports or calls. (Test-internal `monkeypatch` string targets are the single, enumerated exception — Task 6.)
- **No model retypes code.** Every moved line is moved by `split_module.py`. Models author boundary maps (YAML) and tool code only.
- **Standing rule that outlives this plan.** New fork code never goes into an upstream-owned file. It goes into a fork-owned module reached by a minimal hook. Without this rule `kanban_db.py` re-accumulates and the work is undone within months — it went 9,135 → 38,843 lines while the rule was absent.
- **Model routing (fixed by the approved spec):** `split_module.py` / `api_snapshot.py` + their tests → **Codex (gpt-5.6-sol)**. The boundary map → **qwen 3.8 via `claude-qwen -p` one-shot only**. Boundary-map approval, gate verification, merge judgment → **Claude Opus 5**.
- **ToS constraint (binding):** `/usr/local/bin/claude-qwen` line 12 — Token Plan is interactive coding/agent use only. qwen may be used for session-driven one-shots supervised by an interactive session. It must **never** be wired as a kanban lane or cron worker.
- **Repo git rules:** `origin` is NousResearch upstream — never push there. Push only to `piet-fork`, fast-forward, never `--force`. `git status --short` before any git action; this checkout is edited by parallel sessions.
- **Test scope:** `scripts/run-affected.sh` while building. Before merge to main: one collection sweep (`pytest --co -q tests/`) plus affected tests. Never run the full suite in both worker and verifier.
- **Sequencing (operator-set):** the Codex upstream sync in `.claude/worktrees/codex-upstream-sync-20260724` (branch `codex/upstream-sync-20260724`, merging `origin/main` = `306c9f766`) **lands before this restructure starts**. Two operations must not restructure the same file at once. Task 7 Step 0 verifies it landed.
- **Base commit:** `e86c8a66b` on `main`. Upstream reference: `origin/main` = `306c9f766`. Merge-base: `3bfa6001f` (2026-07-15).

  **Baseline refreshed 2026-07-24.** The figures below were first measured on `1ef243502`; they are now stated for `e86c8a66b`. The only change is `+10/−1` inside `scores_digest`, landed by the `kanban/t_57aaa085` merge (`8f29783e0`, via `aa36b6869`). `scores_digest` is a fork-only symbol, so the effect is confined to two numbers — file lines `38,834 → 38,843` and fork-only lines `23,736 → 23,745`. **All symbol counts are unchanged** (973 total; 733 / 111 / 129), so the ownership map and every structural conclusion stand. Confirmed independently by the tooling and by hand. Historical figures inside the Amendment section at the end deliberately keep the `1ef243502` numbers, because they record what was measured there.

## Acceptance criteria

1. **Mergeability (the point of the work).** Conflict hunks in `hermes_cli/kanban_db.py` when upstream's delta is replayed onto the fork, measured before and after. **Baseline: 14 conflict regions, 568 conflicted lines, file 1,589,570 B.** The bar is **strictly fewer than 14** — operator-set and not softened. It is reached by Task 8 (hooks), not by Task 7 (extraction); see the measurement below for why. The metric must be pinned, as follows.

   **Pin the merge base.** Use the explicit-base form, not the automatic one:

   ```bash
   git merge-tree --write-tree --merge-base=3bfa6001f HEAD 306c9f766
   ```

   The automatic form (`git merge-tree --write-tree HEAD origin/main`) reports the same 14 today, but becomes **structurally meaningless the moment the Codex upstream sync lands**: that merge makes `origin/main` an ancestor of `main`, so the computed base collapses to `origin/main` itself and the conflict count drops to 0 — measuring nothing, because there is nothing left to merge. The pinned form replays the *same* known upstream delta (`3bfa6001f → 306c9f766`) against whatever `HEAD` is, so before/after stay comparable across the sync. Verified: both forms report 14 on `1ef243502`.

   **Note on what the sync does to `kanban_db.py`.** In that merge the file is resolved **as ours** — the staged blob is byte-identical to the fork's version. Upstream's `+693` lines of `kanban_db.py` work between `3bfa6001f` and `306c9f766` are therefore recorded as merged without being applied, and git will not offer them again. That is a deliberate call and a reasonable one — applying them into a 38.8k-line divergent file is precisely what this restructure exists to make possible *later* — but it should be a known cost, not a surprise: this restructure does not recover them. Recovering them is a separate, explicit act (`git diff 3bfa6001f 306c9f766 -- hermes_cli/kanban_db.py` replayed by hand), best done after the extraction, when the file is small enough to reason about.
2. **Fork-owned lines in the upstream-owned file drop to ~zero.** `hermes_cli/kanban_db.py` currently carries 23,745 lines of fork-only symbols. After extraction it must carry none except the re-export block and the small import-time carve-out (Task 5 Step 3).
3. **CodeGraph payoff.** `stat -c%s hermes_cli/kanban_db.py` < 1,048,576, and `codegraph query dispatch_once` returns the real definition.
4. **API equivalence.** `api_snapshot.py --compare` reports `API IDENTICAL`, and affected tests are green.

### Operator decision 2026-07-24 23:20 — criterion 1 stays hard, scope extends to hooks

The two-step grading proposed below was **put to the operator and rejected**. The full variant is binding: the restructure extends to the hooks immediately, and criterion 1 keeps its strict form — *strictly fewer* conflict hunks, or it does not land. Brief: `/home/piet/vault/03-Agents/Claude/handoffs/2026-07-24-hook-scope-brief-work5.md`.

The reasoning is the measurement below, which the operator accepted: extraction alone cannot move the number, so the work that *does* move it is now in scope. Session `work:1` additionally objected that `create_task` is the kanban lifecycle spine (create → claim → complete) and that an error there hits every worker. That objection was also overruled. It is **not reopened here**; it is answered instead by the characterization-test requirement in Task 8, which is mandatory and gates every hook.

Everything in the section immediately below remains accurate as *measurement*. Only its recommendation is superseded.

### Why extraction alone cannot move criterion 1 — measured

The delta requires *strictly fewer* conflict hunks after the split, and states that if the count does not drop, "the ownership map was wrong and the split must not land." Measured on `1ef243502`, that criterion **cannot be met by this change**, and the ownership map is not the reason.

All 14 conflict regions sit **inside upstream-owned symbols that stay in `kanban_db.py`**:

| enclosing symbol | ownership | hunks |
|---|---|---:|
| `create_task` | upstream, diverged | 6 |
| `Task` | upstream, diverged | 2 |
| `_backup_corrupt_db`, `_guard_existing_db_is_healthy`, `list_comments`, `_cleanup_worker_tmux`, `_default_spawn` | upstream, diverged | 1 each |
| `DEFAULT_BUSY_TIMEOUT_MS` | upstream, identical | 1 |
| **any fork-only symbol** | — | **0** |

Extracting the 733 fork-only symbols removes zero of them, because every conflict is caused by the fork having edited *inside* a function upstream also edited. Those bodies stay verbatim by design. The delta itself identifies this bucket as "the one that decides whether this exercise actually succeeds" — the measurement confirms it decides it, and it decides against criterion 1 as currently sequenced.

Reducing those conflicts requires the *second* piece of work the delta names as a later candidate: reducing fork divergence inside the 129 shared symbols to hooks. That touches behaviour and cannot ride along with a pure move.

~~Recommendation: re-scope criterion 1 to "must not increase" and defer strict decrease to a follow-on pass.~~ **Superseded by the operator decision above.** Strict decrease stays, and the hook work that achieves it is now Task 8 of this plan rather than a sequel. The extraction (Task 7) keeps its own value — it is the precondition that makes the hook work tractable, it delivers the CodeGraph payoff, and the standing rule needs somewhere to put new fork code — but it is no longer the end of the job.

### A note on hunk attribution: three measurements, one work list

Attributing each conflict hunk to an enclosing symbol turns out to be heuristic-sensitive. Three independent attempts on the same 14 hunks disagreed on 2 of them:

| source | upstream / fork | notable difference |
|---|---|---|
| this plan, regex walk upward | 13 / 1 | placed one hunk in `DEFAULT_BUSY_TIMEOUT_MS` |
| `work:1` brief | 13 / 1 | placed one hunk in `resolve_vault_memory_link_path`; lists `list_comments` and `_backup_corrupt_db` |
| this plan, anchor-mapped to `HEAD` lines | 12 / 2 | placed hunks in `HANDOFF_RAW_ARTIFACT_KIND` and at module level; `list_comments`/`_backup_corrupt_db` absent |

All three agree on what matters: **`create_task` carries 6 hunks**, the great majority sit in upstream symbols, and only 1–2 sit in fork symbols. The disagreement is confined to the tail.

**So the work list is not driven by hunk attribution.** It is driven by *how many fork lines sit inside each upstream symbol*, measured by `difflib` of symbol bodies against `origin/main` — a robust signal that does not depend on merge-region alignment. That measurement is reproduced independently below and matches the `work:1` brief to within ±1 line on two rows.

---

## Measured ground truth (verified on `1ef243502`, do not re-derive)

### Why the other four files are out of scope

Byte sizes against CodeGraph's `MAX_FILE_SIZE` (1,048,576 B), verified with `stat -c%s` and `git show origin/main:<file> | wc -c`:

| file | fork today | CodeGraph | upstream's own copy |
|---|---:|---|---:|
| `hermes_cli/kanban_db.py` | 1,589,570 | **blind** | 406,482 |
| `gateway/run.py` | 1,062,191 | **blind** | **1,178,861** |
| `hermes_cli/web_server.py` | 803,509 | visible | — |
| `cli.py` | 772,748 | visible | — |
| `hermes_cli/main.py` | 618,546 | visible | — |

- **Three of the five are already CodeGraph-visible.** The original spec's premise that five modules are invisible does not survive measurement. Splitting them buys nothing toward either goal.
- **`gateway/run.py` must not be split.** It is 97.6% upstream's file (+519 fork lines vs +3,887 upstream lines since the merge-base), and upstream's own copy is *already* 1.18 MB — blind at upstream too, and growing. Visibility there is purchasable only with permanent divergence against the largest incoming change stream in the repo. `rg` + `docs/kanban/LIFECYCLE.md` remain the documented navigation route for it.
- **`hermes_cli/web_server.py`** is genuinely contested (+3,667 fork vs +3,681 upstream) and already visible. It needs its own ownership analysis in a later pass.

That leaves exactly one file where both goals are reachable at once: `kanban_db.py`, which the fork grew 9,135 → 38,843 lines while upstream contributed 693.

### Ownership map of `kanban_db.py` (fork vs `origin/main`)

A top-level symbol is `UPSTREAM` if a symbol of that name exists in `git show origin/main:hermes_cli/kanban_db.py`; otherwise `FORK`. Measured: fork has 973 top-level symbols, upstream has 259.

| bucket | symbols | fork lines | disposition |
|---|---:|---:|---|
| **FORK-only** | 733 | 23,745 | move out to `hermes_cli/kanban_ext/` |
| **UPSTREAM, body byte-identical** | 111 | 1,293 | stay, untouched |
| **UPSTREAM, body diverged** | 129 | 11,419 | stay; each is a standing conflict site (see below) |
| upstream-only, absent from fork | 19 | — | nothing to do |

**Projected residual size of `kanban_db.py`: ≈ 565 KB** of symbol bodies plus header and re-export block. That is comfortably under the 1 MiB limit, so criterion 3 is met — but it is *not* the ≈406 KB the delta estimated, because the 129 shared symbols are themselves heavily fork-grown (11,419 fork lines against far fewer upstream lines). The largest divergences:

| symbol | fork lines | upstream lines |
|---|---:|---:|
| `_dispatch_once_locked` | 1,297 | 400 |
| `complete_task` | 612 | 208 |
| `_migrate_add_optional_columns` | 569 | 256 |
| `create_task` | 491 | 378 |
| `detect_crashed_workers` | 456 | 268 |
| `SCHEMA_SQL` | 407 | 187 |
| `_record_task_failure` | 404 | 162 |

These 129 symbols are the entire remaining conflict surface. Seven of them carry the conflicts and are the work list for Task 8.

### Hook work list — fork lines inside upstream symbols

Measured by `difflib.unified_diff` of each symbol's body, `origin/main` versus `HEAD`. `+` = present only in the fork (fork-added lines living inside upstream's function); `−` = present only upstream (incoming upstream work the fork has diverged from). Reproduced independently; matches the `work:1` brief to ±1 on `create_task` and `_default_spawn`.

| symbol | hunks | fork lines inside | upstream-only lines | measure |
|---|---:|---:|---:|---|
| `create_task` | 6 | 276 | 163 | **hook — highest risk, lifecycle spine** |
| `_default_spawn` | 1 | 179 | 62 | hook |
| `Task` (class, incl. method `from_row`) | 1–2 | 149 | 35 | hook / field separation |
| `_guard_existing_db_is_healthy` | 1–2 | 45 | 56 | hook |
| `_cleanup_worker_tmux` | 1 | 5 | 2 | trivial realignment |
| `list_comments` | 0–1 | 3 | 0 | trivial realignment — the `kind` fallback is the only fork part |
| `_backup_corrupt_db` | 0–1 | **0** | 3 | **no fork code — resolve as *theirs*, do not hook** |

Net **657 distinct fork lines** inside upstream symbols (the raw sum across the seven; `from_row`'s 89 lines are already inside `Task`'s 149 and are not counted twice).

**Two traps in this list, both verified:**

1. **`from_row` is a method of `Task`**, not a top-level symbol — confirmed: it appears at body line 131 inside `Task`, and `from_row` is absent from the top-level symbol table. Treating it as its own symbol would double-count it and move it twice.
2. **Not every conflict is the fork's fault.** `_backup_corrupt_db` contains **0** fork lines — confirmed independently. Its conflict is pure upstream evolution (the `_prune_corrupt_backups` retention cap) against an older fork state. The correct resolution is *theirs*; building a hook there would add complexity for no benefit.

### Cross-file references created by the ownership cut

Classified with `layering.py`'s import-time/runtime distinction:

| direction | import-time | runtime |
|---|---:|---:|
| FORK → UPSTREAM | 10 | 354 |
| UPSTREAM → FORK | **3** | 233 |

> **Annotations do not count here — checked, twice.** `hermes_cli/kanban_db.py` **does** carry `from __future__ import annotations` (line 71, after a 69-line module docstring), so every annotation is a string and is never evaluated at import time. Runtime proof: `inspect.signature(kanban_db.schedule_task).parameters['due_at'].annotation` is the *string* `'int | None | _ScheduleDueUnspecified'`.
>
> This was briefly recorded the other way round in an earlier revision of this plan, after a `head -35 | rg 'from __future__'` check returned nothing — the import sits at line 71, below the docstring the check truncated away. Had annotations been evaluated, 59 further import-time references would exist and the carve-out would grow by one (`_ScheduleDueUnspecified`). They are not, so it does not.
>
> `import_time_names` must still become future-aware before the tooling is pointed at any *other* module: a file without the future import genuinely does evaluate its annotations. For this target the future-aware result is identical to the current one.

- The **354 + 233 runtime** references are fine. `kanban_ext` reaches `kanban_db` through a module-object import (`from hermes_cli import kanban_db` + `kanban_db.foo(...)`), resolved at call time, and `kanban_db`'s re-export block sits at the very end of the file. The resulting import cycle is benign, exactly as the empirical test in Task 4 shows.
- The **10 FORK → UPSTREAM import-time** references are fine too: `kanban_ext` loads after `kanban_db`'s body has run, so those names already exist.
- The **3 UPSTREAM → FORK import-time** references are **fatal to a naive cut** and drive a carve-out rule in Task 5 Step 3:

  | upstream symbol | needs fork symbol | why it is import-time |
  |---|---|---|
  | `_dispatch_once_locked` | `DEFAULT_AUTO_RETRY_BLOCKED_BACKOFF_SECONDS` | default argument |
  | `dispatch_once` | `DEFAULT_AUTO_RETRY_BLOCKED_BACKOFF_SECONDS` | default argument |
  | `schedule_task` | `_SCHEDULE_DUE_UNSPECIFIED` | default argument |

### Structural facts

`kanban_db.py`'s top level is unusually clean — 1,004 nodes: 1 docstring, 30 imports, 284 assignments, 671 functions, 18 classes, and **zero** conditional or executable top-level statements. AST-based moving is safe here.

---

## File structure

**Created (tooling, lands once, reused five times):**

- `scripts/refactor/__init__.py` — empty, makes the package importable by tests
- `scripts/refactor/api_snapshot.py` — records/diffs a module's public surface. The equivalence gate.
- `scripts/refactor/layering.py` — AST symbol graph, import-time vs runtime reference classification, section assignment, cycle detection. Shared by both CLIs.
- `scripts/refactor/split_module.py` — `--analyze` (emit layering report) and `--apply` (perform the move)
- `tests/refactor/test_layering.py`
- `tests/refactor/test_api_snapshot.py`
- `tests/refactor/test_split_module.py`
- `tests/refactor/fixtures/` — synthetic modules exercising forward edges, back-edges, decorators, class bases, default args

**Created (the extraction):**

- `docs/refactor/boundary-map.kanban_ext.yaml` — the approved ownership-derived boundary map, kept in the repo as the record
- `docs/refactor/ownership.kanban_db.md` — the three-bucket ownership report, including the 129 diverged symbols that become the hook-reduction backlog
- `hermes_cli/kanban_ext/` — the fork-owned package: `__init__.py` plus ~8–12 submodules of 2,000–3,000 lines

**Modified:**

- `hermes_cli/kanban_db.py` — **stays a file**; loses 733 fork-only symbols, gains one trailing re-export block
- `docs/kanban/LIFECYCLE.md` — anchors re-pointed; symbols that moved now live in `hermes_cli/kanban_ext/<module>.py`
- `scripts/check_kanban_lifecycle_anchors.py` — must resolve anchors across both files
- the enumerated test lines with `monkeypatch`/`patch` string targets into `hermes_cli.kanban_db.<symbol>` (Task 6)

---

## Task 0: Branch triage — CLOSED 2026-07-24

**The split is no longer gated on branch triage.** Operator decision taken and executed; `main` is at `f3e6afdd6`. Full record with evidence: `docs/refactor/branch-triage-2026-07-24.md`, sections *Decision* and *Execution record*.

Outcome: ten of the eleven branches were archived as `archive/pre-modularization/<name>` and deleted; one was **landed first**. Branch forest 105 → 94. Any branch is recoverable with:

```bash
git branch <name> archive/pre-modularization/<name>
```

- [x] **Step 1: Triage analysis** — committed `b20d0c8f3`.
- [x] **Step 2: Operator decision** — taken 2026-07-24, recorded in the triage document.
- [x] **Step 3: Land `kanban/t_57aaa085`** — merged as `8f29783e0`. Real conflict resolution in two files in favour of `main`'s newer venv precedence; all of the branch's new tests preserved. Gates green: 13 passed, ruff clean, 48,666 collected, `run-affected` 50 files / 1,572 tests / 0 failures.
- [x] **Step 4: Archive + delete the other ten** — done, all tagged.
- [x] **Step 5: Verify and record** — done.

### Correction to this plan's earlier finding

The recommendation to drop all eleven was **right for ten and wrong for one**.

`kanban/t_57aaa085` carried genuinely unlanded work: `tests/hermes_cli/test_scores_digest.py` at **592 lines against 288 on `main`**, plus +188 lines in `hermes_cli/kanban.py` and the `HERMES_HOME` venv fix in `scripts/cron/scores-weekly-digest.sh`. The triage raised the right caveat — "dated today, 10 commits, do not delete without an explicit call" — and then filed it in the archive bucket anyway. **The caveat should have overridden the bucket.** The lesson is narrow and worth carrying into the rest of this plan: a branch's `kanban_db.py` delta being trivial says nothing about the rest of its diff, and this triage measured only the `kanban_db.py` delta before bucketing.

`backup/grok-kanban-block-kind-20260715-pre-rebase` was independently re-verified as genuinely superseded: `block_kind`/`system park` markers 5:5 on both sides, and `tests/hermes_cli/test_kanban_block_kinds.py` is 696 lines on `main`. That classification held.

### What this leaves for the restructure

No open branch work touches `hermes_cli/kanban_db.py`. The remaining sequencing constraint is not branches but the **Codex upstream sync** — see Global Constraints and Task 7 Step 0.

---

## Task 1: `api_snapshot.py` — the equivalence gate

**Files:**
- Create: `scripts/refactor/__init__.py`, `scripts/refactor/api_snapshot.py`
- Test: `tests/refactor/test_api_snapshot.py`

**Interfaces:**
- Produces: `snapshot(module_name: str) -> dict` returning `{"module": str, "symbols": {name: descriptor}}` where `descriptor` is a dict with keys `kind` (`"function"` | `"class"` | `"value"`), `signature` (str or `None`), and for classes `methods` (dict of method name → signature). `diff(before: dict, after: dict) -> list[str]` returns human-readable difference lines; empty list means identical.
- Consumed by: Task 4's round-trip test, and Task 7 Steps 1 and 4 (the equivalence gate around the extraction).

The snapshot must be taken by **importing** the module and introspecting it, not by parsing source. That is the whole point: it proves what importers actually see, including the re-exported package surface.

- [ ] **Step 1: Write the failing test**

Create `tests/refactor/test_api_snapshot.py`:

```python
import textwrap

import pytest

from scripts.refactor import api_snapshot


@pytest.fixture
def module_pair(tmp_path, monkeypatch):
    """A single-file module and an equivalent package, both named the same."""
    monkeypatch.syspath_prepend(str(tmp_path))
    return tmp_path


def test_snapshot_records_functions_classes_and_values(module_pair, monkeypatch):
    (module_pair / "flatmod.py").write_text(textwrap.dedent("""
        CONST = 7
        _PRIVATE = "p"

        def alpha(a, b=1, *args, **kw):
            return a

        def _helper(x):
            return x

        class Thing:
            def method(self, q):
                return q
    """))
    snap = api_snapshot.snapshot("flatmod")
    assert snap["symbols"]["alpha"]["kind"] == "function"
    assert snap["symbols"]["alpha"]["signature"] == "(a, b=1, *args, **kw)"
    assert snap["symbols"]["Thing"]["kind"] == "class"
    assert snap["symbols"]["Thing"]["methods"]["method"] == "(self, q)"
    assert snap["symbols"]["CONST"]["kind"] == "value"
    # private names are part of the surface: tests monkeypatch them
    assert "_helper" in snap["symbols"]
    assert "_PRIVATE" in snap["symbols"]


def test_diff_is_empty_for_module_converted_to_package(module_pair):
    (module_pair / "flat2.py").write_text(textwrap.dedent("""
        LIMIT = 3

        def alpha(a, b=1):
            return a

        def _helper(x):
            return x
    """))
    before = api_snapshot.snapshot("flat2")

    (module_pair / "flat2.py").unlink()
    pkg = module_pair / "flat2"
    pkg.mkdir()
    (pkg / "consts.py").write_text("LIMIT = 3\n")
    (pkg / "funcs.py").write_text(textwrap.dedent("""
        def alpha(a, b=1):
            return a

        def _helper(x):
            return x
    """))
    (pkg / "__init__.py").write_text(textwrap.dedent("""
        from .consts import LIMIT
        from .funcs import alpha, _helper
    """))

    after = api_snapshot.snapshot("flat2", fresh=True)
    assert api_snapshot.diff(before, after) == []


def test_diff_reports_missing_and_changed_symbols():
    before = {"module": "m", "symbols": {
        "kept": {"kind": "function", "signature": "(a)"},
        "dropped": {"kind": "function", "signature": "()"},
        "retyped": {"kind": "function", "signature": "(a)"},
    }}
    after = {"module": "m", "symbols": {
        "kept": {"kind": "function", "signature": "(a)"},
        "retyped": {"kind": "function", "signature": "(a, b)"},
        "added": {"kind": "value", "signature": None},
    }}
    lines = api_snapshot.diff(before, after)
    assert any("dropped" in l and "missing" in l for l in lines)
    assert any("retyped" in l and "signature" in l for l in lines)
    assert any("added" in l for l in lines)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/refactor/test_api_snapshot.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scripts.refactor'`

- [ ] **Step 3: Implement `api_snapshot.py`**

Create `scripts/refactor/__init__.py` (empty file) and `scripts/refactor/api_snapshot.py`:

```python
"""Record and diff a module's public surface.

This is the equivalence gate for the giant-module split: it proves that
converting a module into a package left every symbol importers can reach
byte-identical in name, kind and signature.

The snapshot is taken by IMPORTING the module, not by parsing it, so it
reflects what an importer actually sees through the package __init__.
"""
from __future__ import annotations

import argparse
import importlib
import inspect
import json
import sys


def _signature(obj) -> str | None:
    try:
        return str(inspect.signature(obj))
    except (TypeError, ValueError):
        return None


def _describe(obj) -> dict:
    if inspect.isclass(obj):
        methods = {}
        for name, member in sorted(vars(obj).items()):
            if inspect.isfunction(member) or inspect.ismethod(member):
                methods[name] = _signature(member)
        return {"kind": "class", "signature": _signature(obj), "methods": methods}
    if inspect.isroutine(obj):
        return {"kind": "function", "signature": _signature(obj)}
    return {"kind": "value", "signature": None}


def _purge(module_name: str) -> None:
    for key in [k for k in sys.modules
                if k == module_name or k.startswith(module_name + ".")]:
        del sys.modules[key]
    importlib.invalidate_caches()


def snapshot(module_name: str, fresh: bool = False) -> dict:
    """Import `module_name` and describe every attribute it exposes.

    Attributes are filtered to names the module itself defines or re-exports:
    imported third-party modules are skipped, but every function, class and
    value bound at module level — including underscore-private ones, which
    tests monkeypatch — is recorded.
    """
    if fresh:
        _purge(module_name)
    module = importlib.import_module(module_name)
    symbols = {}
    for name in sorted(dir(module)):
        if name.startswith("__") and name.endswith("__"):
            continue
        obj = getattr(module, name)
        if inspect.ismodule(obj):
            continue
        symbols[name] = _describe(obj)
    return {"module": module_name, "symbols": symbols}


def diff(before: dict, after: dict) -> list[str]:
    """Return human-readable difference lines. Empty list means identical."""
    out: list[str] = []
    b, a = before["symbols"], after["symbols"]
    for name in sorted(set(b) - set(a)):
        out.append(f"{name}: missing after split (was {b[name]['kind']})")
    for name in sorted(set(a) - set(b)):
        out.append(f"{name}: added by split (now {a[name]['kind']})")
    for name in sorted(set(a) & set(b)):
        if b[name]["kind"] != a[name]["kind"]:
            out.append(f"{name}: kind changed {b[name]['kind']} -> {a[name]['kind']}")
            continue
        if b[name]["signature"] != a[name]["signature"]:
            out.append(
                f"{name}: signature changed {b[name]['signature']} -> {a[name]['signature']}"
            )
        if b[name].get("methods") != a[name].get("methods"):
            out.append(f"{name}: class methods changed")
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("module", help="importable module name, e.g. hermes_cli.kanban_db")
    p.add_argument("--out", help="write the snapshot JSON here")
    p.add_argument("--compare", help="compare against this previously written snapshot")
    args = p.parse_args(argv)

    snap = snapshot(args.module, fresh=True)
    if args.out:
        with open(args.out, "w") as fh:
            json.dump(snap, fh, indent=2, sort_keys=True)
        print(f"wrote {len(snap['symbols'])} symbols to {args.out}")
    if args.compare:
        with open(args.compare) as fh:
            before = json.load(fh)
        lines = diff(before, snap)
        if lines:
            print(f"API DIFF — {len(lines)} difference(s):")
            for line in lines:
                print(f"  {line}")
            return 1
        print(f"API IDENTICAL — {len(snap['symbols'])} symbols match")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/refactor/test_api_snapshot.py -v`
Expected: PASS, 3 tests

- [ ] **Step 5: Prove it works against the real target**

```bash
cd /home/piet/.hermes/hermes-agent
python -m scripts.refactor.api_snapshot hermes_cli.kanban_db --out /tmp/kanban_before.json
python -m scripts.refactor.api_snapshot hermes_cli.kanban_db --compare /tmp/kanban_before.json
```
Expected: first prints a symbol count in the high hundreds; second prints `API IDENTICAL`. If the module cannot be imported standalone, fix the invocation (not the module) before continuing — the gate is worthless if it cannot run on the real file.

- [ ] **Step 6: Commit**

```bash
git add scripts/refactor/__init__.py scripts/refactor/api_snapshot.py tests/refactor/test_api_snapshot.py
git commit -m "refactor tooling: api_snapshot equivalence gate"
```

---

## Task 2: `layering.py` — the symbol graph

**Files:**
- Create: `scripts/refactor/layering.py`
- Test: `tests/refactor/test_layering.py`

**Interfaces:**
- Produces:
  - `top_level_symbols(tree: ast.Module) -> dict[str, ast.AST]` — name → defining node (functions, classes, module-level assignment targets)
  - `banner_sections(lines: list[str]) -> list[tuple[int, str]]` — `(lineno, title)` for each `# ---` divider followed by a `# Title` line
  - `import_time_names(node: ast.AST, top: dict) -> set[str]` — names of `top` this node evaluates **at import time**: assignment values, decorators, function default arguments, class bases, and non-method class-body statements
  - `classify_references(top, owner) -> References` where `References` is a dataclass with `import_time_forward`, `import_time_backward`, `runtime_forward`, `runtime_backward`, each a list of `(referrer_symbol, target_symbol)` tuples
  - `Reference` ordering is decided by `owner`, an ordered mapping symbol → submodule name; a reference is *forward* when the target's submodule appears earlier.
- Consumed by: Task 3 (`--analyze`) and Task 4 (`--apply`).

The import-time/runtime split is the load-bearing distinction in this whole plan: import-time references constrain module order absolutely, runtime references never do.

- [ ] **Step 1: Write the failing test**

Create `tests/refactor/test_layering.py`:

```python
import ast
import textwrap

from scripts.refactor import layering


def parse(src):
    src = textwrap.dedent(src)
    tree = ast.parse(src)
    return tree, src.splitlines()


def test_top_level_symbols_collects_defs_classes_and_assignments():
    tree, _ = parse("""
        LIMIT = 5
        TYPED: int = 6

        def fn():
            inner = 1
            return inner

        class Cls:
            attr = 2
    """)
    top = layering.top_level_symbols(tree)
    assert set(top) == {"LIMIT", "TYPED", "fn", "Cls"}
    assert "inner" not in top   # local, not top-level
    assert "attr" not in top    # class attribute, not top-level


def test_banner_sections_finds_divider_title_pairs():
    _, lines = parse("""
        x = 1
        # ---------------------------------------------------------------------------
        # Schema
        # ---------------------------------------------------------------------------
        y = 2
    """)
    # the reported lineno is the TITLE line, not the divider above it
    assert layering.banner_sections(lines) == [(4, "Schema")]


def test_import_time_names_covers_decorators_defaults_and_bases():
    tree, _ = parse("""
        BASE_LIMIT = 1

        def deco(f):
            return f

        class Base:
            pass

        DERIVED = BASE_LIMIT + 1

        @deco
        def uses_default(x=BASE_LIMIT):
            return helper()

        def helper():
            return BASE_LIMIT

        class Child(Base):
            pass
    """)
    top = layering.top_level_symbols(tree)
    by_name = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            by_name[node.name] = node
        elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            by_name[node.targets[0].id] = node

    assert layering.import_time_names(by_name["DERIVED"], top) == {"BASE_LIMIT"}
    # decorator and default arg are import-time; the call in the body is not
    assert layering.import_time_names(by_name["uses_default"], top) == {"deco", "BASE_LIMIT"}
    assert layering.import_time_names(by_name["Child"], top) == {"Base"}


def test_classify_references_splits_forward_from_backward():
    tree, _ = parse("""
        def early():
            return late()

        def late():
            return 1

        def also_early():
            return early()
    """)
    top = layering.top_level_symbols(tree)
    owner = {"early": "a", "late": "b", "also_early": "a"}
    refs = layering.classify_references(top, owner, module_order=["a", "b"])
    assert ("early", "late") in refs.runtime_backward
    assert refs.runtime_forward == []      # also_early -> early is same-module
    assert refs.import_time_backward == []


def test_import_time_backward_reference_is_detected():
    tree, _ = parse("""
        FIRST = SECOND + 1
        SECOND = 2
    """)
    top = layering.top_level_symbols(tree)
    owner = {"FIRST": "a", "SECOND": "b"}
    refs = layering.classify_references(top, owner, module_order=["a", "b"])
    assert ("FIRST", "SECOND") in refs.import_time_backward
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/refactor/test_layering.py -v`
Expected: FAIL with `ImportError: cannot import name 'layering'`

- [ ] **Step 3: Implement `layering.py`**

```python
"""Symbol-level dependency analysis for the giant-module split.

The load-bearing distinction here is IMPORT-TIME vs RUNTIME references.

An import-time reference (an assignment's value, a decorator, a default
argument, a class base) is evaluated while the module body executes, so it
constrains submodule order absolutely: the target must already exist.

A runtime reference (a name used inside a function body) is evaluated when
the function is called, long after every submodule has finished importing.
It therefore never constrains order — a backward runtime reference is
resolved through a module-object import (`from . import x` + `x.name(...)`),
which is why the emitted import graph can be acyclic by construction.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field


def top_level_symbols(tree: ast.Module) -> dict[str, ast.AST]:
    """Map every top-level name to the node that defines it."""
    top: dict[str, ast.AST] = {}
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            top[n.name] = n
        elif isinstance(n, ast.Assign):
            for tg in n.targets:
                if isinstance(tg, ast.Name):
                    top[tg.id] = n
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            top[n.target.id] = n
    return top


def banner_sections(lines: list[str]) -> list[tuple[int, str]]:
    """Find `# ----` / `# Title` banner pairs. Returns (title_lineno, title)."""
    out: list[tuple[int, str]] = []
    for i, line in enumerate(lines, 1):
        if not line.startswith("# ---"):
            continue
        if i >= len(lines):
            continue
        nxt = lines[i]
        if nxt.startswith("# ") and not nxt.startswith("# ---"):
            out.append((i + 1, nxt[2:].strip()))
    return out


def import_time_names(node: ast.AST, top: dict[str, ast.AST]) -> set[str]:
    """Names of `top` that `node` evaluates while the module body runs."""
    found: set[str] = set()

    def add(expr) -> None:
        for sub in ast.walk(expr):
            if isinstance(sub, ast.Name) and sub.id in top:
                found.add(sub.id)

    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        if node.value is not None:
            add(node.value)
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        for d in node.decorator_list:
            add(d)
        for d in node.args.defaults:
            add(d)
        for d in node.args.kw_defaults:
            if d is not None:
                add(d)
    elif isinstance(node, ast.ClassDef):
        for d in node.decorator_list:
            add(d)
        for b in node.bases:
            add(b)
        for st in node.body:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for d in st.decorator_list:
                    add(d)
                for d in st.args.defaults:
                    add(d)
                for d in st.args.kw_defaults:
                    if d is not None:
                        add(d)
            else:
                add(st)
    return found


def all_names(node: ast.AST, top: dict[str, ast.AST]) -> set[str]:
    return {s.id for s in ast.walk(node) if isinstance(s, ast.Name) and s.id in top}


@dataclass
class References:
    import_time_forward: list[tuple[str, str]] = field(default_factory=list)
    import_time_backward: list[tuple[str, str]] = field(default_factory=list)
    runtime_forward: list[tuple[str, str]] = field(default_factory=list)
    runtime_backward: list[tuple[str, str]] = field(default_factory=list)

    @property
    def needs_module_object_form(self) -> list[tuple[str, str]]:
        """References the splitter must rewrite as `mod.name(...)`."""
        return self.runtime_backward


def classify_references(
    top: dict[str, ast.AST],
    owner: dict[str, str],
    module_order: list[str],
) -> References:
    """Split cross-module references into import-time/runtime × forward/backward."""
    rank = {m: i for i, m in enumerate(module_order)}
    refs = References()
    for name, node in top.items():
        home = owner[name]
        it = import_time_names(node, top) - {name}
        rt = all_names(node, top) - it - {name}
        for target in sorted(it):
            if owner[target] == home:
                continue
            bucket = (refs.import_time_forward
                      if rank[owner[target]] < rank[home]
                      else refs.import_time_backward)
            bucket.append((name, target))
        for target in sorted(rt):
            if owner[target] == home:
                continue
            bucket = (refs.runtime_forward
                      if rank[owner[target]] < rank[home]
                      else refs.runtime_backward)
            bucket.append((name, target))
    return refs
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/refactor/test_layering.py -v`
Expected: PASS, 5 tests

- [ ] **Step 5: Commit**

```bash
git add scripts/refactor/layering.py tests/refactor/test_layering.py
git commit -m "refactor tooling: symbol layering analysis"
```

---

## Task 3: `split_module.py --analyze`

**Files:**
- Create: `scripts/refactor/split_module.py`
- Test: `tests/refactor/test_split_module.py` (analyze half)

**Interfaces:**
- Consumes: `layering.top_level_symbols`, `layering.banner_sections`, `layering.classify_references`
- Produces: `analyze(path: str, boundary_map: dict | None) -> dict` returning `{"symbols": int, "sections": [...], "import_time_backward": [...], "runtime_backward": [...], "oversized": [...]}`. When `boundary_map` is `None`, sections come from banners; otherwise from the map. CLI: `python -m scripts.refactor.split_module --analyze <path> [--map <yaml>]`.

**Acceptance criterion (the reproduction gate for the ground-truth section):** running `--analyze` with no map on `hermes_cli/kanban_db.py` must report `import_time_backward` empty and `runtime_backward` = 140, and `--ownership` must report 733 / 111 / 129. Both are checked in Step 6.

- [ ] **Step 1: Write the failing test**

Append to `tests/refactor/test_split_module.py`:

```python
import textwrap

from scripts.refactor import split_module


def test_analyze_flags_import_time_backward_reference(tmp_path):
    src = tmp_path / "m.py"
    src.write_text(textwrap.dedent("""
        # ---------------------------------------------------------------------------
        # Alpha
        # ---------------------------------------------------------------------------
        FIRST = SECOND + 1

        # ---------------------------------------------------------------------------
        # Beta
        # ---------------------------------------------------------------------------
        SECOND = 2
    """))
    report = split_module.analyze(str(src))
    assert report["import_time_backward"] == [("FIRST", "SECOND")]


def test_analyze_reports_runtime_backward_without_flagging_it_fatal(tmp_path):
    src = tmp_path / "m2.py"
    src.write_text(textwrap.dedent("""
        # ---------------------------------------------------------------------------
        # Alpha
        # ---------------------------------------------------------------------------
        def early():
            return late()

        # ---------------------------------------------------------------------------
        # Beta
        # ---------------------------------------------------------------------------
        def late():
            return 1
    """))
    report = split_module.analyze(str(src))
    assert report["import_time_backward"] == []
    assert report["runtime_backward"] == [("early", "late")]
    assert report["fatal"] is False


def test_analyze_marks_import_time_backward_as_fatal(tmp_path):
    src = tmp_path / "m3.py"
    src.write_text(textwrap.dedent("""
        # ---------------------------------------------------------------------------
        # Alpha
        # ---------------------------------------------------------------------------
        FIRST = SECOND

        # ---------------------------------------------------------------------------
        # Beta
        # ---------------------------------------------------------------------------
        SECOND = 2
    """))
    assert split_module.analyze(str(src))["fatal"] is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/refactor/test_split_module.py -v`
Expected: FAIL with `ImportError: cannot import name 'split_module'`

- [ ] **Step 3: Implement the analyze half**

Create `scripts/refactor/split_module.py` with the analyze path. `analyze` assigns each symbol to a section (from banners, or from a boundary map when given), classifies references, and reports. `fatal` is `True` when any import-time backward reference exists — that is the only condition that makes a boundary map unimplementable without a `_core.py` fallback.

```python
"""Split a giant module into a package. Deterministic, AST-driven, pure move.

Two modes:

  --analyze   report the layering: which symbols land where, and which
              cross-module references are import-time (order-constraining)
              versus runtime (order-free).
  --apply     perform the move.

Ordering rule: submodules are emitted in the order the boundary map lists
them, which for banner-derived maps is the source file's own order. A
reference to an EARLIER submodule becomes `from .that import name` and the
referring line stays byte-identical. A reference to a LATER submodule
becomes `from . import that` plus a rewrite of the reference to
`that.name`, which defers resolution to call time. Import-time backward
references cannot be deferred, so the tool refuses rather than emitting a
cycle.
"""
from __future__ import annotations

import argparse
import ast
import json
import sys

from . import layering


def _section_owner_from_banners(tree, lines):
    banners = layering.banner_sections(lines)
    order = ["__head__"] + [title for _, title in banners]

    def section_of(lineno: int) -> str:
        name = "__head__"
        for bl, bt in banners:
            if bl <= lineno:
                name = bt
        return name

    top = layering.top_level_symbols(tree)
    owner = {n: section_of(node.lineno) for n, node in top.items()}
    used = [m for m in order if m in set(owner.values())]
    return top, owner, used


def _section_owner_from_map(tree, boundary_map):
    top = layering.top_level_symbols(tree)
    owner = {}
    order = []
    for entry in boundary_map["modules"]:
        order.append(entry["name"])
        for sym in entry["symbols"]:
            if sym not in top:
                raise SystemExit(f"boundary map names unknown symbol: {sym}")
            owner[sym] = entry["name"]
    missing = sorted(set(top) - set(owner))
    if missing:
        raise SystemExit(
            f"boundary map does not place {len(missing)} symbol(s): {missing[:20]}"
        )
    return top, owner, order


def analyze(path: str, boundary_map: dict | None = None) -> dict:
    src = open(path).read()
    tree = ast.parse(src)
    lines = src.splitlines()
    if boundary_map is None:
        top, owner, order = _section_owner_from_banners(tree, lines)
    else:
        top, owner, order = _section_owner_from_map(tree, boundary_map)

    refs = layering.classify_references(top, owner, order)

    span = {}
    for name, node in top.items():
        end = getattr(node, "end_lineno", node.lineno)
        span[owner[name]] = span.get(owner[name], 0) + (end - node.lineno + 1)

    return {
        "path": path,
        "lines": len(lines),
        "symbols": len(top),
        "modules": order,
        "module_lines": span,
        "oversized": sorted(
            (m for m, v in span.items() if v > 4000),
            key=lambda m: -span[m],
        ),
        "import_time_forward": refs.import_time_forward,
        "import_time_backward": refs.import_time_backward,
        "runtime_forward": refs.runtime_forward,
        "runtime_backward": refs.runtime_backward,
        "fatal": bool(refs.import_time_backward),
    }


def _load_map(path):
    import yaml
    with open(path) as fh:
        return yaml.safe_load(fh)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("path")
    p.add_argument("--analyze", action="store_true")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--map", help="boundary map YAML")
    p.add_argument("--json", action="store_true", help="emit the raw report")
    args = p.parse_args(argv)

    bmap = _load_map(args.map) if args.map else None

    if args.analyze or not args.apply:
        rep = analyze(args.path, bmap)
        if args.json:
            print(json.dumps(rep, indent=2, default=list))
            return 1 if rep["fatal"] else 0
        print(f"{rep['path']}: {rep['lines']} lines, {rep['symbols']} symbols, "
              f"{len(rep['modules'])} modules")
        print(f"  import-time cross-module refs: forward={len(rep['import_time_forward'])} "
              f"backward={len(rep['import_time_backward'])}")
        print(f"  runtime cross-module refs:     forward={len(rep['runtime_forward'])} "
              f"backward={len(rep['runtime_backward'])} "
              f"(these get the module-object form)")
        for module in rep["oversized"]:
            print(f"  OVERSIZED {rep['module_lines'][module]:6d}  {module}")
        if rep["fatal"]:
            print("  FATAL: import-time backward references — this order is not a "
                  "valid layering:")
            for referrer, target in rep["import_time_backward"][:20]:
                print(f"    {referrer} -> {target}")
            return 1
        return 0

    return apply_split(args.path, bmap)


if __name__ == "__main__":
    raise SystemExit(main())
```

(`apply_split` is Task 4; leave it as `def apply_split(path, bmap): raise NotImplementedError` for now.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/refactor/test_split_module.py -v`
Expected: PASS, 3 tests

- [ ] **Step 5: Add `--ownership`, the mode the re-aim needs**

The boundary map is now derived from ownership against upstream, not from banners. Add to `split_module.py`:

```python
def ownership(path: str, upstream_ref: str = "origin/main") -> dict:
    """Classify every top-level symbol as FORK / UPSTREAM-identical / UPSTREAM-diverged.

    A symbol is UPSTREAM if a symbol of that name exists in the upstream copy
    of the same path. Compared against today's upstream (default origin/main),
    NOT the merge-base: what matters is which symbols upstream still carries,
    because those are the ones future merges will touch.

    The three buckets are reported separately and never silently folded
    together — the diverged bucket is the standing conflict surface.
    """
    import subprocess

    def bodies(src):
        tree = ast.parse(src)
        lines = src.splitlines()
        out = {}
        for n in tree.body:
            names = []
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names = [n.name]
            elif isinstance(n, ast.Assign):
                names = [t.id for t in n.targets if isinstance(t, ast.Name)]
            elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
                names = [n.target.id]
            if not names:
                continue
            start, end = _symbol_span(n)
            body = "\n".join(lines[start - 1:end])
            for nm in names:
                out[nm] = (body, end - start + 1)
        return out

    fork = bodies(open(path).read())
    up = bodies(subprocess.run(
        ["git", "show", f"{upstream_ref}:{path}"],
        capture_output=True, text=True, check=True).stdout)

    fork_only = {k for k in fork if k not in up}
    shared = {k for k in fork if k in up}
    identical = {k for k in shared if fork[k][0] == up[k][0]}
    diverged = shared - identical

    def total(names):
        return sum(fork[n][1] for n in names)

    return {
        "path": path,
        "upstream_ref": upstream_ref,
        "fork_only": sorted(fork_only),
        "upstream_identical": sorted(identical),
        "upstream_diverged": sorted(
            diverged, key=lambda n: -fork[n][1]),
        "lines": {
            "fork_only": total(fork_only),
            "upstream_identical": total(identical),
            "upstream_diverged": total(diverged),
        },
        "diverged_detail": [
            {"symbol": n, "fork_lines": fork[n][1], "upstream_lines": up[n][1]}
            for n in sorted(diverged, key=lambda n: -fork[n][1])
        ],
    }
```

Wire it to `--ownership [--upstream-ref REF]` in `main()`, printing the three bucket counts and line totals, and `--json` for the raw report.

Add to `tests/refactor/test_split_module.py`:

```python
def test_ownership_separates_the_three_buckets(tmp_path, monkeypatch):
    import subprocess

    repo = tmp_path / "r"
    repo.mkdir()
    monkeypatch.chdir(repo)
    subprocess.run(["git", "init", "-q"], check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "config", "user.name", "t"], check=True)

    (repo / "m.py").write_text(
        "SHARED_SAME = 1\n\n\ndef shared_changed():\n    return 1\n")
    subprocess.run(["git", "add", "m.py"], check=True)
    subprocess.run(["git", "commit", "-qm", "up"], check=True)
    subprocess.run(["git", "branch", "-M", "upstream"], check=True)

    (repo / "m.py").write_text(
        "SHARED_SAME = 1\n\n\ndef shared_changed():\n    return 2\n\n\n"
        "def fork_only():\n    return 3\n")

    rep = split_module.ownership("m.py", upstream_ref="upstream")
    assert rep["fork_only"] == ["fork_only"]
    assert rep["upstream_identical"] == ["SHARED_SAME"]
    assert rep["upstream_diverged"] == ["shared_changed"]
```

Run: `python -m pytest tests/refactor/test_split_module.py -v`
Expected: PASS.

- [ ] **Step 6: Reproduce the measured ownership map on the real file**

```bash
cd /home/piet/.hermes/hermes-agent
python -m scripts.refactor.split_module hermes_cli/kanban_db.py --analyze
python -m scripts.refactor.split_module hermes_cli/kanban_db.py --ownership
```
Expected from `--analyze`: import-time `backward=0`, runtime `backward=140`.
Expected from `--ownership`: **733** fork-only (23,745 lines), **111** upstream-identical (1,293 lines), **129** upstream-diverged (11,419 lines). If these numbers differ, upstream has moved — re-derive before continuing, do not proceed on stale figures.

- [ ] **Step 7: Commit**

```bash
git add scripts/refactor/split_module.py tests/refactor/test_split_module.py
git commit -m "refactor tooling: split_module --analyze and --ownership"
```

---

## Task 4: `split_module.py --extract` — the mover

> **Amended 2026-07-24: `--apply` is dropped; build `--extract` only.**
>
> `apply_split` converts a module into a package *at the same path*. The re-aim forbids exactly that shape — it is what makes future upstream diffs unapplyable — so `--apply` can never legitimately run on any file in this repo. Building it would be dead code carrying real review cost.
>
> Nothing is lost by dropping it. Emission rules 1–9 below are shared, and every one of them is exercised by `--extract` through references *between* the extracted submodules: forward edges become symbol imports, backward edges get the module-object rewrite, and the shadowing (rule 8) and split-binding (rule 9) guards apply identically.
>
> **What to build:** `extract_to_package` and its tests (Steps 1, 5, 6 below, plus the `--extract` block in Step 6). **Skip:** `apply_split` and the four `test_apply_*` cases. Re-point the two rule tests at `--extract`:
> - `test_local_binding_shadowing_a_backward_target_is_not_rewritten` → drive it through `extract_to_package`, with the shadowed symbol and its referrer in *different extracted submodules* so the back-edge is intra-package.
> - `test_apply_refuses_split_binding_across_modules` → rename to `test_extract_refuses_split_binding_across_modules`; same assertion, `extract_to_package` instead.
>
> The `apply_split` implementation below is retained as the reference for the shared machinery — read it for the emission mechanics, do not ship it.

**Files:**
- Modify: `scripts/refactor/split_module.py`
- Test: `tests/refactor/test_split_module.py` (apply half)

**Interfaces:**
- Produces: `apply_split(path: str, boundary_map: dict) -> int`. Replaces `path` with a package directory of the same name containing one file per boundary-map module plus a generated `__init__.py`, and returns 0 on success.

**Emission rules (these are the specification — implement exactly):**

1. **Header block.** Everything from line 1 up to and including the last top-level `import`/`from` statement is the *header*. It is copied verbatim into **every** emitted submodule. Unused imports in a submodule are acceptable and expected; removing them would not be a pure move. The module docstring goes into `__init__.py` only.
2. **Symbol bodies.** Each symbol's source is extracted by line span (`node.lineno` through `node.end_lineno`, including any decorator lines, which `ast` places *before* `lineno` — use `min(d.lineno for d in decorator_list)` when decorators exist). Bodies are written in the original source order within their submodule. Preceding comment lines and blank separation up to the previous symbol's end are carried with the symbol so banners and comments survive.
3. **Forward runtime + all import-time references** → `from .<target_module> import <name>` appended to the submodule's import block. The referring line is **not touched**.
4. **Backward runtime references** → `from . import <target_module>` in the import block, and every `ast.Name` load of that symbol inside the referring symbol's body is rewritten to `<target_module>.<name>`. Rewrites are applied by exact `(lineno, col_offset)` from the AST, right-to-left within a line, so column offsets stay valid.
5. **Import-time backward reference** → **refuse**. Print the offending pairs and exit non-zero without writing anything.
6. **`__init__.py`** contains the original module docstring **copied verbatim from its source span** (not re-quoted from `ast.get_docstring`, which loses raw prefixes and breaks on embedded `"""`), then one `from .<module> import (...)` block per submodule in order, naming **every** symbol that submodule defines — including underscore-private ones, because tests monkeypatch them. No `import *`.
7. **Nothing else changes.** No reformatting, no import sorting, no whitespace normalization inside a moved body.
8. **Shadowing guard (correctness-critical).** A backward-target rewrite must not touch a name that is *locally bound* — a parameter, assignment or `del` target, `for`/`with`/`except`/comprehension target, import alias, nested def/class name, or a `global`/`nonlocal` declaration. Rewriting a local to `mod.name` would silently change behaviour. The rule is applied conservatively: if the name is bound **anywhere** inside the referring symbol, no occurrence of it is rewritten.

   This is not merely the safe choice, it is the correct one. Python's function scope is function-wide, not statement-ordered, so a name assigned anywhere in a function is local throughout it — a call to that name earlier in the same function would already raise `UnboundLocalError` in the *original* file. Since the original works, a shadowed name is purely local and must not be rewritten. The one residual case is a name bound only in a *nested* function while the outer function genuinely references the module-level symbol; that is why skips are printed for review rather than assumed. Measured on `kanban_db.py`: **3 of 140** back-edges are shadowed (`vault_memory_links_for_task`→`latest_summary`, `_backfill_legacy_dependency_waits`→`parent_ids`, `check_respawn_guard`→`latest_run`), all three consistent with ordinary local variables.
9. **Split-binding refusal.** If one statement binds several top-level names (`A = B = ...`, or a tuple assignment) and the boundary map places them in different submodules, refuse — the statement can only be emitted once.

- [ ] **Step 1: Write the failing test**

Append to `tests/refactor/test_split_module.py`:

```python
import subprocess
import sys
import textwrap

import pytest

from scripts.refactor import split_module


BACK_EDGE_SOURCE = '''\
"""Module docstring stays in __init__."""
import os

LIMIT = 3


def early(n):
    """Calls a symbol that lands in a LATER submodule."""
    return late(n) + LIMIT


def late(n):
    return n * 2


def uses_os():
    return os.sep
'''


@pytest.fixture
def split_pkg(tmp_path, monkeypatch):
    src = tmp_path / "target.py"
    src.write_text(BACK_EDGE_SOURCE)
    bmap = {"modules": [
        {"name": "consts", "symbols": ["LIMIT"]},
        {"name": "front", "symbols": ["early", "uses_os"]},
        {"name": "back", "symbols": ["late"]},
    ]}
    split_module.apply_split(str(src), bmap)
    monkeypatch.syspath_prepend(str(tmp_path))
    return tmp_path / "target"


def test_apply_creates_package_and_removes_original(split_pkg):
    assert split_pkg.is_dir()
    assert (split_pkg / "__init__.py").exists()
    assert {p.name for p in split_pkg.glob("*.py")} == {
        "__init__.py", "consts.py", "front.py", "back.py"}
    assert not (split_pkg.parent / "target.py").exists()


def test_backward_reference_uses_module_object_form(split_pkg):
    front = (split_pkg / "front.py").read_text()
    assert "from . import back" in front
    assert "back.late(n)" in front
    # the forward reference to LIMIT is a plain symbol import, line untouched
    assert "from .consts import LIMIT" in front
    assert "return back.late(n) + LIMIT" in front


def test_header_imports_are_copied_verbatim_into_every_submodule(split_pkg):
    for name in ("consts.py", "front.py", "back.py"):
        assert "import os" in (split_pkg / name).read_text()


def test_init_reexports_every_symbol_including_private(split_pkg):
    init = (split_pkg / "__init__.py").read_text()
    assert init.startswith('"""Module docstring stays in __init__."""')
    for sym in ("LIMIT", "early", "late", "uses_os"):
        assert sym in init
    assert "import *" not in init


def test_split_package_imports_and_behaves_identically(split_pkg):
    import target
    assert target.early(4) == 11        # late(4)=8, +LIMIT=3
    assert target.late(4) == 8
    assert target.LIMIT == 3


def test_apply_refuses_import_time_backward_reference(tmp_path):
    src = tmp_path / "bad.py"
    src.write_text("FIRST = SECOND\nSECOND = 2\n")
    bmap = {"modules": [
        {"name": "a", "symbols": ["FIRST"]},
        {"name": "b", "symbols": ["SECOND"]},
    ]}
    with pytest.raises(SystemExit):
        split_module.apply_split(str(src), bmap)
    assert src.exists()                  # nothing was written
    assert not (tmp_path / "bad").exists()


def test_apply_refuses_boundary_map_that_omits_a_symbol(tmp_path):
    src = tmp_path / "partial.py"
    src.write_text("def a():\n    return 1\n\n\ndef b():\n    return 2\n")
    bmap = {"modules": [{"name": "only", "symbols": ["a"]}]}
    with pytest.raises(SystemExit):
        split_module.apply_split(str(src), bmap)


def test_local_binding_shadowing_a_backward_target_is_not_rewritten(tmp_path, monkeypatch):
    """Emission rule 8. A local named `late` must NOT become `back.late`."""
    src = tmp_path / "shadow.py"
    src.write_text(textwrap.dedent('''\
        def early(n):
            calls = [late(n)]
            late = n + 100          # rebinds `late` locally AFTER the call
            return calls, late


        def shadow_param(late):
            return late * 2


        def late(n):
            return n * 2
    '''))
    split_module.apply_split(str(src), {"modules": [
        {"name": "front", "symbols": ["early", "shadow_param"]},
        {"name": "back", "symbols": ["late"]},
    ]})
    front = (tmp_path / "shadow" / "front.py").read_text()
    # `late` is local throughout `early`, so no reference in it is rewritten
    assert "back.late" not in front
    assert "return late * 2" in front       # parameter, untouched


def test_apply_refuses_split_binding_across_modules(tmp_path):
    """Emission rule 9."""
    src = tmp_path / "tup.py"
    src.write_text("A = B = 1\n")
    bmap = {"modules": [
        {"name": "one", "symbols": ["A"]},
        {"name": "two", "symbols": ["B"]},
    ]}
    with pytest.raises(SystemExit):
        split_module.apply_split(str(src), bmap)
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/refactor/test_split_module.py -v -k apply or backward or header or init or identically`
Expected: FAIL with `NotImplementedError`

- [ ] **Step 3: Implement `apply_split`**

Add to `scripts/refactor/split_module.py`:

```python
import os
import shutil


def _header_end_lineno(tree) -> int:
    last = 0
    for n in tree.body:
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            last = max(last, getattr(n, "end_lineno", n.lineno))
    return last


def _symbol_span(node) -> tuple[int, int]:
    start = node.lineno
    decorators = getattr(node, "decorator_list", None)
    if decorators:
        start = min(start, min(d.lineno for d in decorators))
    return start, getattr(node, "end_lineno", node.lineno)


def locally_bound_anywhere(node) -> set[str]:
    """Every name bound anywhere inside `node` — parameters, assignment and
    del targets, `for`/`with`/`except`/comprehension targets, import aliases,
    nested def/class names, and `global`/`nonlocal` declarations.

    Used conservatively: if a backward target is bound ANYWHERE inside the
    referring symbol, no occurrence of it in that symbol is rewritten. This
    can never rewrite a local by mistake (emission rule 8). The cost is that
    a genuine module-level reference sharing a name with an unrelated local
    is also skipped — so those cases are REPORTED, not silently dropped.
    """
    bound: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and isinstance(sub.ctx, (ast.Store, ast.Del)):
            bound.add(sub.id)
        elif isinstance(sub, ast.arg):
            bound.add(sub.arg)
        elif isinstance(sub, (ast.Import, ast.ImportFrom)):
            for alias in sub.names:
                bound.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(sub, (ast.Global, ast.Nonlocal)):
            bound.update(sub.names)
        elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(sub.name)
    return bound


def _rewrite_backward_refs(body_lines, first_lineno, node, targets, owner, skipped):
    """Rewrite `name` -> `module.name` for every load of a backward target.

    `targets` maps symbol name -> owning submodule. Targets shadowed by a
    local binding anywhere in `node` are skipped and appended to `skipped`
    for the operator to review. Rewrites are applied right-to-left per line
    so earlier column offsets stay valid.
    """
    shadowed = locally_bound_anywhere(node) & set(targets)
    for name in sorted(shadowed):
        skipped.append((getattr(node, "name", "<assignment>"), name))

    edits = {}
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load) \
                and sub.id in targets and sub.id not in shadowed:
            edits.setdefault(sub.lineno, []).append((sub.col_offset, sub.id))
    for lineno, hits in edits.items():
        idx = lineno - first_lineno
        if idx < 0 or idx >= len(body_lines):
            continue
        line = body_lines[idx]
        for col, name in sorted(hits, reverse=True):
            if line[col:col + len(name)] != name:
                raise SystemExit(
                    f"rewrite offset mismatch at line {lineno} for {name!r}")
            line = line[:col] + f"{targets[name]}.{name}" + line[col + len(name):]
        body_lines[idx] = line
    return body_lines


def apply_split(path: str, boundary_map: dict) -> int:
    src = open(path).read()
    tree = ast.parse(src)
    lines = src.split("\n")
    top, owner, order = _section_owner_from_map(tree, boundary_map)
    refs = layering.classify_references(top, owner, order)

    if refs.import_time_backward:
        print("REFUSING: import-time backward references would create a cycle:")
        for referrer, target in refs.import_time_backward:
            print(f"  {referrer} ({owner[referrer]}) -> {target} ({owner[target]})")
        raise SystemExit(2)

    # emission rule 9: one statement binding names across two modules is unemittable
    by_node: dict[int, set[str]] = {}
    for name, node in top.items():
        by_node.setdefault(id(node), set()).add(name)
    for names in by_node.values():
        homes = {owner[n] for n in names}
        if len(homes) > 1:
            raise SystemExit(
                f"REFUSING: {sorted(names)} are bound by one statement but the "
                f"boundary map splits them across {sorted(homes)}"
            )

    header_end = _header_end_lineno(tree)
    header = "\n".join(lines[:header_end])

    # emission rule 6: copy the docstring's source span verbatim
    docstring_src = None
    if tree.body and isinstance(tree.body[0], ast.Expr) \
            and isinstance(tree.body[0].value, ast.Constant) \
            and isinstance(tree.body[0].value.value, str):
        d = tree.body[0]
        docstring_src = "\n".join(lines[d.lineno - 1:d.end_lineno])

    # which symbols each module must import, and in which form
    symbol_imports: dict[str, dict[str, set[str]]] = {m: {} for m in order}
    module_imports: dict[str, set[str]] = {m: set() for m in order}
    backward_targets: dict[str, dict[str, str]] = {m: {} for m in order}
    for referrer, target in refs.import_time_forward + refs.runtime_forward:
        home, tgt = owner[referrer], owner[target]
        symbol_imports[home].setdefault(tgt, set()).add(target)
    for referrer, target in refs.runtime_backward:
        home, tgt = owner[referrer], owner[target]
        module_imports[home].add(tgt)
        backward_targets[home][target] = tgt

    # collect each module's bodies in original source order
    ordered = sorted(top.items(), key=lambda kv: _symbol_span(kv[1])[0])
    bodies: dict[str, list[str]] = {m: [] for m in order}
    defined: dict[str, list[str]] = {m: [] for m in order}
    seen_nodes: set[int] = set()
    shadow_skips: list[tuple[str, str]] = []
    prev_end = header_end
    for name, node in ordered:
        if id(node) in seen_nodes:      # one statement binding several names
            defined[owner[name]].append(name)
            continue
        seen_nodes.add(id(node))
        start, end = _symbol_span(node)
        # carry leading comments/blank lines that belong to this symbol
        lead = start - 1
        while lead > prev_end and (lines[lead - 1].strip() == ""
                                   or lines[lead - 1].lstrip().startswith("#")):
            lead -= 1
        chunk = lines[lead:end]
        home = owner[name]
        if backward_targets[home]:
            chunk = _rewrite_backward_refs(
                list(chunk), lead + 1, node, backward_targets[home], owner,
                shadow_skips)
        bodies[home].append("\n".join(chunk))
        defined[home].append(name)
        prev_end = end

    pkg_dir = os.path.splitext(path)[0]
    tmp_dir = pkg_dir + ".__split_tmp__"
    if os.path.exists(tmp_dir):
        shutil.rmtree(tmp_dir)
    os.makedirs(tmp_dir)

    for module in order:
        parts = [header, ""]
        for tgt in sorted(symbol_imports[module]):
            names = ", ".join(sorted(symbol_imports[module][tgt]))
            parts.append(f"from .{tgt} import {names}")
        for tgt in sorted(module_imports[module]):
            parts.append(f"from . import {tgt}")
        parts.append("")
        parts.extend(bodies[module])
        text = "\n".join(parts)
        if not text.endswith("\n"):
            text += "\n"
        with open(os.path.join(tmp_dir, f"{module}.py"), "w") as fh:
            fh.write(text)

    init_parts = []
    if docstring_src is not None:
        init_parts.append(docstring_src)
        init_parts.append("")
    for module in order:
        names = defined[module]
        if not names:
            continue
        init_parts.append(f"from .{module} import (")
        for name in names:
            init_parts.append(f"    {name},")
        init_parts.append(")")
    init_parts.append("")
    with open(os.path.join(tmp_dir, "__init__.py"), "w") as fh:
        fh.write("\n".join(init_parts))

    os.remove(path)
    os.rename(tmp_dir, pkg_dir)
    print(f"split {path} -> {pkg_dir}/ ({len(order)} modules, {len(top)} symbols, "
          f"{len(refs.runtime_backward)} module-object rewrites)")
    if shadow_skips:
        print(f"REVIEW REQUIRED — {len(shadow_skips)} backward reference(s) were NOT "
              f"rewritten because a local binding shadows the name:")
        for referrer, target in shadow_skips:
            print(f"  {referrer} references {target} ({owner[target]}) but also "
                  f"binds {target} locally")
        print("  Confirm each is genuinely local. If any is a real module-level "
              "reference, move it into the same submodule via the boundary map.")
    return 0
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/refactor/test_split_module.py -v`
Expected: PASS, 12 tests (3 analyze + 9 apply)

- [ ] **Step 5: Add the round-trip property test**

Append to `tests/refactor/test_split_module.py`:

```python
def test_split_preserves_api_snapshot_exactly(tmp_path, monkeypatch):
    """The property the whole plan rests on, tested end to end."""
    from scripts.refactor import api_snapshot

    src = tmp_path / "roundtrip.py"
    src.write_text(BACK_EDGE_SOURCE.replace("target", "roundtrip"))
    monkeypatch.syspath_prepend(str(tmp_path))
    before = api_snapshot.snapshot("roundtrip", fresh=True)

    split_module.apply_split(str(src), {"modules": [
        {"name": "consts", "symbols": ["LIMIT"]},
        {"name": "front", "symbols": ["early", "uses_os"]},
        {"name": "back", "symbols": ["late"]},
    ]})
    after = api_snapshot.snapshot("roundtrip", fresh=True)
    assert api_snapshot.diff(before, after) == []
```

Run: `python -m pytest tests/refactor/test_split_module.py::test_split_preserves_api_snapshot_exactly -v`
Expected: PASS

- [ ] **Step 6: Add `--extract`, the mode the re-aim actually uses**

`apply_split` converts a module into a package at the same path. The re-aim needs the opposite shape: pull a *subset* of symbols out into a **sibling** package, leave the rest in the original file, and append one re-export block.

**This shape is strictly simpler than `--apply`, because it needs no body rewrites at all.** Verified empirically:

- The re-export block is the **last** statement in `kanban_db.py`, so when `kanban_ext` is imported every upstream symbol is already defined. `kanban_ext` submodules therefore reach back with plain `from hermes_cli.kanban_db import X` symbol imports, and their bodies stay byte-identical.
- Conversely, the re-export block binds every fork symbol as a module global of `kanban_db`, so the 233 runtime UPSTREAM → FORK references resolve at call time with bodies byte-identical too.
- The circular import is benign: `kanban_db` is in `sys.modules` and fully populated by the time `kanban_ext` executes.
- The **3 import-time** UPSTREAM → FORK references are the sole exception and fail loudly — a direct test reproduces `NameError: name 'FORK_CONST' is not defined`. Hence the carve-out rule below.

Emission rules for `--extract`:

1. Symbols named by the boundary map move to `<package_dir>/<module>.py`; every other symbol **stays in the original file, byte-identical, in its original position**.
2. The original file's header (docstring + imports) is untouched. Each extracted submodule gets that header copied verbatim, exactly as `--apply` does.
3. Each extracted submodule gets `from <origin_module> import <names>` for the symbols it references that stayed behind. **No body rewriting.**
4. References *between* extracted submodules follow the existing `--apply` rules: forward → symbol import, backward → module-object import plus rewrite, with the emission rules 8 and 9 shadowing and split-binding guards.
5. The original file gains, as its **final** statement, `from <package> import (...)` naming every extracted symbol explicitly. No `import *`.
6. **Refuse** if any symbol that stays references an extracted symbol at import time — that reference cannot be satisfied by a trailing block. Print each offending pair; the fix is to keep that symbol behind (Task 5 Step 3), never to reorder the block.

```python
def extract_to_package(path: str, boundary_map: dict, package_dir: str) -> int:
    """Move the boundary map's symbols out to a sibling package.

    Unlike apply_split, `path` remains a module FILE — required, because
    upstream's future diffs are addressed to that literal path.
    """
    src = open(path).read()
    tree = ast.parse(src)
    lines = src.split("\n")
    top = layering.top_level_symbols(tree)

    order = [e["name"] for e in boundary_map["modules"]]
    moved: dict[str, str] = {}
    for entry in boundary_map["modules"]:
        for sym in entry["symbols"]:
            if sym not in top:
                raise SystemExit(f"boundary map names unknown symbol: {sym}")
            moved[sym] = entry["name"]
    stays = {n for n in top if n not in moved}

    # rule 6: a symbol that stays may not need an extracted symbol at import time
    offenders = []
    for name in sorted(stays):
        for target in sorted(layering.import_time_names(top[name], top) - {name}):
            if target in moved:
                offenders.append((name, target))
    if offenders:
        print("REFUSING: symbols that stay reference extracted symbols at import "
              "time; a trailing re-export block runs too late for these:")
        for referrer, target in offenders:
            print(f"  {referrer} needs {target} (mapped to {moved[target]})")
        raise SystemExit(2)

    # ... emit submodules exactly as apply_split does, but with the origin
    #     module added as an import source for every reference to `stays`;
    #     then rewrite `path` keeping only `stays`, and append the block.
    return 0
```

Add to `tests/refactor/test_split_module.py`:

```python
EXTRACT_SOURCE = '''\
"""Origin module docstring."""
import os

DEFAULT_TTL = 300


def connect():
    return "conn:" + os.sep


def upstream_uses_fork():
    return fork_helper() + "|" + str(DEFAULT_TTL)


def fork_helper():
    return "fork(" + connect() + "," + str(DEFAULT_TTL) + ")"
'''


def test_extract_leaves_origin_a_file_and_moves_only_mapped_symbols(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    src = tmp_path / "origin.py"
    src.write_text(EXTRACT_SOURCE)
    split_module.extract_to_package(
        str(src), {"modules": [{"name": "helpers", "symbols": ["fork_helper"]}]},
        str(tmp_path / "origin_ext"))

    assert src.is_file()                       # NOT a directory — the whole point
    assert (tmp_path / "origin_ext" / "helpers.py").exists()
    text = src.read_text()
    assert "def connect()" in text             # stayed, byte-identical
    assert "def fork_helper()" not in text     # moved out
    assert text.rstrip().endswith(")")         # re-export block is last
    assert "from origin_ext import (" in text


def test_extract_preserves_behaviour_and_api(tmp_path, monkeypatch):
    from scripts.refactor import api_snapshot

    monkeypatch.syspath_prepend(str(tmp_path))
    src = tmp_path / "origin2.py"
    src.write_text(EXTRACT_SOURCE)
    before = api_snapshot.snapshot("origin2", fresh=True)

    split_module.extract_to_package(
        str(src), {"modules": [{"name": "helpers", "symbols": ["fork_helper"]}]},
        str(tmp_path / "origin2_ext"))

    after = api_snapshot.snapshot("origin2", fresh=True)
    assert api_snapshot.diff(before, after) == []

    import origin2
    assert origin2.upstream_uses_fork() == "fork(conn:/,300)|300"


def test_extract_refuses_import_time_reference_into_the_extracted_set(tmp_path):
    src = tmp_path / "origin3.py"
    src.write_text(
        "FORK_CONST = 'fc'\n\n\ndef stays(_x=FORK_CONST):\n    return _x\n")
    with pytest.raises(SystemExit):
        split_module.extract_to_package(
            str(src), {"modules": [{"name": "c", "symbols": ["FORK_CONST"]}]},
            str(tmp_path / "origin3_ext"))
```

Run: `python -m pytest tests/refactor/test_split_module.py -v`
Expected: PASS.

- [ ] **Step 7: Run ruff and the affected suite**

```bash
ruff check scripts/refactor tests/refactor
scripts/run-affected.sh
```
Expected: both clean.

- [ ] **Step 8: Commit**

```bash
git add scripts/refactor/split_module.py tests/refactor/test_split_module.py
git commit -m "refactor tooling: split_module --apply and --extract, deterministic AST moves"
```

---

## Task 5: The ownership boundary map for `kanban_ext`

**Files:**
- Create: `docs/refactor/ownership.kanban_db.md`, `docs/refactor/boundary-map.kanban_ext.yaml`

**Interfaces:**
- Produces the YAML consumed by `split_module.py --extract`. Schema:

```yaml
module: hermes_cli/kanban_db.py
package: hermes_cli/kanban_ext
upstream_ref: origin/main
modules:
  - name: waits
    symbols: [_wait_is_satisfied, _register_wait, ...]
  - name: worker_ctx
    symbols: [build_worker_context, render_worker_brief_for_task, ...]
```

Only fork-only symbols appear. Everything absent from the map stays in `kanban_db.py`. Target: **8–12 submodules of 2,000–3,000 lines**, covering all 733 fork-only symbols minus the carve-out from Step 3.

- [ ] **Step 1: Generate the ownership report**

```bash
cd /home/piet/.hermes/hermes-agent
python -m scripts.refactor.split_module hermes_cli/kanban_db.py --ownership --json \
  > /tmp/kdb_ownership.json
python - <<'EOF' > docs/refactor/ownership.kanban_db.md
import json
r = json.load(open('/tmp/kdb_ownership.json'))
L = r['lines']
print("# Ownership of hermes_cli/kanban_db.py\n")
print(f"- Upstream reference: `{r['upstream_ref']}`")
print(f"- FORK-only: {len(r['fork_only'])} symbols, {L['fork_only']} lines -> move to hermes_cli/kanban_ext/")
print(f"- UPSTREAM identical: {len(r['upstream_identical'])} symbols, {L['upstream_identical']} lines -> stay")
print(f"- UPSTREAM diverged: {len(r['upstream_diverged'])} symbols, {L['upstream_diverged']} lines -> stay\n")
print("## Standing conflict surface (the hook-reduction backlog)\n")
print("Each row is a symbol the fork edited inside a body upstream also owns. These are")
print("the only remaining merge-conflict sites once the extraction lands.\n")
print("| symbol | fork lines | upstream lines |")
print("|---|---:|---:|")
for d in r['diverged_detail']:
    print(f"| `{d['symbol']}` | {d['fork_lines']} | {d['upstream_lines']} |")
EOF
head -20 docs/refactor/ownership.kanban_db.md
```

Expected header numbers: 733 / 111 / 129 as in the ground-truth section. This file is also the deliverable that seeds Task 8's hook work, and the backlog of diverged symbols beyond the seven in scope.

- [ ] **Step 2: Record the "before" mergeability baseline**

```bash
cd /home/piet/.hermes/hermes-agent
git merge-tree --write-tree --merge-base=3bfa6001f HEAD 306c9f766 > /tmp/mt_before.txt
TREE=$(head -1 /tmp/mt_before.txt)
git show "$TREE:hermes_cli/kanban_db.py" > /tmp/kdb_merged_before.txt
echo "conflict hunks BEFORE: $(rg -c '^<<<<<<<' /tmp/kdb_merged_before.txt)"
stat -c%s hermes_cli/kanban_db.py
```
Expected: **14** conflict hunks, **1,589,570** bytes. Write both into the commit message later; they are the denominators for acceptance criteria 1 and 3. Use the pinned `--merge-base` form for the reason given under Acceptance criteria — the automatic form reads 0 once the upstream sync has landed.

- [ ] **Step 3: Compute the import-time carve-out — the symbols that must NOT move**

Three fork symbols are referenced at import time by symbols that stay, so a trailing re-export block runs too late for them. They stay in `kanban_db.py`:

```bash
cd /home/piet/.hermes/hermes-agent
python - <<'EOF'
import ast
from scripts.refactor import layering
import subprocess
src = open('hermes_cli/kanban_db.py').read()
tree = ast.parse(src)
top = layering.top_level_symbols(tree)
up = subprocess.run(['git','show','origin/main:hermes_cli/kanban_db.py'],
                    capture_output=True, text=True, check=True).stdout
up_names = set(layering.top_level_symbols(ast.parse(up)))
fork_only = {n for n in top if n not in up_names}
carve = set()
for name, node in top.items():
    if name in fork_only:
        continue
    for target in layering.import_time_names(node, top) - {name}:
        if target in fork_only:
            carve.add(target)
            print(f"CARVE-OUT: {name} (stays) needs {target} at import time")
print(f"\ncarve-out set ({len(carve)}): {sorted(carve)}")
EOF
```
Expected: exactly 3 references naming 2 distinct symbols — `DEFAULT_AUTO_RETRY_BLOCKED_BACKOFF_SECONDS` (default argument of both `_dispatch_once_locked` and `dispatch_once`) and `_SCHEDULE_DUE_UNSPECIFIED` (default argument of `schedule_task`).

Annotations are **not** a source of carve-out here: the module carries `from __future__ import annotations`, so they are strings. Do not add `_ScheduleDueUnspecified` to this set — an earlier revision of this plan did, wrongly. If this step ever reports annotation-driven references for this file, the classifier has become future-*unaware* and is over-reporting.

These stay in `kanban_db.py`. They are small constants, and they become a second small fork-owned hunk alongside the re-export block — accepted, and far cheaper than the alternatives (reordering the block would break it; converting the default arguments to sentinels would be a behaviour change and violates pure-move discipline).

**If this set is larger than 3, stop and reconsider before mapping** — a large carve-out means the ownership boundary is not clean and the extraction buys less than projected.

- [ ] **Step 4: Produce the grouping brief for qwen**

```bash
cd /home/piet/.hermes/hermes-agent
python - <<'EOF' > /tmp/kext_brief.txt
import ast, json
from scripts.refactor import layering
r = json.load(open('/tmp/kdb_ownership.json'))
fork_only = set(r['fork_only']) - {'DEFAULT_AUTO_RETRY_BLOCKED_BACKOFF_SECONDS',
                                   '_SCHEDULE_DUE_UNSPECIFIED'}
src = open('hermes_cli/kanban_db.py').read()
tree, lines = ast.parse(src), src.splitlines()
top = layering.top_level_symbols(tree)
banners = layering.banner_sections(lines)
def sect(ln):
    s = '(head)'
    for bl, bt in banners:
        if bl <= ln:
            s = bt
    return s
for name in sorted(fork_only, key=lambda n: top[n].lineno):
    node = top[name]
    end = getattr(node, 'end_lineno', node.lineno)
    uses = sorted((layering.all_names(node, top) - {name}) & fork_only)
    print(f"{node.lineno:6d} {end-node.lineno+1:5d}  {name}   [{sect(node.lineno)}]")
    if uses:
        print(f"            uses: {', '.join(uses[:10])}")
EOF
wc -l /tmp/kext_brief.txt; head -30 /tmp/kext_brief.txt
```

The brief carries each fork symbol's source position, size, originating banner section, and which other fork symbols it uses. The banner section is a *hint* about responsibility, not the boundary — the boundary is ownership.

- [ ] **Step 5: Dispatch the grouping to qwen (one-shot, interactive-supervised)**

A single `claude-qwen -p` call. Never a kanban lane, never a cron worker (ToS).

```bash
claude-qwen -p "$(cat <<'PROMPT'
Group 731 fork-owned Python symbols into 8-12 submodules of 2,000-3,000 lines
each. They are being extracted out of hermes_cli/kanban_db.py into a new
fork-owned package hermes_cli/kanban_ext/.

The inventory is in /tmp/kext_brief.txt: source line, size in lines, symbol
name, the banner section it came from in square brackets, and which other
fork symbols it uses.

Rules:
- Group by RESPONSIBILITY. The bracketed banner section is a strong hint;
  symbols from the same section usually belong together.
- Output order must preserve source order: a symbol may only be grouped with
  neighbours in the listing, never reordered.
- Every symbol in the brief appears exactly once. Do not invent or drop any.
- Name each module snake_case after what it does (waits, respawn_guard,
  worker_ctx, disposition, lanes...), never after position.

Output ONLY YAML, no prose:

module: hermes_cli/kanban_db.py
package: hermes_cli/kanban_ext
upstream_ref: origin/main
modules:
  - name: <snake_case responsibility>
    symbols: [<names in source order>]
PROMPT
)" > /tmp/kext_map_draft.yaml
head -30 /tmp/kext_map_draft.yaml
```

- [ ] **Step 6: Review and approve the map (Claude Opus 5 — the architectural gate)**

Check in this order; reject back to Step 5 on any failure:

1. Every symbol in `/tmp/kext_brief.txt` appears exactly once, and **no symbol outside it appears at all** — an upstream symbol sneaking into the map would move upstream code out of the upstream file, which is the exact failure this whole re-aim exists to prevent.
2. Neither carve-out symbol (`DEFAULT_AUTO_RETRY_BLOCKED_BACKOFF_SECONDS`, `_SCHEDULE_DUE_UNSPECIFIED`) is in the map.
3. Source order is preserved; only neighbours are grouped.
4. Each module is 2,000–3,000 lines; module count is 8–12.
5. Names describe responsibility, not position.

Verify 1 and 2 mechanically rather than by eye:

```bash
cd /home/piet/.hermes/hermes-agent
python - <<'EOF'
import json, yaml
r = json.load(open('/tmp/kdb_ownership.json'))
carve = {'DEFAULT_AUTO_RETRY_BLOCKED_BACKOFF_SECONDS', '_SCHEDULE_DUE_UNSPECIFIED'}
expected = set(r['fork_only']) - carve
m = yaml.safe_load(open('/tmp/kext_map_draft.yaml'))
mapped = [s for e in m['modules'] for s in e['symbols']]
assert len(mapped) == len(set(mapped)), "duplicate symbol in the map"
mapped = set(mapped)
upstream = set(r['upstream_identical']) | set(r['upstream_diverged'])
assert not (mapped & upstream), f"map moves UPSTREAM symbols: {sorted(mapped & upstream)[:10]}"
assert not (mapped & carve), f"map moves a carve-out symbol: {sorted(mapped & carve)}"
missing, extra = expected - mapped, mapped - expected
assert not missing, f"{len(missing)} fork symbols unplaced: {sorted(missing)[:10]}"
assert not extra, f"map names unknown symbols: {sorted(extra)[:10]}"
print(f"OK: {len(mapped)} fork symbols placed across {len(m['modules'])} modules")
EOF
```

Then present the map to the operator for approval, and save it:

```bash
cp /tmp/kext_map_draft.yaml docs/refactor/boundary-map.kanban_ext.yaml
```

- [ ] **Step 7: Validate the map against the tooling without applying it**

```bash
python -m scripts.refactor.split_module hermes_cli/kanban_db.py \
  --analyze --map docs/refactor/boundary-map.kanban_ext.yaml
```
Expected: exit 0, import-time `backward=0`, no `OVERSIZED` module. A non-zero exit means the grouping is not a valid layering — back to Step 5.

- [ ] **Step 8: Commit the map and the ownership report**

```bash
git add docs/refactor/boundary-map.kanban_ext.yaml docs/refactor/ownership.kanban_db.md
git commit -m "refactor: ownership map and approved kanban_ext boundary map"
```

---

## Task 6: Enumerate the test patch sites

**Files:** produces `docs/refactor/patch-targets.kanban_db.md`. No source edits yet.

**Why this is necessary and why it is not a violation of "zero call-site edits":** the guarantee covers production importers — the ~275 files that call `kanban_db.foo()`. Those do not change, because the re-export block keeps every name bound on `kanban_db`. But `monkeypatch.setattr("hermes_cli.kanban_db.connect", fake)` rebinds the attribute on `kanban_db` only, while a `kanban_ext` submodule that did `from hermes_cli.kanban_db import connect` holds its own binding and never sees the patch. Most such tests fail loudly; at least one (`task_age`) could pass silently against the real implementation. Silent is the unacceptable outcome, so these are re-targeted deliberately and enumerated.

**Known targets (verified on `1ef243502`, re-derive rather than trusting):** string-patched — `connect`, `init_db`, `_record_task_failure`, `_record_worker_exit`, `task_age` (33 lines). Attribute-patched via `setattr(kanban_db, "...")` — `tests/test_planspec_disposition.py` (2), `tests/hermes_cli/test_operator_inventory.py`, `tests/hermes_cli/test_kanban_cli_dispatch_passthrough.py`, `tests/hermes_cli/test_kanban_workflow_routing.py`.

- [ ] **Step 1: Re-derive every patch site**

```bash
cd /home/piet/.hermes/hermes-agent
rg -n '(monkeypatch\.setattr|mock\.patch|patch)\(\s*"hermes_cli\.kanban_db\.' tests/ hermes_cli/ gateway/ scripts/
rg -n 'setattr\(\s*kanban_db\s*,\s*"' tests/ hermes_cli/ gateway/ scripts/
```

- [ ] **Step 2: Classify each target by where it lands**

A patch target only needs re-pointing if the symbol **moves**. Symbols that stay in `kanban_db.py` are unaffected. Cross-check each against the boundary map:

```bash
cd /home/piet/.hermes/hermes-agent
python - <<'EOF'
import yaml
m = yaml.safe_load(open('docs/refactor/boundary-map.kanban_ext.yaml'))
where = {s: e['name'] for e in m['modules'] for s in e['symbols']}
for sym in ['connect', 'init_db', '_record_task_failure', '_record_worker_exit', 'task_age']:
    dest = where.get(sym)
    print(f"{sym:28s} -> {'hermes_cli.kanban_ext.' + dest if dest else 'STAYS in kanban_db.py (no change needed)'}")
EOF
```

Write the result, with file:line for every site, to `docs/refactor/patch-targets.kanban_db.md`. The edits themselves land in Task 7 Step 6, once the new paths exist.

- [ ] **Step 3: Commit the list**

```bash
git add docs/refactor/patch-targets.kanban_db.md
git commit -m "refactor: enumerate kanban_db test patch targets before extraction"
```

---

## Task 7: Extract `kanban_ext` (the whole point)

**Prerequisites:** Task 0 is closed (done). The Codex upstream sync has landed — verified in Step 0.

**Files:**
- Modify: `hermes_cli/kanban_db.py` (stays a file), `docs/kanban/LIFECYCLE.md`, `scripts/check_kanban_lifecycle_anchors.py`, the patch sites from Task 6
- Create: `hermes_cli/kanban_ext/`

- [ ] **Step 0: Confirm the upstream sync landed first (operator-set sequencing)**

Two operations must not restructure `kanban_db.py` at the same time. The Codex sync merges `origin/main` (`306c9f766`) and resolves `kanban_db.py` as *ours*; it must be on `main` before anything here starts.

```bash
cd /home/piet/.hermes/hermes-agent
git merge-base --is-ancestor 306c9f766 HEAD && echo "sync LANDED — proceed" || echo "sync NOT landed — STOP"
git worktree list | rg upstream-sync || echo "(sync worktree already cleaned up)"
git status --short          # this checkout must be clean of foreign work
```

If the sync has not landed, stop here — do not start the extraction and do not touch that worktree. When it has landed, re-run Task 3 Step 6 and Task 5 Step 1 before continuing: the sync brings ~2,500 files, and while `kanban_db.py` itself is resolved as *ours* and so is byte-unchanged, the ownership numbers must be re-confirmed rather than assumed.

- [ ] **Step 1: Branch and snapshot the API**

```bash
cd /home/piet/.hermes/hermes-agent
git status --short                       # must be clean of foreign work
git checkout -b refactor/extract-kanban-ext main
python -m scripts.refactor.api_snapshot hermes_cli.kanban_db \
  --out docs/refactor/api-snapshot.kanban_db.json
```

- [ ] **Step 2: Extract**

```bash
python -m scripts.refactor.split_module hermes_cli/kanban_db.py \
  --extract --map docs/refactor/boundary-map.kanban_ext.yaml \
  --package hermes_cli/kanban_ext
test -f hermes_cli/kanban_db.py && echo "origin is still a FILE — correct"
test -d hermes_cli/kanban_db && echo "FATAL: origin became a directory" && exit 1
ls -la hermes_cli/kanban_ext/
wc -l hermes_cli/kanban_ext/*.py hermes_cli/kanban_db.py | sort -n
```
Expected: 9–13 files in `kanban_ext/`, none over ~3,000 lines; `kanban_db.py` down to roughly 13,000 lines. Resolve any `REVIEW REQUIRED` shadowing report exactly as in the `--apply` path: confirm each is genuinely local, and fix via the boundary map, never by hand-editing emitted code.

- [ ] **Step 3: Acceptance criterion 3 — the CodeGraph payoff**

```bash
stat -c%s hermes_cli/kanban_db.py
```
Expected: **under 1,048,576**, projected ≈ 565,000. If it is still over the limit, the ownership map moved too little — stop.

- [ ] **Step 4: Acceptance criterion 4 — API equivalence and import smoke**

```bash
python -m scripts.refactor.api_snapshot hermes_cli.kanban_db \
  --compare docs/refactor/api-snapshot.kanban_db.json
python -c "import hermes_cli.kanban_db as k; print(len(dir(k)), 'attributes')"
python -c "from hermes_cli import kanban_db; print(kanban_db.VALID_STATUSES)"
python -c "from hermes_cli.kanban_db import create_task, claim_task, complete_task; print('symbol-level import OK')"
python -c "import hermes_cli.kanban_ext; print('ext imports standalone OK')"
```
Expected: `API IDENTICAL`, then all four imports succeed. **Any API diff means fix the boundary map — never hand-edit the emitted package.**

The fourth check matters on its own: importing `kanban_ext` first, before `kanban_db`, must also work. It exercises the circular import from the other side.

- [ ] **Step 5: Acceptance criterion 1 — mergeability, measured**

Use the **pinned-base** form. After the Codex sync, the automatic form collapses to zero and measures nothing:

```bash
cd /home/piet/.hermes/hermes-agent
git merge-tree --write-tree --merge-base=3bfa6001f HEAD 306c9f766 > /tmp/mt_after.txt
TREE=$(head -1 /tmp/mt_after.txt)
git show "$TREE:hermes_cli/kanban_db.py" > /tmp/kdb_merged_after.txt
echo "conflict hunks AFTER : $(rg -c '^<<<<<<<' /tmp/kdb_merged_after.txt)"
echo "conflict hunks BEFORE: 14   (pinned-base baseline)"
```

For **this task** the count **must not increase**. It is not expected to fall much — at most the 1–2 hunks that sit in fork symbols — because the rest sit inside upstream bodies that Task 7 leaves alone. If it *rises*, the extraction has torn apart text git was previously matching: that is a blocking defect, re-examine the map.

The strict-decrease bar itself is gated at **Task 8 Step 8**, after the hooks have moved the fork lines that cause the remaining hunks. Task 7 does not attempt it and is not judged against it.

Also record the primary success measure for this pass:

```bash
python -m scripts.refactor.split_module hermes_cli/kanban_db.py --ownership
```
Expected: **fork-only symbols in `kanban_db.py` drop from 733 to 2** (the carve-out), and fork-only lines from 23,745 to single digits.

- [ ] **Step 6: Re-target the patch sites from Task 6**

For each site whose symbol moved, change `hermes_cli.kanban_db.<symbol>` to `hermes_cli.kanban_ext.<submodule>.<symbol>`, and `setattr(kanban_db, "<symbol>", ...)` to `setattr(kanban_ext.<submodule>, "<symbol>", ...)`. Sites whose symbol stayed in `kanban_db.py` are left alone.

```bash
scripts/run-affected.sh
ruff check hermes_cli/kanban_ext hermes_cli/kanban_db.py
```
Expected: green and clean.

- [ ] **Step 7: Re-anchor `docs/kanban/LIFECYCLE.md`**

Anchors point at `../../hermes_cli/kanban_db.py#L<n>`. Symbols that stayed need only a line-number refresh; symbols that moved need a new path. Generate both mechanically:

```bash
cd /home/piet/.hermes/hermes-agent
python - <<'EOF'
import ast, os, re
loc = {}
def index(path, rel):
    tree = ast.parse(open(path).read())
    for n in tree.body:
        names = []
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names = [n.name]
        elif isinstance(n, ast.Assign):
            names = [t.id for t in n.targets if isinstance(t, ast.Name)]
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            names = [n.target.id]
        for nm in names:
            loc[nm] = (rel, n.lineno)
index('hermes_cli/kanban_db.py', 'hermes_cli/kanban_db.py')
for fn in sorted(os.listdir('hermes_cli/kanban_ext')):
    if fn.endswith('.py') and fn != '__init__.py':
        index(f'hermes_cli/kanban_ext/{fn}', f'hermes_cli/kanban_ext/{fn}')
doc = open('docs/kanban/LIFECYCLE.md').read()
def fix(m):
    sym = m.group(1)
    if sym not in loc:
        raise SystemExit(f"LIFECYCLE.md anchors unknown symbol {sym!r}")
    rel, ln = loc[sym]
    return f"[`{sym}`](../../{rel}#L{ln})"
out, n = re.subn(
    r"\[`([A-Za-z_][A-Za-z_0-9]*)`\]\(\.\./\.\./hermes_cli/kanban_db\.py#L\d+\)",
    fix, doc)
open('docs/kanban/LIFECYCLE.md', 'w').write(out)
print(f"re-anchored {n} symbol links")
EOF
```

Then fix by hand what is not symbol-shaped:

- The **"Section index"** table lists banner line numbers in the monolith. Replace it with a two-part index: the banner sections still in `kanban_db.py`, and one row per `kanban_ext` submodule with its path, line count and responsibility.
- The **opening paragraph** says the file is larger than 1 MiB and CodeGraph does not index it. After this task the opposite is true — say so, and point at `docs/refactor/ownership.kanban_db.md` for the fork/upstream split.

- [ ] **Step 8: Update the anchor checker**

`scripts/check_kanban_lifecycle_anchors.py` verifies anchors against `kanban_db.py` alone. Teach it to resolve `hermes_cli/kanban_ext/<file>#L<n>` as well.

```bash
python scripts/check_kanban_lifecycle_anchors.py
```
Expected: exit 0.

- [ ] **Step 9: CodeGraph proof**

```bash
codegraph reindex 2>&1 | tail -5
codegraph query dispatch_once
```
Expected: the real `dispatch_once` in `hermes_cli/kanban_db.py` — not only `fake_dispatch_once` from test files. Record the output verbatim in the commit message.

- [ ] **Step 10: Pre-merge gates**

```bash
python -m pytest --co -q tests/ 2>&1 | tail -5
scripts/run-affected.sh
ruff check .
```

- [ ] **Step 11: Commit**

```bash
git add -A
git commit -m "$(cat <<'MSG'
refactor: extract fork-owned kanban code to hermes_cli/kanban_ext (pure move)

kanban_db.py stays a MODULE FILE, deliberately: upstream's future diffs are
addressed to that literal path and would not apply to a directory.

733 fork-only symbols (23,745 lines) move to hermes_cli/kanban_ext/. Upstream
symbols stay in place, byte-identical. One trailing re-export block keeps all
~275 importers and the private-symbol monkeypatches working. Two fork
constants stay behind because upstream-owned symbols use them as default
arguments, which a trailing block cannot satisfy.

Bodies are byte-identical in both directions: kanban_ext reaches back with
plain symbol imports (kanban_db is fully populated by the time the trailing
block runs), and kanban_db reaches forward through the names that block binds.

  kanban_db.py size:      1,589,570 -> <after> bytes  (CodeGraph limit 1,048,576)
  fork-only lines in it:     23,745 -> <after>
  merge conflict hunks:          14 -> <after>

Remaining conflict surface is the 129 upstream symbols the fork edited
in place; they are listed in docs/refactor/ownership.kanban_db.md and are
the input to Task 8's hook work (seven of them) and to the residual backlog (the rest).

CodeGraph now resolves the real definitions:
<paste the `codegraph query dispatch_once` output here>

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
MSG
)"
```

- [ ] **Step 12: Review and merge**

Independent review, builder family ≠ reviewer family (Codex built the tooling, so this review goes to a Grok or Claude reviewer). The brief is narrow and mechanical:

1. Confirm `hermes_cli/kanban_db.py` is still a file.
2. Confirm no upstream-owned symbol moved: `--ownership` on the post-extraction file must show the same 111 identical + 129 diverged symbols still present.
3. Confirm every moved body is byte-identical to its pre-extraction text.
4. Confirm none of the five Follow-up defects was "fixed" in passing.
5. Confirm the re-export block is the last statement in the file.

```bash
git diff main...refactor/extract-kanban-ext --stat
git checkout main && git merge --ff-only refactor/extract-kanban-ext
```

---

## Task 8: Hooks — move fork logic out of upstream function bodies

**This is the task that satisfies acceptance criterion 1.** Task 7 removes fork *symbols* from an upstream file; this task removes fork *lines* from inside upstream *functions*. Only the second moves the conflict count.

**Prerequisite:** Task 7 merged. The file is ~565 KB and the seven target symbols are readable.

**Files:**
- Modify: `hermes_cli/kanban_db.py` (the seven symbols in the work list)
- Modify/create: `hermes_cli/kanban_ext/hooks.py` and siblings — the fork behaviour being lifted
- Create: `tests/hermes_cli/test_kanban_db_hooks_characterization.py`

### Discipline change — read this before starting

Tasks 1–7 were **pure moves**, proven equivalent mechanically by `api_snapshot.py`. Task 8 is **not** a pure move: it restructures the inside of functions the fork and upstream both edited. `api_snapshot.py` still applies — signatures must not change — but it can no longer prove behaviour, because behaviour now flows through a hook instead of inline code.

The replacement proof is **characterization tests written before the refactor**: capture what the function does today, then require the identical result after. Nothing else in this plan carries the same risk profile, and `create_task` is the kanban lifecycle spine — an error there hits every worker on every board.

**Rules for this task, all mandatory:**

1. **One symbol per commit.** Seven symbols, seven commits, each independently revertible.
2. **Characterization tests land in their own commit *before* the refactor commit for that symbol**, and must pass against the *unmodified* function first. A test written after the change proves nothing.
3. **No behaviour change.** Not a bug fix, not a docstring correction, not a rename. The five known defects under Follow-ups stay unfixed here too. If a hook reveals a bug, record it and move on.
4. **The upstream body must end up textually closer to `origin/main`'s version**, measured — that is the entire point. Verify per symbol (see the per-symbol gate below).
5. **Hooks live in `hermes_cli/kanban_ext/`**, reached through the existing re-export. Do not add new fork names to `kanban_db.py` beyond the single hook call.

### Order of work — easiest first, riskiest last

Deliberately inverted from size, so the mechanism is proven on trivial cases before it touches the lifecycle spine.

- [ ] **Step 1: `_backup_corrupt_db` — resolve as *theirs*, no hook**

It contains 0 fork lines; the conflict is upstream's `_prune_corrupt_backups` retention cap against a stale fork copy. Take upstream's version wholesale.

```bash
cd /home/piet/.hermes/hermes-agent
git checkout -b refactor/kanban-db-hooks main
python - <<'EOF'
import ast, subprocess
# print upstream's version of the symbol for review before replacing
up = subprocess.run(['git','show','origin/main:hermes_cli/kanban_db.py'],
                    capture_output=True, text=True, check=True).stdout
t = ast.parse(up); ls = up.splitlines()
for n in t.body:
    if getattr(n, 'name', None) == '_backup_corrupt_db':
        print("\n".join(ls[n.lineno-1:n.end_lineno]))
EOF
```

Read both versions. Confirm the fork's copy really adds nothing, then replace the fork body with upstream's. Run `scripts/run-affected.sh`. Commit as `refactor(hooks): take upstream _backup_corrupt_db (fork adds nothing)`.

- [ ] **Step 2: `list_comments` — trivial realignment (3 fork lines)**

The only fork part is a `kind` fallback. Write the characterization test first:

```python
def test_list_comments_kind_fallback_is_preserved(tmp_path):
    """Characterization: comments without an explicit kind still report the
    fork's default, and comments with one are untouched."""
    from hermes_cli import kanban_db
    db = tmp_path / "k.db"
    conn = kanban_db.connect(str(db))
    kanban_db.init_db(conn)
    tid = kanban_db.create_task(conn, title="t", body="b")
    kanban_db.add_comment(conn, tid, "no kind given")
    rows = kanban_db.list_comments(conn, tid)
    assert rows, "characterization needs at least one comment"
    assert rows[0].kind is not None, "fork's kind fallback must survive"
```

Adjust the call signatures to whatever the real API is — read them, do not guess. Run it against the **unmodified** function and confirm it passes. Commit the test. Then lift the fallback into `kanban_ext`, re-run, commit.

- [ ] **Step 3: `_cleanup_worker_tmux` — trivial realignment (5 fork lines)**

Same shape as Step 2: characterization test first, covering whatever the 5 fork lines do (inspect them with the diff command in the per-symbol gate below), then lift, then re-run.

- [ ] **Step 4: `_guard_existing_db_is_healthy` — hook (45 fork lines)**

Note this symbol has **more upstream-only lines (56) than fork lines (45)** — upstream has moved on here more than the fork has. Read upstream's version first and prefer adopting its structure, lifting only the genuinely fork-specific guard into a hook.

Characterization tests must cover: a healthy DB passes; a corrupt DB is caught; whatever fork-specific condition the 45 lines add.

- [ ] **Step 5: `Task` — field separation (149 fork lines, incl. `from_row`)**

`Task` is a dataclass. The fork added fields and extended `from_row`. **`from_row` is a method inside `Task`** — do not treat it as a top-level symbol.

Preferred shape: keep upstream's field set on `Task`; move fork-added fields to a `kanban_ext` companion (a mixin or a dataclass composed alongside), and have `from_row` call one hook that populates them. Characterization tests must pin: every field name currently on `Task` still exists with the same type, and `from_row` on a real DB row produces an identical object.

```python
def test_task_field_surface_is_unchanged():
    """Characterization: the full field set of Task, pinned by name."""
    import dataclasses
    from hermes_cli import kanban_db
    got = {f.name for f in dataclasses.fields(kanban_db.Task)}
    # paste the ACTUAL current set here, generated once before refactoring:
    #   python -c "import dataclasses;from hermes_cli import kanban_db;\
    #     print(sorted(f.name for f in dataclasses.fields(kanban_db.Task)))"
    expected = {...}
    assert got == expected
```

- [ ] **Step 6: `_default_spawn` — hook (179 fork lines)**

This is the worker launch path. Characterization tests must cover the environment the child process receives, because that is what the fork extends: assert on the constructed env/argv rather than on side effects, so the test does not spawn anything. Read `docs/kanban/LIFECYCLE.md` §"What the spawned worker sees" for the contract this must preserve.

- [ ] **Step 7: `create_task` — hook (276 fork lines, 6 of the 14 hunks)**

**The highest-risk change in this entire plan.** `create_task` is the entry point of the kanban lifecycle (create → claim → complete); every worker on every board goes through it. The operator was warned and chose to proceed, so it proceeds — with the heaviest test protection in the plan.

Before touching it, enumerate what it currently does:

```bash
cd /home/piet/.hermes/hermes-agent
python - <<'EOF'
import ast
t = ast.parse(open('hermes_cli/kanban_db.py').read())
for n in t.body:
    if getattr(n, 'name', None) == 'create_task':
        print(f"lines {n.lineno}-{n.end_lineno}, args:")
        for a in n.args.args + n.args.kwonlyargs:
            print(f"   {a.arg}")
EOF
rg -n 'create_task\(' --type py -g '!tests/**' | wc -l   # production call sites
rg -n 'create_task\(' tests/ | wc -l                     # existing test coverage
```

Characterization tests must cover, at minimum, one case per initial status the function accepts (`triage`, `todo`, `ready`, `blocked` — see `VALID_INITIAL_STATUSES`), plus parent-linking, plus the role/workspace contract validation, plus at least one rejection path. Existing tests in `tests/hermes_cli/test_kanban_db_lifecycle.py` are a starting point, not a substitute — they were written for behaviour, not for pinning it against a refactor.

Only once those pass against the unmodified function may the hook extraction begin.

### Per-symbol gate (run for every one of Steps 1–7)

```bash
cd /home/piet/.hermes/hermes-agent
SYM=create_task   # the symbol just refactored
python - <<'EOF'
import ast, difflib, os, subprocess
sym = os.environ['SYM']
def body(src):
    t = ast.parse(src); ls = src.splitlines()
    for n in t.body:
        names = ([n.name] if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                 else [x.id for x in getattr(n, 'targets', []) if isinstance(x, ast.Name)])
        if sym in names:
            s = n.lineno
            d = getattr(n, 'decorator_list', None)
            if d: s = min(s, min(x.lineno for x in d))
            return ls[s-1:n.end_lineno]
    return []
up = body(subprocess.run(['git','show','origin/main:hermes_cli/kanban_db.py'],
                         capture_output=True, text=True, check=True).stdout)
new = body(open('hermes_cli/kanban_db.py').read())
old = body(subprocess.run(['git','show','main:hermes_cli/kanban_db.py'],
                          capture_output=True, text=True, check=True).stdout)
def forklines(a, b):
    d = list(difflib.unified_diff(a, b, lineterm='', n=0))
    return sum(1 for l in d if l.startswith('+') and not l.startswith('+++'))
print(f"{sym}: fork lines inside, before={forklines(up, old)}  after={forklines(up, new)}")
EOF
```

**The `after` number must be strictly lower than `before`.** If it is not, the hook did not actually move fork code out of upstream's body — it only relocated it within the same function. Also required per symbol:

```bash
python -m scripts.refactor.api_snapshot hermes_cli.kanban_db --compare docs/refactor/api-snapshot.kanban_db.json
python -m pytest tests/hermes_cli/test_kanban_db_hooks_characterization.py -v
scripts/run-affected.sh
```

- [ ] **Step 8: Acceptance criterion 1 — the hard gate**

```bash
cd /home/piet/.hermes/hermes-agent
git merge-tree --write-tree --merge-base=3bfa6001f HEAD 306c9f766 > /tmp/mt_hooks.txt
TREE=$(head -1 /tmp/mt_hooks.txt)
git cat-file -p "$TREE:hermes_cli/kanban_db.py" | rg -c '^<<<<<<<'
echo "baseline: 14"
```

**Must be strictly below 14.** This is the criterion the operator declined to soften; it is now reachable because the fork lines causing those hunks have moved.

Re-measure the baseline once more first if the Codex upstream sync landed in the meantime — same command, same file. The sync keeps `kanban_db.py` on *ours*, so the baseline is expected to be unchanged at 14, but confirm rather than assume.

- [ ] **Step 9: Full gates, then review and merge**

```bash
python -m pytest --co -q tests/ 2>&1 | tail -5
scripts/run-affected.sh
ruff check .
python -m scripts.refactor.api_snapshot hermes_cli.kanban_db --compare docs/refactor/api-snapshot.kanban_db.json
```

Review brief for the independent reviewer (family ≠ builder), which differs from Task 7's because this is **not** a pure move:

1. For each of the seven symbols: does the upstream body now match `origin/main` more closely, and is every remaining difference justified?
2. Did any behaviour change? Specifically: are the characterization tests genuine pins, or were any weakened/deleted to make the refactor pass?
3. Was each characterization test committed *before* its refactor, and did it pass against the unmodified function?
4. `create_task` gets its own pass: every initial status, parent-linking, contract validation, rejection paths.
5. None of the five Follow-up defects fixed in passing.

---

## Task 9: Close out

- [ ] **Step 1: Verify every acceptance criterion**

```bash
cd /home/piet/.hermes/hermes-agent
stat -c%s hermes_cli/kanban_db.py                       # < 1048576
python -m scripts.refactor.split_module hermes_cli/kanban_db.py --ownership
python -m scripts.refactor.api_snapshot hermes_cli.kanban_db \
  --compare docs/refactor/api-snapshot.kanban_db.json
git merge-tree --write-tree --merge-base=3bfa6001f HEAD 306c9f766 > /tmp/mt_final.txt
git show "$(head -1 /tmp/mt_final.txt):hermes_cli/kanban_db.py" | rg -c '^<<<<<<<'
codegraph query dispatch_once
```

- [ ] **Step 2: Record the standing rule where it will actually be read**

Add to `AGENTS.md` and `CLAUDE.md`, in the section that governs where new code goes:

> **New fork code never goes into an upstream-owned file.** `hermes_cli/kanban_db.py` is upstream's file; the fork's extension lives in `hermes_cli/kanban_ext/`. Add fork behaviour there and reach it through the existing re-export, not by editing `kanban_db.py`. The file went 9,135 → 38,843 lines while this rule was absent; without it the extraction is undone within months. The same reasoning applies to `gateway/run.py`, which is 97.6% upstream's and deliberately not split.

- [ ] **Step 3: Correct the code-map guidance**

`CLAUDE.md`'s "Code map" section says CodeGraph skips `hermes_cli/kanban_db.py` and `gateway/run.py`. After this work only the second is true. Update it to: `kanban_db.py` is indexed again; `gateway/run.py` remains blind **by decision, not accident** — upstream's own copy is 1.18 MB, so `rg` plus `docs/kanban/LIFECYCLE.md` stays the documented route there. Check `AGENTS.md` and `docs/agent-dev-guide.md` for the same claim.

- [ ] **Step 4: File the follow-ups**

Two backlogs come out of this work:

1. **Residual divergence beyond the seven hooked symbols.** Task 8 hooks the symbols that actually cause today's 14 conflict hunks. It does **not** exhaust the 129 diverged symbols — `docs/refactor/ownership.kanban_db.md` lists them all, largest first: `_dispatch_once_locked` (1,297 fork lines vs 400 upstream), `complete_task` (612 vs 208), `_migrate_add_optional_columns` (569 vs 256). These carry no conflict *today* only because upstream has not touched them recently; they are the next conflict surface as soon as it does. Hook them opportunistically, worst-ratio first, under the same characterization-test discipline as Task 8.
2. **Upstream's skipped `kanban_db.py` delta.** The Codex sync resolved the file as *ours*, so upstream's `3bfa6001f → 306c9f766` changes to it (+693 lines) are recorded as merged but never applied, and git will not re-offer them. Now that the file is ~13k lines instead of 38.8k, replaying them by hand is tractable for the first time:

   ```bash
   git diff 3bfa6001f 306c9f766 -- hermes_cli/kanban_db.py
   ```

   Decide per hunk: apply, or record as deliberately-declined in `docs/refactor/ownership.kanban_db.md`. This is the first real test of whether the restructure achieved its stated goal — "Updates leicht reinholen".

3. **The five defects deliberately left unfixed**, each needing its own change and its own test: the unguarded `_dispatch_once_locked` DB-path-resolution path; the `failed`/`canceled`/`cancelled` status vocabulary; `block_task`'s docstring; the review-dispatch "back to running" comment; the orphan banner divider.

- [ ] **Step 5: Push the fork**

```bash
git status --short
git log --oneline -8
git push piet-fork main     # fast-forward only, never --force, never origin
```

---

## Superseded by the re-aim

The following are **not** part of this plan any more. They are recorded so nobody re-derives them from the original spec:

- Splitting `gateway/run.py` — actively harmful. 97.6% upstream's file; upstream's own copy is already 1.18 MB, so a split buys blindness-relief that upstream will not sustain, at the price of permanent divergence against the repo's largest incoming change stream.
- Splitting `hermes_cli/web_server.py` — deferred. Genuinely contested ownership (+3,667 fork vs +3,681 upstream) and already CodeGraph-visible at 803 KB. Needs its own ownership analysis first.
- Splitting `cli.py` and `hermes_cli/main.py` — dropped. Both already visible (772 KB, 618 KB) and under no merge pressure. The `hermes_cli/main.py` profile-override ordering risk is moot as a result.
- Converting `hermes_cli/kanban_db.py` into a package at the same path — inverted. It preserves Python compatibility but destroys merge compatibility, which is now the overriding goal.

## Amendment to the original approved spec (still applicable)

The spec's cycle mitigation — *"anything that would cycle stays in `_core.py` for that round"* — does not work as written, and the measurement that showed it is what produced the import-time/runtime distinction the current design still rests on.

Partitioning `kanban_db.py` by its banner sections and converting intra-module references into symbol-level imports puts **28 of 37 sections, 34,764 of 38,834 lines (89.5%), into a single module-level import cycle**. Under the `_core.py` fallback that yields a 34.7k-line `_core.py`: still over 1 MiB, no goal met. The cause is that symbol-level imports resolve at import time and therefore fail on cycles, while the file's real dependency structure is fine — its symbol graph is a near-perfect DAG (973 symbols, exactly one 3-symbol cycle).

The fix, verified empirically and carried into the re-aimed design: only **import-time** references (assignment values, decorators, default arguments, class bases) constrain module order; **runtime** references (names used inside function bodies) do not, and resolve fine across a cycle. Measured across the five original files, import-time backward references number **zero**.

In the re-aimed extraction this pays off twice over. Because the re-export block is the *last* statement in `kanban_db.py`, every cross-file reference in both directions is a runtime reference resolved after both modules are fully populated — so unlike the abandoned package split, which needed 140 mechanical rewrites, the extraction moves **every body byte-identically**. The only exceptions are the 3 import-time references that drive the Task 5 Step 3 carve-out, and they fail loudly rather than silently.
