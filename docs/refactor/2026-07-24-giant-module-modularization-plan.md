# Giant-Module Modularization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert the five giant modules (112,912 lines) into packages so every source file is under 1 MiB and CodeGraph-visible, with the public API of each module provably byte-identical before and after and zero production call-site edits.

**Architecture:** Each giant module becomes a package whose `__init__.py` explicitly re-exports every top-level symbol, so the 275 importing files never change. A deterministic AST-based tool (`split_module.py`) performs the move; a second tool (`api_snapshot.py`) proves the public surface is unchanged. Submodules are emitted in the source file's own top-level order, which measurement shows is already a valid import-time layering for all five files — cross-module references to *earlier* submodules become plain symbol imports (line stays byte-identical), references to *later* submodules become module-object imports (`from . import x` + `x.name(...)`), which makes the import graph acyclic by construction.

**Tech Stack:** Python 3 `ast` + `tokenize` (stdlib only, no new dependencies), PyYAML (already vendored in the repo's env) for boundary maps, pytest, `scripts/run-affected.sh`, `codegraph`.

## Global Constraints

- **Pure-move discipline.** Split commits contain moves only. No renames, no reformatting, no bug fixes, no docstring corrections — explicitly including the five defects in the spec's Follow-ups section. Mixing fixes in invalidates the equivalence gate.
- **Zero production call-site edits.** No file outside the module being split may change its imports or calls. (Test-internal `monkeypatch` string targets are the single, enumerated exception — see Task 7 Step 6.)
- **No model retypes code.** Every moved line is moved by `split_module.py`. Models author boundary maps (YAML) and tool code only.
- **Per-file isolation.** One branch and one commit per giant module, so a bad split of one file never forces unwinding another.
- **Model routing (fixed by the approved spec):** `split_module.py` / `api_snapshot.py` + their tests → **Codex (gpt-5.6-sol)**. The five boundary maps → **qwen 3.8 via `claude-qwen -p` one-shot only**. Boundary-map approval, gate verification, merge judgment → **Claude Opus 5**.
- **ToS constraint (binding):** `/usr/local/bin/claude-qwen` line 12 — Token Plan is interactive coding/agent use only. qwen may be used for session-driven one-shots supervised by an interactive session. It must **never** be wired as a kanban lane or cron worker.
- **Repo git rules:** `origin` is NousResearch upstream — never push there. Push only to `piet-fork`, fast-forward, never `--force`. `git status --short` before any git action; this checkout is edited by parallel sessions.
- **Test scope:** `scripts/run-affected.sh` while building. Before any merge to main: one collection sweep (`pytest --co -q tests/`) plus affected tests. Never run the full suite in both worker and verifier.
- **Base commit:** `1ef243502` on `main`.

---

## Measured ground truth (do not re-derive)

Measured on `1ef243502` with `scripts/refactor/split_module.py --analyze` (prototype output archived in the Task 3 acceptance criteria):

| file | lines | top-level symbols | banner sections | import-time back-edges | runtime back-edges | sections >4000 lines |
|---|---:|---:|---:|---:|---:|---:|
| `hermes_cli/kanban_db.py` | 38,834 | 973 | 37 | **0** | 140 | 0 |
| `gateway/run.py` | 21,875 | 134 | 2 | **0** | 1 | 1 (20,122) |
| `hermes_cli/web_server.py` | 20,314 | 836 | 39 | **0** | 66 | 0 |
| `cli.py` | 16,797 | 153 | 1 | **0** | 1 | 1 (13,874) |
| `hermes_cli/main.py` | 15,092 | 260 | 3 | **0** | 1 | 2 (9,734 / 4,274) |

Three consequences that shape this plan:

1. **Zero import-time back-edges in all five files.** The source order is already a valid layering, so the spec's `_core.py` cycle fallback is never triggered. The splitter still implements the refusal path as a safety net, but it must not fire.
2. **`kanban_db.py` (37 banners) and `web_server.py` (39 banners) have real banner structure**; their boundary maps are banner-derived. **`gateway/run.py`, `cli.py`, and `main.py` do not** (2, 1 and 3 banners, with a single section holding 92%, 83% and 64% of the file). Their maps must come from call-clustering, and need proportionally more scrutiny at the approval gate.
3. `hermes_cli/kanban_db.py`'s top level is unusually clean — 1,004 nodes: 1 docstring, 30 imports, 284 assignments, 671 functions, 18 classes, and **zero** conditional/executable top-level statements. AST-based moving is safe here.

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

**Created (per split, ×5):**

- `docs/refactor/boundary-map.<module>.yaml` — the approved boundary map, kept in the repo as the record of the split
- `hermes_cli/kanban_db/` (etc.) — the package replacing the module

**Modified:**

- `docs/kanban/LIFECYCLE.md` — 95 anchors re-pointed at new module paths (Task 7)
- `scripts/check_kanban_lifecycle_anchors.py` — must resolve anchors across the package (Task 7)
- ~33 test lines with `monkeypatch`/`patch` string targets into `hermes_cli.kanban_db.<symbol>` (Task 7)

---

## Task 0: Branch triage (operator gate — blocks every split)

Eleven branches carry `kanban_db.py` deltas. After the split their diffs cannot auto-merge, because the file no longer exists at that path.

**Files:** no source changes. Produces `docs/refactor/branch-triage-2026-07-24.md`.

**Verified state on `1ef243502`:**

| branch | commits ahead | last commit | `kanban_db.py` delta | bucket |
|---|---:|---|---|---|
| `kanban/t_c254b029` | 5 | 2026-07-22 | 626+/284− | decide |
| `codex/board-model-truth-20260713` | 1 | 2026-07-14 | 713+/47− | decide |
| `kanban/t_610a9f84` | 11 | 2026-07-14 | 187+/34− | decide |
| `backup/grok-kanban-block-kind-20260715-pre-rebase` | 4 | 2026-07-15 | 627+/113− | archive+delete |
| `kanban/t_80809063` | 1 | 2026-07-12 | 40+/11− | archive+delete |
| `kanban/t_d2d25240` | 1 | 2026-07-18 | 30+/0− | archive+delete |
| `kanban/t_49c1e99b` | 1 | 2026-07-17 | 19+/1− | archive+delete |
| `worktree-bridge-cse_01HZiECqoEjuEdJuA5DWYFys` | 1 | 2026-07-17 | 19+/2− | archive+delete |
| `kanban/t_57aaa085` | 10 | **2026-07-24** | 10+/1− | archive+delete — **flag to operator** |
| `salvage/dirty-main-20260712T014834` | 2 | 2026-07-12 | 9+/7− | archive+delete |
| `kanban/t_69536fff` | 1 | 2026-07-12 | 7+/16− | archive+delete |

**Discrepancy to raise with the operator:** the spec buckets `kanban/t_57aaa085` as "trivial or ≥1 week stale", but it is dated **today** with 10 commits ahead. Its `kanban_db.py` delta is trivial (10+/1−), so the bucketing is defensible, but the branch may hold live non-`kanban_db` work. Do not delete it without an explicit call.

`backup/grok-kanban-block-kind-20260715-pre-rebase` carries the second-largest delta in the whole set (627+/113−) yet sits in the archive bucket. That is consistent with its name (a pre-rebase backup, i.e. superseded), but confirm before deleting.

- [ ] **Step 1: Write the triage summary for the three "decide" branches**

For each of `kanban/t_c254b029`, `codex/board-model-truth-20260713`, `kanban/t_610a9f84`, capture what the branch actually does:

```bash
cd /home/piet/.hermes/hermes-agent
for b in kanban/t_c254b029 codex/board-model-truth-20260713 kanban/t_610a9f84; do
  echo "##### $b"
  git log --oneline main..$b
  git diff --stat main...$b
  git diff main...$b -- hermes_cli/kanban_db.py | head -200
done
```

Write `docs/refactor/branch-triage-2026-07-24.md` containing, per branch: the commit subjects, the full `--stat`, a two-to-four sentence description of the change in behavioural terms, whether it is already superseded by something on `main`, and a land-or-drop recommendation with reasoning.

- [ ] **Step 2: Get the operator's land-or-drop call**

Present the summary. Do not proceed past this step without an explicit decision per branch. Record the decision inline in the triage document.

- [ ] **Step 3: Execute "land" decisions (only those the operator approved)**

For each branch marked *land*, merge it to `main` **before** any split, resolve conflicts normally, run gates, and commit. This is ordinary merge work and is deliberately not scripted here — the point is that it happens while `kanban_db.py` still exists at its original path.

```bash
git checkout main
git merge --no-ff <branch>
scripts/run-affected.sh
```

- [ ] **Step 4: Archive-then-delete the eight (or nine) drop branches**

Tag first so nothing is unrecoverable, then delete the branch ref:

```bash
cd /home/piet/.hermes/hermes-agent
for b in kanban/t_49c1e99b kanban/t_57aaa085 kanban/t_69536fff kanban/t_80809063 \
         kanban/t_d2d25240 salvage/dirty-main-20260712T014834 \
         backup/grok-kanban-block-kind-20260715-pre-rebase \
         worktree-bridge-cse_01HZiECqoEjuEdJuA5DWYFys; do
  git tag "archive/pre-modularization/$(echo $b | tr '/' '_')" "$b"
  git branch -D "$b"
done
git tag -l 'archive/pre-modularization/*'
```

Note: `kanban/t_57aaa085` is in this list only if the operator confirmed it in Step 2. Remove it from the loop otherwise.

- [ ] **Step 5: Verify no branch was lost and commit the triage record**

```bash
git tag -l 'archive/pre-modularization/*' | wc -l   # expect 8 (or 7)
git branch --list 'kanban/t_49c1e99b'               # expect empty
git add docs/refactor/branch-triage-2026-07-24.md
git commit -m "docs: branch triage before giant-module modularization"
```

---

## Task 1: `api_snapshot.py` — the equivalence gate

**Files:**
- Create: `scripts/refactor/__init__.py`, `scripts/refactor/api_snapshot.py`
- Test: `tests/refactor/test_api_snapshot.py`

**Interfaces:**
- Produces: `snapshot(module_name: str) -> dict` returning `{"module": str, "symbols": {name: descriptor}}` where `descriptor` is a dict with keys `kind` (`"function"` | `"class"` | `"value"`), `signature` (str or `None`), and for classes `methods` (dict of method name → signature). `diff(before: dict, after: dict) -> list[str]` returns human-readable difference lines; empty list means identical.
- Consumed by: Task 4 (`split_module.py` calls neither — the gate is run separately), Tasks 7–11 (the per-file sequence).

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

**Acceptance criterion (this is the reproduction gate for the ground-truth table):** running `--analyze` with no map on the five giant modules must reproduce the measured table above exactly — in particular `import_time_backward` must be empty for all five, and `runtime_backward` must be 140 / 1 / 66 / 1 / 1.

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

- [ ] **Step 5: Reproduce the ground-truth table on the real files**

```bash
cd /home/piet/.hermes/hermes-agent
for f in hermes_cli/kanban_db.py gateway/run.py hermes_cli/web_server.py cli.py hermes_cli/main.py; do
  python -m scripts.refactor.split_module "$f" --analyze
done
```
Expected: `backward=0` on the import-time line for all five; runtime backward counts 140, 1, 66, 1, 1 respectively. **If any file reports a non-zero import-time backward count, stop and escalate** — the plan's central assumption has changed.

- [ ] **Step 6: Commit**

```bash
git add scripts/refactor/split_module.py tests/refactor/test_split_module.py
git commit -m "refactor tooling: split_module --analyze"
```

---

## Task 4: `split_module.py --apply` — the mover

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

- [ ] **Step 6: Run ruff and the affected suite**

```bash
ruff check scripts/refactor tests/refactor
scripts/run-affected.sh
```
Expected: both clean.

- [ ] **Step 7: Commit**

```bash
git add scripts/refactor/split_module.py tests/refactor/test_split_module.py
git commit -m "refactor tooling: split_module --apply, deterministic AST move"
```

---

## Task 5: `kanban_db.py` boundary map

**Files:**
- Create: `docs/refactor/boundary-map.kanban_db.yaml`

**Interfaces:**
- Produces: the YAML consumed by `split_module.py --map`. Schema:

```yaml
module: hermes_cli/kanban_db.py
modules:
  - name: constants
    banners: ["Constants", "Paths"]
    symbols: [VALID_STATUSES, TERMINAL_TASK_STATUSES, ...]
  - name: schema
    banners: ["Schema"]
    symbols: [...]
```

Order in the list **is** the emission order and must be the source file's banner order. `symbols` must cover every one of the 973 top-level names exactly once — the splitter refuses otherwise.

**Target:** 12–16 modules of 2,000–4,000 lines, merging adjacent banner sections. The 37 sections and their line counts are in the analysis output; the largest single section is 3,729 lines, so no section needs sub-splitting.

- [ ] **Step 1: Produce the section inventory qwen will work from**

```bash
cd /home/piet/.hermes/hermes-agent
python -m scripts.refactor.split_module hermes_cli/kanban_db.py --analyze --json \
  > /tmp/kanban_sections.json
python - <<'EOF' > /tmp/kanban_brief.txt
import json
r = json.load(open('/tmp/kanban_sections.json'))
for m in r['modules']:
    print(f"{r['module_lines'].get(m,0):6d}  {m}")
EOF
cat /tmp/kanban_brief.txt
```

- [ ] **Step 2: Dispatch the boundary-map proposal to qwen (one-shot, interactive-supervised)**

Single `claude-qwen -p` call — never a kanban lane, never a cron worker (ToS).

```bash
claude-qwen -p "$(cat <<'PROMPT'
You are proposing a boundary map for splitting hermes_cli/kanban_db.py (38,834
lines, 973 top-level symbols) into a Python package.

Constraints:
- Group the 37 existing banner sections into 12-16 modules of 2,000-4,000 lines.
- ONLY merge ADJACENT sections. The output order must be the source order —
  it is a valid import-time layering and reordering would break it.
- Give each module a short snake_case name describing its responsibility.
- Do not invent, rename, split or drop any section.

Section line counts, in source order, are in /tmp/kanban_brief.txt.
Read hermes_cli/kanban_db.py section banners for context on what each contains.

Output ONLY YAML in this shape, no prose:

module: hermes_cli/kanban_db.py
modules:
  - name: constants
    banners: ["Constants", "Paths"]
  - name: schema
    banners: ["Schema"]
PROMPT
)" > /tmp/kanban_map_draft.yaml
cat /tmp/kanban_map_draft.yaml
```

- [ ] **Step 3: Review and approve the map (Claude Opus 5 — the human/architectural gate)**

Check, in this order, and reject back to Step 2 on any failure:
1. Every one of the 37 banner titles appears exactly once.
2. Sections are merged only with their neighbours; source order is preserved.
3. Every module's line total is between 2,000 and 4,000 (allow the first and last to be smaller).
4. Module count is 12–16.
5. Names describe responsibility, not position (`review_gate`, not `part_7`).

Then present the map to the operator for approval before it is used. This is the human gate the spec calls for.

- [ ] **Step 4: Expand banner names to explicit symbol lists**

The splitter validates against symbols, not banners, so expand mechanically:

```bash
cd /home/piet/.hermes/hermes-agent
python - <<'EOF'
import ast, yaml
from scripts.refactor import layering
src = open('hermes_cli/kanban_db.py').read()
tree, lines = ast.parse(src), src.splitlines()
banners = layering.banner_sections(lines)
top = layering.top_level_symbols(tree)
def section_of(ln):
    name = '__head__'
    for bl, bt in banners:
        if bl <= ln:
            name = bt
    return name
by_section = {}
for n, node in sorted(top.items(), key=lambda kv: kv[1].lineno):
    by_section.setdefault(section_of(node.lineno), []).append(n)
draft = yaml.safe_load(open('/tmp/kanban_map_draft.yaml'))
for entry in draft['modules']:
    syms = []
    for b in entry['banners']:
        syms.extend(by_section.pop(b))
    entry['symbols'] = syms
leftover = {k: len(v) for k, v in by_section.items()}
assert not leftover, f"sections not placed by the map: {leftover}"
with open('docs/refactor/boundary-map.kanban_db.yaml', 'w') as fh:
    yaml.safe_dump(draft, fh, sort_keys=False, width=100)
print("wrote docs/refactor/boundary-map.kanban_db.yaml")
EOF
```

Note the `__head__` pseudo-section holds symbols defined before the first banner; it must be assigned to the first module in the map. If the assertion fires naming `__head__`, add it to the first entry's `banners` list and re-run.

- [ ] **Step 5: Validate the map against the splitter without applying it**

```bash
python -m scripts.refactor.split_module hermes_cli/kanban_db.py \
  --analyze --map docs/refactor/boundary-map.kanban_db.yaml
```
Expected: `backward=0` on the import-time line, no `OVERSIZED` lines, exit 0. A non-zero exit means the map is not a valid layering — go back to Step 2.

- [ ] **Step 6: Commit the approved map**

```bash
git add docs/refactor/boundary-map.kanban_db.yaml
git commit -m "refactor: approved boundary map for kanban_db"
```

---

## Task 6: Re-target the test patch sites

**Files:**
- Modify: the ~33 lines across `tests/` that patch `hermes_cli.kanban_db.<symbol>` by string path

**Why this is necessary and why it is not a violation of "zero call-site edits":** the spec's guarantee covers production importers — the 275 files that call `kanban_db.foo()`. Those do not change. But `monkeypatch.setattr("hermes_cli.kanban_db.connect", fake)` sets the attribute on the *package*, while a submodule that did `from .connection import connect` holds its own binding and never sees the patch. Most such tests will fail loudly; at least one (`task_age`) could pass silently with the real implementation. Silent is the unacceptable outcome, so these are re-targeted deliberately, in the same commit as the split, and enumerated here.

**Known targets (verified on `1ef243502`):** string-patched — `connect`, `init_db`, `_record_task_failure`, `_record_worker_exit`, `task_age`. Attribute-patched via `setattr(kanban_db, "...")` — in `tests/test_planspec_disposition.py` (2), `tests/hermes_cli/test_operator_inventory.py`, `tests/hermes_cli/test_kanban_cli_dispatch_passthrough.py`, `tests/hermes_cli/test_kanban_workflow_routing.py`.

- [ ] **Step 1: Enumerate every patch site freshly (do not trust this list — re-derive it)**

```bash
cd /home/piet/.hermes/hermes-agent
rg -n '(monkeypatch\.setattr|mock\.patch|patch)\(\s*"hermes_cli\.kanban_db\.' tests/ hermes_cli/ gateway/ scripts/
rg -n 'setattr\(\s*kanban_db\s*,\s*"' tests/ hermes_cli/ gateway/ scripts/
```

- [ ] **Step 2: Add a splitter check that fails on unenumerated targets**

Append to `scripts/refactor/split_module.py` a `--check-patch-targets` mode that greps the repo for string literals of the form `<module.dotted.path>.<symbol>` where `<symbol>` is a top-level name, and prints each with the submodule it will land in. Run it and reconcile against Step 1:

```bash
python -m scripts.refactor.split_module hermes_cli/kanban_db.py \
  --map docs/refactor/boundary-map.kanban_db.yaml --check-patch-targets
```

- [ ] **Step 3: Do not edit yet**

The re-targeting edits land in Task 7 Step 6, after the split, because the new submodule paths do not exist until then. This task's deliverable is the verified list, written to `docs/refactor/patch-targets.kanban_db.md`.

- [ ] **Step 4: Commit the list**

```bash
git add docs/refactor/patch-targets.kanban_db.md
git commit -m "refactor: enumerate kanban_db test patch targets before split"
```

---

## Task 7: Split `kanban_db.py` (the pilot)

**Files:**
- Delete: `hermes_cli/kanban_db.py`
- Create: `hermes_cli/kanban_db/` (12–16 modules + `__init__.py`)
- Modify: `docs/kanban/LIFECYCLE.md`, `scripts/check_kanban_lifecycle_anchors.py`, the patch sites from Task 6

**Prerequisite:** Task 0 is complete and the operator's land-or-drop decisions are executed.

- [ ] **Step 1: Branch and record the "before" snapshot**

```bash
cd /home/piet/.hermes/hermes-agent
git status --short                       # must be clean of foreign work
git checkout -b refactor/split-kanban-db main
python -m scripts.refactor.api_snapshot hermes_cli.kanban_db \
  --out docs/refactor/api-snapshot.kanban_db.json
wc -l docs/refactor/api-snapshot.kanban_db.json
```

- [ ] **Step 2: Apply the split**

```bash
python -m scripts.refactor.split_module hermes_cli/kanban_db.py \
  --apply --map docs/refactor/boundary-map.kanban_db.yaml
ls -la hermes_cli/kanban_db/
wc -l hermes_cli/kanban_db/*.py | sort -n
```
Expected: 13–17 files, none over ~4,000 lines, and the largest well under 1 MiB.

If the splitter prints a `REVIEW REQUIRED` block, resolve it before going further: for each listed pair, read the referring function and confirm the shadowed name really is a local. If any is a genuine module-level reference, the fix is in the boundary map (put referrer and target in the same submodule), never a hand-edit of the emitted package.

- [ ] **Step 3: The equivalence gate — API snapshot diff MUST be empty**

```bash
python -m scripts.refactor.api_snapshot hermes_cli.kanban_db \
  --compare docs/refactor/api-snapshot.kanban_db.json
```
Expected: `API IDENTICAL — <N> symbols match`, exit 0. **Any diff means stop and fix the boundary map — do not hand-edit the emitted package.**

- [ ] **Step 4: Import-time smoke**

```bash
python -c "import hermes_cli.kanban_db as k; print(len(dir(k)), 'attributes')"
python -c "from hermes_cli import kanban_db; print(kanban_db.VALID_STATUSES)"
python -c "from hermes_cli.kanban_db import create_task, claim_task, complete_task; print('symbol-level import OK')"
```
Expected: all three succeed. The third proves the 14 symbol-level importers still work.

- [ ] **Step 5: Run the affected suite**

```bash
scripts/run-affected.sh
ruff check hermes_cli/kanban_db
```
Expected: green. Failures here are almost certainly patch-target failures — go to Step 6, then re-run.

- [ ] **Step 6: Re-target the patch sites from Task 6**

For each site in `docs/refactor/patch-targets.kanban_db.md`, change the string target from `hermes_cli.kanban_db.<symbol>` to `hermes_cli.kanban_db.<submodule>.<symbol>`, and each `setattr(kanban_db, "<symbol>", ...)` to `setattr(kanban_db.<submodule>, "<symbol>", ...)`. Then re-run:

```bash
scripts/run-affected.sh
```
Expected: green.

- [ ] **Step 7: Re-anchor `docs/kanban/LIFECYCLE.md`**

Its 95 anchors point at `../../hermes_cli/kanban_db.py#L<n>`. Re-point each at the new file and line. Generate the mapping mechanically:

```bash
cd /home/piet/.hermes/hermes-agent
python - <<'EOF'
import ast, os, re
new = {}
for fn in sorted(os.listdir('hermes_cli/kanban_db')):
    if not fn.endswith('.py') or fn == '__init__.py':
        continue
    tree = ast.parse(open(f'hermes_cli/kanban_db/{fn}').read())
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            new[n.name] = (fn, n.lineno)
        elif isinstance(n, ast.Assign):
            for tg in n.targets:
                if isinstance(tg, ast.Name):
                    new[tg.id] = (fn, n.lineno)
doc = open('docs/kanban/LIFECYCLE.md').read()
# anchors look like [`symbol`](../../hermes_cli/kanban_db.py#L1234)
def fix(m):
    sym = m.group(1)
    if sym not in new:
        raise SystemExit(f"LIFECYCLE.md anchors unknown symbol {sym!r}")
    fn, ln = new[sym]
    return f"[`{sym}`](../../hermes_cli/kanban_db/{fn}#L{ln})"
out, n = re.subn(r"\[`([A-Za-z_][A-Za-z_0-9]*)`\]\(\.\./\.\./hermes_cli/kanban_db\.py#L\d+\)",
                 fix, doc)
open('docs/kanban/LIFECYCLE.md', 'w').write(out)
print(f"re-anchored {n} symbol links")
EOF
```

Then fix by hand the anchors that are **not** symbol-shaped — the "Section index" table at the bottom of `LIFECYCLE.md` lists banner line numbers, which no longer exist as such. Replace that table with a module index: one row per new submodule, giving its path, line count, and the banner titles it absorbed (that information is in the boundary map). Also update the opening paragraph, which currently says the file is over 1 MiB and CodeGraph does not index it — after this task the opposite is true.

- [ ] **Step 8: Update the anchor checker**

`scripts/check_kanban_lifecycle_anchors.py` verifies anchors against `kanban_db.py`. Point it at the package directory so it resolves `hermes_cli/kanban_db/<file>#L<n>` anchors. Then:

```bash
python scripts/check_kanban_lifecycle_anchors.py
```
Expected: exit 0, all anchors resolve.

- [ ] **Step 9: The success proof — CodeGraph visibility**

```bash
codegraph reindex 2>&1 | tail -5
codegraph query dispatch_once
```
Expected: the real `dispatch_once` in `hermes_cli/kanban_db/<dispatch module>.py` is returned — **not** only `fake_dispatch_once` from test files. This is the measurable success criterion from the spec. Record the output verbatim in the commit message.

- [ ] **Step 10: Pre-merge gates**

```bash
python -m pytest --co -q tests/ 2>&1 | tail -5      # collection sweep
scripts/run-affected.sh
ruff check .
```
Expected: collection reports no errors; affected tests green; ruff clean.

- [ ] **Step 11: Commit as a single pure-move commit**

```bash
git add -A
git commit -m "$(cat <<'MSG'
refactor: split hermes_cli/kanban_db.py into a package (pure move)

38,834 lines -> N modules of 2-4k lines. Public API proven identical by
scripts/refactor/api_snapshot.py; no production call site changed.

Backward runtime references use the module-object import form so the
package import graph is acyclic by construction. Test monkeypatch string
targets re-pointed at the new submodule paths (enumerated in
docs/refactor/patch-targets.kanban_db.md).

CodeGraph now resolves the real definitions:
<paste the `codegraph query dispatch_once` output here>

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
MSG
)"
```

- [ ] **Step 12: Review and merge**

Request an independent review of the diff (review family must differ from the builder family — Codex built the tooling, so this review goes to a Grok or Claude reviewer). The reviewer's job is narrow and mechanical: confirm the commit contains **only** moves, that no line inside a moved body differs except the enumerated module-object rewrites, and that none of the five Follow-up defects were "fixed" in passing.

```bash
git diff main...refactor/split-kanban-db --stat
git checkout main && git merge --ff-only refactor/split-kanban-db
```

---

## Task 8: Split `gateway/run.py`

**Files:**
- Delete: `gateway/run.py`; Create: `gateway/run/`
- Create: `docs/refactor/boundary-map.gateway_run.yaml`

**Key difference from Task 7:** this file has only **2 banner sections**, one of which holds 20,122 of its 21,875 lines. Banners are useless here. The boundary map must be derived from call clustering over its 134 top-level symbols, and needs proportionally harder scrutiny at the approval gate. With only 134 symbols and 1 runtime back-edge, the graph is simple — the risk is a map that is *semantically* arbitrary, not one that is technically invalid.

- [ ] **Step 1: Produce the clustering brief**

```bash
cd /home/piet/.hermes/hermes-agent
python -m scripts.refactor.split_module gateway/run.py --analyze --json > /tmp/gwrun.json
python - <<'EOF' > /tmp/gwrun_brief.txt
import ast
from scripts.refactor import layering
src = open('gateway/run.py').read()
tree = ast.parse(src)
top = layering.top_level_symbols(tree)
for name, node in sorted(top.items(), key=lambda kv: kv[1].lineno):
    end = getattr(node, 'end_lineno', node.lineno)
    calls = sorted(layering.all_names(node, top) - {name})
    print(f"{node.lineno:6d} {end-node.lineno+1:5d}  {name}")
    if calls:
        print(f"            uses: {', '.join(calls[:12])}")
EOF
head -60 /tmp/gwrun_brief.txt
```

- [ ] **Step 2: Dispatch the boundary-map proposal to qwen (one-shot)**

```bash
claude-qwen -p "$(cat <<'PROMPT'
Propose a boundary map splitting gateway/run.py (21,875 lines, 134 top-level
symbols) into 6-10 Python submodules of 2,000-4,000 lines each.

This file has almost no section banners, so group by RESPONSIBILITY inferred
from names and call clustering. The inventory (source order, line span, and
what each symbol references) is in /tmp/gwrun_brief.txt.

Hard constraint: the output order must preserve source order — a symbol may
only be grouped with neighbours, never reordered.

Output ONLY YAML:

module: gateway/run.py
modules:
  - name: <snake_case responsibility>
    symbols: [<names in source order>]
PROMPT
)" > /tmp/gwrun_map_draft.yaml
```

- [ ] **Step 3: Approve the map**

Same five checks as Task 5 Step 3, plus: every symbol appears exactly once, source order preserved. Present to the operator. Save to `docs/refactor/boundary-map.gateway_run.yaml`.

- [ ] **Step 4: Validate without applying**

```bash
python -m scripts.refactor.split_module gateway/run.py \
  --analyze --map docs/refactor/boundary-map.gateway_run.yaml
```
Expected: import-time `backward=0`, no `OVERSIZED`, exit 0.

- [ ] **Step 5: Branch, snapshot, split, gate**

```bash
git checkout -b refactor/split-gateway-run main
python -m scripts.refactor.api_snapshot gateway.run \
  --out docs/refactor/api-snapshot.gateway_run.json
python -m scripts.refactor.split_module gateway/run.py \
  --apply --map docs/refactor/boundary-map.gateway_run.yaml
python -m scripts.refactor.api_snapshot gateway.run \
  --compare docs/refactor/api-snapshot.gateway_run.json
python -c "import gateway.run; print('import OK')"
scripts/run-affected.sh
ruff check gateway/run
```
Expected: `API IDENTICAL`, import OK, tests green, ruff clean.

- [ ] **Step 6: Re-target patch sites**

```bash
rg -n '(monkeypatch\.setattr|mock\.patch|patch)\(\s*"gateway\.run\.' tests/ gateway/ scripts/
rg -n 'setattr\(\s*run\s*,\s*"' tests/ gateway/
```
Re-point each at the new submodule path, then re-run `scripts/run-affected.sh`.

- [ ] **Step 7: CodeGraph proof**

```bash
codegraph reindex 2>&1 | tail -3
codegraph query <a symbol you know lives in gateway/run.py>
```
Expected: the real definition is returned. `gateway/run.py` was the second CodeGraph blind spot; this closes it.

- [ ] **Step 8: Gate, commit, review, merge**

```bash
python -m pytest --co -q tests/ 2>&1 | tail -5
git add -A
git commit -m "refactor: split gateway/run.py into a package (pure move)

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
git checkout main && git merge --ff-only refactor/split-gateway-run
```

Independent review before merge, same narrow mechanical brief as Task 7 Step 12.

---

## Task 9: Split `hermes_cli/web_server.py`

**Files:**
- Delete: `hermes_cli/web_server.py`; Create: `hermes_cli/web_server/`
- Create: `docs/refactor/boundary-map.web_server.yaml`

**Key difference:** 39 banner sections, none over 4,000 lines, 836 symbols, 66 runtime back-edges. This is the same shape as `kanban_db.py` — banner-derived map, merge adjacent sections into 6–10 modules of 2,000–4,000 lines.

- [ ] **Step 1: Produce the section inventory**

```bash
cd /home/piet/.hermes/hermes-agent
python -m scripts.refactor.split_module hermes_cli/web_server.py --analyze --json \
  > /tmp/ws.json
python - <<'EOF' > /tmp/ws_brief.txt
import json
r = json.load(open('/tmp/ws.json'))
for m in r['modules']:
    print(f"{r['module_lines'].get(m,0):6d}  {m}")
EOF
cat /tmp/ws_brief.txt
```

- [ ] **Step 2: Dispatch to qwen (one-shot)**

```bash
claude-qwen -p "$(cat <<'PROMPT'
Propose a boundary map grouping the 39 banner sections of
hermes_cli/web_server.py (20,314 lines) into 6-10 Python submodules of
2,000-4,000 lines each.

Only merge ADJACENT sections; output order must be source order.
Section line counts in source order are in /tmp/ws_brief.txt.

Output ONLY YAML:

module: hermes_cli/web_server.py
modules:
  - name: <snake_case responsibility>
    banners: ["<exact banner title>", ...]
PROMPT
)" > /tmp/ws_map_draft.yaml
```

- [ ] **Step 3: Approve, expand banners to symbols, validate**

Apply the same five approval checks as Task 5 Step 3. Expand banner names to explicit symbol lists with the Task 5 Step 4 script (change the source path and output path), then:

```bash
python -m scripts.refactor.split_module hermes_cli/web_server.py \
  --analyze --map docs/refactor/boundary-map.web_server.yaml
```
Expected: import-time `backward=0`, no `OVERSIZED`, exit 0.

- [ ] **Step 4: Branch, snapshot, split, gate**

```bash
git checkout -b refactor/split-web-server main
python -m scripts.refactor.api_snapshot hermes_cli.web_server \
  --out docs/refactor/api-snapshot.web_server.json
python -m scripts.refactor.split_module hermes_cli/web_server.py \
  --apply --map docs/refactor/boundary-map.web_server.yaml
python -m scripts.refactor.api_snapshot hermes_cli.web_server \
  --compare docs/refactor/api-snapshot.web_server.json
python -c "import hermes_cli.web_server; print('import OK')"
scripts/run-affected.sh
ruff check hermes_cli/web_server
```
Expected: `API IDENTICAL`, import OK, tests green, ruff clean.

- [ ] **Step 5: Re-target patch sites and re-run**

```bash
rg -n '(monkeypatch\.setattr|mock\.patch|patch)\(\s*"hermes_cli\.web_server\.' tests/ hermes_cli/ gateway/ scripts/
rg -n 'setattr\(\s*web_server\s*,\s*"' tests/ hermes_cli/ gateway/
scripts/run-affected.sh
```

- [ ] **Step 6: Dashboard live check**

`web_server.py` serves `/control`. A green unit suite is not sufficient evidence here.

```bash
systemctl --user restart hermes-dashboard.service
sleep 3
systemctl --user is-active hermes-dashboard.service
HERMES_DASHBOARD_URL=https://<host>:9443 \
HERMES_DASHBOARD_USERNAME=<user> HERMES_DASHBOARD_PASSWORD=<pass> \
  scripts/smoke_health_status_auth.py --no-prompt
```
Expected: service active; the auth smoke reaches `/api/health-status` and reports healthy. A bare loopback curl returns 401 and proves nothing — the SPA injects its token.

- [ ] **Step 7: Gate, commit, review, merge**

```bash
python -m pytest --co -q tests/ 2>&1 | tail -5
git add -A
git commit -m "refactor: split hermes_cli/web_server.py into a package (pure move)

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
git checkout main && git merge --ff-only refactor/split-web-server
```

---

## Task 10: Split `cli.py`

**Files:**
- Delete: `cli.py`; Create: `cli/`
- Create: `docs/refactor/boundary-map.cli.yaml`

**Key difference:** 1 banner section holding 13,874 of 16,797 lines, 153 symbols, 1 runtime back-edge. Call-clustering map, like Task 8. Target 4–8 modules of 2,000–4,000 lines.

**Extra risk specific to this file:** `cli.py` sits at the repo root, so `cli/` becomes a root-level package. Confirm nothing on `sys.path` shadows it and that no packaging manifest lists `cli` as a `py_modules` entry rather than a package.

- [ ] **Step 1: Check the packaging declaration before anything else**

```bash
cd /home/piet/.hermes/hermes-agent
rg -n 'py_modules|packages\s*=|\bcli\b' pyproject.toml setup.py setup.cfg 2>/dev/null
```
If `cli` is declared as a module rather than a package, update that declaration as part of this task's commit. If it is discovered automatically, nothing to do.

- [ ] **Step 2: Produce the clustering brief**

```bash
python - <<'EOF' > /tmp/cli_brief.txt
import ast
from scripts.refactor import layering
src = open('cli.py').read()
tree = ast.parse(src)
top = layering.top_level_symbols(tree)
for name, node in sorted(top.items(), key=lambda kv: kv[1].lineno):
    end = getattr(node, 'end_lineno', node.lineno)
    calls = sorted(layering.all_names(node, top) - {name})
    print(f"{node.lineno:6d} {end-node.lineno+1:5d}  {name}")
    if calls:
        print(f"            uses: {', '.join(calls[:12])}")
EOF
head -40 /tmp/cli_brief.txt
```

- [ ] **Step 3: Dispatch to qwen (one-shot)**

```bash
claude-qwen -p "$(cat <<'PROMPT'
Propose a boundary map splitting cli.py (16,797 lines, 153 top-level symbols)
into 4-8 Python submodules of 2,000-4,000 lines each.

This file has no usable section banners, so group by RESPONSIBILITY inferred
from names and call clustering. The inventory (source order, line span, and
what each symbol references) is in /tmp/cli_brief.txt.

Hard constraint: output order must preserve source order — a symbol may only
be grouped with neighbours, never reordered.

Output ONLY YAML:

module: cli.py
modules:
  - name: <snake_case responsibility>
    symbols: [<names in source order>]
PROMPT
)" > /tmp/cli_map_draft.yaml
```

- [ ] **Step 4: Approve and validate**

Same approval checks as Task 5 Step 3. Save to `docs/refactor/boundary-map.cli.yaml`, then:

```bash
python -m scripts.refactor.split_module cli.py --analyze --map docs/refactor/boundary-map.cli.yaml
```
Expected: import-time `backward=0`, no `OVERSIZED`, exit 0.

- [ ] **Step 5: Branch, snapshot, split, gate**

```bash
git checkout -b refactor/split-cli main
python -m scripts.refactor.api_snapshot cli --out docs/refactor/api-snapshot.cli.json
python -m scripts.refactor.split_module cli.py --apply --map docs/refactor/boundary-map.cli.yaml
python -m scripts.refactor.api_snapshot cli --compare docs/refactor/api-snapshot.cli.json
python -c "import cli; print('import OK')"
scripts/run-affected.sh
ruff check cli
```
Expected: `API IDENTICAL`, import OK, tests green, ruff clean.

- [ ] **Step 6: Entry-point smoke**

`cli.py` is an entry point, so an import test is not enough:

```bash
python -m cli --help 2>&1 | head -20
hermes --help 2>&1 | head -20
```
Expected: both print usage without traceback.

- [ ] **Step 7: Re-target patch sites, gate, commit, review, merge**

```bash
rg -n '(monkeypatch\.setattr|mock\.patch|patch)\(\s*"cli\.' tests/ hermes_cli/ gateway/ scripts/
scripts/run-affected.sh
python -m pytest --co -q tests/ 2>&1 | tail -5
git add -A
git commit -m "refactor: split cli.py into a package (pure move)

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
git checkout main && git merge --ff-only refactor/split-cli
```

---

## Task 11: Split `hermes_cli/main.py`

**Files:**
- Delete: `hermes_cli/main.py`; Create: `hermes_cli/main/`
- Create: `docs/refactor/boundary-map.main.yaml`

**Key difference and the sharpest risk in the whole plan:** this file's banner analysis reports a section titled *"Profile override — MUST happen before any hermes module impo[rt]"* (4,274 lines). That is an explicit ordering contract in the source. A split that changes when profile-override code runs relative to other imports would break it silently — the API snapshot would still be identical, because the surface is unchanged while the *timing* is not.

**Mitigation, mandatory:** the first module in this boundary map must contain the entire profile-override section and everything the source places before it, and `__init__.py` must import that module first. Verify by reading the section's code before approving the map, and add an explicit runtime assertion to the smoke step below.

- [ ] **Step 1: Read the ordering contract before proposing anything**

```bash
cd /home/piet/.hermes/hermes-agent
rg -n 'MUST happen before any hermes module impo' hermes_cli/main.py
```
Read ±80 lines around the hit. Write down, in `docs/refactor/boundary-map.main.yaml` as a leading comment, exactly what must run before what. If the contract cannot be preserved by a source-order-preserving split, **stop and escalate to the operator** rather than proceeding.

- [ ] **Step 2: Produce the clustering brief**

```bash
python - <<'EOF' > /tmp/main_brief.txt
import ast
from scripts.refactor import layering
src = open('hermes_cli/main.py').read()
tree = ast.parse(src)
top = layering.top_level_symbols(tree)
for name, node in sorted(top.items(), key=lambda kv: kv[1].lineno):
    end = getattr(node, 'end_lineno', node.lineno)
    calls = sorted(layering.all_names(node, top) - {name})
    print(f"{node.lineno:6d} {end-node.lineno+1:5d}  {name}")
    if calls:
        print(f"            uses: {', '.join(calls[:12])}")
EOF
head -40 /tmp/main_brief.txt
```

- [ ] **Step 3: Dispatch to qwen (one-shot)**

```bash
claude-qwen -p "$(cat <<'PROMPT'
Propose a boundary map splitting hermes_cli/main.py (15,092 lines, 260
top-level symbols) into 4-8 Python submodules of 2,000-4,000 lines each.

Group by RESPONSIBILITY inferred from names and call clustering. The
inventory (source order, line span, references) is in /tmp/main_brief.txt.

TWO hard constraints:
1. Output order must preserve source order — group neighbours only.
2. The FIRST module must contain everything up to and including the
   "Profile override" section. That section documents an ordering contract
   ("MUST happen before any hermes module import") and must load first.

Output ONLY YAML:

module: hermes_cli/main.py
modules:
  - name: <snake_case responsibility>
    symbols: [<names in source order>]
PROMPT
)" > /tmp/main_map_draft.yaml
```

- [ ] **Step 4: Approve and validate**

Same checks as Task 5 Step 3, **plus** an explicit confirmation that the profile-override symbols are all in the first module. Save to `docs/refactor/boundary-map.main.yaml`, then:

```bash
python -m scripts.refactor.split_module hermes_cli/main.py \
  --analyze --map docs/refactor/boundary-map.main.yaml
```
Expected: import-time `backward=0`, no `OVERSIZED`, exit 0.

- [ ] **Step 5: Branch, snapshot, split, gate**

```bash
git checkout -b refactor/split-main main
python -m scripts.refactor.api_snapshot hermes_cli.main \
  --out docs/refactor/api-snapshot.main.json
python -m scripts.refactor.split_module hermes_cli/main.py \
  --apply --map docs/refactor/boundary-map.main.yaml
python -m scripts.refactor.api_snapshot hermes_cli.main \
  --compare docs/refactor/api-snapshot.main.json
python -c "import hermes_cli.main; print('import OK')"
scripts/run-affected.sh
ruff check hermes_cli/main
```
Expected: `API IDENTICAL`, import OK, tests green, ruff clean.

- [ ] **Step 6: Prove the profile-override ordering contract still holds**

```bash
cd /home/piet/.hermes/hermes-agent
HERMES_PROFILE=<a non-default profile that exists on disk> \
  python -c "import hermes_cli.main; import hermes_cli; print(hermes_cli.__file__)"
hermes --help 2>&1 | head -20
HERMES_PROFILE=<same profile> hermes profile show 2>&1 | head -20
```
Expected: the profile override takes effect exactly as it did before the split. Compare against the same three commands run on `main` before merging — capture both outputs and diff them. If they differ, the ordering contract broke; revert and re-map.

- [ ] **Step 7: Re-target patch sites, gate, commit, review, merge**

```bash
rg -n '(monkeypatch\.setattr|mock\.patch|patch)\(\s*"hermes_cli\.main\.' tests/ hermes_cli/ gateway/ scripts/
scripts/run-affected.sh
python -m pytest --co -q tests/ 2>&1 | tail -5
git add -A
git commit -m "refactor: split hermes_cli/main.py into a package (pure move)

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
git checkout main && git merge --ff-only refactor/split-main
```

---

## Task 12: Close out

- [ ] **Step 1: Verify every success criterion from the spec**

```bash
cd /home/piet/.hermes/hermes-agent
find . -name '*.py' -size +1M -not -path './.claude/*' -not -path './node_modules/*'
```
Expected: no output. Criterion 1 (every file under 1 MiB) met.

```bash
codegraph reindex 2>&1 | tail -3
codegraph query dispatch_once
```
Expected: the real definition. Criterion 1 (CodeGraph visibility) met.

```bash
for m in hermes_cli.kanban_db gateway.run hermes_cli.web_server cli hermes_cli.main; do
  python -m scripts.refactor.api_snapshot "$m" \
    --compare "docs/refactor/api-snapshot.$(echo $m | tr '.' '_' | sed 's/hermes_cli_//').json"
done
```
Expected: `API IDENTICAL` five times. Criterion 2 met.

```bash
git log --oneline main --since=2026-07-24 -- hermes_cli/ gateway/ cli.py
git diff --stat <first-split-commit>~1..main -- ':!hermes_cli/kanban_db*' ':!gateway/run*' \
  ':!hermes_cli/web_server*' ':!cli*' ':!hermes_cli/main*' ':!docs/' ':!scripts/refactor/' ':!tests/refactor/'
```
Expected: the second command shows only the enumerated test patch-target lines. Criterion 3 (zero production call-site edits) met.

- [ ] **Step 2: Update the repo's own navigation docs**

`CLAUDE.md` contains a "Code map" section stating that CodeGraph skips `hermes_cli/kanban_db.py` and `gateway/run.py` and that `rg` plus `LIFECYCLE.md` must be used instead. That is now false. Update it: CodeGraph indexes everything; the `rg`-only workaround for those two files is retired. Keep the `rg` over `grep -r` guidance (worktrees), which is unrelated and still true.

`AGENTS.md` and `docs/agent-dev-guide.md` may carry the same claim — check and update both.

- [ ] **Step 3: Record the follow-ups as their own tasks**

The five defects deliberately left unfixed (spec, Follow-ups) still exist. File each as its own kanban task with its own test requirement, referencing the new module path rather than the old line number:

1. `_dispatch_once_locked` unguarded DB-path-resolution path weakens the single-writer invariant
2. Status vocabulary: `failed` / `canceled` / `cancelled` referenced by filters but absent from `VALID_STATUSES`
3. `block_task` docstring understates real behaviour
4. Review-dispatch comment says rejection goes "back to running"; it lands in `blocked`
5. Inconsistent banner layout around scheduling/dispatch

- [ ] **Step 4: Push the fork**

```bash
git status --short          # clean
git log --oneline -8
git push piet-fork main     # fast-forward only, never --force, never origin
```

- [ ] **Step 5: Commit the closeout doc changes**

```bash
git add CLAUDE.md AGENTS.md docs/agent-dev-guide.md
git commit -m "docs: retire the CodeGraph blind-spot workaround after modularization"
```

---

## Amendment to the approved spec (recorded for the operator)

The spec's stated cycle mitigation — *"anything that would cycle stays in `_core.py` for that round"* — was measured against the real file before planning and does not work as written. Partitioning `kanban_db.py` by its 34 (actually 37) banner sections and converting intra-module references into symbol-level imports puts **28 of 37 sections, 34,764 of 38,834 lines (89.5%), into a single module-level import cycle**. Under the `_core.py` fallback that produces a 34.7k-line `_core.py`: still over 1 MiB, still CodeGraph-invisible, no goal met.

The cause is that symbol-level imports (`from .b import fn`) are resolved at import time and therefore fail on cycles, while the file's genuine dependency structure is fine — its symbol graph is a near-perfect DAG (973 symbols, exactly one 3-symbol cycle).

The fix, verified empirically and adopted throughout this plan: distinguish **import-time** references (assignment values, decorators, default arguments, class bases) from **runtime** references (names used inside function bodies). Only import-time references constrain module order. Runtime references to a later submodule are emitted as module-object imports (`from . import b` + `b.fn(...)`), which resolve at call time and tolerate cycles — confirmed by direct test. Measured across all five files, **import-time backward references number zero**, so the source order is already a valid layering and `_core.py` is never needed. The cost is 140 mechanically-rewritten reference sites in `kanban_db.py` (66 in `web_server.py`, 1 each in the others) — still tool-generated, never model-authored, so the spec's core safety property is preserved. What changes is the "byte-exact" claim: ~99.6% of moved lines are byte-identical rather than 100%.

The spec's claim that "the other four files scale proportionally" is also not accurate: `gateway/run.py`, `cli.py` and `hermes_cli/main.py` have 2, 1 and 3 banner sections respectively, with one section holding 92%, 83% and 64% of each file. Their boundary maps must be derived from call clustering, not banners, and `hermes_cli/main.py` carries an explicit source-documented ordering contract ("Profile override — MUST happen before any hermes module import") that the split must preserve and prove. Tasks 8, 10 and 11 handle this.
