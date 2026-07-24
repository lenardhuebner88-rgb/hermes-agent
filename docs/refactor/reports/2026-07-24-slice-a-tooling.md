# Slice A tooling report

Status: **complete**. Tasks 1–3 are implemented and committed on
`refactor/split-tooling`. All requested gates pass against the corrected
measurements for base `e86c8a66b`. No Slice B behavior was implemented.

## Branch and commits

- Base: `e86c8a66b11dfe1532908e9d7f7bea802ab857bc`
- Task 1: `1fa990bae` — `refactor tooling: api_snapshot equivalence gate`
- Task 2: `25e5de727` — `refactor tooling: symbol layering analysis`
- Task 3: `64e2cb1a0` — `refactor tooling: split_module --analyze and --ownership`
- Report: committed separately after this report was finalized

The branch is intentionally one docs-only commit behind current `main`; it was
not rebased because the brief pins `e86c8a66b` as the implementation base.

## Files created

| File | Lines | Commit |
|---|---:|---|
| `scripts/refactor/__init__.py` | 1 | Task 1 |
| `scripts/refactor/api_snapshot.py` | 116 | Task 1 |
| `tests/refactor/test_api_snapshot.py` | 87 | Task 1 |
| `scripts/refactor/layering.py` | 132 | Task 2 |
| `tests/refactor/test_layering.py` | 106 | Task 2 |
| `scripts/refactor/split_module.py` | 243 | Task 3 |
| `tests/refactor/test_split_module.py` | 83 | Task 3 |
| `docs/refactor/reports/2026-07-24-slice-a-tooling.md` | 148 | report commit |

Both `apply_split` and `extract_to_package` remain `NotImplementedError` stubs.

## Real-data commands

Commands ran from the isolated worktree on the requested base. The live
checkout's `venv` was activated because worktrees do not carry a virtualenv.
The command output below is verbatim.

### `python -m scripts.refactor.split_module hermes_cli/kanban_db.py --analyze`

```text
hermes_cli/kanban_db.py: 38843 lines, 973 symbols, 37 modules
  import-time cross-module refs: forward=58 backward=0
  runtime cross-module refs:     forward=878 backward=140 (these get the module-object form)
```

Exit code: 0.

### `python -m scripts.refactor.split_module hermes_cli/kanban_db.py --ownership`

```text
hermes_cli/kanban_db.py against origin/main:
  fork-only:           733 symbols, 23745 lines
  upstream-identical:  111 symbols, 1293 lines
  upstream-diverged:   129 symbols, 11419 lines
```

Exit code: 0.

### `python -m scripts.refactor.api_snapshot hermes_cli.kanban_db --out /tmp/kdb_api.json`

```text
wrote 992 symbols to /tmp/kdb_api.json
```

Exit code: 0.

### `python -m scripts.refactor.api_snapshot hermes_cli.kanban_db --compare /tmp/kdb_api.json`

```text
API IDENTICAL — 992 symbols match
```

Exit code: 0.

The corrected real-data gate passes exactly: 38,843 file lines, 973 symbols,
37 banner-derived modules, import-time 58/0, runtime 878/140, and ownership
buckets 733/111/129 with line totals 23,745/1,293/11,419.

## Gate exit codes

| Command | Exit | Result |
|---|---:|---|
| `ruff check scripts/refactor tests/refactor` | 0 | `All checks passed!` |
| `python -m pytest tests/refactor/ -v` | 0 | 12 passed |
| `scripts/run-affected.sh` | 0 | 3 files, 12 tests passed |

`scripts/run-affected.sh` emitted the expected advisory that this pinned branch
is one commit behind `main`; it selected only the three `tests/refactor/` files
and completed with 12/12 passing. No frontend or generated served assets were
in scope.

## Plan issues and implementation notes

1. **Task 3 references a Task 4 helper.** The Task 3 ownership block calls
   `_symbol_span`, but the helper's definition appears only in Task 4. The exact
   helper body was brought forward; no mover/extractor implementation was.
2. **The supplied file I/O fails this repository's Ruff gate.** Five text-mode
   `open()` calls omitted an explicit encoding. They now use UTF-8.
3. **Raw signatures are not stable across processes.** The supplied
   `api_snapshot` code serialized `inspect.signature()` verbatim. Real defaults
   in `kanban_db.py` include object and function reprs containing process-local
   hexadecimal addresses, so a fresh compare initially produced three false
   differences (`field`, `schedule_task`, `urlencode`). `_signature` now
   canonicalizes only the ` at 0x...` address fragment. The real two-process
   command sequence then reported `API IDENTICAL`.
4. **The original line totals were stale for the pinned base.** Read-only
   attribution found 10 additions and 1 deletion between measured commit
   `1ef243502` and requested base `e86c8a66b`, all inside fork-only
   `scores_digest`. Commit `aa36b6869`, landed through `8f29783e0`, accounts
   exactly for the corrected +9 file and fork-only line totals.

## Import-time classification judgment

The current `import_time_names` set is **incomplete as a general Python
classifier**, although I found no evidence that it misses an import-time edge
in this specific `kanban_db.py`.

Expression positions evaluated during module execution but not modeled include:

- function and method parameter/return annotations when annotations are not
  postponed;
- module/class annotated-assignment annotation expressions under the same
  condition;
- class keyword expressions such as `metaclass=...` and `**keywords`;
- Python 3.12+ type-parameter bounds/defaults;
- unsupported top-level defining/executable shapes such as tuple-unpack
  assignments, augmented assignments, and compound statements.

There is also conservative over-classification: walking an assigned lambda or
generator expression, or a nested scope inside a non-method class-body
statement, can mark deferred names as import-time. That creates false
constraints or false fatal reports, not a silently broken extraction.

Live AST checks on the target found:

- `from __future__ import annotations` is active;
- zero top-level classes with keyword/metaclass expressions;
- zero top-level lambda or generator-expression assignments;
- zero non-simple assignment targets;
- zero unsupported top-level executable/compound statements.

Therefore the omitted annotation positions are postponed in this module and
the other identified gaps do not occur. The measured import-time graph is
credible for this target. Before Slice B promotes this into a reusable mover,
the safe choice is to either implement these positions with awareness of
postponed annotations or make analysis fail closed when unsupported constructs
are present.
