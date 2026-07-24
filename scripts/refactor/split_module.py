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


def _symbol_span(node) -> tuple[int, int]:
    start = node.lineno
    decorators = getattr(node, "decorator_list", None)
    if decorators:
        start = min(start, min(d.lineno for d in decorators))
    return start, getattr(node, "end_lineno", node.lineno)


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


def extract_to_package(path, boundary_map, package_dir):
    raise NotImplementedError


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("path")
    p.add_argument("--analyze", action="store_true")
    p.add_argument("--ownership", action="store_true")
    p.add_argument("--upstream-ref", default="origin/main")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--map", help="boundary map YAML")
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
