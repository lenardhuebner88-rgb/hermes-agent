"""Mutation-test based test hardening for Hermes.

The foundry never mutates the live checkout. Each run creates a disposable
git worktree from HEAD, applies mutants there, and persists only validated test
proposals unless an explicit apply branch is requested.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from hermes_cli._ast_mutator import Mutant, generate_mutants

_REPO = Path(__file__).resolve().parents[1]
_DEFAULT_AUDIT = _REPO / ".hermes" / "skill-audit"
_FOUNDRY_SCHEMA = "test-foundry-result-v1"
_REQUEST_SCHEMA = "test-foundry-request-v1"
_STATUS_SCHEMA = "test-foundry-status-v1"
_MAX_MUTANTS_HARD_CAP = 100
_DEFAULT_APPLY_BRANCH = "f-test-foundry"
_FORBIDDEN_APPLY_BRANCHES = {"main", "master", "f-autoresearch-v2"}
_TARGETS = (
    "hermes_cli/kanban_db.py",
    "hermes_state.py",
    "hermes_cli/kanban.py",
    "hermes_cli/kanban_decompose.py",
    "tools/kanban_tools.py",
)
_SOURCE_INSPECTION_PATTERNS = (
    "inspect.getsource",
    "getsource(",
    "ast.parse",
    "ast.walk",
    "ast.nodevisitor",
    "ast.nodetransformer",
    "open(",
    "read_text(",
    ".read(",
)

SuiteRunner = Callable[..., Any]
LlmCall = Callable[..., Any]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _audit_dir() -> Path:
    override = os.environ.get("HERMES_AUTORESEARCH_AUDIT_DIR")
    return Path(override) if override else _DEFAULT_AUDIT


def _state_dir() -> Path:
    override = os.environ.get("HERMES_TEST_FOUNDRY_STATE_DIR")
    return Path(override) if override else (_audit_dir() / "test-foundry-state")


def _requests_dir() -> Path:
    return _state_dir() / "requests"


def curated_targets() -> list[str]:
    return list(_TARGETS)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        if not path.exists() or path.stat().st_size == 0:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _lock_path() -> Path:
    return _state_dir() / "current.lock"


def _status_path() -> Path:
    return _state_dir() / "current.status"


def write_lock(*, target: str, pid: int) -> None:
    _write_json(_lock_path(), {"pid": pid, "target": target, "started_at": _utc_now()})


def clear_lock() -> None:
    try:
        _lock_path().unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def read_status() -> dict[str, Any]:
    lock = _read_json(_lock_path())
    status = _read_json(_status_path()) or {}
    return {
        "schema": _STATUS_SCHEMA,
        "state": "running" if lock else str(status.get("state") or "idle"),
        "pid": (lock or {}).get("pid"),
        "target": (lock or {}).get("target") or status.get("target"),
        "started_at": (lock or {}).get("started_at"),
        "last_run": status.get("last_run"),
    }


def write_request(*, target: str | None = None, max_mutants: int = 30, apply: bool = False) -> dict[str, Any]:
    target = target or _TARGETS[0]
    if target not in _TARGETS:
        raise ValueError(f"unsupported test-foundry target: {target}")
    request_id = f"test-foundry-{int(time.time())}-{hashlib.sha1(target.encode()).hexdigest()[:8]}"
    request_path = _requests_dir() / f"{request_id}.json"
    request = {
        "schema": _REQUEST_SCHEMA,
        "request_id": request_id,
        "target": target,
        "max_mutants": max(1, min(int(max_mutants or 30), _MAX_MUTANTS_HARD_CAP)),
        "apply": bool(apply),
        "created_at": _utc_now(),
        "request_path": str(request_path),
    }
    _write_json(request_path, request)
    return request


def run_request_file(path: Path) -> dict[str, Any]:
    request = _read_json(path)
    if not request:
        result = _empty_result("", reason=f"request file unreadable: {path}")
        _write_json(_status_path(), {"state": "error", "target": "", "last_run": result})
        return result
    target = str(request.get("target") or "")
    pid = os.getpid()
    write_lock(target=target, pid=pid)
    try:
        apply_branch = _DEFAULT_APPLY_BRANCH if request.get("apply") else None
        result = run_test_foundry(
            target,
            max_mutants=int(request.get("max_mutants") or 30),
            apply_branch=apply_branch,
        )
        _write_json(_status_path(), {"state": "idle", "target": target, "last_run": result, "updated_at": _utc_now()})
        return result
    finally:
        clear_lock()


def _target_relpath(target_module: str) -> str:
    raw = Path(str(target_module))
    if raw.is_absolute():
        try:
            return raw.resolve().relative_to(_REPO.resolve()).as_posix()
        except ValueError as exc:
            raise ValueError("target_module must be inside the repo") from exc
    rel = raw.as_posix().lstrip("/")
    if rel.startswith("../") or rel == ".." or "/../" in rel:
        raise ValueError("target_module must be repo-relative")
    return rel


def _target_is_clean(rel_target: str) -> bool:
    proc = subprocess.run(
        ["git", "status", "--porcelain", "--", rel_target],
        cwd=_REPO,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return proc.returncode == 0 and proc.stdout.strip() == ""


def _create_worktree() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="hermes-test-foundry-wt-"))
    try:
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(tmp), "HEAD"],
            cwd=_REPO,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return tmp


def _remove_worktree(path: Path | None) -> None:
    if path is None:
        return
    try:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(path)],
            cwd=_REPO,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _create_hermes_home() -> Path:
    return Path(tempfile.mkdtemp(prefix="hermes-test-foundry-home-"))


def _remove_hermes_home(path: Path | None) -> None:
    if path is not None:
        shutil.rmtree(path, ignore_errors=True)


def _affected_tests(root: Path, target_module: str) -> list[str]:
    stem = Path(target_module).stem
    name = f"test_{stem}.py"
    tests_root = root / "tests"
    if not tests_root.exists():
        return []
    matches = sorted(
        p.relative_to(root).as_posix()
        for p in tests_root.rglob(name)
        if p.is_file()
    )
    return matches


def _foundry_test_relpath(target_module: str) -> str:
    stem = Path(target_module).stem
    return f"tests/test_{stem}_foundry.py"


def _default_run_suite(paths: list[str], *, cwd: Path, env: dict[str, str]) -> int:
    proc = subprocess.run(
        ["bash", "scripts/run_tests.sh", *paths],
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    return proc.returncode


def _suite_passed(result: Any) -> bool:
    if isinstance(result, bool):
        return result
    if isinstance(result, int):
        return result == 0
    if isinstance(result, dict):
        for key in ("ok", "passed", "success"):
            if key in result:
                return bool(result[key])
        if "returncode" in result:
            return int(result["returncode"]) == 0
    code = getattr(result, "returncode", None)
    if code is not None:
        return int(code) == 0
    return bool(result)


def _invoke_run_suite(run_suite: SuiteRunner | None, paths: list[str], *, cwd: Path, env: dict[str, str]) -> bool:
    runner = run_suite or _default_run_suite
    try:
        result = runner(paths, cwd=cwd, env=env)
    except TypeError:
        result = runner(paths)
    return _suite_passed(result)


def _default_llm_call(**kwargs: Any) -> Any:
    from agent.auxiliary_client import call_llm

    return call_llm(**kwargs)


def _response_text_tokens_model(response: Any) -> tuple[str, int, str | None]:
    if isinstance(response, str):
        return response, 0, None
    model = str(getattr(response, "model", "") or "") or None
    tokens = 0
    usage = getattr(response, "usage", None)
    if usage is not None:
        tokens = int(getattr(usage, "total_tokens", 0) or 0)
    choices = getattr(response, "choices", None)
    if choices:
        message = getattr(choices[0], "message", None)
        content = getattr(message, "content", None)
        if isinstance(content, str):
            return content, tokens, model
    if isinstance(response, dict):
        tokens = int(response.get("tokens") or response.get("total_tokens") or tokens)
        model = str(response.get("model") or model or "") or None
        content = response.get("test_code") or response.get("content") or response.get("text")
        if isinstance(content, str):
            return content, tokens, model
    return str(response or ""), tokens, model


def _extract_test_code(text: str) -> str:
    stripped = text.strip()
    try:
        data = json.loads(stripped)
    except ValueError:
        data = None
    if isinstance(data, dict) and isinstance(data.get("test_code"), str):
        return data["test_code"].strip() + "\n"
    fence = re.search(r"```(?:python|py)?\s*(.*?)```", stripped, re.DOTALL | re.IGNORECASE)
    if fence:
        return fence.group(1).strip() + "\n"
    return stripped + ("\n" if stripped else "")


def _has_source_inspection(test_code: str, target_module: str) -> bool:
    lowered = test_code.lower()
    if any(pattern in lowered for pattern in _SOURCE_INSPECTION_PATTERNS):
        return True
    target_bits = {target_module.lower(), Path(target_module).name.lower(), Path(target_module).stem.lower()}
    return any(bit in lowered for bit in target_bits) and any(token in lowered for token in ("pathlib", "__file__"))


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _mutant_summary(mutant: Mutant, index: int) -> dict[str, Any]:
    return {
        "index": index,
        "operator": mutant.operator,
        "lineno": mutant.lineno,
        "description": mutant.description,
    }


def _make_diff(original: str, mutated: str, rel_target: str) -> str:
    return "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            mutated.splitlines(keepends=True),
            fromfile=f"a/{rel_target}",
            tofile=f"b/{rel_target}",
        )
    )


def _test_prompt(*, target_module: str, source: str, mutant: Mutant, diff: str, affected_tests: list[str]) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "You write one pytest test that checks public behavior. "
                "Do not inspect source text, AST, files, or implementation internals. "
                "Return only the pytest code."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Target module: {target_module}\n"
                f"Affected tests: {', '.join(affected_tests)}\n"
                f"Mutation: {mutant.operator} at line {mutant.lineno}: {mutant.description}\n\n"
                f"Survivor diff:\n{diff}\n\n"
                f"Current target source:\n```python\n{source}\n```\n\n"
                "Write exactly one pytest test function. It must pass on HEAD and fail on this mutant."
            ),
        },
    ]


def _call_hardening_llm(
    llm_call: LlmCall | None,
    *,
    target_module: str,
    source: str,
    mutant: Mutant,
    diff: str,
    affected_tests: list[str],
) -> tuple[str, int, str | None]:
    caller = llm_call or _default_llm_call
    response = caller(
        task="test_hardening",
        messages=_test_prompt(
            target_module=target_module,
            source=source,
            mutant=mutant,
            diff=diff,
            affected_tests=affected_tests,
        ),
        temperature=0,
        max_tokens=3000,
    )
    text, tokens, model = _response_text_tokens_model(response)
    return _extract_test_code(text), tokens, model


def _proposal_id(target_module: str, mutant: Mutant, test_code: str) -> str:
    digest = hashlib.sha256(
        f"{target_module}\0{mutant.operator}\0{mutant.lineno}\0{mutant.description}\0{test_code}".encode()
    ).hexdigest()[:16]
    stem = re.sub(r"[^a-z0-9]+", "-", Path(target_module).stem.lower()).strip("-")
    return f"test-foundry-{stem}-{digest}"


def _save_test_proposal(
    *,
    target_module: str,
    test_code: str,
    mutant: Mutant,
    mutant_index: int,
    affected_tests: list[str],
    diff: str,
    model: str | None,
) -> str:
    from hermes_cli import autoresearch_proposals

    pid = _proposal_id(target_module, mutant, test_code)
    proposal = {
        "id": pid,
        "schema": autoresearch_proposals.PROPOSAL_SCHEMA,
        "mode": "test",
        "proposal_type": "mutation_test",
        "target": target_module,
        "target_path": str((_REPO / target_module).resolve()),
        "section": None,
        "eval_label": None,
        "category": "mutation_survivor",
        "severity": "medium",
        "evidence": mutant.description,
        "fix_hint": "Add the generated pytest test to cover the surviving mutation.",
        "title": f"Mutation survivor in {target_module}:{mutant.lineno}",
        "rationale_plain": f"A {mutant.operator} mutation survived the affected suite.",
        "before_text": None,
        "after_text": None,
        "new_text": test_code,
        "test_code": test_code,
        "writer": "test-foundry",
        "writer_rationale": f"{model or 'aux-model'} via test_hardening; gated green@HEAD and red@mutant.",
        "diff_before_after": diff,
        "status": "proposed",
        "last_outcome": None,
        "created_at": _utc_now(),
        "applied_at": None,
        "result": None,
        "apply_blocked_reason": "Use the test-foundry apply branch gate to apply automatically.",
        "rank_score": 20.0,
        "rank_reason": "mutation survivor caught by generated public-behavior pytest",
        "caught_mutant": _mutant_summary(mutant, mutant_index),
        "affected_tests": affected_tests,
    }
    autoresearch_proposals.save_proposal(proposal)
    return pid


def _annotate_applied_proposals(
    proposal_ids: list[str],
    *,
    branch: str | None,
    commit: str | None,
    test_file: str | None,
) -> None:
    """Record where auto-applied tests landed on the stored proposals.

    Visibility only: after the foundry commits the kept tests on its apply
    branch, stamp each contributing proposal with the branch/commit so the
    operator can find the auto-applied tests. The proposal stays out of the
    manual autoresearch apply-gate (its ``apply_blocked_reason`` is retained and
    ``status`` flips to ``applied``)."""
    from hermes_cli import autoresearch_proposals

    short = (commit or "")[:12] or None
    where = f"branch {branch}" + (f" @ {short}" if short else "")
    for pid in proposal_ids:
        try:
            proposal = autoresearch_proposals.load_proposal(pid)
            if proposal is None:
                continue
            proposal["status"] = "applied"
            proposal["last_outcome"] = "applied"
            proposal["applied_at"] = _utc_now()
            proposal["apply_branch"] = branch
            proposal["apply_commit"] = commit
            proposal["apply_test_file"] = test_file
            proposal["result"] = f"✓ auto-applied on {where}"
            autoresearch_proposals.save_proposal(proposal)
        except Exception:
            continue


def _write_generated_test(worktree: Path, rel_test: str, test_code: str) -> None:
    _write_text(worktree / rel_test, test_code)


def _remove_generated_test(worktree: Path, rel_test: str) -> None:
    try:
        (worktree / rel_test).unlink()
    except FileNotFoundError:
        pass


_TEST_DEF_RE = re.compile(r"^(\s*def\s+)(test_[A-Za-z0-9_]*)(\s*\()", re.MULTILINE)


def _suffix_token(raw: str) -> str:
    """Sanitize an arbitrary string into a Python-identifier-safe suffix fragment."""
    token = re.sub(r"[^A-Za-z0-9_]+", "_", str(raw)).strip("_")
    return token or "x"


def _uniquify_test_names(test_code: str, suffix: str) -> str:
    """Rewrite every top-level ``def test_...(`` in *test_code* so its name ends
    with ``__<suffix>``.

    Combining several LLM-generated test blocks via ``"\\n\\n".join`` silently drops
    a test if two blocks define an identically named function (e.g. both emit
    ``def test_foundry_public_behavior``). Appending a stable per-block suffix
    guarantees each kept block contributes a distinct, collision-free test name.
    Only ``test_*`` function definitions are touched; helpers, imports, fixtures
    and assertions are left exactly as-is.
    """
    safe = _suffix_token(suffix)

    def _rename(match: re.Match[str]) -> str:
        return f"{match.group(1)}{match.group(2)}__{safe}{match.group(3)}"

    return _TEST_DEF_RE.sub(_rename, test_code)


def _apply_branch_commit(
    *,
    worktree: Path,
    branch: str,
    target_module: str,
    test_code: str,
    affected_tests: list[str],
    run_suite: SuiteRunner | None,
    env: dict[str, str],
) -> dict[str, Any]:
    if branch in _FORBIDDEN_APPLY_BRANCHES:
        return {"ok": False, "reason": f"refusing to commit on protected branch {branch!r}"}
    rel_test = _foundry_test_relpath(target_module)
    subprocess.run(["git", "switch", "-C", branch], cwd=worktree, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    _write_generated_test(worktree, rel_test, test_code)
    gate_paths = sorted(set([*affected_tests, rel_test]))
    if not _invoke_run_suite(run_suite, gate_paths, cwd=worktree, env=env):
        return {"ok": False, "reason": "apply branch regate failed", "test_file": rel_test}
    subprocess.run(["git", "add", rel_test], cwd=worktree, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    commit = subprocess.run(
        ["git", "commit", "-m", f"codex: add mutation test for {Path(target_module).stem}"],
        cwd=worktree,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    sha: str | None = None
    if commit.returncode == 0:
        rev = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if rev.returncode == 0:
            sha = rev.stdout.strip() or None
    return {
        "ok": commit.returncode == 0,
        "branch": branch,
        "commit": sha,
        "test_file": rel_test,
        "reason": "" if commit.returncode == 0 else (commit.stderr.strip() or commit.stdout.strip()),
    }


def _empty_result(target: str, *, reason: str) -> dict[str, Any]:
    return {
        "schema": _FOUNDRY_SCHEMA,
        "ok": False,
        "target": target,
        "survivors": [],
        "tests_kept": 0,
        "proposals": [],
        "reason": reason,
        "tokens": 0,
        "model": None,
        "mutants_run": 0,
    }


def _record_roi(*, tokens: int, tests_kept: int, model: str | None, mutants_run: int) -> None:
    try:
        from hermes_cli import autoresearch_runs

        autoresearch_runs.append_run(
            lane="test",
            tokens=tokens,
            proposed=tests_kept,
            model=model,
            scanned=mutants_run,
        )
    except Exception:
        pass


def run_test_foundry(
    target_module: str,
    *,
    max_mutants: int = 30,
    apply_branch: str | None = None,
    llm_call: LlmCall | None = None,
    run_suite: SuiteRunner | None = None,
) -> dict[str, Any]:
    """Run the mutation-based Test-Foundry loop for one Python module."""
    worktree: Path | None = None
    hermes_home: Path | None = None
    tokens = 0
    model: str | None = None
    tests_kept = 0
    mutants_run = 0
    proposals: list[str] = []
    survivors: list[dict[str, Any]] = []

    def finish(result: dict[str, Any]) -> dict[str, Any]:
        _record_roi(
            tokens=int(result.get("tokens") or tokens),
            tests_kept=int(result.get("tests_kept") or tests_kept),
            model=str(result.get("model") or model or "") or None,
            mutants_run=int(result.get("mutants_run") or mutants_run),
        )
        return result

    try:
        rel_target = _target_relpath(target_module)
        target_path = _REPO / rel_target
        if not target_path.exists():
            return finish(_empty_result(rel_target, reason="target file not found"))
        if not _target_is_clean(rel_target):
            return finish(_empty_result(rel_target, reason="target file is not clean in the main checkout"))

        worktree = _create_worktree()
        hermes_home = _create_hermes_home()
        env = os.environ.copy()
        env["HERMES_HOME"] = str(hermes_home)

        affected = _affected_tests(worktree, rel_target)
        if not affected:
            return finish(_empty_result(rel_target, reason="no affected tests found"))
        if not _invoke_run_suite(run_suite, affected, cwd=worktree, env=env):
            return finish(_empty_result(rel_target, reason="affected baseline tests failed"))

        wt_target = worktree / rel_target
        source = wt_target.read_text(encoding="utf-8")
        mutants = generate_mutants(source, max_mutants=max(1, min(int(max_mutants or 30), _MAX_MUTANTS_HARD_CAP)))
        generated_test_rel = _foundry_test_relpath(rel_target)
        kept_test_blocks: list[tuple[str, str]] = []
        second_mutation_skipped = 0

        for idx, mutant in enumerate(mutants):
            _write_text(wt_target, mutant.mutated_source)
            mutants_run += 1
            mutant_passed = _invoke_run_suite(run_suite, affected, cwd=worktree, env=env)
            _write_text(wt_target, source)
            if not mutant_passed:
                continue

            diff = _make_diff(source, mutant.mutated_source, rel_target)
            survivor = _mutant_summary(mutant, idx)
            survivors.append(survivor)
            try:
                test_code, used_tokens, used_model = _call_hardening_llm(
                    llm_call,
                    target_module=rel_target,
                    source=source,
                    mutant=mutant,
                    diff=diff,
                    affected_tests=affected,
                )
                tokens += used_tokens
                model = used_model or model
            except Exception as exc:
                survivor["reason"] = f"llm failed: {exc}"
                continue

            if not test_code.strip():
                survivor["reason"] = "llm returned empty test"
                continue
            if _has_source_inspection(test_code, rel_target):
                survivor["reason"] = "generated test inspects source"
                continue

            gate_paths = sorted(set([*affected, generated_test_rel]))
            try:
                _write_generated_test(worktree, generated_test_rel, test_code)
                _write_text(wt_target, source)
                green_head = _invoke_run_suite(run_suite, gate_paths, cwd=worktree, env=env)
                _write_text(wt_target, mutant.mutated_source)
                red_mutant = not _invoke_run_suite(run_suite, gate_paths, cwd=worktree, env=env)

                other = next((m for j, m in enumerate(mutants) if j != idx and m.mutated_source != mutant.mutated_source), None)
                green_other = True
                if other is None:
                    second_mutation_skipped += 1
                    survivor["second_mutation_check"] = "skipped"
                else:
                    _write_text(wt_target, other.mutated_source)
                    green_other = _invoke_run_suite(run_suite, gate_paths, cwd=worktree, env=env)
                    survivor["second_mutation_check"] = "passed" if green_other else "failed"

                _write_text(wt_target, source)
                if green_head and red_mutant and green_other:
                    pid = _save_test_proposal(
                        target_module=rel_target,
                        test_code=test_code,
                        mutant=mutant,
                        mutant_index=idx,
                        affected_tests=affected,
                        diff=diff,
                        model=model,
                    )
                    proposals.append(pid)
                    kept_test_blocks.append((test_code, f"{mutant.operator}_{mutant.lineno}_{idx}"))
                    tests_kept += 1
                    survivor["proposal_id"] = pid
                    survivor["kept"] = True
                else:
                    survivor["kept"] = False
                    survivor["reason"] = (
                        f"gate failed: green_head={green_head}, red_mutant={red_mutant}, "
                        f"green_other={green_other}"
                    )
            finally:
                _write_text(wt_target, source)
                _remove_generated_test(worktree, generated_test_rel)

        apply_result = None
        apply_branch_name: str | None = None
        apply_commit: str | None = None
        if apply_branch and kept_test_blocks:
            uniq_blocks = [
                _uniquify_test_names(code.strip(), suffix)
                for code, suffix in kept_test_blocks
                if code.strip()
            ]
            combined = "\n\n".join(uniq_blocks) + "\n"
            apply_result = _apply_branch_commit(
                worktree=worktree,
                branch=apply_branch,
                target_module=rel_target,
                test_code=combined,
                affected_tests=affected,
                run_suite=run_suite,
                env=env,
            )
            if apply_result.get("ok"):
                apply_branch_name = apply_result.get("branch") or apply_branch
                apply_commit = apply_result.get("commit")
                _annotate_applied_proposals(
                    proposals,
                    branch=apply_branch_name,
                    commit=apply_commit,
                    test_file=apply_result.get("test_file"),
                )

        ok = tests_kept > 0
        result = {
            "schema": _FOUNDRY_SCHEMA,
            "ok": ok,
            "target": rel_target,
            "affected_tests": affected,
            "survivors": survivors,
            "tests_kept": tests_kept,
            "proposals": proposals,
            "reason": "" if ok else "no validated mutation tests kept",
            "tokens": tokens,
            "model": model,
            "mutants_run": mutants_run,
            "second_mutation_skipped": second_mutation_skipped,
            "apply_result": apply_result,
            "apply_branch": apply_branch_name,
            "apply_commit": apply_commit,
        }
        return finish(result)
    except Exception as exc:
        result = {
            "schema": _FOUNDRY_SCHEMA,
            "ok": False,
            "target": str(target_module),
            "survivors": survivors,
            "tests_kept": tests_kept,
            "proposals": proposals,
            "reason": str(exc),
            "tokens": tokens,
            "model": model,
            "mutants_run": mutants_run,
        }
        return finish(result)
    finally:
        _remove_worktree(worktree)
        _remove_hermes_home(hermes_home)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Hermes Test-Foundry request")
    parser.add_argument("--request", type=Path, required=True)
    args = parser.parse_args(argv)
    result = run_request_file(args.request)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
