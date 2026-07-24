# Slice A tooling report

Status: **complete**. Tasks 1–3 are implemented and committed on
`refactor/split-tooling`. All requested gates pass against the corrected
measurements for base `e86c8a66b`. No Slice B behavior was implemented.

## Branch and commits

- Base: `e86c8a66b11dfe1532908e9d7f7bea802ab857bc`
- Task 1: `1fa990bae` — `refactor tooling: api_snapshot equivalence gate`
- Task 2: `25e5de727` — `refactor tooling: symbol layering analysis`
- Task 3: `64e2cb1a0` — `refactor tooling: split_module --analyze and --ownership`
- Annotation correction: `6390c9ccc` — `codex: make layering annotation-aware`
- Initial report: `86094d994` — `codex: document Slice A tooling gates`
- Corrected report: committed separately after this report was finalized

The branch is intentionally behind current `main`; it was not rebased because
the brief pins `e86c8a66b` as the implementation base.

## Files created

| File | Lines | Commit |
|---|---:|---|
| `scripts/refactor/__init__.py` | 1 | Task 1 |
| `scripts/refactor/api_snapshot.py` | 116 | Task 1 |
| `tests/refactor/test_api_snapshot.py` | 87 | Task 1 |
| `scripts/refactor/layering.py` | 175 | Task 2 + annotation correction |
| `tests/refactor/test_layering.py` | 209 | Task 2 + annotation correction |
| `scripts/refactor/split_module.py` | 243 | Task 3 |
| `tests/refactor/test_split_module.py` | 83 | Task 3 |
| `docs/refactor/reports/2026-07-24-slice-a-tooling.md` | 198 | report commits |

Both `apply_split` and `extract_to_package` remain `NotImplementedError` stubs.

## Real-data commands

Commands ran from the isolated worktree on the requested base. The live
checkout's `venv` was activated because worktrees do not carry a virtualenv.
The command output below is verbatim.

### `/home/piet/.hermes/hermes-agent/venv/bin/python -m scripts.refactor.split_module hermes_cli/kanban_db.py --analyze`

```text
hermes_cli/kanban_db.py: 38843 lines, 973 symbols, 37 modules
  import-time cross-module refs: forward=58 backward=0
  runtime cross-module refs:     forward=878 backward=140 (these get the module-object form)
```

Exit code: 0.

### `/home/piet/.hermes/hermes-agent/venv/bin/python -m scripts.refactor.split_module hermes_cli/kanban_db.py --ownership`

```text
hermes_cli/kanban_db.py against origin/main:
  fork-only:           733 symbols, 23745 lines
  upstream-identical:  111 symbols, 1293 lines
  upstream-diverged:   129 symbols, 11419 lines
```

Exit code: 0.

### `/home/piet/.hermes/hermes-agent/venv/bin/python -m scripts.refactor.api_snapshot hermes_cli.kanban_db --out /tmp/kdb_api.json`

```text
wrote 992 symbols to /tmp/kdb_api.json
```

Exit code: 0.

### `/home/piet/.hermes/hermes-agent/venv/bin/python -m scripts.refactor.api_snapshot hermes_cli.kanban_db --compare /tmp/kdb_api.json`

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
| `/home/piet/.hermes/hermes-agent/venv/bin/ruff check scripts/refactor tests/refactor` | 0 | `All checks passed!` |
| `PYTHONPATH=$(pwd) /home/piet/.hermes/hermes-agent/venv/bin/python -m pytest tests/refactor/ -v` | 0 | 15 passed |
| `scripts/run-affected.sh` | 0 | 3 files, 15 tests passed |

`scripts/run-affected.sh` emitted the expected advisory that this pinned branch
is four commits behind `main`; it selected only the three `tests/refactor/`
files and completed with 15/15 passing. No frontend or generated served assets
were in scope.

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
5. **Annotation evaluation must be module-aware.** Function/method argument and
   return annotations, plus `AnnAssign` annotations, are now import-time only
   when the module does not enable postponed annotations. Class keywords such
   as `metaclass=...` are always import-time. `classify_references` receives the
   module AST so this decision is made once from the actual source.

## Future annotations and banner versus ownership cuts

`hermes_cli/kanban_db.py` has a 69-line module docstring followed by
`from __future__ import annotations` at line 71. Runtime inspection confirms
that `schedule_task.__annotations__["due_at"]` is the string
`"int | None | _ScheduleDueUnspecified"`.

The regression test therefore pins both sides:

- `_SCHEDULE_DUE_UNSPECIFIED`, used as a default value, is import-time;
- `_ScheduleDueUnspecified`, used only in the postponed annotation, is not.

This distinction applies independently of the boundary scheme. `--analyze`
currently groups symbols by banner sections; Task 7's eventual boundary map
groups by fork/upstream ownership. The same module-level future flag is threaded
into either classification. The banner graph remains import-time 58/0. The
ownership cut remains FORK→UPSTREAM 10 and UPSTREAM→FORK 3; its carve-out stays
the original two default-value symbols. `_ScheduleDueUnspecified` is not a
carve-out.

Postponed annotation names remain conservatively visible to the existing
runtime bucket through `all_names`; that preserves the measured 878/140 totals.
They are not claimed to be runtime evaluation edges.

## Remaining import-execution audit

The requested constructs were checked as follows:

- **Walrus in a module-level comprehension.** `add()` walks the complete
  assignment value, so loads inside eager list/set/dict comprehensions are
  seen, including a walrus value. A generator expression is deferred and is
  currently over-classified. A standalone top-level comprehension is not owned
  by `top_level_symbols` and remains unsupported. The target contains no
  comprehension with a walrus.
- **`__init_subclass__`.** Python calls the immediate parent's hook during
  class creation. The analyzer records the base expression but does not
  propagate globals read inside the hook; that interprocedural behavior cannot
  be ruled out generally. The target defines no `__init_subclass__`, has no
  internal top-level base edges, and its bases are built-in exception classes.
- **Decorators on nested classes.** A class nested directly in a class-body
  statement is covered conservatively by that statement's AST walk; a class
  nested inside a function is correctly deferred with the function body.
  Calls and transitive globals inside decorator implementations are not
  modeled. The target has no decorated nested classes and no internal
  decorator edge.
- **Legacy `TypeVar` bounds.** `TypeVar(..., bound=Foo)` evaluates `Foo` as an
  ordinary call argument and is covered when the call occurs in an analyzed
  assignment/class-body statement. The target has no `TypeVar` calls.
- **PEP 695 type parameters.** Bounds, constraints, and type-alias values are
  lazy, while other generic-definition expressions have more nuanced eager
  behavior. This worktree runs Python 3.11, where PEP 695 syntax fails parsing,
  and the target has none. Behavior when the tool itself runs on Python 3.12+
  is not validated; explicit AST-version support or a fail-closed check is
  required before claiming general support.
- **`dataclasses.field(default_factory=...)`.** The factory object expression is
  loaded when `field()` runs in the class body and is covered by the class-body
  AST walk; the factory is invoked later during instance initialization. The
  target has 21 occurrences, all `default_factory=list`, so there is no
  internal symbol edge. A lambda factory body is deferred but would currently
  be over-classified.

Other unresolved interprocedural class-creation effects include metaclass
`__prepare__`/`__new__`, descriptor `__set_name__`, decorator bodies, and helper
functions called from assignments/defaults/class bodies. The analyzer sees the
direct callable expression but does not promote globals used inside the called
body to import-time. It also does not own arbitrary top-level compound
statements, tuple-unpack assignments, or augmented assignments.

For this target, live AST checks found none of the unsupported shapes above,
no custom class-creation hooks, no class keywords, no PEP 695 nodes, and no
internal base/decorator edges. The graph is credible for `kanban_db.py`.
Before Slice B treats the analyzer as a general mover, unsupported syntax and
interprocedural hooks should fail closed rather than silently pass.

Language-semantics references used for this audit:

- [Python data model — customizing class creation](https://docs.python.org/3/reference/datamodel.html#customizing-class-creation)
- [Python dataclasses — default factories](https://docs.python.org/3/library/dataclasses.html#default-factory-functions)
- [PEP 695 — lazy evaluation](https://peps.python.org/pep-0695/#lazy-evaluation)
