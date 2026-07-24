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
import re
import sys


def _signature(obj) -> str | None:
    try:
        signature = str(inspect.signature(obj))
        return re.sub(r" at 0x[0-9a-fA-F]+", " at 0xADDR", signature)
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
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(snap, fh, indent=2, sort_keys=True)
        print(f"wrote {len(snap['symbols'])} symbols to {args.out}")
    if args.compare:
        with open(args.compare, encoding="utf-8") as fh:
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
