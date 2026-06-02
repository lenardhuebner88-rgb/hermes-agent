from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import deep_audit

_REPO = Path(__file__).resolve().parents[1]


def _response(*, content: str = "", tool_calls=None, model: str = "unit-model", tokens: int = 11):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(
        model=model,
        usage=SimpleNamespace(total_tokens=tokens),
        choices=[SimpleNamespace(message=msg)],
    )


def _tool_call(name: str, arguments: dict, call_id: str = "call_1"):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


def test_sandbox_refuses_outside_allowed_forbidden_and_traversal():
    allowed = [_REPO / "hermes_cli" / "autoresearch_runs.py"]
    sandbox = deep_audit.DeepAuditSandbox(allowed)

    assert sandbox.read_file("hermes_cli/autoresearch_runs.py")["ok"] is True
    assert sandbox.read_file("hermes_cli/autoresearch_view.py")["ok"] is False
    assert "subsystem file list" in sandbox.read_file("hermes_cli/autoresearch_view.py")["error"]
    assert sandbox.read_file("../AGENTS.md")["ok"] is False
    assert sandbox.grep("token", "config.yaml")["ok"] is False
    assert sandbox.grep("token", "auth.json")["ok"] is False
    assert sandbox.grep("token", ".env")["ok"] is False
    assert sandbox.list_dir("../")["ok"] is False


def test_subsystem_resolution_filters_forbidden_and_caps(monkeypatch):
    monkeypatch.setitem(deep_audit.SUBSYSTEM_GLOBS, "unit", (
        "hermes_cli/autoresearch_*.py",
        "tests/*.py",
        "config.yaml",
    ))
    files = deep_audit.resolve_subsystem_files("unit", max_files=2)
    rels = [p.relative_to(_REPO).as_posix() for p in files]
    assert len(rels) <= 2
    assert rels
    assert all(not rel.startswith("tests/") for rel in rels)
    assert "config.yaml" not in rels


def test_tool_loop_reads_file_then_parses_grounded_finding(monkeypatch):
    monkeypatch.setitem(deep_audit.SUBSYSTEM_GLOBS, "unit", ("hermes_cli/autoresearch_runs.py",))
    calls = []

    def fake_llm(**kwargs):
        calls.append(kwargs["messages"])
        if len(calls) == 1:
            return _response(tool_calls=[_tool_call("read_file", {"path": "hermes_cli/autoresearch_runs.py"})])
        return _response(
            content=json.dumps({
                "findings": [{
                    "fileline": "hermes_cli/autoresearch_runs.py:23",
                    "severity": "high",
                    "category": "bug_risk",
                    "title": "Lane allowlist needs explicit Deep-Audit",
                    "problem": "The lane list must preserve the new audit lane.",
                    "evidence": "_VALID_LANES",
                    "fix_hint": "Keep the deep-audit lane in the allowlist.",
                }]
            }),
            tokens=17,
        )

    result = deep_audit.run_deep_audit(subsystem="unit", focus="lanes", llm_call=fake_llm)
    assert result["ok"] is True
    assert result["tokens"] == 28
    assert result["iterations"] == 2
    assert result["model"] == "unit-model"
    assert result["findings"][0]["fileline"] == "hermes_cli/autoresearch_runs.py:23"
    assert calls[1][-1]["role"] == "tool"


def test_run_request_file_persists_detection_only_proposal(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AUTORESEARCH_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv("HERMES_AUTORESEARCH_STATE_DIR", str(tmp_path / "state"))
    import hermes_cli.autoresearch_proposals as proposals
    import hermes_cli.autoresearch_runs as runs

    importlib.reload(proposals)
    importlib.reload(runs)

    monkeypatch.setitem(deep_audit.SUBSYSTEM_GLOBS, "unit", ("hermes_cli/autoresearch_runs.py",))
    request = deep_audit.write_request(subsystem="unit", focus="proposal", max_files=1)

    def fake_run(**_kwargs):
        return {
            "ok": True,
            "findings": [{
                "fileline": "hermes_cli/autoresearch_runs.py:23",
                "severity": "critical",
                "category": "bug_risk",
                "title": "Critical audit finding",
                "problem": "Manual fix is required.",
                "evidence": "_VALID_LANES",
                "fix_hint": "Patch manually.",
                "_model_label": "unit-model",
            }],
            "subsystem": "unit",
            "model": "unit-model",
            "tokens": 123,
            "iterations": 2,
            "reason": "",
            "files": ["hermes_cli/autoresearch_runs.py"],
        }

    monkeypatch.setattr(deep_audit, "run_deep_audit", fake_run)
    deep_audit.run_request_file(Path(request["request_path"]))
    payload = proposals.proposals_payload()
    assert payload["proposals"][0]["proposal_type"] == "deep_audit"
    assert payload["proposals"][0]["apply_blocked_reason"] == "Deep-Audit-Befund — Fix manuell"
    assert proposals.apply_proposal(payload["proposals"][0]["id"], confirm=True)["ok"] is False
    assert runs.read_runs()[0]["lane"] == "deep-audit"


def test_deep_audit_endpoints_trigger_409_and_findings(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AUTORESEARCH_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv("HERMES_AUTORESEARCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setitem(deep_audit.SUBSYSTEM_GLOBS, "unit", ("hermes_cli/autoresearch_runs.py",))

    import hermes_cli.autoresearch_view as view
    importlib.reload(view)
    monkeypatch.setattr(view, "_spawn_deep_audit_runner", lambda _path: 4242)

    app = FastAPI()
    view.register_autoresearch_routes(app)
    client = TestClient(app)

    subsystems = client.get("/api/autoresearch/deep-audit/subsystems").json()
    assert "unit" in subsystems["subsystems"]

    first = client.post("/api/autoresearch/deep-audit/trigger", json={"subsystem": "unit", "focus": "auth"})
    assert first.status_code == 200, first.text
    assert first.json()["pid"] == 4242
    assert first.json()["files"] == ["hermes_cli/autoresearch_runs.py"]

    busy = client.post("/api/autoresearch/deep-audit/trigger", json={"subsystem": "unit"})
    assert busy.status_code == 409

    status = client.get("/api/autoresearch/deep-audit/status").json()
    assert status["state"] == "running"
    assert status["pid"] == 4242

    findings_payload = {
        "ok": True,
        "subsystem": "unit",
        "model": "unit-model",
        "tokens": 5,
        "iterations": 1,
        "reason": "",
        "findings": [{"fileline": "hermes_cli/autoresearch_runs.py:23"}],
        "proposals": ["deep-audit-x"],
    }
    target = tmp_path / "state" / "deep-audit" / "last-findings.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(findings_payload), encoding="utf-8")
    findings = client.get("/api/autoresearch/deep-audit/findings").json()
    assert findings["findings"][0]["fileline"] == "hermes_cli/autoresearch_runs.py:23"
    assert findings["proposals"] == ["deep-audit-x"]


def test_sandbox_refuses_absolute_out_of_repo_paths():
    """Absolute paths pointing outside the repo must be refused with no leak."""
    allowed = [_REPO / "hermes_cli" / "autoresearch_runs.py"]
    sandbox = deep_audit.DeepAuditSandbox(allowed)

    for outside in ("/etc/passwd", "/home/piet/.hermes/auth.json"):
        read_res = sandbox.read_file(outside)
        assert read_res["ok"] is False
        assert "content" not in read_res

        grep_res = sandbox.grep("root", outside)
        assert grep_res["ok"] is False
        assert grep_res["results"] == []


def test_sandbox_refuses_in_repo_symlink_escape():
    """A symlink living inside the repo but pointing outside must not be readable.

    ``resolve()`` follows the link to the external target, so ``_under(_REPO)``
    fails and the sandbox refuses. The symlink is cleaned up unconditionally so
    no artifact leaks into the worktree.
    """
    link = _REPO / "._deep_audit_symlink_test"
    if link.exists() or link.is_symlink():
        link.unlink()
    os.symlink("/etc/hostname", link)
    try:
        sandbox = deep_audit.DeepAuditSandbox([link])
        res = sandbox.read_file("._deep_audit_symlink_test")
        assert res["ok"] is False
        assert "content" not in res
    finally:
        try:
            os.unlink(link)
        except OSError:
            pass
    assert not link.exists() and not link.is_symlink()


def test_dispatch_rejects_unknown_tool_and_malformed_args():
    """Tool dispatch must survive hostile LLM output without raising."""
    allowed = [_REPO / "hermes_cli" / "autoresearch_runs.py"]
    sandbox = deep_audit.DeepAuditSandbox(allowed)

    unknown = json.loads(sandbox.dispatch("write_file", {"path": "x", "content": "y"}))
    assert unknown["ok"] is False
    assert "unknown tool" in unknown["error"]

    # Non-string / missing path argument must not raise.
    missing = json.loads(sandbox.dispatch("read_file", {}))
    assert missing["ok"] is False
    non_string = json.loads(sandbox.dispatch("read_file", {"path": 1234}))
    assert non_string["ok"] is False

    # Embedded newline smuggling an out-of-repo path must be refused, not crash.
    newline = json.loads(sandbox.dispatch("read_file", {"path": "hermes_cli/x.py\n/etc/passwd"}))
    assert newline["ok"] is False
    assert "content" not in newline

    grep_newline = json.loads(sandbox.dispatch("grep", {"pattern": "root", "path": "hermes_cli/x.py\n/etc/passwd"}))
    assert grep_newline["ok"] is False


def test_allowlist_is_the_binding_gate():
    """An in-repo, non-forbidden file that is not on the allowlist is still refused."""
    target = _REPO / "hermes_cli" / "auth.py"
    assert target.exists()  # in-repo, real file, not on the forbidden deny-list
    sandbox = deep_audit.DeepAuditSandbox([_REPO / "hermes_cli" / "autoresearch_runs.py"])

    res = sandbox.read_file("hermes_cli/auth.py")
    assert res["ok"] is False
    assert "subsystem file list" in res["error"]
    assert "content" not in res
