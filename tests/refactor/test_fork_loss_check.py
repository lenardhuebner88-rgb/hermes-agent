"""Ground truth for fork_loss_check: synthetic repos where the loss is known.

The real fork history is huge, slow and moves under our feet — a test that
asserts a number against it is a test that rots. So every case here is a tiny
git repo built in tmp_path with the answer constructed by hand:

    old_base --> fork_tip        (what the fork added)
             \\-> upstream_tip    (what upstream changed)
                 \\-> current     (the merge result someone has to judge)

The load-bearing case is `test_moved_and_reflowed_fork_lines_are_not_a_loss`:
a tool that calls every reformatting a loss produces exactly the unverifiable
number this script exists to replace.
"""
from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path

import pytest

from scripts.refactor import fork_loss_check as flc

REPO_ROOT = Path(__file__).resolve().parents[2]

# Keep the operator's ~/.gitconfig (signing, hooks, templates) out of fixtures.
_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_AUTHOR_NAME": "fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    "PATH": "/usr/bin:/bin:/usr/local/bin",
}


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True, env=_GIT_ENV,
    )
    return proc.stdout.strip()


def _write(repo: Path, name: str, body: str) -> None:
    (repo / name).write_text(textwrap.dedent(body).lstrip("\n"), encoding="utf-8")


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    # --allow-empty: a merge that simply takes upstream's file wholesale leaves
    # the tree identical to upstream_tip — that is a legitimate fixture state.
    _git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _checkout(repo: Path, ref: str) -> None:
    _git(repo, "checkout", "-q", "--detach", ref)
    _git(repo, "clean", "-fdq")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "fixture-repo"
    path.mkdir()
    _git(path, "init", "-q")
    return path


def _build(repo: Path, base: dict, fork: dict, upstream: dict, merged: dict) -> dict:
    """Lay down the four states; a None body means "delete this file"."""
    def apply(files: dict) -> None:
        for name, body in files.items():
            if body is None:
                (repo / name).unlink(missing_ok=True)
            else:
                _write(repo, name, body)

    apply(base)
    old_base = _commit(repo, "base")
    apply(fork)
    fork_tip = _commit(repo, "fork")
    _checkout(repo, old_base)
    apply(upstream)
    upstream_tip = _commit(repo, "upstream")
    apply(merged)
    current = _commit(repo, "merged")
    return {
        "old_base": old_base,
        "fork_tip": fork_tip,
        "upstream_tip": upstream_tip,
        "current": current,
    }


def _check(repo: Path, refs: dict) -> dict:
    return flc.check(
        repo, refs["old_base"], refs["fork_tip"], refs["current"],
        refs["upstream_tip"],
    )


BASE_MOD = """
    LIMIT = 10


    def upstream_one(value):
        total = value * LIMIT
        return total
"""

FORK_MOD = """
    LIMIT = 10


    def upstream_one(value):
        total = value * LIMIT
        return total


    def fork_helper(payload, retries=3):
        normalised = normalise_payload(payload)
        return dispatch_with_retries(normalised, retries=retries)
"""

UPSTREAM_MOD = """
    LIMIT = 10


    def upstream_one(value, factor=2):
        total = value * LIMIT * factor
        return total
"""


def test_fork_function_kept_by_the_merge_is_not_a_loss(repo):
    merged = """
        LIMIT = 10


        def upstream_one(value, factor=2):
            total = value * LIMIT * factor
            return total


        def fork_helper(payload, retries=3):
            normalised = normalise_payload(payload)
            return dispatch_with_retries(normalised, retries=retries)
    """
    refs = _build(
        repo,
        {"mod.py": BASE_MOD},
        {"mod.py": FORK_MOD},
        {"mod.py": UPSTREAM_MOD},
        {"mod.py": merged},
    )
    report = _check(repo, refs)
    assert report["risk_surface"]["intersection"] == 1
    assert report["findings"] == []
    assert report["stage1"]["missing"] == 0


def test_fork_function_dropped_by_the_merge_is_symbol_gone(repo):
    refs = _build(
        repo,
        {"mod.py": BASE_MOD},
        {"mod.py": FORK_MOD},
        {"mod.py": UPSTREAM_MOD},
        {"mod.py": UPSTREAM_MOD},
    )
    report = _check(repo, refs)
    assert [f["kind"] for f in report["findings"]] == [flc.KIND_SYMBOL_GONE]
    finding = report["findings"][0]
    assert finding["path"] == "mod.py"
    assert finding["symbol"] == "fork_helper"
    assert finding["missing_lines"] == 3


def test_fork_lines_inside_an_upstream_function_are_symbol_changed(repo):
    base = """
        CEILING = 12


        def compute(values):
            total = sum(values)
            return total
    """
    fork = """
        CEILING = 12


        def compute(values):
            total = sum(values)
            total = apply_fork_policy(total, ceiling=CEILING)
            return total
    """
    upstream = """
        CEILING = 12


        def compute(values):
            total = sum(values)
            total = round(total, 4)
            return total
    """
    refs = _build(
        repo, {"calc.py": base}, {"calc.py": fork}, {"calc.py": upstream},
        {"calc.py": upstream},
    )
    report = _check(repo, refs)
    assert [f["kind"] for f in report["findings"]] == [flc.KIND_SYMBOL_CHANGED]
    finding = report["findings"][0]
    assert finding["symbol"] == "compute"
    assert finding["missing_lines"] == 1
    assert "apply_fork_policy" in finding["lines"][0]["text"]


def test_moved_and_reflowed_fork_lines_are_not_a_loss(repo):
    """The false-positive test the whole tool stands or falls on.

    The merge keeps every fork line, but moves the function to the bottom of the
    file, re-indents it into a class and collapses a three-line call onto one
    line. None of that is lost content.
    """
    base = """
        HEADER = "base"


        def upstream_calc(a, b):
            return a + b
    """
    fork = """
        HEADER = "base"


        def fork_policy(alpha, beta):
            result = merge_values(
                alpha,
                beta,
            )
            return result


        def upstream_calc(a, b):
            return a + b
    """
    upstream = """
        HEADER = "base"


        def upstream_calc(a, b, c=0):
            return a + b + c
    """
    merged = """
        HEADER = "base"


        def upstream_calc(a, b, c=0):
            return a + b + c


        class ForkPolicies:
            @staticmethod
            def fork_policy(alpha, beta):
                result = merge_values(alpha, beta)
                return result
    """
    refs = _build(
        repo, {"policy.py": base}, {"policy.py": fork}, {"policy.py": upstream},
        {"policy.py": merged},
    )
    report = _check(repo, refs)
    assert report["findings"] == []
    # The guard must have fired, otherwise this test passes for the wrong
    # reason: `fork_policy` even changed its qualified name to
    # `ForkPolicies.fork_policy`, so a naive symbol check would cry SYMBOL_GONE.
    assert report["stage1"]["moved_or_reflowed"] == 1
    assert report["stage1"]["present"] >= 2


def test_fork_only_file_is_outside_the_risk_surface(repo):
    fork_only = """
        def fork_tool(name):
            return f"tool::{name}"
    """
    refs = _build(
        repo,
        {"mod.py": BASE_MOD},
        {"forkonly.py": fork_only},
        {"mod.py": UPSTREAM_MOD},
        {"mod.py": UPSTREAM_MOD, "forkonly.py": fork_only},
    )
    report = _check(repo, refs)
    assert report["risk_surface"]["intersection"] == 0
    assert report["risk_surface"]["scanned"] == 0
    assert report["risk_surface"]["fork_only"] == 1
    assert report["findings"] == []


def test_fork_only_file_deleted_by_the_merge_is_file_gone(repo):
    fork_only = """
        def fork_tool(name):
            return f"tool::{name}"
    """
    refs = _build(
        repo,
        {"mod.py": BASE_MOD},
        {"forkonly.py": fork_only},
        {"mod.py": UPSTREAM_MOD},
        {"mod.py": UPSTREAM_MOD, "forkonly.py": None},
    )
    report = _check(repo, refs)
    assert [(f["kind"], f["path"]) for f in report["findings"]] == [
        (flc.KIND_FILE_GONE, "forkonly.py")
    ]
    assert report["findings"][0]["scope"] == "fork_only"


def test_file_deleted_upstream_but_changed_by_the_fork_is_reported(repo):
    base = """
        def legacy(entry):
            return entry
    """
    fork = """
        def legacy(entry):
            entry = annotate_for_fork(entry, source="fork")
            return entry
    """
    refs = _build(
        repo,
        {"mod.py": BASE_MOD, "legacy.py": base},
        {"legacy.py": fork},
        {"legacy.py": None},
        {"legacy.py": None},
    )
    report = _check(repo, refs)
    assert [(f["kind"], f["path"]) for f in report["findings"]] == [
        (flc.KIND_FILE_GONE, "legacy.py")
    ]
    assert report["findings"][0]["scope"] == "risk_surface"
    assert report["findings"][0]["missing_lines"] == 1


def test_file_deleted_by_the_fork_itself_is_not_a_loss(repo):
    """Found by the first real run against the 1962-commit sync.

    `tests/hermes_cli/test_kanban_core_functionality.py` was deleted BY THE FORK
    and upstream had also touched it, so it landed in the risk surface and was
    reported FILE_GONE — a phantom finding. Absence the fork asked for is not
    loss, whether or not upstream also touched the file.
    """
    doomed = """
        def doomed(entry):
            return entry
    """
    refs = _build(
        repo,
        {"mod.py": BASE_MOD, "doomed.py": doomed, "forkdrop.py": doomed},
        {"doomed.py": None, "forkdrop.py": None},
        {"mod.py": UPSTREAM_MOD, "doomed.py": doomed + "\n\nEXTRA = 1\n"},
        {"mod.py": UPSTREAM_MOD, "doomed.py": None, "forkdrop.py": None},
    )
    report = _check(repo, refs)
    assert report["findings"] == []
    assert report["risk_surface"]["fork_deleted"] == 2
    assert report["risk_surface"]["scanned"] == 0
    assert report["risk_surface"]["fork_only"] == 0


def test_non_python_file_falls_back_to_line_only(repo):
    refs = _build(
        repo,
        {"notes.md": "# Notes\n\nupstream paragraph about defaults\n"},
        {"notes.md": "# Notes\n\nupstream paragraph about defaults\n"
                     "\nfork paragraph about the retry ceiling\n"},
        {"notes.md": "# Notes\n\nupstream rewrote the defaults paragraph\n"},
        {"notes.md": "# Notes\n\nupstream rewrote the defaults paragraph\n"},
    )
    report = _check(repo, refs)
    assert [f["kind"] for f in report["findings"]] == [flc.KIND_LINE_ONLY]
    assert report["findings"][0]["symbol"] is None


def test_cli_exit_codes_and_json(repo, tmp_path, capsys):
    refs = _build(
        repo,
        {"mod.py": BASE_MOD},
        {"mod.py": FORK_MOD},
        {"mod.py": UPSTREAM_MOD},
        {"mod.py": UPSTREAM_MOD},
    )
    out = tmp_path / "report.json"
    code = flc.main([
        "--repo", str(repo), "--old-base", refs["old_base"],
        "--fork-tip", refs["fork_tip"], "--current", refs["current"],
        "--upstream-tip", refs["upstream_tip"], "--json", str(out),
    ])
    assert code == 1
    assert "SYMBOL_GONE" in capsys.readouterr().out
    assert json.loads(out.read_text(encoding="utf-8"))["counts"]["SYMBOL_GONE"] == 1

    clean = flc.main([
        "--repo", str(repo), "--old-base", refs["old_base"],
        "--fork-tip", refs["fork_tip"], "--current", refs["fork_tip"],
        "--upstream-tip", refs["upstream_tip"],
    ])
    assert clean == 0

    bad = flc.main([
        "--repo", str(repo), "--old-base", "does-not-exist",
        "--fork-tip", refs["fork_tip"], "--current", refs["current"],
        "--upstream-tip", refs["upstream_tip"],
    ])
    assert bad == 2


def test_smoke_against_real_history_terminates_with_wellformed_json(tmp_path):
    """No number is nailed down here — only that a real run terminates."""
    try:
        old_base = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD~4"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except subprocess.CalledProcessError:  # pragma: no cover - shallow clone
        pytest.skip("history too short for the smoke run")

    out = tmp_path / "smoke.json"
    code = flc.main([
        "--repo", str(REPO_ROOT), "--old-base", old_base, "--fork-tip", "HEAD",
        "--current", "HEAD", "--upstream-tip", "HEAD~2",
        "--limit-paths", "5", "--json", str(out),
    ])
    assert code in (0, 1)
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["refs"]["old_base"] == old_base
    assert report["risk_surface"]["scanned"] <= 5
    assert isinstance(report["findings"], list)
    assert set(report["counts"]) == set(flc.SEVERITY)
