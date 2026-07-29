"""Loop 5b (A-M2): one canonical gateway-lifecycle guard.

`cron/lifecycle_guard.py` is the single source of truth for the command
shapes; `hermes_cli/cron.py` delegates to it instead of maintaining a
parallel copy (the two diverged once already, fix/merge-losses-20260703).
These tests pin known attack strings against the canonical guard, pin the
legitimate negatives, and prove both enforcement surfaces agree on the whole
corpus.
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent.resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from cron.lifecycle_guard import contains_gateway_lifecycle_command  # noqa: E402

ATTACK_STRINGS = [
    # Branch A: hermes ... gateway restart|stop, incl. flags in between.
    "hermes gateway restart",
    "hermes gateway stop",
    "hermes --profile coder gateway restart",
    "hermes gateway --force restart",
    "sudo hermes gateway restart",
    "/usr/local/bin/hermes gateway stop",
    # Branch B/C: python module / script-path invocation equivalents.
    "python -m hermes_cli gateway restart",
    "python3 -m hermes_cli.main gateway stop",
    "python3.11 -m hermes_cli --profile x gateway restart",
    "python3 /opt/hermes/hermes_cli/main.py gateway restart",
    # Branch D: launchctl ops on a hermes-gateway label.
    "launchctl kickstart ai.hermes.gateway",
    "launchctl unload ~/Library/LaunchAgents/ai.hermes.gateway.plist",
    "launchctl stop hermes-gateway",
    # Branch E: systemctl ops on a hermes-gateway unit.
    "systemctl restart hermes-gateway",
    "systemctl --user stop hermes-gateway.service",
    "systemctl stop ai.hermes.gateway",
    # Branch F: pkill/pgrep against hermes processes.
    "pkill -f hermes",
    "pkill -f hermes.*gateway",
    "kill -9 $(pgrep hermes)",
    # Whitespace-normalization bypass: command split across lines.
    "hermes gateway\nrestart",
    "python3 -m hermes_cli.main\ngateway restart",
]

LEGITIMATE_NEGATIVES = [
    # Prose mentioning gateways/restarts is not a command shape.
    "summarize the API gateway logs and report restart events",
    "research the gateway architecture",
    "check server health and restart watchers",
    "Kong API gateway autoscaling and restart behavior",
    # `start` is benign — intentionally not blocked.
    "hermes gateway start",
    # Separator-limited lookahead must not cross a shell command boundary.
    "hermes cron list; echo gateway restart",
    "hermes status && systemctl --user status hermes-gateway",
    # Unrelated services/units/labels.
    "systemctl restart nginx",
    "launchctl unload ai.hermes.update-checker.plist",
    "pkill -f nginx",
    "pgrep -af postgres",
    # Plain status/inspect commands.
    "hermes cron status",
    "systemctl status hermes-gateway",
]


@pytest.mark.parametrize("text", ATTACK_STRINGS)
def test_canonical_guard_blocks_attack_strings(text):
    assert contains_gateway_lifecycle_command(text), f"should block: {text!r}"


@pytest.mark.parametrize("text", LEGITIMATE_NEGATIVES)
def test_canonical_guard_allows_legitimate_text(text):
    assert not contains_gateway_lifecycle_command(text), f"should allow: {text!r}"


class TestCliDelegatesToCanonicalGuard:
    """The CLI surface must not carry a second pattern definition."""

    def test_cli_has_no_parallel_pattern_definition(self):
        import hermes_cli.cron as cli_cron

        assert not hasattr(cli_cron, "_GATEWAY_LIFECYCLE_PATTERNS")
        assert not hasattr(cli_cron, "_HERMES_GATEWAY_ACTION")
        assert not hasattr(cli_cron, "_SERVICE_GATEWAY_NAME")

    @pytest.mark.parametrize("text", ATTACK_STRINGS + LEGITIMATE_NEGATIVES)
    def test_cli_wrapper_agrees_with_canonical_on_corpus(self, text):
        from hermes_cli.cron import _contains_gateway_lifecycle_command

        assert _contains_gateway_lifecycle_command(text) is (
            contains_gateway_lifecycle_command(text)
        )
