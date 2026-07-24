# Test patch sites against `hermes_cli.kanban_db`

Plan Task 6. **Rewritten 2026-07-25 after the first version was proven wrong by execution.**

## The first answer was wrong: it said zero, the real number is 470

The original version of this file concluded *"no site needs re-pointing — all 9 patched
symbols are upstream-owned and stay."* Task 7 then ran the extraction and **64 tests failed,
reproduced identically on a rerun** (890 passed / 64 failed, twice), against a `main` baseline
of 116 passed on the same files.

**Why the first answer was wrong.** It used the plan's two greps, which match the literal
token `kanban_db`:

```bash
rg -n '(monkeypatch\.setattr|mock\.patch|patch)\(\s*"hermes_cli\.kanban_db\.' …
rg -n 'setattr\(\s*kanban_db\s*,\s*"' …
```

But the test suite almost never uses that name. It aliases the module:

| alias | occurrences |
|---|---:|
| `kb` | 352 |
| `_kb` | 78 |
| `kdb` | 3 |
| `kanban_db_module` | 1 |
| `kanban_db` | 1 |

Every `monkeypatch.setattr(kb, "…")` was invisible to the greps.

**A control probe was run and still missed it.** The probe proved the *classifier* was live —
fed symbols that move, it reported them. It did not test whether the *input list* was
complete. Proving the lookup works says nothing about the sweep that produced its input. That
is the lesson: control-probe the collection step, not only the decision step.

## Correct method: resolve aliases with the AST, never grep

```python
# per test file: find local names bound to hermes_cli.kanban_db, then find every
# setattr / patch / patch.object against those names, plus string targets.
```

Full script: this task's commit. Grep cannot do this — the alias is established by an
`import … as …` elsewhere in the file.

## Measured result on `59191ccec`

| | count |
|---|---:|
| patch sites | **470** |
| distinct symbols patched | **104** |
| of those, symbols the boundary map **moves** | **58** |
| symbols that stay | 46 |

## Why carving the moved symbols out does not fix it

The obvious repair — add the 58 patched-and-moved symbols to the carve-out so they stay in
`kanban_db.py` — was built and measured. Carve-out grows to **67** after transitive closure
(4,199 lines kept behind), `kanban_db.py` lands at 782,209 B, `API IDENTICAL`, imports fine.

**It does not fix the tests.** A probe over the three worst files still failed 13 tests.

The reason is a namespace fact, not a mapping fact: `kanban_ext/impl.py` reaches back with

```python
from hermes_cli.kanban_db import connect, _append_event, …
```

which **binds at import time**. `monkeypatch.setattr(kb, "connect", fake)` rebinds the
attribute on `kanban_db`; `impl`'s own global still points at the original function. So any
call made *from extracted code* ignores the patch — no matter which side of the boundary the
symbol lives on.

The 470 patch sites depend on `kanban_db` being **one namespace**. Splitting it changes
monkeypatch semantics by construction. No boundary map avoids this.

## The options that remain

1. **Re-point the patch sites.** Patch where the callee looks the symbol up:
   `hermes_cli.kanban_ext.impl.X` for extracted code. ~470 sites across ~66 files, plus an
   import per file. Mechanical but large, and a mis-aimed patch fails **silently** — the exact
   `task_age` hazard this task exists to prevent, now at scale. Needs a verification pass that
   each re-pointed patch still actually intercepts.
2. **Emit cross-boundary references as module-attribute access** (`kanban_db.X` at call time)
   instead of `from … import X`. Restores patch visibility for stayed symbols without touching
   tests, but requires body rewrites — so Task 7 stops being a pure move, and the byte-identity
   equivalence proof is lost.
3. **Do not extract; go straight to Task 8 (hooks).** The hooks are what move the merge
   metric; the extraction moves file size and CodeGraph visibility. This decouples the two.

## Standing rule

Re-run the AST sweep, not a grep, whenever the boundary map changes. Aliased monkeypatching is
the dominant shape in this suite.
