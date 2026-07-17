"""Verdict-Cage Phase 2 E2E tests.

Acceptance criteria:
  AC-1: Verdict lanes use --allowedTools (allowlist fail-closed), not
        --dangerously-skip-permissions + --disallowedTools (denylist).
  AC-2: Worker + in-place mutation verbs (sed -i, perl -i, ln -s, truncate,
        dd of=) are blocked by the guard-dangerous-ops.sh hook.
  AC-4: CONFIRMED=1 does NOT neutralise the block for workers (IS_WORKER
        is checked BEFORE the CONFIRMED gate).

These tests exercise the real hook script in subprocess invocations with
controlled environments, and they exercise the _spawn_claude_worker argv
construction for verdict-lane allowlist enforcement.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

# ---------------------------------------------------------------------------
# Hook path resolution
# ---------------------------------------------------------------------------

HOOK_PATH = Path.home() / ".claude" / "hooks" / "guard-dangerous-ops.sh"


def _run_hook(command: str, *, env_overrides: dict[str, str] | None = None) -> str | None:
    """Run the guard-dangerous-ops.sh hook with a given bash command.

    Returns the hook's stdout (JSON block reason) if the command would be
    blocked, or None if it would be allowed.
    """
    if not HOOK_PATH.exists():
        pytest.skip(f"Hook script not found at {HOOK_PATH}")

    payload = json.dumps({"tool_input": {"command": command}})
    env = {
        **os.environ,
        "HERMES_KANBAN_TASK": "t_verdict_cage_test",
    }
    if env_overrides:
        env.update(env_overrides)

    result = subprocess.run(
        ["bash", str(HOOK_PATH)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    if result.stdout.strip():
        try:
            data = json.loads(result.stdout.strip())
            return data.get("reason", result.stdout.strip())
        except json.JSONDecodeError:
            return result.stdout.strip()
    return None


# ---------------------------------------------------------------------------
# AC-2: In-place mutation verbs are blocked for workers
# ---------------------------------------------------------------------------

WORKER_ENV = {"HERMES_KANBAN_TASK": "t_verdict_cage_test"}


class TestInPlaceMutationBlockedForWorkers:
    """AC-2: each in-place mutation verb must be blocked when IS_WORKER=1."""

    @pytest.mark.parametrize(
        "command",
        [
            "sed -i 's/a/b/' /tmp/target",
            "perl -i -pe 's/a/b/' /tmp/target",
            "ln -s /tmp/source /tmp/target_link",
            "truncate -s 0 /tmp/target",
            "dd if=/dev/zero of=/tmp/target bs=1 count=1",
        ],
        ids=["sed-i", "perl-i", "ln-s", "truncate", "dd-of"],
    )
    def test_mutation_verb_blocked_for_worker(self, command: str):
        reason = _run_hook(command, env_overrides=WORKER_ENV)
        assert reason is not None, f"Expected block for worker command: {command}"
        assert "Worker-FS-Cage" in reason or "blocked" in reason.lower()

    @pytest.mark.parametrize(
        "command",
        [
            "sed -i 's/a/b/' /tmp/target",
            "perl -i -pe 's/a/b/' /tmp/target",
            "ln -s /tmp/source /tmp/target_link",
            "truncate -s 0 /tmp/target",
            "dd if=/dev/zero of=/tmp/target bs=1 count=1",
        ],
        ids=["sed-i", "perl-i", "ln-s", "truncate", "dd-of"],
    )
    def test_mutation_verb_with_confirmed_still_blocked_for_worker(
        self, command: str
    ):
        """AC-4: CONFIRMED=1 does NOT neutralise the block for workers."""
        full_cmd = f"CONFIRMED=1 {command}"
        reason = _run_hook(full_cmd, env_overrides=WORKER_ENV)
        assert reason is not None, (
            f"Expected block even with CONFIRMED=1 for worker command: {full_cmd}"
        )


# ---------------------------------------------------------------------------
# AC-2: target file unchanged (sha256) + no symlink created
# ---------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TestAC2TargetFileUnchanged:
    """AC-2: the hook DENIES the mutation, so (respecting the deny) the target
    file's sha256 is unchanged and no symlink is created at the target path.

    The guard hook is a PreToolUse *deny* gate: a deny means Claude Code never
    executes the command, so the file is protected. The test asserts the deny
    AND demonstrates the file is byte-identical and no symlink landed.
    """

    def test_inplace_mutations_leave_target_unchanged(self, tmp_path):
        target = tmp_path / "target.txt"
        target.write_text("alpha beta gamma\n", encoding="utf-8")
        before = _sha256(target)

        for command in (
            f"sed -i 's/alpha/zzz/' {target}",
            f"perl -i -pe 's/alpha/zzz/' {target}",
            f"truncate -s 0 {target}",
            f"dd if=/dev/zero of={target} bs=1 count=1",
        ):
            reason = _run_hook(command, env_overrides=WORKER_ENV)
            assert reason is not None, f"hook must deny: {command}"
            # Deny respected => command not executed => file byte-identical.
            assert _sha256(target) == before, (
                f"target changed after denied command: {command}"
            )

    def test_symlink_creation_blocked_and_absent(self, tmp_path):
        source = tmp_path / "source.txt"
        source.write_text("payload\n", encoding="utf-8")
        link = tmp_path / "target_link"

        command = f"ln -s {source} {link}"
        reason = _run_hook(command, env_overrides=WORKER_ENV)
        assert reason is not None, "hook must deny ln -s for a worker"
        # Deny respected => no symlink created at the target path.
        assert not link.exists(), "no file/symlink may exist at the link target"
        assert not link.is_symlink(), "no symlink may be created at the link target"


# ---------------------------------------------------------------------------
# AC-3: has_worker_write_intent() covers the in-place verbs on config paths
# ---------------------------------------------------------------------------


class TestAC3WorkerWriteIntentConfigPath:
    """AC-3: the in-place mutation verbs are caught by has_worker_write_intent()
    on the live Agent-/Profil-config paths the cage protects. Each verb is
    exercised in isolation against a ~/.claude config target.
    """

    @pytest.mark.parametrize(
        "command",
        [
            "sed -i 's/a/b/' /home/piet/.claude/settings.json",
            "perl -i -pe 's/a/b/' /home/piet/.claude/settings.json",
            "ln -s /tmp/evil /home/piet/.claude/hooks/guard-dangerous-ops.sh",
            "truncate -s 0 /home/piet/.claude/settings.json",
            "dd if=/dev/zero of=/home/piet/.claude/settings.json bs=1 count=1",
            "cp /tmp/evil /home/piet/.hermes/config.yaml",
            "mv /tmp/evil /home/piet/.hermes/config.yaml",
        ],
        ids=["sed-i", "perl-i", "ln-s", "truncate", "dd-of", "cp", "mv"],
    )
    def test_config_path_write_intent_blocked(self, command: str):
        reason = _run_hook(command, env_overrides=WORKER_ENV)
        assert reason is not None, f"worker config-path write must be blocked: {command}"


# ---------------------------------------------------------------------------
# AC-2: Read-only commands are NOT blocked for workers (negative test)
# ---------------------------------------------------------------------------


class TestReadOnlyCommandsAllowedForWorkers:
    """AC-2 negative: read-only commands must pass through for workers."""

    @pytest.mark.parametrize(
        "command",
        [
            "cat /tmp/target",
            "grep pattern /tmp/target",
            "ls -la /tmp",
            "head -10 /tmp/target",
            "tail -10 /tmp/target",
        ],
        ids=["cat", "grep", "ls", "head", "tail"],
    )
    def test_read_only_command_allowed_for_worker(self, command: str):
        reason = _run_hook(command, env_overrides=WORKER_ENV)
        assert reason is None, (
            f"Read-only command should be allowed for worker: {command}, "
            f"but got block reason: {reason}"
        )


# ---------------------------------------------------------------------------
# AC-1: Verdict lane allowlist fail-closed in _spawn_claude_worker
# ---------------------------------------------------------------------------

def _import_kanban_db():
    try:
        from hermes_cli import kanban_db as kb
        return kb
    except Exception as exc:
        pytest.skip(f"Cannot import hermes_cli.kanban_db: {exc}")


class _BaseSpawnTest:
    """Shared fixtures for _spawn_claude_worker argv capture tests."""

    def _make_task(self, tmp_path, *, assignee="reviewer"):
        kb = _import_kanban_db()
        return kb.Task(
            id="t_verdict_allowlist",
            title="review the thing",
            body="verify the changes",
            assignee=assignee,
            status="ready",
            priority=0,
            created_by=None,
            created_at=0,
            started_at=None,
            completed_at=None,
            workspace_kind="worktree",
            workspace_path=str(tmp_path / "ws"),
            claim_lock=None,
            claim_expires=None,
            tenant=None,
            branch_name="wt/t_verdict_allowlist",
            model_override=None,
        )

    def _set_home(self, monkeypatch, tmp_path):
        default_home = tmp_path / ".hermes"
        default_home.mkdir(exist_ok=True)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(default_home))
        monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)

    def _run_default_spawn(
        self,
        tmp_path,
        monkeypatch,
        *,
        assignee,
        lane_entry="default",
        enforceable="1",
        review_bash=None,
    ):
        """Run ``_default_spawn`` with a faked Popen + faked lane resolver.

        Returns (and stashes on ``self._captured``) a dict
        ``{"popen_called": bool, "cmd": [...], "env": {...}}``. The dict is
        populated even when ``_default_spawn`` raises (fail-closed paths), so a
        caller can assert ``popen_called is False`` after catching the error.

        lane_entry="default": synthesise a claude-cli lane_entry for verdict
        profiles (reviewer/critic), None for others.
        lane_entry=dict: use the given dict as the lane_entry.
        lane_entry=None: simulate "no lane entry found" (unverifiable lane).
        enforceable: value for HERMES_VERDICT_ALLOWLIST_ENFORCEABLE ("1"/"0"),
        or None to leave it unset (real version probe of the stub binary).
        """
        kb = _import_kanban_db()
        self._set_home(monkeypatch, tmp_path)
        captured: dict = {"popen_called": False}
        self._captured = captured

        class _FakePopen:
            def __init__(self, cmd, **kwargs):
                captured["popen_called"] = True
                captured["cmd"] = list(cmd)
                captured["env"] = kwargs.get("env", {})
                self.pid = 7777

        monkeypatch.setattr("subprocess.Popen", _FakePopen)
        monkeypatch.setenv("HERMES_CLAUDE_BIN", "/usr/local/bin/claude-test")
        monkeypatch.setenv("HERMES_CLAUDE_CLI_PROFILES", assignee)
        if enforceable is None:
            monkeypatch.delenv(
                "HERMES_VERDICT_ALLOWLIST_ENFORCEABLE", raising=False
            )
        else:
            monkeypatch.setenv(
                "HERMES_VERDICT_ALLOWLIST_ENFORCEABLE", enforceable
            )
        # Default-clear the review-bash seam so strict-equality allowlist tests
        # are immune to an ambient operator config; opt in via review_bash=.
        if review_bash is None:
            monkeypatch.delenv("HERMES_REVIEW_BASH_ALLOWLIST", raising=False)
        else:
            monkeypatch.setenv("HERMES_REVIEW_BASH_ALLOWLIST", review_bash)

        # Resolve the lane_entry to pass to the mock.
        if lane_entry == "default":
            _resolved_lane_entry = {
                "worker_runtime": "claude-cli",
                "model": None,
                "provider": None,
                "fallback_providers": [],
            }
        else:
            _resolved_lane_entry = lane_entry

        def _fake_lane_entry(profile_arg, *, board=None, strict=False):
            return _resolved_lane_entry

        monkeypatch.setattr(kb, "_active_lane_entry_for_profile", _fake_lane_entry)

        task = self._make_task(tmp_path, assignee=assignee)
        kb._default_spawn(task, str(tmp_path / "ws"))
        return captured

    def _spawn_and_capture_cmd(
        self, tmp_path, monkeypatch, *, assignee, lane_entry="default"
    ):
        """Spawn a task and return the captured cmd argv (positive path)."""
        return self._run_default_spawn(
            tmp_path, monkeypatch, assignee=assignee, lane_entry=lane_entry
        )["cmd"]


class TestVerdictLaneAllowlistFailClosed(_BaseSpawnTest):
    """AC-1: verdict-lane claude spawns use --allowedTools, not denylist+bypass."""

    def test_verdict_lane_uses_allowedtools_not_denylist(self, tmp_path, monkeypatch):
        """AC-1: reviewer (verdict lane) spawn uses --allowedTools."""
        cmd = self._spawn_and_capture_cmd(
            tmp_path, monkeypatch, assignee="reviewer"
        )

        assert "--allowedTools" in cmd, (
            f"Verdict lane must use --allowedTools, got: {cmd}"
        )
        allow_idx = cmd.index("--allowedTools")
        allowlist = cmd[allow_idx + 1]
        assert "Read" in allowlist
        assert "Grep" in allowlist
        assert "Glob" in allowlist
        assert "Write" not in allowlist
        assert "Edit" not in allowlist
        assert "Bash" not in allowlist

    def test_verdict_lane_no_dangerously_skip_permissions(self, tmp_path, monkeypatch):
        """AC-1: verdict lane must NOT use --dangerously-skip-permissions."""
        cmd = self._spawn_and_capture_cmd(
            tmp_path, monkeypatch, assignee="reviewer"
        )
        assert "--dangerously-skip-permissions" not in cmd, (
            f"Verdict lane must not use --dangerously-skip-permissions, got: {cmd}"
        )

    def test_verdict_lane_no_disallowed_tools(self, tmp_path, monkeypatch):
        """AC-1: verdict lane must NOT use --disallowedTools (denylist model)."""
        cmd = self._spawn_and_capture_cmd(
            tmp_path, monkeypatch, assignee="reviewer"
        )
        assert "--disallowedTools" not in cmd, (
            f"Verdict lane must not use --disallowedTools, got: {cmd}"
        )

    def test_non_verdict_lane_keeps_denylist_model(self, tmp_path, monkeypatch):
        """AC-1 negative: coder (non-verdict) lane keeps the denylist+bypass model."""
        cmd = self._spawn_and_capture_cmd(
            tmp_path, monkeypatch, assignee="coder"
        )
        assert "--dangerously-skip-permissions" in cmd
        assert "--disallowedTools" in cmd
        assert "--allowedTools" not in cmd

    def test_critic_verdict_lane_uses_allowlist(self, tmp_path, monkeypatch):
        """AC-1: critic (also verdict lane) uses allowlist."""
        cmd = self._spawn_and_capture_cmd(
            tmp_path, monkeypatch, assignee="critic"
        )
        assert "--allowedTools" in cmd
        assert "--dangerously-skip-permissions" not in cmd

    def test_review_bash_allowlist_default_empty_no_bash(self, tmp_path, monkeypatch):
        """AC-1: with no HERMES_REVIEW_BASH_ALLOWLIST, the verdict allowlist is
        exactly the read-only set — no Bash leaks in."""
        monkeypatch.delenv("HERMES_REVIEW_BASH_ALLOWLIST", raising=False)
        cmd = self._spawn_and_capture_cmd(
            tmp_path, monkeypatch, assignee="reviewer"
        )
        allow_idx = cmd.index("--allowedTools")
        allowlist = cmd[allow_idx + 1]
        assert allowlist == "Read,Grep,Glob"
        assert "Bash" not in allowlist

    def test_review_bash_allowlist_appends_when_configured(
        self, tmp_path, monkeypatch
    ):
        """AC-1: an operator-configured read-only Bash allowlist is appended to
        the verdict --allowedTools verbatim (the only sanctioned Bash seam)."""
        cmd = self._run_default_spawn(
            tmp_path, monkeypatch, assignee="reviewer",
            review_bash="Bash(git diff:*),Bash(git log:*)",
        )["cmd"]
        allow_idx = cmd.index("--allowedTools")
        allowlist = cmd[allow_idx + 1]
        assert allowlist.startswith("Read,Grep,Glob")
        assert "Bash(git diff:*)" in allowlist
        assert "Bash(git log:*)" in allowlist
        # still no unrestricted Edit/Write
        assert "Write" not in allowlist
        assert "Edit" not in allowlist

    def test_verdict_lane_allowlist_is_read_only_set(self, tmp_path, monkeypatch):
        """AC-1: the CLAUDE_CLI_VERDICT_ALLOWLIST constant is read-only."""
        kb = _import_kanban_db()
        allowlist = kb._CLAUDE_CLI_VERDICT_ALLOWLIST
        assert isinstance(allowlist, (tuple, list))
        for tool in allowlist:
            assert tool in ("Read", "Grep", "Glob"), (
                f"Verdict allowlist contains non-read-only tool: {tool}"
            )
        assert "Read" in allowlist

    def test_verdict_lane_unenforceable_allowlist_fails_closed(
        self, tmp_path, monkeypatch
    ):
        """AC-1: if a verdict profile is routed through claude-CLI spawn but
        the lane_entry is missing/unverifiable, the spawn MUST fail-closed
        (RuntimeError) rather than silently falling back to the denylist+bypass
        model with --dangerously-skip-permissions. No child process is spawned.
        """
        with pytest.raises(RuntimeError, match="Verdict-Cage fail-closed"):
            self._run_default_spawn(
                tmp_path, monkeypatch, assignee="reviewer", lane_entry=None
            )
        assert self._captured["popen_called"] is False, (
            "fail-closed lane_entry path must not spawn a child process"
        )

    def test_allowlist_fail_closed(self, tmp_path, monkeypatch):
        """AC-1 (Next-Step Verification #1): when --allowedTools is NOT
        enforceable on the installed Claude CLI, _spawn_claude_worker must
        fail-closed — raise spawn_refused_allowlist_unenforceable and NOT call
        subprocess.Popen (no child process before exec).
        """
        with pytest.raises(
            RuntimeError, match="spawn_refused_allowlist_unenforceable"
        ):
            self._run_default_spawn(
                tmp_path, monkeypatch, assignee="reviewer", enforceable="0"
            )
        assert self._captured["popen_called"] is False, (
            "unenforceable allowlist must not spawn a child process"
        )

    def test_allowlist_fail_closed_logs_marker(self, tmp_path, monkeypatch, caplog):
        """AC-1: the fail-closed refusal logs the spawn_refused_allowlist_
        unenforceable marker for observability."""
        import logging

        with caplog.at_level(logging.ERROR):
            with pytest.raises(RuntimeError):
                self._run_default_spawn(
                    tmp_path, monkeypatch, assignee="critic", enforceable="0"
                )
        assert any(
            "spawn_refused_allowlist_unenforceable" in rec.getMessage()
            for rec in caplog.records
        ), "expected spawn_refused_allowlist_unenforceable in the error log"

    def test_enforceable_gate_unset_probes_version(self, tmp_path, monkeypatch):
        """AC-1: with no override env, the gate falls back to a Claude CLI
        version probe. An undetectable version (probe -> None) resolves to NOT
        enforceable => fail-closed, and the worker argv is never launched.
        """
        kb = _import_kanban_db()
        # Simulate an undetectable Claude CLI version (missing/broken binary).
        monkeypatch.setattr(
            kb._worker_runtime, "claude_cli_version",
            lambda claude_bin, *, env=None: None,
        )
        with pytest.raises(
            RuntimeError, match="spawn_refused_allowlist_unenforceable"
        ):
            self._run_default_spawn(
                tmp_path, monkeypatch, assignee="reviewer", enforceable=None
            )
        # The version probe is itself allowed to use a subprocess, but the
        # read-only worker argv (-p <prompt> --allowedTools ...) must NEVER be
        # handed to Popen on the fail-closed path.
        worker_cmd = self._captured.get("cmd", [])
        assert "--allowedTools" not in worker_cmd, (
            "fail-closed path must not launch the verdict worker argv"
        )
        assert "-p" not in worker_cmd


class TestWorkerMcpIsolation(_BaseSpawnTest):
    """disposition-di_109b5a17-S1: claude-cli kanban workers must spawn with
    ``--strict-mcp-config`` so no external MCP servers (vault qmd,
    @playwright/mcp headless chromium, claude.ai connectors) load.

    Those server child processes keep the Node event loop alive, so ``claude -p``
    cannot exit after its agent turn — it sits in ``ep_poll`` and the buffered
    ``--output-format json`` result is never flushed (the post-commit idle hang:
    0-byte log, last output >1000s, worker slot + token stream pinned). The
    kanban lifecycle is driven via Bash → ``hermes kanban`` (not MCP), so
    stripping MCP costs the worker nothing.
    """

    def test_non_verdict_lane_spawn_uses_strict_mcp_config(self, tmp_path, monkeypatch):
        """The default (denylist+bypass) worker spawn pins --strict-mcp-config."""
        cmd = self._spawn_and_capture_cmd(
            tmp_path, monkeypatch, assignee="coder"
        )
        assert "--strict-mcp-config" in cmd, (
            f"non-verdict worker must isolate MCP via --strict-mcp-config, got: {cmd}"
        )

    def test_verdict_lane_spawn_uses_strict_mcp_config(self, tmp_path, monkeypatch):
        """The verdict (allowlist) worker spawn also pins --strict-mcp-config."""
        cmd = self._spawn_and_capture_cmd(
            tmp_path, monkeypatch, assignee="reviewer"
        )
        assert "--strict-mcp-config" in cmd, (
            f"verdict worker must isolate MCP via --strict-mcp-config, got: {cmd}"
        )

    def test_strict_mcp_config_loads_no_external_mcp_config(self, tmp_path, monkeypatch):
        """--strict-mcp-config must be the EMPTY-server-set form: no companion
        --mcp-config may smuggle external servers back in (defeats the cage)."""
        for assignee in ("coder", "reviewer"):
            cmd = self._spawn_and_capture_cmd(
                tmp_path, monkeypatch, assignee=assignee
            )
            assert cmd.count("--strict-mcp-config") == 1, (
                f"expected exactly one --strict-mcp-config for {assignee}, got: {cmd}"
            )
            assert "--mcp-config" not in cmd, (
                f"--strict-mcp-config must not be paired with --mcp-config for "
                f"{assignee} (that would re-enable external servers), got: {cmd}"
            )


# ---------------------------------------------------------------------------
# Per-profile claude-cli --effort and fast-mode --settings pass-through.
# ---------------------------------------------------------------------------


class TestClaudeProfileEffortAndFastMode(_BaseSpawnTest):
    """Optional root-level profile config keys ``claude_effort`` and
    ``claude_fast_mode`` (config.yaml — same shape as
    /home/piet/.hermes/profiles/reviewer/config.yaml) feed --effort and the
    --settings JSON for claude-cli kanban worker spawns, on BOTH the verdict
    allowlist path and the denylist+bypass path."""

    def _write_profile_config(self, tmp_path, extra: dict) -> None:
        home = tmp_path / ".hermes"
        home.mkdir(parents=True, exist_ok=True)
        cfg = {
            "worker_runtime": "claude-cli",
            "claude_model": "claude-opus-4-8",
            **extra,
        }
        (home / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")

    def test_claude_effort_valid_appends_flag_verdict_lane(
        self, tmp_path, monkeypatch
    ):
        self._write_profile_config(tmp_path, {"claude_effort": "high"})
        cmd = self._spawn_and_capture_cmd(
            tmp_path, monkeypatch, assignee="reviewer"
        )
        assert "--effort" in cmd, f"expected --effort in verdict-lane argv: {cmd}"
        idx = cmd.index("--effort")
        assert cmd[idx + 1] == "high"

    def test_claude_effort_valid_appends_flag_non_verdict_lane(
        self, tmp_path, monkeypatch
    ):
        self._write_profile_config(tmp_path, {"claude_effort": "high"})
        cmd = self._spawn_and_capture_cmd(tmp_path, monkeypatch, assignee="coder")
        assert "--effort" in cmd, f"expected --effort in non-verdict argv: {cmd}"
        idx = cmd.index("--effort")
        assert cmd[idx + 1] == "high"

    def test_claude_effort_invalid_omits_flag_and_logs_warning(
        self, tmp_path, monkeypatch, caplog
    ):
        """Invalid claude_effort must never fail the spawn — omit the flag
        and log a warning instead."""
        import logging

        self._write_profile_config(tmp_path, {"claude_effort": "turbo"})
        with caplog.at_level(logging.WARNING):
            cmd = self._spawn_and_capture_cmd(
                tmp_path, monkeypatch, assignee="coder"
            )
        assert "--effort" not in cmd, f"invalid claude_effort must be omitted: {cmd}"
        assert any(
            "claude_effort" in rec.getMessage() and "turbo" in rec.getMessage()
            for rec in caplog.records
        ), "expected a warning naming the invalid claude_effort value"

    def test_claude_fast_mode_true_adds_settings_key(self, tmp_path, monkeypatch):
        self._write_profile_config(tmp_path, {"claude_fast_mode": True})
        cmd = self._spawn_and_capture_cmd(tmp_path, monkeypatch, assignee="coder")
        assert "--settings" in cmd
        settings = json.loads(cmd[cmd.index("--settings") + 1])
        assert settings["enabledPlugins"] == {"memsearch@memsearch-plugins": False}
        assert settings["fastMode"] is True

    def test_absent_keys_argv_identical_to_pre_change(self, tmp_path, monkeypatch):
        """No claude_effort / claude_fast_mode configured (no config.yaml at
        all) => argv is byte-identical to the pre-change behavior: no
        --effort, and --settings carries only the memsearch-disable literal."""
        cmd = self._spawn_and_capture_cmd(tmp_path, monkeypatch, assignee="coder")
        assert "--effort" not in cmd
        settings_value = cmd[cmd.index("--settings") + 1]
        assert settings_value == (
            '{"enabledPlugins": {"memsearch@memsearch-plugins": false}}'
        )
