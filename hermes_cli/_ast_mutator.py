"""AST-based code mutator for mutation testing.

Pure Python standard library only (uses :mod:`ast`). No external or
project-specific imports.

The public API is :func:`generate_mutants`, which takes Python source code and
returns a deterministic list of :class:`Mutant` objects. Each mutant is the full
source of the file with *exactly one* targeted mutation applied, produced by
re-parsing the original source and applying a fresh transformer that only edits
the i-th matching node. This guarantees that mutations never interact and that
the original is never returned unmutated.

Mutation operators implemented:

* ``negate_if``       -- ``if COND:`` -> ``if not (COND):`` (also ``elif``)
* ``comparison_swap`` -- ``==``<->``!=``, ``<``<->``<=``, ``>``<->``>=``,
                         ``is``<->``is not``
* ``bool_op_swap``    -- ``and``<->``or``
* ``const_offset``    -- numeric literal ``n`` -> ``n + 1`` (int/float only)
* ``return_none``     -- ``return EXPR`` -> ``return None``
* ``remove_guard``    -- ``if ...: raise ...`` / ``if ...: return ...`` -> ``pass``
* ``boolean_flip``    -- ``True``<->``False``

The output is deterministic: candidate sites are discovered by a single
depth-first walk of the tree (which visits nodes in source order), then the
resulting mutants are stably sorted by ``(operator, lineno, col_offset)`` before
the ``max_mutants`` cap is applied.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

__all__ = ["Mutant", "generate_mutants"]


@dataclass
class Mutant:
    """A single mutation of a source file.

    Attributes:
        mutated_source: Full source text of the file *with the one mutation*.
        operator: Name of the mutation operator, e.g. ``"negate_if"``.
        lineno: 1-based line of the mutated node in the *original* source.
        description: Short human-readable description of the change.
    """

    mutated_source: str
    operator: str
    lineno: int
    description: str


# --------------------------------------------------------------------------- #
# Candidate site discovery
# --------------------------------------------------------------------------- #
#
# A "site" is one place in the AST that a given operator could mutate. We
# describe each site with a small record so that, later, we can re-parse the
# original source from scratch and apply a transformer that fires on exactly the
# i-th site of that operator.
#
# We identify a site positionally: ``(operator, occurrence_index)`` where
# ``occurrence_index`` is the index of this site among all sites of the same
# operator, in depth-first / source order. The transformer counts matching
# nodes during its own walk and edits only the one whose running index equals
# the target occurrence_index. Because :func:`ast.parse` is deterministic and
# the walk order is fixed, the i-th node is always the same node.


@dataclass
class _Site:
    operator: str
    occurrence: int          # index among sites of this operator (0-based)
    lineno: int              # 1-based line in the original source
    col_offset: int          # column in the original source (for stable sort)
    description: str
    # ``variant`` carries operator-specific detail needed to build the
    # description / disambiguate (e.g. which comparison swap). It is not used by
    # the transformer beyond what is already encoded by ``occurrence``.
    variant: str = ""


# Comparison operator swaps: maps an ast comparison-op type to its replacement
# type plus the symbol pair used for descriptions.
_CMP_SWAP: dict[type[ast.cmpop], tuple[type[ast.cmpop], str, str]] = {
    ast.Eq: (ast.NotEq, "==", "!="),
    ast.NotEq: (ast.Eq, "!=", "=="),
    ast.Lt: (ast.LtE, "<", "<="),
    ast.LtE: (ast.Lt, "<=", "<"),
    ast.Gt: (ast.GtE, ">", ">="),
    ast.GtE: (ast.Gt, ">=", ">"),
    ast.Is: (ast.IsNot, "is", "is not"),
    ast.IsNot: (ast.Is, "is not", "is"),
}


def _is_dunder_main_test(node: ast.If) -> bool:
    """True if this ``if`` is the ``if __name__ == "__main__":`` guard."""
    test = node.test
    if isinstance(test, ast.Compare):
        left = test.left
        if isinstance(left, ast.Name) and left.id == "__name__":
            return True
        # also catch the (rare) reversed form: "__main__" == __name__
        for comp in test.comparators:
            if isinstance(comp, ast.Name) and comp.id == "__name__":
                return True
    return False


def _is_simple_guard(node: ast.If) -> bool:
    """True if ``node`` is a removable guard: ``if ...: raise/return`` with a
    single-statement body and no ``elif``/``else`` branch."""
    if node.orelse:
        return False
    if len(node.body) != 1:
        return False
    stmt = node.body[0]
    return isinstance(stmt, (ast.Raise, ast.Return))


def _const_is_numeric(node: ast.Constant) -> bool:
    """True for int/float constants that are not bool (bool is an int subclass)."""
    value = node.value
    if isinstance(value, bool):
        return False
    return isinstance(value, (int, float, complex))


def _collect_annotation_nodes(tree: ast.AST) -> set[int]:
    """Return ``id()`` of every node that lives inside a type annotation.

    Constants and booleans inside annotations must not be mutated. We gather the
    annotation subtrees and record the identities of all contained nodes.
    """
    annotated: set[int] = set()

    def mark(sub: ast.AST | None) -> None:
        if sub is None:
            return
        for n in ast.walk(sub):
            annotated.add(id(n))

    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign):
            mark(node.annotation)
        elif isinstance(node, ast.arg):
            mark(node.annotation)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            mark(node.returns)
    return annotated


def _discover_sites(tree: ast.AST) -> list[_Site]:
    """Walk ``tree`` once (source order) and collect all mutable sites.

    The per-operator occurrence counters are assigned here so that the i-th site
    of an operator corresponds to the i-th matching node encountered by an
    identical walk in the transformer.
    """
    annotation_nodes = _collect_annotation_nodes(tree)
    counters: dict[str, int] = {}
    sites: list[_Site] = []

    def next_occ(op: str) -> int:
        idx = counters.get(op, 0)
        counters[op] = idx + 1
        return idx

    # ast.walk yields nodes in a deterministic breadth-first order. We instead
    # need a stable order that the transformer can reproduce exactly. Both the
    # discovery walk and the transformer use ast.walk, so any consistent order
    # works as long as it is identical on both sides. We use ast.walk here and
    # the NodeTransformer's generic_visit (DFS) in mutation -- these differ, so
    # to keep occurrence indices aligned we instead count via a shared DFS here
    # too. Implement an explicit DFS that matches NodeTransformer traversal.

    def visit(node: ast.AST) -> None:
        # --- if-based operators (negate_if, remove_guard) ---
        if isinstance(node, ast.If):
            if not _is_dunder_main_test(node):
                occ = next_occ("negate_if")
                sites.append(
                    _Site(
                        operator="negate_if",
                        occurrence=occ,
                        lineno=node.lineno,
                        col_offset=node.col_offset,
                        description=f"if cond -> if not (cond) (Zeile {node.lineno})",
                    )
                )
            if _is_simple_guard(node):
                occ = next_occ("remove_guard")
                kind = type(node.body[0]).__name__.lower()
                sites.append(
                    _Site(
                        operator="remove_guard",
                        occurrence=occ,
                        lineno=node.lineno,
                        col_offset=node.col_offset,
                        description=(
                            f"Guard entfernt: if ...: {kind} -> pass "
                            f"(Zeile {node.lineno})"
                        ),
                    )
                )

        # --- comparison_swap ---
        elif isinstance(node, ast.Compare):
            for i, op in enumerate(node.ops):
                swap = _CMP_SWAP.get(type(op))
                if swap is None:
                    continue
                _new_type, old_sym, new_sym = swap
                occ = next_occ("comparison_swap")
                # comparison op nodes carry no position; use the Compare's.
                sites.append(
                    _Site(
                        operator="comparison_swap",
                        occurrence=occ,
                        lineno=node.lineno,
                        col_offset=node.col_offset,
                        description=(
                            f"Vergleich {old_sym} -> {new_sym} "
                            f"(Zeile {node.lineno})"
                        ),
                        variant=f"{i}",
                    )
                )

        # --- bool_op_swap ---
        elif isinstance(node, ast.BoolOp):
            old_sym = "and" if isinstance(node.op, ast.And) else "or"
            new_sym = "or" if isinstance(node.op, ast.And) else "and"
            occ = next_occ("bool_op_swap")
            sites.append(
                _Site(
                    operator="bool_op_swap",
                    occurrence=occ,
                    lineno=node.lineno,
                    col_offset=node.col_offset,
                    description=f"{old_sym} -> {new_sym} (Zeile {node.lineno})",
                )
            )

        # --- return_none ---
        elif isinstance(node, ast.Return):
            val = node.value
            already_none = val is None or (
                isinstance(val, ast.Constant) and val.value is None
            )
            if not already_none:
                occ = next_occ("return_none")
                sites.append(
                    _Site(
                        operator="return_none",
                        occurrence=occ,
                        lineno=node.lineno,
                        col_offset=node.col_offset,
                        description=f"return EXPR -> return None (Zeile {node.lineno})",
                    )
                )

        # --- const_offset / boolean_flip (Constant nodes) ---
        elif isinstance(node, ast.Constant):
            if id(node) not in annotation_nodes:
                if isinstance(node.value, bool):
                    occ = next_occ("boolean_flip")
                    old_b = "True" if node.value else "False"
                    new_b = "False" if node.value else "True"
                    sites.append(
                        _Site(
                            operator="boolean_flip",
                            occurrence=occ,
                            lineno=node.lineno,
                            col_offset=node.col_offset,
                            description=f"{old_b} -> {new_b} (Zeile {node.lineno})",
                        )
                    )
                elif _const_is_numeric(node):
                    occ = next_occ("const_offset")
                    sites.append(
                        _Site(
                            operator="const_offset",
                            occurrence=occ,
                            lineno=node.lineno,
                            col_offset=node.col_offset,
                            description=(
                                f"Konstante {node.value!r} -> {node.value!r} + 1 "
                                f"(Zeile {node.lineno})"
                            ),
                        )
                    )

        # Recurse into children in field order (matches NodeTransformer DFS).
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(tree)
    return sites


# --------------------------------------------------------------------------- #
# Per-site transformer: edits exactly the i-th occurrence of one operator.
# --------------------------------------------------------------------------- #
#
# The transformer re-walks a *fresh* copy of the tree using the same DFS order
# as discovery, counts matching nodes, and mutates only the one whose running
# index equals ``target_occurrence``. It records whether it actually fired so
# callers can assert that a mutation was produced.


class _SingleSiteMutator(ast.NodeTransformer):
    """Applies one operator to exactly one occurrence (the target index)."""

    def __init__(self, operator: str, target_occurrence: int, annotation_nodes: set[int]):
        self.operator = operator
        self.target = target_occurrence
        self.annotation_nodes = annotation_nodes
        self._seen = 0
        self.fired = False

    # -- helpers --------------------------------------------------------- #
    def _hit(self) -> bool:
        """Return True if the current matching node is the target, advancing
        the per-operator counter exactly as discovery did."""
        is_target = self._seen == self.target
        self._seen += 1
        return is_target

    # -- visitors -------------------------------------------------------- #
    def visit_If(self, node: ast.If) -> ast.AST:
        # We must visit children FIRST is wrong here: discovery counted the If
        # node itself before recursing, so we evaluate the If match before
        # descending, to keep occurrence indices identical.
        if self.operator == "negate_if":
            if not _is_dunder_main_test(node):
                if self._hit():
                    self.fired = True
                    node.test = ast.UnaryOp(op=ast.Not(), operand=node.test)
                    # descend into the (now-wrapped) children too
                    self.generic_visit(node)
                    return node
            return self.generic_visit(node)

        if self.operator == "remove_guard":
            if _is_simple_guard(node):
                if self._hit():
                    self.fired = True
                    return ast.copy_location(ast.Pass(), node)
            return self.generic_visit(node)

        return self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> ast.AST:
        if self.operator == "comparison_swap":
            for i, op in enumerate(node.ops):
                swap = _CMP_SWAP.get(type(op))
                if swap is None:
                    continue
                if self._hit():
                    self.fired = True
                    new_type = swap[0]
                    node.ops[i] = new_type()
                    # keep descending for completeness
                    break
        return self.generic_visit(node)

    def visit_BoolOp(self, node: ast.BoolOp) -> ast.AST:
        if self.operator == "bool_op_swap":
            if self._hit():
                self.fired = True
                node.op = ast.Or() if isinstance(node.op, ast.And) else ast.And()
        return self.generic_visit(node)

    def visit_Return(self, node: ast.Return) -> ast.AST:
        if self.operator == "return_none":
            val = node.value
            already_none = val is None or (
                isinstance(val, ast.Constant) and val.value is None
            )
            if not already_none:
                if self._hit():
                    self.fired = True
                    node.value = ast.Constant(value=None)
                    return node
        return self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        if id(node) in self.annotation_nodes:
            return node
        if self.operator == "boolean_flip" and isinstance(node.value, bool):
            if self._hit():
                self.fired = True
                return ast.copy_location(ast.Constant(value=not node.value), node)
            return node
        if self.operator == "const_offset" and _const_is_numeric(node):
            if self._hit():
                self.fired = True
                new_node = ast.BinOp(
                    left=ast.Constant(value=node.value),
                    op=ast.Add(),
                    right=ast.Constant(value=1),
                )
                return ast.copy_location(new_node, node)
            return node
        return node


# --------------------------------------------------------------------------- #
# target scoping
# --------------------------------------------------------------------------- #


def _extract_target(tree: ast.Module, target: str) -> ast.AST | None:
    """Return the function/method node named ``target`` (first match in source
    order), or ``None`` if not found. Searches nested functions and methods."""
    found: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == target:
            found.append(node)
    if not found:
        return None
    # Deterministic: pick the one appearing earliest in the source.
    found.sort(key=lambda n: (getattr(n, "lineno", 0), getattr(n, "col_offset", 0)))
    return found[0]


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #


def generate_mutants(
    source: str,
    *,
    target: str | None = None,
    max_mutants: int = 40,
) -> list[Mutant]:
    """Generate up to ``max_mutants`` single-point mutations of ``source``.

    Args:
        source: Python source code to mutate.
        target: Optional name of a function/method. When given, only sites
            *inside* that function are mutated; if no such function exists, an
            empty list is returned.
        max_mutants: Upper bound on the number of returned mutants (applied
            after deterministic sorting).

    Returns:
        A deterministic list of :class:`Mutant`. Empty if ``source`` cannot be
        parsed, if ``target`` is not found, or if there are no mutable sites.
        Each ``mutated_source`` is syntactically valid Python and differs from
        the original at exactly one site.
    """
    if max_mutants <= 0:
        return []

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    # Determine the subtree within which sites are discovered.
    if target is not None:
        scope = _extract_target(tree, target)
        if scope is None:
            return []
    else:
        scope = tree

    sites = _discover_sites(scope)
    if not sites:
        return []

    # The transformer counts occurrences relative to the *scope* tree (so that a
    # targeted function's first comparison is occurrence 0 within that scope).
    # We therefore mutate a fresh parse of the source, then navigate to the same
    # scope inside that fresh tree before running the transformer.

    # Normalized baseline (formatting-immune) used to assert each mutant truly
    # differs from the original.
    baseline = ast.unparse(tree)

    # Each entry pairs a Mutant with its sort key, built together so the two can
    # never drift out of alignment (a site that fails to fire is simply skipped).
    built: list[tuple[tuple[str, int, int, int], Mutant]] = []
    for site in sites:
        fresh_tree = ast.parse(source)
        if target is not None:
            fresh_scope = _extract_target(fresh_tree, target)
            if fresh_scope is None:  # pragma: no cover - source is identical
                continue
        else:
            fresh_scope = fresh_tree

        annotation_nodes = _collect_annotation_nodes(fresh_scope)
        mutator = _SingleSiteMutator(site.operator, site.occurrence, annotation_nodes)
        mutator.visit(fresh_scope)
        if not mutator.fired:  # pragma: no cover - defensive
            continue

        ast.fix_missing_locations(fresh_tree)
        try:
            mutated_source = ast.unparse(fresh_tree)
        except Exception:  # pragma: no cover - unparse should not fail
            continue

        # Guard: the mutated source must differ from the (normalized) original.
        if mutated_source == baseline:  # pragma: no cover - defensive
            continue

        key = (site.operator, site.lineno, site.col_offset, site.occurrence)
        built.append(
            (
                key,
                Mutant(
                    mutated_source=mutated_source,
                    operator=site.operator,
                    lineno=site.lineno,
                    description=site.description,
                ),
            )
        )

    # Deterministic ordering: (operator, lineno, col, occurrence). col/occurrence
    # break ties so the order is fully stable across runs.
    built.sort(key=lambda pair: pair[0])
    ordered = [m for _, m in built]

    return ordered[:max_mutants]
