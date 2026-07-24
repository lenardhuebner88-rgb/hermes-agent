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
