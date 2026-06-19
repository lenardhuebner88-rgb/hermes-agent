"""Tests for gateway restart-loop defenses (#30719).

Covers:
- Defense 1: gateway stop/restart refuse when _HERMES_GATEWAY=1
- Defense 2: cron create rejects prompts containing gateway lifecycle commands
- _contains_gateway_lifecycle_command pattern matching
"""

import json
import os
from argparse import Namespace

import pytest

from hermes_cli.cron import (
    _contains_gateway_lifecycle_command,
    cron_create,
    cron_edit,
    cron_command,
)


# ---------------------------------------------------------------------------
# Defense 2: _contains_gateway_lifecycle_command pattern tests
# ---------------------------------------------------------------------------

class TestGatewayLifecyclePattern:
    """Verify the regex catches gateway lifecycle commands."""

    @pytest.mark.parametrize("text", [
        "hermes gateway restart",
        "hermes gateway stop",
        "hermes gateway start",
        "hermes  gateway  restart",         # double spaces
        "Hermez Gateway Restart".lower().replace("z", "s"),  # case handled
        "HERMES GATEWAY RESTART",           # uppercase
    ])
    def test_hermes_gateway_commands(self, text):
        assert _contains_gateway_lifecycle_command(text), f"Should match: {text!r}"

    @pytest.mark.parametrize("text", [
        "launchctl kickstart gui/501/ai.hermes.gateway",
        "launchctl unload ~/Library/LaunchAgents/ai.hermes.gateway.plist",
        "launchctl stop ai.hermes.gateway",
        "systemctl restart hermes-gateway",
        "systemctl stop hermes-gateway.service",
        "systemctl start hermes-gateway",
    ])
    def test_service_manager_commands(self, text):
        assert _contains_gateway_lifecycle_command(text), f"Should match: {text!r}"

    @pytest.mark.parametrize("text", [
        "kill hermes gateway process",
        "pkill -f hermes.*gateway",
    ])
    def test_kill_commands(self, text):
        assert _contains_gateway_lifecycle_command(text), f"Should match: {text!r}"

    @pytest.mark.parametrize("text", [
        'pkill -f "hermes"\npkill -9 "gateway"',
        "kill $(pgrep -f hermes)",
        "pgrep -f hermes",
        "pkill -f hermes",
    ])
    def test_multiline_and_hermes_kill_commands(self, text):
        assert _contains_gateway_lifecycle_command(text), f"Should match: {text!r}"

    @pytest.mark.parametrize("text", [
        "restart the server application",
        "hermes cron list",
        "hermes update",
        "hermes config set model claude",
        "echo 'just a normal cron job'",
        "run the backup script",
        "gateway is running fine",
        # Regression (#30728 follow-up): legit prompts that merely mention an
        # unrelated gateway + a restart must NOT be blocked.
        "Summarize the API gateway logs and report any restart events from last night",
        "Check if the payment gateway needs a restart after the deploy",
        "Monitor the gateway and tell me if a restart is recommended",
    ])
    def test_safe_commands(self, text):
        assert not _contains_gateway_lifecycle_command(text), f"Should NOT match: {text!r}"


class TestCronCreateLifecycleBlock:
    """Verify cron create rejects gateway lifecycle prompts."""

    @pytest.fixture(autouse=True)
    def _setup_cron_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
        monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
        monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")

    def test_block_hermes_gateway_restart(self, capsys):
        args = Namespace(
            cron_command="create",
            schedule="30m",
            prompt="Upgrade hermes then run hermes gateway restart",
            name=None,
            deliver=None,
            repeat=None,
            skill=None,
            skills=None,
            script=None,
            workdir=None,
            profile=None,
            no_agent=False,
        )
        rc = cron_command(args)
        assert rc == 1
        out = capsys.readouterr().out
        assert "Blocked" in out
        assert "#30719" in out

    def test_block_launchctl_kickstart(self, capsys):
        args = Namespace(
            cron_command="create",
            schedule="0 9 * * *",
            prompt="Run launchctl kickstart -k gui/501/ai.hermes.gateway",
            name=None,
            deliver=None,
            repeat=None,
            skill=None,
            skills=None,
            script=None,
            workdir=None,
            profile=None,
            no_agent=False,
        )
        rc = cron_command(args)
        assert rc == 1
        out = capsys.readouterr().out
        assert "Blocked" in out

    def test_block_script_with_lifecycle_command(self, tmp_path, capsys):
        script = tmp_path / "restart.sh"
        script.write_text("#!/bin/bash\nhermes gateway restart\n")
        args = Namespace(
            cron_command="create",
            schedule="1h",
            prompt=None,
            name=None,
            deliver=None,
            repeat=None,
            skill=None,
            skills=None,
            script=str(script),
            workdir=None,
            profile=None,
            no_agent=False,
        )
        rc = cron_command(args)
        assert rc == 1
        out = capsys.readouterr().out
        assert "Blocked" in out

    def test_allow_safe_prompt(self, capsys):
        args = Namespace(
            cron_command="create",
            schedule="30m",
            prompt="Check server health and report status",
            name=None,
            deliver=None,
            repeat=None,
            skill=None,
            skills=None,
            script=None,
            workdir=None,
            profile=None,
            no_agent=False,
        )
        rc = cron_command(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "Created job" in out

    def test_allow_empty_prompt(self, capsys):
        """Empty prompt (no lifecycle content) should pass the filter — the
        API will still reject it for lacking prompt+skill, but that's a
        separate validation, not the lifecycle guard."""
        args = Namespace(
            cron_command="create",
            schedule="30m",
            prompt=None,
            name=None,
            deliver=None,
            repeat=None,
            skill=None,
            skills=None,
            script=None,
            workdir=None,
            profile=None,
            no_agent=False,
        )
        rc = cron_command(args)
        # The lifecycle guard passes (no gateway command in prompt).
        # The API rejects it for "requires prompt or skill" → rc 1, but
        # the error message is about prompt/skill, NOT about "Blocked".
        out = capsys.readouterr().out
        assert "Blocked" not in out


class TestCronLifecycleGuardApiBoundary:
    """Verify cron create/edit block before reaching the cron API."""

    def _edit_args(self, **overrides):
        values = {
            "job_id": "job-1",
            "schedule": None,
            "prompt": None,
            "name": None,
            "deliver": None,
            "repeat": None,
            "skill": None,
            "skills": None,
            "add_skills": None,
            "remove_skills": None,
            "clear_skills": False,
            "script": None,
            "workdir": None,
            "no_agent": None,
        }
        values.update(overrides)
        return Namespace(**values)

    def _create_args(self, **overrides):
        values = {
            "schedule": "30m",
            "prompt": "Check server health",
            "name": None,
            "deliver": None,
            "repeat": None,
            "skill": None,
            "skills": None,
            "script": None,
            "workdir": None,
            "no_agent": False,
        }
        values.update(overrides)
        return Namespace(**values)

    @pytest.fixture
    def edit_job(self, monkeypatch):
        job = {
            "id": "job-1",
            "job_id": "job-1",
            "name": "Existing Job",
            "prompt": "Existing prompt",
            "skills": [],
            "skill": None,
        }
        monkeypatch.setattr("cron.jobs.resolve_job_ref", lambda job_id: job)
        return job

    @pytest.fixture
    def cron_api_calls(self, monkeypatch):
        calls = []

        def fake_cron_api(**kwargs):
            calls.append(kwargs)
            action = kwargs["action"]
            if action == "create":
                return {
                    "success": True,
                    "job_id": "job-1",
                    "name": "Created Job",
                    "schedule": kwargs["schedule"],
                    "next_run_at": "2026-06-14T12:00:00Z",
                    "job": {
                        "job_id": "job-1",
                        "name": "Created Job",
                        "schedule": kwargs["schedule"],
                    },
                }
            return {
                "success": True,
                "job": {
                    "job_id": kwargs["job_id"],
                    "name": "Updated Job",
                    "schedule": kwargs.get("schedule") or "30m",
                    "skills": kwargs.get("skills") or [],
                },
            }

        monkeypatch.setattr("hermes_cli.cron._cron_api", fake_cron_api)
        return calls

    def test_cron_edit_blocks_gateway_restart_prompt(self, edit_job, cron_api_calls, capsys):
        rc = cron_edit(self._edit_args(prompt="hermes gateway restart"))

        assert rc == 1
        assert cron_api_calls == []
        assert "Blocked" in capsys.readouterr().out

    def test_cron_edit_blocks_multiline_kill_prompt(self, edit_job, cron_api_calls, capsys):
        rc = cron_edit(self._edit_args(prompt='pkill -f "hermes"\npkill -9 "gateway"'))

        assert rc == 1
        assert cron_api_calls == []
        assert "Blocked" in capsys.readouterr().out

    def test_cron_edit_blocks_pgrep_kill_prompt(self, edit_job, cron_api_calls, capsys):
        rc = cron_edit(self._edit_args(prompt="kill $(pgrep -f hermes)"))

        assert rc == 1
        assert cron_api_calls == []
        assert "Blocked" in capsys.readouterr().out

    def test_cron_edit_blocks_lifecycle_command_in_skills(self, edit_job, cron_api_calls, capsys):
        rc = cron_edit(self._edit_args(skills=["hermes gateway restart"]))

        assert rc == 1
        assert cron_api_calls == []
        assert "Blocked" in capsys.readouterr().out

    @pytest.mark.parametrize("overrides", [
        {"prompt": 'pkill -f "hermes"\npkill -9 "gateway"'},
        {"prompt": "kill $(pgrep -f hermes)"},
        {"skills": ["hermes gateway restart"]},
    ])
    def test_cron_create_blocks_lifecycle_variants(self, overrides, cron_api_calls, capsys):
        rc = cron_create(self._create_args(**overrides))

        assert rc == 1
        assert cron_api_calls == []
        assert "Blocked" in capsys.readouterr().out

    def test_cron_edit_allows_benign_gateway_restart_report_prompt(
        self,
        edit_job,
        cron_api_calls,
        capsys,
    ):
        prompt = "summarize the API gateway logs and report restart events"

        rc = cron_edit(self._edit_args(prompt=prompt))

        assert rc == 0
        assert cron_api_calls == [
            {
                "action": "update",
                "job_id": "job-1",
                "schedule": None,
                "prompt": prompt,
                "name": None,
                "deliver": None,
                "repeat": None,
                "skills": None,
                "script": None,
                "workdir": None,
                "no_agent": None,
            }
        ]
        assert "Updated job" in capsys.readouterr().out

    def test_cron_create_allows_benign_gateway_restart_report_prompt(self, cron_api_calls, capsys):
        prompt = "summarize the API gateway logs and report restart events"

        rc = cron_create(self._create_args(prompt=prompt))

        assert rc == 0
        assert cron_api_calls[0]["action"] == "create"
        assert cron_api_calls[0]["prompt"] == prompt
        assert "Created job" in capsys.readouterr().out

    def test_cron_edit_blocks_unreadable_script_path(
        self,
        tmp_path,
        edit_job,
        cron_api_calls,
        capsys,
    ):
        missing_script = tmp_path / "missing.sh"

        rc = cron_edit(self._edit_args(prompt="Check server health", script=str(missing_script)))

        assert rc == 1
        assert cron_api_calls == []
        out = capsys.readouterr().out
        assert "Blocked" in out
        assert "could not be read" in out

    def test_cron_edit_allows_benign_prompt_without_script(self, edit_job, cron_api_calls, capsys):
        rc = cron_edit(self._edit_args(prompt="Check server health", script=None))

        assert rc == 0
        assert cron_api_calls[0]["action"] == "update"
        assert cron_api_calls[0]["script"] is None
        assert "Updated job" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Defense 1: gateway stop/restart refuse inside gateway
# ---------------------------------------------------------------------------

class TestGatewaySelfTargetingGuard:
    """Verify hermes gateway stop/restart refuse when _HERMES_GATEWAY=1."""

    def test_stop_refuses_inside_gateway(self, monkeypatch):
        monkeypatch.setenv("_HERMES_GATEWAY", "1")
        from hermes_cli.gateway import gateway_command
        args = Namespace(gateway_command="stop", all=False, system=False)
        with pytest.raises(SystemExit) as exc_info:
            gateway_command(args)
        assert exc_info.value.code == 1

    def test_restart_refuses_inside_gateway(self, monkeypatch):
        monkeypatch.setenv("_HERMES_GATEWAY", "1")
        from hermes_cli.gateway import gateway_command
        args = Namespace(gateway_command="restart", all=False, system=False)
        with pytest.raises(SystemExit) as exc_info:
            gateway_command(args)
        assert exc_info.value.code == 1

    def test_stop_allows_outside_gateway(self, monkeypatch):
        # With the gateway marker unset, the self-targeting guard must NOT
        # fire. Prove control reaches the real stop path (rather than driving
        # real signal delivery, which would trip the live-system guard) by
        # short-circuiting the first downstream call with a sentinel.
        monkeypatch.delenv("_HERMES_GATEWAY", raising=False)
        import hermes_cli.gateway as gw

        class _Reached(Exception):
            pass

        def _sentinel(*a, **k):
            raise _Reached()

        monkeypatch.setattr(gw, "_dispatch_via_service_manager_if_s6", _sentinel)
        monkeypatch.setattr(gw, "_dispatch_all_via_service_manager_if_s6", _sentinel)
        args = Namespace(gateway_command="stop", all=False, system=False)
        with pytest.raises(_Reached):
            gw.gateway_command(args)

    def test_restart_allows_outside_gateway(self, monkeypatch):
        # Same as above for restart: guard must not fire when the marker is
        # unset. The first thing restart does after the guard is the s6
        # dispatch check — sentinel it so we never reach real signal delivery.
        monkeypatch.delenv("_HERMES_GATEWAY", raising=False)
        import hermes_cli.gateway as gw

        class _Reached(Exception):
            pass

        def _sentinel(*a, **k):
            raise _Reached()

        monkeypatch.setattr(gw, "_dispatch_via_service_manager_if_s6", _sentinel)
        monkeypatch.setattr(gw, "_dispatch_all_via_service_manager_if_s6", _sentinel)
        args = Namespace(gateway_command="restart", all=False, system=False)
        with pytest.raises(_Reached):
            gw.gateway_command(args)


# ---------------------------------------------------------------------------
# Defense 3: terminal_tool hard-blocks gateway lifecycle commands inside gateway
# ---------------------------------------------------------------------------

class TestTerminalToolGatewayLifecycleGuard:
    """terminal_tool must refuse gateway lifecycle commands when _HERMES_GATEWAY=1.

    Issue #37453: systemctl --user restart hermes-gateway runs as a child of the
    gateway process.  When systemd delivers SIGTERM the gateway kills its own
    restart command mid-execution — the service may never restart.  The guard
    must fire before execution, unconditionally (force=True cannot bypass it).
    """

    def _make_fake_env(self):
        class _FakeEnv:
            env = {}
            def execute(self, command, **kwargs):  # pragma: no cover
                raise AssertionError("execute must not be reached")
        return _FakeEnv()

    def _minimal_config(self):
        return {"env_type": "local", "cwd": "/tmp", "timeout": 60, "lifetime_seconds": 3600}

    def _patch_env(self, monkeypatch, fake_env, *, inside_gateway: bool):
        import tools.terminal_tool as tt
        eid = "default"
        monkeypatch.setattr(tt, "_active_environments", {eid: fake_env})
        monkeypatch.setattr(tt, "_last_activity", {eid: 0.0})
        monkeypatch.setattr(tt, "_task_env_overrides", {})
        monkeypatch.setattr(tt, "_get_env_config", self._minimal_config)
        if inside_gateway:
            monkeypatch.setenv("_HERMES_GATEWAY", "1")
        else:
            monkeypatch.delenv("_HERMES_GATEWAY", raising=False)

    @pytest.mark.parametrize("cmd", [
        "systemctl restart hermes-gateway",
        "systemctl --user restart hermes-gateway",
        "systemctl stop hermes-gateway.service",
        "hermes gateway restart",
        "launchctl kickstart gui/501/ai.hermes.gateway",
        "pkill -f hermes.*gateway",
    ])
    def test_blocks_lifecycle_commands_inside_gateway(self, monkeypatch, cmd):
        import tools.terminal_tool as tt
        self._patch_env(monkeypatch, self._make_fake_env(), inside_gateway=True)

        result = json.loads(tt.terminal_tool(command=cmd))

        assert result["exit_code"] == 1
        assert "Blocked" in result["error"]

    def test_force_true_cannot_bypass_block(self, monkeypatch):
        import tools.terminal_tool as tt
        self._patch_env(monkeypatch, self._make_fake_env(), inside_gateway=True)

        result = json.loads(tt.terminal_tool(
            command="systemctl restart hermes-gateway", force=True
        ))

        assert result["exit_code"] == 1
        assert "Blocked" in result["error"]

    def test_safe_systemctl_commands_pass_through(self, monkeypatch):
        """Non-hermes systemctl commands must not be blocked by this guard."""
        import tools.terminal_tool as tt

        calls = []

        class _FakeEnv:
            env = {}
            def execute(self, command, **kwargs):
                calls.append(command)
                return {"output": "Active: running", "returncode": 0}

        self._patch_env(monkeypatch, _FakeEnv(), inside_gateway=True)
        monkeypatch.setattr(tt, "_check_all_guards", lambda cmd, env: {"approved": True})

        result = json.loads(tt.terminal_tool(command="systemctl status nginx"))

        assert result["exit_code"] == 0
        assert calls == ["systemctl status nginx"]

    def test_guard_inactive_outside_gateway(self, monkeypatch):
        """Without _HERMES_GATEWAY=1 the lifecycle guard must not fire."""
        import tools.terminal_tool as tt

        calls = []

        class _FakeEnv:
            env = {}
            def execute(self, command, **kwargs):
                calls.append(command)
                return {"output": "restarting...", "returncode": 0}

        self._patch_env(monkeypatch, _FakeEnv(), inside_gateway=False)
        monkeypatch.setattr(tt, "_check_all_guards", lambda cmd, env: {"approved": True})

        result = json.loads(tt.terminal_tool(command="systemctl restart hermes-gateway"))

        # Outside the gateway the lifecycle guard doesn't block — the normal
        # approval flow handles it (here mocked as approved).
        assert result["exit_code"] == 0
        assert calls == ["systemctl restart hermes-gateway"]
