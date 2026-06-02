"""Unit tests for the AST-based code mutator.

Plain pytest, standard library only. Run with:

    cd /home/piet/orchestration/scratch/ast-mutator && python3 -m pytest test_ast_mutator.py -q
"""

import ast
import textwrap

import pytest

from hermes_cli._ast_mutator import Mutant, generate_mutants


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _ops(mutants):
    return {m.operator for m in mutants}


def _by_op(mutants, operator):
    return [m for m in mutants if m.operator == operator]


def _normalize(src):
    """Re-parse + unparse to compare modulo formatting."""
    return ast.unparse(ast.parse(src))


def _contains_stmt(mutated_source, snippet):
    """True if a normalized form of ``snippet`` appears in the (re-normalized)
    mutated source. Robust against whitespace/formatting differences."""
    norm_mut = _normalize(mutated_source)
    norm_snip = ast.unparse(ast.parse(snippet)).strip()
    return norm_snip in norm_mut


def _all_valid(mutants):
    for m in mutants:
        ast.parse(m.mutated_source)  # raises SyntaxError if invalid


# --------------------------------------------------------------------------- #
# per-operator tests
# --------------------------------------------------------------------------- #


def test_negate_if():
    src = textwrap.dedent(
        """
        def f(x):
            if x > 0:
                return 1
            return 0
        """
    )
    mutants = generate_mutants(src)
    neg = _by_op(mutants, "negate_if")
    assert neg, "expected at least one negate_if mutant"
    # The condition x > 0 must become not (x > 0) somewhere.
    assert any(
        "not (x > 0)" in _normalize(m.mutated_source)
        or "not x > 0" in _normalize(m.mutated_source)
        for m in neg
    )
    _all_valid(mutants)


def test_negate_if_skips_dunder_main():
    src = textwrap.dedent(
        """
        def f():
            return 1

        if __name__ == "__main__":
            f()
        """
    )
    mutants = generate_mutants(src)
    # No negate_if should target the __name__ guard. There are no other ifs,
    # so there must be zero negate_if mutants.
    assert _by_op(mutants, "negate_if") == []
    # And comparison_swap must also not touch __name__ == "__main__"? The spec
    # only forbids negate_if there; comparison_swap on it is allowed. So we do
    # not assert against comparison_swap here.


def test_negate_if_elif():
    src = textwrap.dedent(
        """
        def f(x):
            if x == 1:
                return "a"
            elif x == 2:
                return "b"
            return "c"
        """
    )
    mutants = generate_mutants(src)
    neg = _by_op(mutants, "negate_if")
    # Both the if and the elif are If nodes -> 2 negate_if candidates.
    assert len(neg) == 2
    _all_valid(mutants)


def test_comparison_swap():
    src = textwrap.dedent(
        """
        def f(a, b):
            return a == b
        """
    )
    mutants = generate_mutants(src)
    cmp = _by_op(mutants, "comparison_swap")
    assert cmp, "expected a comparison_swap mutant"
    assert any(_contains_stmt(m.mutated_source, "a != b") for m in cmp)
    _all_valid(mutants)


def test_comparison_swap_all_variants():
    src = textwrap.dedent(
        """
        def f(a, b):
            r1 = a < b
            r2 = a <= b
            r3 = a > b
            r4 = a >= b
            r5 = a is b
            r6 = a is not b
            return r1
        """
    )
    mutants = generate_mutants(src)
    bodies = [_normalize(m.mutated_source) for m in _by_op(mutants, "comparison_swap")]
    joined = "\n".join(bodies)
    assert "a <= b" in joined  # from a < b
    assert "a < b" in joined   # from a <= b
    assert "a >= b" in joined  # from a > b
    assert "a > b" in joined   # from a >= b
    assert "a is not b" in joined  # from a is b
    assert "a is b" in joined       # from a is not b
    _all_valid(mutants)


def test_bool_op_swap():
    src = textwrap.dedent(
        """
        def f(a, b):
            return a and b
        """
    )
    mutants = generate_mutants(src)
    bo = _by_op(mutants, "bool_op_swap")
    assert bo
    assert any(_contains_stmt(m.mutated_source, "a or b") for m in bo)
    _all_valid(mutants)


def test_bool_op_swap_or_to_and():
    src = textwrap.dedent(
        """
        def f(a, b):
            return a or b
        """
    )
    mutants = generate_mutants(src)
    bo = _by_op(mutants, "bool_op_swap")
    assert any(_contains_stmt(m.mutated_source, "a and b") for m in bo)


def test_const_offset():
    src = textwrap.dedent(
        """
        def f():
            x = 10
            return x
        """
    )
    mutants = generate_mutants(src)
    co = _by_op(mutants, "const_offset")
    assert co
    assert any(_contains_stmt(m.mutated_source, "x = 10 + 1") for m in co)
    _all_valid(mutants)


def test_const_offset_float():
    src = textwrap.dedent(
        """
        def f():
            return 3.5
        """
    )
    mutants = generate_mutants(src)
    co = _by_op(mutants, "const_offset")
    assert any("3.5 + 1" in _normalize(m.mutated_source) for m in co)


def test_const_offset_ignores_bool():
    # True/False are int subclasses; they must be handled by boolean_flip, not
    # const_offset.
    src = textwrap.dedent(
        """
        def f():
            return True
        """
    )
    mutants = generate_mutants(src)
    assert _by_op(mutants, "const_offset") == []
    assert _by_op(mutants, "boolean_flip")


def test_const_offset_not_in_annotation():
    src = textwrap.dedent(
        """
        def f(x: int = 5) -> int:
            y: int = 7
            return y
        """
    )
    mutants = generate_mutants(src)
    co = _by_op(mutants, "const_offset")
    bodies = "\n".join(_normalize(m.mutated_source) for m in co)
    # default value 5 and assigned 7 are mutable; annotation 'int' has no
    # numeric constants. Both 5 and 7 should be offset somewhere.
    assert "5 + 1" in bodies
    assert "7 + 1" in bodies
    _all_valid(mutants)


def test_return_none():
    src = textwrap.dedent(
        """
        def f(x):
            return x + 1
        """
    )
    mutants = generate_mutants(src)
    rn = _by_op(mutants, "return_none")
    assert rn
    assert any(_contains_stmt(m.mutated_source, "return None") for m in rn)
    _all_valid(mutants)


def test_return_none_skips_existing_none():
    src = textwrap.dedent(
        """
        def f():
            return
        def g():
            return None
        """
    )
    mutants = generate_mutants(src)
    # Neither bare `return` nor `return None` should yield a return_none mutant.
    assert _by_op(mutants, "return_none") == []


def test_remove_guard_raise():
    src = textwrap.dedent(
        """
        def f(x):
            if x < 0:
                raise ValueError("neg")
            return x
        """
    )
    mutants = generate_mutants(src)
    rg = _by_op(mutants, "remove_guard")
    assert rg, "expected a remove_guard mutant"
    # The guard if-block should be gone; in its place a `pass`. The raise must
    # not appear in that mutant.
    guard_mut = rg[0]
    norm = _normalize(guard_mut.mutated_source)
    assert "raise ValueError" not in norm
    assert "pass" in norm
    _all_valid(mutants)


def test_remove_guard_return():
    src = textwrap.dedent(
        """
        def f(x):
            if x is None:
                return -1
            return x
        """
    )
    mutants = generate_mutants(src)
    rg = _by_op(mutants, "remove_guard")
    assert rg
    # After removing the guard, `return -1` should not be reachable in the
    # mutated guard position (it is replaced by pass).
    assert any("pass" in _normalize(m.mutated_source) for m in rg)
    _all_valid(mutants)


def test_remove_guard_skips_multiline_body():
    src = textwrap.dedent(
        """
        def f(x):
            if x < 0:
                y = 1
                raise ValueError(y)
            return x
        """
    )
    mutants = generate_mutants(src)
    # Body has 2 statements -> not a simple single-line guard -> no remove_guard.
    assert _by_op(mutants, "remove_guard") == []


def test_remove_guard_skips_if_with_else():
    src = textwrap.dedent(
        """
        def f(x):
            if x < 0:
                raise ValueError()
            else:
                return x
        """
    )
    mutants = generate_mutants(src)
    assert _by_op(mutants, "remove_guard") == []


def test_boolean_flip():
    src = textwrap.dedent(
        """
        def f():
            flag = True
            return flag
        """
    )
    mutants = generate_mutants(src)
    bf = _by_op(mutants, "boolean_flip")
    assert bf
    assert any(_contains_stmt(m.mutated_source, "flag = False") for m in bf)
    _all_valid(mutants)


def test_boolean_flip_false_to_true():
    src = textwrap.dedent(
        """
        def f():
            return False
        """
    )
    mutants = generate_mutants(src)
    bf = _by_op(mutants, "boolean_flip")
    assert any("return True" in _normalize(m.mutated_source) for m in bf)


def test_boolean_flip_not_in_annotation():
    # A bool literal default IS mutable; one inside an annotation is not. Use a
    # Literal-style annotation to ensure annotation bools are skipped.
    src = textwrap.dedent(
        """
        from typing import Literal
        def f(x: Literal[True] = False) -> bool:
            return x
        """
    )
    mutants = generate_mutants(src)
    bf = _by_op(mutants, "boolean_flip")
    # Exactly one bool is mutable (the default `False`); the annotation's True
    # must be skipped.
    assert len(bf) == 1
    norm = _normalize(bf[0].mutated_source)
    # The default `False` became True (ast.unparse renders defaults as `=True`).
    assert "Literal[True]=True" in norm or "x=True" in norm
    # The annotation Literal[True] must remain True -> never Literal[False].
    bodies = "\n".join(_normalize(m.mutated_source) for m in bf)
    assert "Literal[False]" not in bodies
    _all_valid(mutants)


# --------------------------------------------------------------------------- #
# target scoping
# --------------------------------------------------------------------------- #


def test_target_scoping_only_inside_function():
    src = textwrap.dedent(
        """
        def outside(a, b):
            return a == b

        def inside(a, b):
            return a < b
        """
    )
    mutants = generate_mutants(src, target="inside")
    assert mutants, "expected mutants inside the target function"
    joined = "\n".join(_normalize(m.mutated_source) for m in mutants)
    # comparison from `inside` (a < b -> a <= b) should appear...
    assert "a <= b" in joined
    # ...and the `outside` comparison (a == b) must NOT be mutated to a != b.
    assert "a != b" not in joined
    _all_valid(mutants)


def test_target_scoping_method():
    src = textwrap.dedent(
        """
        class C:
            def method(self, x):
                if x > 0:
                    return 1
                return 0

            def other(self, x):
                return x == 5
        """
    )
    mutants = generate_mutants(src, target="method")
    assert mutants
    joined = "\n".join(_normalize(m.mutated_source) for m in mutants)
    # `other`'s comparison x == 5 must not be mutated.
    assert "x != 5" not in joined
    # method's `if x > 0` should be negatable.
    assert any(m.operator == "negate_if" for m in mutants)
    _all_valid(mutants)


def test_target_not_found_returns_empty():
    src = "def f():\n    return 1\n"
    assert generate_mutants(src, target="does_not_exist") == []


# --------------------------------------------------------------------------- #
# global properties
# --------------------------------------------------------------------------- #


def test_all_mutants_are_valid_python():
    src = textwrap.dedent(
        """
        def classify(n, flag):
            if n == 0 and flag:
                return None
            if n < 0:
                raise ValueError("neg")
            total = n + 10
            if flag is True:
                return total
            return total > 5
        """
    )
    mutants = generate_mutants(src)
    assert mutants
    for m in mutants:
        ast.parse(m.mutated_source)  # must not raise


def test_determinism_same_order_and_count():
    src = textwrap.dedent(
        """
        def classify(n, flag):
            if n == 0 and flag:
                return None
            if n < 0:
                raise ValueError("neg")
            total = n + 10
            if flag is True:
                return total
            return total > 5
        """
    )
    run1 = generate_mutants(src)
    run2 = generate_mutants(src)
    assert len(run1) == len(run2)
    sig1 = [(m.operator, m.lineno, m.description, m.mutated_source) for m in run1]
    sig2 = [(m.operator, m.lineno, m.description, m.mutated_source) for m in run2]
    assert sig1 == sig2


def test_deterministic_sort_order():
    src = textwrap.dedent(
        """
        def f(a, b):
            x = a > b
            y = a < b
            return x and y
        """
    )
    mutants = generate_mutants(src)
    keys = [(m.operator, m.lineno) for m in mutants]
    # Must be sorted primarily by operator name, then lineno.
    assert keys == sorted(keys)


def test_max_mutants_cap():
    # Generate a source with many mutable sites.
    lines = ["def f():"]
    for i in range(50):
        lines.append(f"    x{i} = {i} == {i}")
    lines.append("    return 0")
    src = "\n".join(lines)

    full = generate_mutants(src, max_mutants=1000)
    assert len(full) > 5  # sanity: plenty of sites

    capped = generate_mutants(src, max_mutants=5)
    assert len(capped) == 5
    # The cap is a prefix of the deterministically sorted full list.
    full_sigs = [(m.operator, m.lineno, m.mutated_source) for m in full]
    capped_sigs = [(m.operator, m.lineno, m.mutated_source) for m in capped]
    assert capped_sigs == full_sigs[:5]


def test_max_mutants_zero_or_negative():
    src = "def f():\n    return 1 == 1\n"
    assert generate_mutants(src, max_mutants=0) == []
    assert generate_mutants(src, max_mutants=-3) == []


def test_broken_source_returns_empty():
    assert generate_mutants("def f(:\n    pass") == []
    assert generate_mutants("this is (((not python") == []
    assert generate_mutants("    return 1  # leading indent, no def") == []


def test_empty_source_returns_empty():
    assert generate_mutants("") == []
    assert generate_mutants("\n\n# just a comment\n") == []


def test_original_never_returned_unmutated():
    src = textwrap.dedent(
        """
        def f(a, b):
            if a == b:
                return a + 1
            return b
        """
    )
    baseline = _normalize(src)
    mutants = generate_mutants(src)
    assert mutants
    for m in mutants:
        assert _normalize(m.mutated_source) != baseline, (
            f"mutant for operator {m.operator} is identical to original"
        )


def test_each_mutant_changes_exactly_one_site():
    # A source with two identical comparisons; each comparison_swap mutant must
    # change exactly one of them, never both.
    src = textwrap.dedent(
        """
        def f(a, b):
            p = a == b
            q = a == b
            return p and q
        """
    )
    mutants = generate_mutants(src)
    cmp_mutants = _by_op(mutants, "comparison_swap")
    # There are exactly 2 `==` comparisons -> 2 comparison_swap mutants.
    assert len(cmp_mutants) == 2
    for m in cmp_mutants:
        norm = _normalize(m.mutated_source)
        # Exactly one `!=` and exactly one remaining `==`.
        assert norm.count("!=") == 1
        assert norm.count("==") == 1


def test_mutant_dataclass_fields():
    src = "def f(x):\n    return x == 1\n"
    mutants = generate_mutants(src)
    assert mutants
    m = mutants[0]
    assert isinstance(m, Mutant)
    assert isinstance(m.mutated_source, str)
    assert isinstance(m.operator, str)
    assert isinstance(m.lineno, int) and m.lineno >= 1
    assert isinstance(m.description, str) and m.description


def test_lineno_is_one_based_and_plausible():
    src = textwrap.dedent(
        """
        def f(x):
            return x == 1
        """
    ).strip()  # so `def` is line 1, return is line 2
    mutants = generate_mutants(src)
    cmp = _by_op(mutants, "comparison_swap")
    assert cmp
    # The comparison is on line 2.
    assert all(m.lineno == 2 for m in cmp)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
