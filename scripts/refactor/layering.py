"""Symbol-level dependency analysis for the giant-module split.

The load-bearing distinction here is IMPORT-TIME vs RUNTIME references.

An import-time reference (an assignment's value or evaluated annotation, a
decorator, a default argument, a class base or keyword) is evaluated while the
module body executes, so it constrains submodule order absolutely: the target
must already exist.

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


def annotations_are_postponed(tree: ast.Module) -> bool:
    """Whether `tree` enables `from __future__ import annotations`."""
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == "__future__"
        and any(alias.name == "annotations" for alias in node.names)
        for node in tree.body
    )


def import_time_names(
    node: ast.AST,
    top: dict[str, ast.AST],
    *,
    annotations_postponed: bool = False,
) -> set[str]:
    """Names of `top` that `node` evaluates while the module body runs."""
    found: set[str] = set()

    def add(expr) -> None:
        for sub in ast.walk(expr):
            if isinstance(sub, ast.Name) and sub.id in top:
                found.add(sub.id)

    def add_function_header(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for decorator in fn.decorator_list:
            add(decorator)
        for default in fn.args.defaults:
            add(default)
        for default in fn.args.kw_defaults:
            if default is not None:
                add(default)
        if annotations_postponed:
            return
        args = [*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs]
        if fn.args.vararg is not None:
            args.append(fn.args.vararg)
        if fn.args.kwarg is not None:
            args.append(fn.args.kwarg)
        for arg in args:
            if arg.annotation is not None:
                add(arg.annotation)
        if fn.returns is not None:
            add(fn.returns)

    if isinstance(node, ast.Assign):
        if node.value is not None:
            add(node.value)
    elif isinstance(node, ast.AnnAssign):
        if node.value is not None:
            add(node.value)
        if not annotations_postponed:
            add(node.annotation)
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        add_function_header(node)
    elif isinstance(node, ast.ClassDef):
        for decorator in node.decorator_list:
            add(decorator)
        for base in node.bases:
            add(base)
        for keyword in node.keywords:
            add(keyword.value)
        for statement in node.body:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                add_function_header(statement)
            else:
                add(statement)
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
    *,
    module_tree: ast.Module | None = None,
) -> References:
    """Split cross-module references into import-time/runtime × forward/backward."""
    rank = {m: i for i, m in enumerate(module_order)}
    refs = References()
    postponed = (
        annotations_are_postponed(module_tree)
        if module_tree is not None
        else False
    )
    for name, node in top.items():
        home = owner[name]
        it = import_time_names(
            node,
            top,
            annotations_postponed=postponed,
        ) - {name}
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
