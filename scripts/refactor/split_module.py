"""Split a giant module into a package. Deterministic, AST-driven, pure move.

Three modes:

  --analyze   report the layering: which symbols land where, and which
              cross-module references are import-time (order-constraining)
              versus runtime (order-free).
  --ownership compare symbol ownership with an upstream ref.
  --extract   move selected symbols to a sibling package while keeping the
              origin module a file.

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
import os

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
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    lines = src.splitlines()
    if boundary_map is None:
        top, owner, order = _section_owner_from_banners(tree, lines)
    else:
        top, owner, order = _section_owner_from_map(tree, boundary_map)

    refs = layering.classify_references(top, owner, order, module_tree=tree)

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


def _symbol_span(node) -> tuple[int, int]:
    start = node.lineno
    decorators = getattr(node, "decorator_list", None)
    if decorators:
        start = min(start, min(d.lineno for d in decorators))
    return start, getattr(node, "end_lineno", node.lineno)


def _header_end_lineno(tree) -> int:
    last = 0
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            last = max(last, getattr(node, "end_lineno", node.lineno))
    return last


def _docstring_span(tree) -> tuple[int, int] | None:
    if (
        tree.body
        and isinstance(tree.body[0], ast.Expr)
        and isinstance(tree.body[0].value, ast.Constant)
        and isinstance(tree.body[0].value.value, str)
    ):
        node = tree.body[0]
        return node.lineno, getattr(node, "end_lineno", node.lineno)
    return None


def _top_level_symbols_for_move(tree: ast.Module) -> dict[str, ast.AST]:
    """Top-level symbols, including names nested in tuple/list assignments."""
    top = layering.top_level_symbols(tree)
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            for sub in ast.walk(target):
                if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                    top[sub.id] = node
    return top


def locally_bound_anywhere(node) -> set[str]:
    """Every name bound anywhere inside `node`.

    Used conservatively for emission rule 8: if a backward target is bound
    anywhere inside the referring symbol, no occurrence is rewritten.
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


def _rewrite_backward_refs(
    body_lines,
    first_lineno,
    node,
    targets,
    skipped,
):
    """Rewrite unshadowed backward loads to `module.name`."""
    shadowed = locally_bound_anywhere(node) & set(targets)
    for name in sorted(shadowed):
        skipped.append((getattr(node, "name", "<assignment>"), name))

    edits = {}
    for sub in ast.walk(node):
        if (
            isinstance(sub, ast.Name)
            and isinstance(sub.ctx, ast.Load)
            and sub.id in targets
            and sub.id not in shadowed
        ):
            edits.setdefault(sub.lineno, []).append((sub.col_offset, sub.id))
    for lineno, hits in edits.items():
        idx = lineno - first_lineno
        if idx < 0 or idx >= len(body_lines):
            continue
        line = body_lines[idx]
        for col, name in sorted(hits, reverse=True):
            if line[col:col + len(name)] != name:
                raise SystemExit(
                    f"rewrite offset mismatch at line {lineno} for {name!r}"
                )
            line = (
                line[:col]
                + f"{targets[name]}.{name}"
                + line[col + len(name):]
            )
        body_lines[idx] = line
    return body_lines


def _import_name_for_path(path: str) -> str:
    without_suffix = os.path.splitext(os.path.abspath(path))[0]
    relative = os.path.relpath(without_suffix, os.getcwd())
    if relative != ".." and not relative.startswith(f"..{os.sep}"):
        return relative.replace(os.sep, ".")
    return os.path.basename(without_suffix)


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

    fork = bodies(open(path, encoding="utf-8").read())
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


def _load_map(path):
    import yaml
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def apply_split(path, bmap):
    raise NotImplementedError


def extract_to_package(path: str, boundary_map: dict, package_dir: str) -> int:
    """Move selected symbols to a sibling package while `path` stays a file."""
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    lines = src.split("\n")
    top = _top_level_symbols_for_move(tree)

    order = [entry["name"] for entry in boundary_map["modules"]]
    moved: dict[str, str] = {}
    for entry in boundary_map["modules"]:
        for symbol in entry["symbols"]:
            if symbol not in top:
                raise SystemExit(f"boundary map names unknown symbol: {symbol}")
            if symbol in moved and moved[symbol] != entry["name"]:
                raise SystemExit(
                    f"boundary map places {symbol} in both "
                    f"{moved[symbol]} and {entry['name']}"
                )
            moved[symbol] = entry["name"]
    if not moved:
        raise SystemExit("boundary map does not select any symbols to extract")
    stays = set(top) - set(moved)

    by_node: dict[int, list[str]] = {}
    nodes_by_id: dict[int, ast.AST] = {}
    for name, node in top.items():
        by_node.setdefault(id(node), []).append(name)
        nodes_by_id[id(node)] = node
    for names in by_node.values():
        homes = {moved.get(name, "__origin__") for name in names}
        if len(homes) > 1 and any(name in moved for name in names):
            raise SystemExit(
                f"REFUSING: {sorted(names)} are bound by one statement but "
                f"extraction splits them across {sorted(homes)}"
            )

    postponed = layering.annotations_are_postponed(tree)
    offenders = []
    for name in sorted(stays):
        import_time = layering.import_time_names(
            top[name],
            top,
            annotations_postponed=postponed,
        ) - {name}
        for target in sorted(import_time):
            if target in moved:
                offenders.append((name, target))
    if offenders:
        print(
            "REFUSING: symbols that stay reference extracted symbols at import "
            "time; a trailing re-export block runs too late for these:"
        )
        for referrer, target in offenders:
            print(f"  {referrer} needs {target} (mapped to {moved[target]})")
        raise SystemExit(2)

    moved_top = {name: top[name] for name in moved}
    refs = layering.classify_references(
        moved_top,
        moved,
        order,
        module_tree=tree,
    )
    if refs.import_time_backward:
        print(
            "REFUSING: import-time backward references between extracted "
            "submodules would create a cycle:"
        )
        for referrer, target in refs.import_time_backward:
            print(
                f"  {referrer} ({moved[referrer]}) -> "
                f"{target} ({moved[target]})"
            )
        raise SystemExit(2)

    symbol_imports: dict[str, dict[str, set[str]]] = {m: {} for m in order}
    module_imports: dict[str, set[str]] = {m: set() for m in order}
    backward_targets: dict[str, dict[str, str]] = {m: {} for m in order}
    origin_imports: dict[str, set[str]] = {m: set() for m in order}
    for referrer, target in refs.import_time_forward + refs.runtime_forward:
        home, target_home = moved[referrer], moved[target]
        symbol_imports[home].setdefault(target_home, set()).add(target)
    for referrer, target in refs.runtime_backward:
        home, target_home = moved[referrer], moved[target]
        module_imports[home].add(target_home)
        backward_targets[home][target] = target_home
    for name, node in moved_top.items():
        origin_imports[moved[name]].update(
            layering.all_names(node, top) & stays
        )

    header_end = _header_end_lineno(tree)
    docstring_span = _docstring_span(tree)
    docstring_src = None
    header_start = 0
    if docstring_span is not None:
        doc_start, doc_end = docstring_span
        docstring_src = "\n".join(lines[doc_start - 1:doc_end])
        header_start = doc_end
    header = "\n".join(lines[header_start:header_end])

    ordered_nodes = sorted(
        nodes_by_id.values(),
        key=lambda node: _symbol_span(node)[0],
    )
    node_spans: dict[int, tuple[int, int]] = {}
    prev_end = header_end
    for node in ordered_nodes:
        start, end = _symbol_span(node)
        lead = start - 1
        while lead > prev_end and (
            lines[lead - 1].strip() == ""
            or lines[lead - 1].lstrip().startswith("#")
        ):
            lead -= 1
        node_spans[id(node)] = (lead, end)
        prev_end = end

    bodies: dict[str, list[str]] = {m: [] for m in order}
    defined: dict[str, list[str]] = {m: [] for m in order}
    shadow_skips: list[tuple[str, str]] = []
    removed_lines: set[int] = set()
    for node in ordered_nodes:
        names = by_node[id(node)]
        moved_names = [name for name in names if name in moved]
        if not moved_names:
            continue
        home = moved[moved_names[0]]
        lead, end = node_spans[id(node)]
        chunk = list(lines[lead:end])
        if backward_targets[home]:
            chunk = _rewrite_backward_refs(
                chunk,
                lead + 1,
                node,
                backward_targets[home],
                shadow_skips,
            )
        bodies[home].append("\n".join(chunk))
        defined[home].extend(moved_names)
        removed_lines.update(range(lead, end))

    origin_module = _import_name_for_path(path)
    package_module = boundary_map.get("package")
    if package_module:
        package_module = os.path.splitext(package_module)[0].replace(os.sep, ".")
    else:
        package_module = _import_name_for_path(package_dir)

    rendered_modules: dict[str, str] = {}
    for module in order:
        parts = [header, ""]
        if origin_imports[module]:
            names = ", ".join(sorted(origin_imports[module]))
            parts.append(f"from {origin_module} import {names}")
        for target_module in sorted(symbol_imports[module]):
            names = ", ".join(sorted(symbol_imports[module][target_module]))
            parts.append(f"from .{target_module} import {names}")
        for target_module in sorted(module_imports[module]):
            parts.append(f"from . import {target_module}")
        parts.append("")
        parts.extend(bodies[module])
        text = "\n".join(parts)
        if not text.endswith("\n"):
            text += "\n"
        rendered_modules[module] = text

    init_parts = []
    if docstring_src is not None:
        init_parts.extend([docstring_src, ""])
    for module in order:
        if not defined[module]:
            continue
        init_parts.append(f"from .{module} import (")
        init_parts.extend(f"    {name}," for name in defined[module])
        init_parts.append(")")
    init_parts.append("")
    init_text = "\n".join(init_parts)

    remaining = [
        line for index, line in enumerate(lines)
        if index not in removed_lines
    ]
    origin_text = "\n".join(remaining)
    if origin_text and not origin_text.endswith("\n"):
        origin_text += "\n"
    if origin_text and not origin_text.endswith("\n\n"):
        origin_text += "\n"
    reexport_names = [
        name for module in order for name in defined[module]
    ]
    reexport = [f"from {package_module} import ("]
    reexport.extend(f"    {name}," for name in reexport_names)
    reexport.append(")")
    origin_text += "\n".join(reexport) + "\n"

    package_dir = os.path.abspath(package_dir)
    tmp_dir = package_dir + ".__extract_tmp__"
    origin_tmp = os.path.abspath(path) + ".__extract_tmp__"
    for target in (package_dir, tmp_dir, origin_tmp):
        if os.path.exists(target):
            raise SystemExit(f"REFUSING: output path already exists: {target}")

    os.makedirs(os.path.dirname(package_dir) or ".", exist_ok=True)
    os.makedirs(tmp_dir)
    for module, text in rendered_modules.items():
        with open(
            os.path.join(tmp_dir, f"{module}.py"),
            "w",
            encoding="utf-8",
        ) as handle:
            handle.write(text)
    with open(
        os.path.join(tmp_dir, "__init__.py"),
        "w",
        encoding="utf-8",
    ) as handle:
        handle.write(init_text)
    with open(origin_tmp, "w", encoding="utf-8") as handle:
        handle.write(origin_text)

    os.rename(tmp_dir, package_dir)
    os.replace(origin_tmp, path)
    print(
        f"extracted {len(moved)} symbols from {path} -> {package_dir}/ "
        f"({len(order)} modules, {len(refs.runtime_backward)} "
        "module-object rewrites)"
    )
    if shadow_skips:
        print(
            f"REVIEW REQUIRED — {len(shadow_skips)} backward reference(s) "
            "were NOT rewritten because a local binding shadows the name:"
        )
        for referrer, target in shadow_skips:
            print(
                f"  {referrer} references {target} ({moved[target]}) but "
                f"also binds {target} locally"
            )
        print(
            "  Confirm each is genuinely local. If any is a real module-level "
            "reference, move it into the same extracted submodule."
        )
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("path")
    p.add_argument("--analyze", action="store_true")
    p.add_argument("--ownership", action="store_true")
    p.add_argument("--extract", action="store_true")
    p.add_argument("--upstream-ref", default="origin/main")
    p.add_argument("--map", help="boundary map YAML")
    p.add_argument("--package", help="sibling package output directory")
    p.add_argument("--json", action="store_true", help="emit the raw report")
    args = p.parse_args(argv)

    if args.ownership:
        rep = ownership(args.path, args.upstream_ref)
        if args.json:
            print(json.dumps(rep, indent=2, default=list))
            return 0
        print(f"{rep['path']} against {rep['upstream_ref']}:")
        print(
            f"  fork-only:           {len(rep['fork_only'])} symbols, "
            f"{rep['lines']['fork_only']} lines"
        )
        print(
            f"  upstream-identical:  {len(rep['upstream_identical'])} symbols, "
            f"{rep['lines']['upstream_identical']} lines"
        )
        print(
            f"  upstream-diverged:   {len(rep['upstream_diverged'])} symbols, "
            f"{rep['lines']['upstream_diverged']} lines"
        )
        return 0

    if args.extract:
        if not args.map:
            p.error("--extract requires --map")
        if not args.package:
            p.error("--extract requires --package")
        return extract_to_package(
            args.path,
            _load_map(args.map),
            args.package,
        )

    bmap = _load_map(args.map) if args.map else None
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


if __name__ == "__main__":
    raise SystemExit(main())
