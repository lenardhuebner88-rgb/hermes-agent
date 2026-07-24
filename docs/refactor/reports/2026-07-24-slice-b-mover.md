# Slice B — sibling-package mover

Date: 2026-07-24  
Branch: `refactor/split-tooling`  
Implementation commit: `3dc1d8c61`

## Scope and files

Implemented `extract_to_package` and the `--extract` CLI only. The origin
remains a module file, `apply_split` remains a `NotImplementedError` stub, and
`--apply` is not exposed.

Files changed in the implementation stage:

- `scripts/refactor/split_module.py` — 598 lines
- `tests/refactor/test_split_module.py` — 291 lines

This report is the only file added in the report stage.

No giant module was modified, and `--extract` was not applied to real data.

## Gate results

### Refactor tests

Command:

```text
PYTHONPATH=$(pwd) python3 -m pytest tests/refactor/ -v
```

Exit code: `0`

```text
============================= test session starts ==============================
platform linux -- Python 3.12.3, pytest-7.4.4, pluggy-1.4.0 -- /usr/bin/python3
cachedir: .pytest_cache
rootdir: /home/piet/.hermes/worktrees/codex-refactor-split-tooling
configfile: pyproject.toml
plugins: anyio-4.13.0, timeout-2.4.0, cov-4.1.0
collecting ... collected 23 items

tests/refactor/test_api_snapshot.py::test_snapshot_records_functions_classes_and_values PASSED [  4%]
tests/refactor/test_api_snapshot.py::test_diff_is_empty_for_module_converted_to_package PASSED [  8%]
tests/refactor/test_api_snapshot.py::test_diff_reports_missing_and_changed_symbols PASSED [ 13%]
tests/refactor/test_layering.py::test_top_level_symbols_collects_defs_classes_and_assignments PASSED [ 17%]
tests/refactor/test_layering.py::test_banner_sections_finds_divider_title_pairs PASSED [ 21%]
tests/refactor/test_layering.py::test_import_time_names_covers_decorators_defaults_and_bases PASSED [ 26%]
tests/refactor/test_layering.py::test_classify_references_splits_forward_from_backward PASSED [ 30%]
tests/refactor/test_layering.py::test_import_time_backward_reference_is_detected PASSED [ 34%]
tests/refactor/test_layering.py::test_evaluated_annotations_and_class_keywords_are_import_time PASSED [ 39%]
tests/refactor/test_layering.py::test_postponed_annotations_are_excluded_but_class_keywords_are_not PASSED [ 43%]
tests/refactor/test_layering.py::test_kanban_schedule_task_uses_default_but_postpones_annotation PASSED [ 47%]
tests/refactor/test_split_module.py::test_analyze_flags_import_time_backward_reference PASSED [ 52%]
tests/refactor/test_split_module.py::test_analyze_reports_runtime_backward_without_flagging_it_fatal PASSED [ 56%]
tests/refactor/test_split_module.py::test_analyze_marks_import_time_backward_as_fatal PASSED [ 60%]
tests/refactor/test_split_module.py::test_ownership_separates_the_three_buckets PASSED [ 65%]
tests/refactor/test_split_module.py::test_extract_leaves_origin_a_file_and_moves_only_mapped_symbols PASSED [ 69%]
tests/refactor/test_split_module.py::test_extract_imports_stayed_symbols_without_rewriting_body PASSED [ 73%]
tests/refactor/test_split_module.py::test_extract_rewrites_only_intra_package_backward_edges PASSED [ 78%]
tests/refactor/test_split_module.py::test_extract_copies_docstring_source_span_and_header PASSED [ 82%]
tests/refactor/test_split_module.py::test_extract_preserves_behaviour_and_api PASSED [ 86%]
tests/refactor/test_split_module.py::test_extract_refuses_import_time_reference_into_the_extracted_set PASSED [ 91%]
tests/refactor/test_split_module.py::test_local_binding_shadowing_a_backward_target_is_not_rewritten PASSED [ 95%]
tests/refactor/test_split_module.py::test_extract_refuses_split_binding_across_modules PASSED [100%]

============================== 23 passed in 0.60s ==============================
```

### Ruff

Command:

```text
/home/piet/.hermes/hermes-agent/venv/bin/ruff check scripts/refactor tests/refactor
```

Exit code: `0`

```text
All checks passed!
```

### Affected suite

Command:

```text
scripts/run-affected.sh
```

Exit code: `0`

```text
[branch-age] HEAD ist 5 Commits hinter main — rebase empfohlen.
▶ running per-file parallel test suite via run_tests_parallel.py
  (TZ=UTC LANG=C.UTF-8 PYTHONHASHSEED=0; clean env)
▶ pre-compiling bytecode cache
▶ launching test runner
Discovered 3 test files (~23 tests) under ['tests/refactor/test_api_snapshot.py', 'tests/refactor/test_layering.py', 'tests/refactor/test_split_module.py']; running with -j 6
[ 13.0% |     3/~23 | ✓3 | ✗0] ✓ tests/refactor/test_api_snapshot.py (3✓, 0.7s)
[ 65.2% |    15/~23 | ✓15 | ✗ 0] ✓ tests/refactor/test_split_module.py (12✓, 0.8s)
[100.0% |    23/~23 | ✓23 | ✗ 0] ✓ tests/refactor/test_layering.py (8✓, 1.2s)

=== Summary: 3 files, 23 tests passed, 0 failed (100% complete) in 1.2s (6 workers) ===
  Durations cached to test_durations.json (3 files)

=== Per-file subprocess time distribution ===
  Files:   3
  Total subprocess CPU-wall: 2.8s  (runner wall: 1.2s, parallelism: 6x)
  P50: 0.84s  P90: 1.25s  P95: 1.25s  P99: 1.25s  Max: 1.25s
  <1s: 2 files (67%)  <2s: 3 files (100%)
  Top 10 slowest:
      1.25s  tests/refactor/test_layering.py
      0.84s  tests/refactor/test_split_module.py
      0.69s  tests/refactor/test_api_snapshot.py
```

## Live-data measurements

These commands only read `hermes_cli/kanban_db.py`.

```text
$ python3 -m scripts.refactor.split_module hermes_cli/kanban_db.py --analyze
hermes_cli/kanban_db.py: 38843 lines, 973 symbols, 37 modules
  import-time cross-module refs: forward=58 backward=0
  runtime cross-module refs:     forward=878 backward=140 (these get the module-object form)
```

Exit code: `0`

```text
$ python3 -m scripts.refactor.split_module hermes_cli/kanban_db.py --ownership
hermes_cli/kanban_db.py against origin/main:
  fork-only:           733 symbols, 23745 lines
  upstream-identical:  111 symbols, 1293 lines
  upstream-diverged:   129 symbols, 11419 lines
```

Exit code: `0`

```text
$ python3 -m scripts.refactor.api_snapshot hermes_cli.kanban_db --out /tmp/kdb_api.json
wrote 992 symbols to /tmp/kdb_api.json
```

Exit code: `0`

```text
$ python3 -m scripts.refactor.api_snapshot hermes_cli.kanban_db --compare /tmp/kdb_api.json
API IDENTICAL — 992 symbols match
```

Exit code: `0`

## Real-data refusal dry run

Map:

```yaml
module: hermes_cli/kanban_db.py
package: hermes_cli/kanban_ext
modules:
  - name: consts
    symbols: [DEFAULT_AUTO_RETRY_BLOCKED_BACKOFF_SECONDS, _SCHEDULE_DUE_UNSPECIFIED]
```

Command and verbatim output:

```text
$ PYTHONPATH=$(pwd) python3 -m scripts.refactor.split_module hermes_cli/kanban_db.py --extract --map /tmp/bad_map.yaml --package /tmp/should_not_exist_ext
REFUSING: symbols that stay reference extracted symbols at import time; a trailing re-export block runs too late for these:
  _dispatch_once_locked needs DEFAULT_AUTO_RETRY_BLOCKED_BACKOFF_SECONDS (mapped to consts)
  auto_retry_blocked_tasks needs DEFAULT_AUTO_RETRY_BLOCKED_BACKOFF_SECONDS (mapped to consts)
  dispatch_once needs DEFAULT_AUTO_RETRY_BLOCKED_BACKOFF_SECONDS (mapped to consts)
  escalate_silent_blocks_sweep needs DEFAULT_AUTO_RETRY_BLOCKED_BACKOFF_SECONDS (mapped to consts)
  schedule_task needs _SCHEDULE_DUE_UNSPECIFIED (mapped to consts)
  silent_block_task_ids needs DEFAULT_AUTO_RETRY_BLOCKED_BACKOFF_SECONDS (mapped to consts)
$ echo "exit=$?"
exit=2
$ test -e /tmp/should_not_exist_ext && echo "FAIL: wrote despite refusing" || echo "OK: nothing written"
OK: nothing written
$ git status --short hermes_cli/kanban_db.py
```

The origin file remained unmodified and `/tmp/should_not_exist_ext` was not
created.

The brief calls out the three upstream-owned referrers
`_dispatch_once_locked`, `dispatch_once`, and `schedule_task`. The tool also
correctly reports three fork-owned functions because this deliberately minimal
map moves only the two constants and therefore leaves those functions behind:
`auto_retry_blocked_tasks`, `escalate_silent_blocks_sweep`, and
`silent_block_task_ids` each evaluate
`DEFAULT_AUTO_RETRY_BLOCKED_BACKOFF_SECONDS` as a default argument at import
time. Suppressing those genuine offenders would weaken the refusal rule.

## Plan findings and ambiguities

- The Task 4 amendment asks for six `--extract` cases plus two re-pointed
  shared-rule cases, while its literal Step 6 lists only three extract tests.
  The suite covers six separate extraction mechanics (origin shape, imports and
  byte-identical moved body, intra-package back-edge rewriting, source-span
  docstring/header fidelity, API/behaviour preservation, and import-time
  refusal) plus the shadowing and split-binding rules.
- Emission rule 1 says the module docstring belongs in `__init__.py` only, but
  the later `--extract` prose calls the copied submodule header “docstring +
  imports,” and the retained `apply_split` reference code includes the
  docstring in its `header`. The implementation follows the explicit emission
  rule: the docstring is copied verbatim from its AST source span into
  `__init__.py`; extracted submodules receive the remainder of the header
  through the last top-level import. This preserves raw prefixes and embedded
  triple quotes while avoiding duplicate module docstrings.
- The bad-map expectation names the three upstream-owned offenders but does not
  mention the three fork-owned offenders described above. All six are genuine
  for that deliberately partial map, and all are printed before any write.

The optional read-only `claude-review` cross-family review was attempted but
could not connect (`API Error: Unable to connect to API (ENOTIMP)`), so no
second-opinion verdict was available. The required gates above are unaffected.

---

# Reviewer verification — Claude Code work:6

Everything above is the builder's own account. This section is written by the
reviewing session; **every gate was re-run independently** and no number was
copied from the builder's log. Reviewer family != builder family (Codex built).

All three gates reproduce exactly: 23 passed / exit 0, ruff `All checks
passed!` / exit 0, `run-affected.sh` 3 files 23 tests / exit 0. All live
numbers reproduce: 38843 / 973 / 37, import-time 58 / 0, runtime 878 / 140,
ownership 733/23745 · 111/1293 · 129/11419, `API IDENTICAL — 992 symbols
match`, `stat -c%s` = 1,589,570. The refusal dry run reproduces verbatim,
exit 2, nothing written, target file untouched.

## Control probes — proving the green and empty results could have been red

A refusal that writes nothing is indistinguishable from a tool that never
writes. Each negative result below has a matching positive control.

**1. The writer does write — positive control on real data.** A copy of
`hermes_cli/kanban_db.py` with a *valid* map extracting 5 fork-only functions:

- exit 0; origin stayed a **file** (1,585,489 B); package emitted with
  `__init__.py` + `probe.py`;
- diff origin-before → origin-after: **5 `delete` opcodes and 1 `insert` (the
  re-export block), zero `replace` opcodes** — every surviving line
  byte-identical;
- all 5 moved bodies present **byte-identical** in the submodule;
- top-level symbol set across origin + package unchanged: 0 lost, 0 gained;
- all three emitted files parse; `ruff check` on the emitted output exits 0.

This is the pure-move property Task 7 rests on, demonstrated on the real file.

**2. `api_snapshot --compare` can fail.** Comparing against itself is a
tautology. Removing one symbol from the baseline yielded
`API DIFF — 1 difference(s)`, exit 1. The `API IDENTICAL` above is a real
comparison.

**3. Mutation testing of the four design-carrying rules.** Each mutant was
caught by exactly the test that should catch it, and by no other:

| mutant | caught by | suite |
|---|---|---|
| shadowing guard → `shadowed = set()` (rule 8) | `test_local_binding_shadowing_a_backward_target_is_not_rewritten` | 1 failed, 22 passed |
| import-time-into-extracted refusal disabled (rule 6) | `test_extract_refuses_import_time_reference_into_the_extracted_set` | 1 failed, 22 passed |
| split-binding refusal disabled (rule 9) | `test_extract_refuses_split_binding_across_modules` | 1 failed, 22 passed |
| docstring source span dropped (rule 6 docstring) | `test_extract_copies_docstring_source_span_and_header` | 1 failed, 22 passed |

`split_module.py` was restored byte-identical afterwards (md5
`85523fe3eec3458023c33a2fa3a46b6f`) and the committed blob `3dc1d8c61` was
diffed against the reviewer's pre-mutation copy: identical. No mutant reached
the commit. (Hazard noted: the builder committed *during* this mutation run —
in-place mutation testing in a worktree with a live concurrent writer is
unsafe and should use a scratch copy next time.)

**4. Structural preconditions on the real file — each with its own control.**
The mover has three silent-failure modes that no test covers because they
cannot occur in this file. Verified that they indeed cannot:

| check | result | control that proves the check works |
|---|---|---|
| symbols defined before the last top-level import — would be duplicated into every submodule *and* moved | **none** (header boundary = line 102, `from hermes_cli import kanban_worker_runtime as _worker_runtime`) | boundary measured, not assumed |
| bare top-level statements (`if`/`try`/`for`/expression) referencing a moved symbol at import time — **undetected** by the refusal rule, which only inspects top-level *symbols* | **none** — body is exclusively docstring + 30 imports + 269 assigns + 671 defs + 18 classes + 15 annassigns = 1004 nodes | same census on a synthetic file correctly reports `If` / `Expr` |
| imports nested in a top-level `try`/`if` — would not be copied into submodule headers | **none** | — |
| multi-name top-level bindings (`A = B = …`, tuple assignment) | **none** | detector returns `[['A','B'],['C','D']]` on a synthetic file |

**5. Untested code path exercised by hand.** The committed rule-9 test covers
only the case where *both* names move to different submodules. The
Task 7-relevant variant — one name moves, one stays — has no test but behaves
correctly:

```
refused, exit: REFUSING: ['A', 'B'] are bound by one statement but extraction splits them across ['__origin__', 'one']
nothing written: True
```

Moot for `kanban_db.py` (zero multi-name bindings), so no test was added.

## On the six refusal referrers

The builder's reading is correct and is confirmed by ownership data. The three
referrers the plan names are all `upstream-diverged`; the three extra ones are
all `FORK-ONLY`:

```
_dispatch_once_locked          upstream-diverged
dispatch_once                  upstream-diverged
schedule_task                  upstream-diverged
auto_retry_blocked_tasks       FORK-ONLY
escalate_silent_blocks_sweep   FORK-ONLY
silent_block_task_ids          FORK-ONLY
```

The fork-only three appear only because this deliberately minimal map leaves
them behind; Task 5's real map moves them into `kanban_ext`, at which point
they stop being "stays" and drop out of the refusal. **The plan's §6 carve-out
of exactly 2 constants is unchanged and needs no correction.**

## Review findings — none blocking

1. **`_import_name_for_path` is `os.getcwd()`-relative.** The origin module
   name baked into every emitted `from <origin> import …` depends on the
   working directory. Task 7 **must** run from the repo root, or submodules
   import a bare basename instead of `hermes_cli.kanban_db`. Not a defect — it
   produced the correct name from the root — but it belongs in the Task 7
   runbook.
2. **`apply_split` remains a `NotImplementedError` stub.** Brief-permitted.
   Delete it in Task 9 rather than carrying a dead entry point.
3. **The circular-import direction is load-bearing.** `kanban_ext` reaches
   back with `from hermes_cli.kanban_db import …`, which works only because
   `kanban_db` is imported first and is fully populated by the time its
   trailing block runs. Any module importing `hermes_cli.kanban_ext`
   **directly**, without `kanban_db` already in `sys.modules`, gets a
   partially-initialised package. Nothing should — the package is
   fork-internal — but this belongs in Task 9's standing rule.

## Review lens

Escalation-ladder level 1 in its strictest form: main-model fresh eyes over the
full implementation, plus live evidence and real-data ground-truth probes run
by the reviewer. Justified because the slice is **additive tooling with no
production-code intervention** — `hermes_cli/kanban_db.py` is untouched, and
anti-scope held (no giant module modified, `--extract` never applied for real).

The blast radius is deferred, not absent: this tool will mechanically rewrite a
1.5 MB upstream-owned file in Task 7. **The second-family review belongs there,
on the Task 7 output diff, rather than here on the tooling.** Codex's own
attempt at a cross-family review failed with `ENOTIMP` and produced no verdict;
that is recorded as a gap, not as a pass.

**Verdict: PASS.** Slice B is fit to merge to `main`.
