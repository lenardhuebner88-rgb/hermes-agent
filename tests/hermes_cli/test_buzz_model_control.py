from __future__ import annotations

import json
import stat
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli.buzz_model_control import (
    BuzzModelControlService,
    CommandResult,
    register_buzz_model_control_routes,
)

# Real ~/.config/buzz-agents/claude.env structure (private key + auth tag
# swapped for obviously-fake values; everything else — comments, quoting,
# line order, blank lines — copied verbatim from the live file).
_REAL_ENV_FIXTURE = """\
BUZZ_PRIVATE_KEY=FAKESECRETFAKESECRETFAKESECRETFAKESECRETFAKESECRETFAKESECRETFA
BUZZ_RELAY_URL=wss://huebners.tail50819a.ts.net:9444
BUZZ_ACP_AGENT_COMMAND=claude-agent-acp
BUZZ_ACP_AGENT_ARGS=
BUZZ_ACP_SUBSCRIBE=config
BUZZ_ACP_AGENTS=1
BUZZ_ACP_MAX_TURNS_PER_SESSION=100
BUZZ_ACP_CONTEXT_MESSAGE_LIMIT=20
BUZZ_ACP_IDLE_TIMEOUT=600
BUZZ_ACP_AGENT_OWNER=447eedf4a7fa32f4444396dbd511c59aa7521cd003396113fe121882fcdb20cd
BUZZ_ACP_SYSTEM_PROMPT_FILE=/home/piet/.config/buzz-agents/prompts/system-claude.md
BUZZ_ACP_CONFIG=/home/piet/.config/buzz-agents/claude-rules.toml
BUZZ_ACP_MODEL="opus[1m]"
RUST_LOG=buzz_acp=info,acp::tool=info,acp::plan=info,pool::model=info
CLAUDE_CODE_EFFORT_LEVEL=medium

# Workflow-Zustellung (2026-08-01): comment lines must round-trip untouched.
BUZZ_ACP_RESPOND_TO=allowlist
BUZZ_ACP_RESPOND_TO_ALLOWLIST=7d122ae6eff098ccb06b5a472ec6e702cb28110a66049bec7b2bc93cb09f5daf
GIT_CONFIG_GLOBAL=/home/piet/.config/buzz-agents/gitconfig/claude

# NIP-OA-Attestation fake tag, must never be echoed by any endpoint.
BUZZ_AUTH_TAG='["auth","447eedf4a7fa32f4444396dbd511c59aa7521cd003396113fe121882fcdb20cd","","FAKESIGFAKESIGFAKESIGFAKESIGFAKESIGFAKESIGFAKESIGFAKESIGFAKESIGFAKESIGFAKESIGFAKESIGFAKESIGFAKE"]'
"""

_REAL_MODELS_JSON = json.dumps({
    "agent": {"name": "@agentclientprotocol/claude-agent-acp", "version": "0.64.0"},
    "stable": {
        "configOptions": [
            {
                "category": "model",
                "currentValue": "opus[1m]",
                "description": "AI model to use",
                "id": "model",
                "name": "Model",
                "options": [
                    {
                        "description": "Opus 5 with 1M context",
                        "name": "Default (recommended)",
                        "value": "default",
                    },
                    {
                        "description": "Opus 5 with 1M context",
                        "name": "Opus (1M context)",
                        "value": "opus[1m]",
                    },
                    {
                        "description": "Sonnet 5",
                        "name": "Sonnet",
                        "value": "sonnet",
                    },
                ],
                "type": "select",
            }
        ]
    },
    "unstable": None,
})


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.models_returncode = 0
        self.models_stdout = _REAL_MODELS_JSON
        self.restart_returncode = 0
        self.is_active_returncode = 0
        self.journal_hit = True

    def run(self, args: list[str], *, timeout: float | None = None) -> CommandResult:
        self.calls.append(list(args))
        if "models" in args and "--json" in args:
            return CommandResult(self.models_returncode, self.models_stdout, "")
        if args[:3] == ["systemctl", "--user", "show"]:
            return CommandResult(
                0,
                "ActiveState=active\nActiveEnterTimestamp=Wed 2026-08-05 09:00:00 UTC",
                "",
            )
        if args[:3] == ["systemctl", "--user", "restart"]:
            return CommandResult(self.restart_returncode, "", "")
        if args[:3] == ["systemctl", "--user", "is-active"]:
            return CommandResult(
                self.is_active_returncode,
                "active\n" if self.is_active_returncode == 0 else "failed\n",
                "",
            )
        if args and args[0] == "journalctl":
            if self.journal_hit:
                return CommandResult(0, "subscribed to membership notifications\n", "")
            return CommandResult(0, "\n", "")
        raise AssertionError(f"unexpected command: {args}")


def _service(
    tmp_path: Path,
    runner: FakeRunner,
    *,
    stems: tuple[str, ...] = ("claude",),
    fixture: str = _REAL_ENV_FIXTURE,
) -> BuzzModelControlService:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    for stem in stems:
        path = config_dir / f"{stem}.env"
        path.write_text(fixture, encoding="utf-8")
        path.chmod(0o640)
    return BuzzModelControlService(
        runner=runner,
        config_dir=config_dir,
        sleep=lambda _seconds: None,
        restart_wait_timeout=0.01,
    )


def _app(service: BuzzModelControlService) -> TestClient:
    app = FastAPI()
    register_buzz_model_control_routes(app, service_factory=lambda: service)
    return TestClient(app)


def test_list_agents_never_leaks_the_private_key_or_auth_tag(tmp_path: Path) -> None:
    runner = FakeRunner()
    service = _service(tmp_path, runner)
    client = _app(service)

    response = client.get("/api/buzz/agents")

    assert response.status_code == 200
    body_text = response.text
    assert "FAKESECRETFAKESECRET" not in body_text
    assert "FAKESIGFAKESIG" not in body_text
    assert "BUZZ_PRIVATE_KEY" not in body_text
    assert "BUZZ_AUTH_TAG" not in body_text
    agents = response.json()["agents"]
    assert agents == [
        {
            "stem": "claude",
            "display_name": "Claude",
            "model": "opus[1m]",
            "agent_command": "claude-agent-acp",
            "active_state": "active",
            "last_start": "Wed 2026-08-05 09:00:00 UTC",
        }
    ]


def test_get_models_never_leaks_secrets_either(tmp_path: Path) -> None:
    runner = FakeRunner()
    service = _service(tmp_path, runner)
    client = _app(service)

    response = client.get("/api/buzz/agents/claude/models")

    assert response.status_code == 200
    assert "FAKESECRETFAKESECRET" not in response.text
    assert "FAKESIGFAKESIG" not in response.text
    body = response.json()
    assert body["current_model"] == "opus[1m]"
    assert body["error"] is None
    assert {m["value"] for m in body["models"]} == {"default", "opus[1m]", "sonnet"}


def test_models_are_cached_within_ttl(tmp_path: Path) -> None:
    runner = FakeRunner()
    service = _service(tmp_path, runner)

    first = service.get_models("claude")
    second = service.get_models("claude")

    assert first["cached"] is False
    assert second["cached"] is True
    probe_calls = [call for call in runner.calls if "models" in call and "--json" in call]
    assert len(probe_calls) == 1


def test_models_probe_failure_returns_honest_error_and_current_model(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    runner.models_returncode = 1
    service = _service(tmp_path, runner)
    client = _app(service)

    response = client.get("/api/buzz/agents/claude/models")

    assert response.status_code == 200
    body = response.json()
    assert body["models"] is None
    assert body["current_model"] == "opus[1m]"
    assert body["error"]["code"] == "models_probe_failed"


def test_set_model_round_trips_env_file_preserving_every_other_line(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    service = _service(tmp_path, runner)
    original_mode = stat.S_IMODE((service.config_dir / "claude.env").stat().st_mode)

    result = service.set_model("claude", "sonnet")

    assert result["old_model"] == "opus[1m]"
    assert result["new_model"] == "sonnet"
    assert result["restart"] == {"restarted": True, "ready": True, "error": None}

    new_text = (service.config_dir / "claude.env").read_text(encoding="utf-8")
    new_lines = new_text.splitlines()
    old_lines = _REAL_ENV_FIXTURE.splitlines()
    assert len(new_lines) == len(old_lines)
    for old_line, new_line in zip(old_lines, new_lines):
        if old_line.startswith("BUZZ_ACP_MODEL="):
            assert new_line == 'BUZZ_ACP_MODEL="sonnet"'
        else:
            assert old_line == new_line
    assert "FAKESECRETFAKESECRET" in new_text  # untouched secret line still present on disk
    new_mode = stat.S_IMODE((service.config_dir / "claude.env").stat().st_mode)
    assert new_mode == original_mode


def test_set_model_marks_unverified_when_no_probe_happened_yet(tmp_path: Path) -> None:
    runner = FakeRunner()
    service = _service(tmp_path, runner)

    result = service.set_model("claude", "sonnet")

    assert result["unverified"] is True


def test_set_model_marks_unverified_when_not_in_a_prior_probed_list(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    service = _service(tmp_path, runner)
    service.get_models("claude")  # populates the cache with default/opus[1m]/sonnet

    result = service.set_model("claude", "some-future-model-id")

    assert result["unverified"] is True


def test_set_model_marks_verified_when_in_a_prior_probed_list(tmp_path: Path) -> None:
    runner = FakeRunner()
    service = _service(tmp_path, runner)
    service.get_models("claude")  # populates the cache with default/opus[1m]/sonnet

    result = service.set_model("claude", "sonnet")

    assert result["unverified"] is False


def test_set_model_reports_restart_failure_without_raising(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.restart_returncode = 1
    service = _service(tmp_path, runner)

    result = service.set_model("claude", "sonnet")

    assert result["restart"] == {
        "restarted": False,
        "ready": False,
        "error": "restart_failed",
    }


def test_set_model_reports_readiness_timeout_without_raising(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.journal_hit = False
    service = _service(tmp_path, runner)

    result = service.set_model("claude", "sonnet")

    assert result["restart"] == {
        "restarted": True,
        "ready": False,
        "error": "readiness_timeout",
    }


def test_malicious_stem_with_path_traversal_never_reaches_a_shell_call(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    service = _service(tmp_path, runner)
    client = _app(service)

    response = client.get("/api/buzz/agents/../etc/models")

    assert response.status_code in (400, 404)
    assert runner.calls == []


def test_malicious_stem_with_semicolon_is_rejected(tmp_path: Path) -> None:
    runner = FakeRunner()
    service = _service(tmp_path, runner)
    client = _app(service)

    response = client.get("/api/buzz/agents/claude;rm -rf/models")

    assert response.status_code in (400, 404)
    assert runner.calls == []


def test_malicious_stem_with_space_is_rejected_on_post(tmp_path: Path) -> None:
    runner = FakeRunner()
    service = _service(tmp_path, runner)
    client = _app(service)

    response = client.post(
        "/api/buzz/agents/claude space/model", json={"model": "sonnet"}
    )

    assert response.status_code in (400, 404)
    assert runner.calls == []


def test_unknown_stem_is_404_not_a_shell_call(tmp_path: Path) -> None:
    runner = FakeRunner()
    service = _service(tmp_path, runner)
    client = _app(service)

    response = client.get("/api/buzz/agents/nonexistent/models")

    assert response.status_code == 404
    assert runner.calls == []


def test_list_agents_covers_all_configured_stems(tmp_path: Path) -> None:
    runner = FakeRunner()
    service = _service(tmp_path, runner, stems=("claude", "grok", "kimi"))
    client = _app(service)

    response = client.get("/api/buzz/agents")

    stems = [agent["stem"] for agent in response.json()["agents"]]
    assert stems == ["claude", "grok", "kimi"]
