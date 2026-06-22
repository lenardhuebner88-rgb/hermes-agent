"""Tests for the Gateway-Lifecycle-Guard quote/heredoc stripping.

Covers the false-positive scenario where the literal pattern
"systemctl ... restart ... hermes" appears as string content (Python -c
literals, heredoc bodies, echo arguments) rather than as an actual
shell command.
"""

import sys
from pathlib import Path

# Make the in-repo tools/ package importable when running this file
# directly (``python -m pytest`` from the repo root already handles this).
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.terminal_tool import _strip_quotes  # noqa: E402
from hermes_cli.cron import _contains_gateway_lifecycle_command  # noqa: E402


# ---------------------------------------------------------------------------
# _strip_quote behavior
# ---------------------------------------------------------------------------

def test_strip_quotes_removes_single_quoted_content():
    """Guard pattern inside single quotes must be stripped."""
    raw = "python3 -c 'print(\"systemctl restart hermes-gateway\")'"
    stripped = _strip_quotes(raw)
    assert "systemctl restart hermes-gateway" not in stripped
    # The actual python3 invocation survives (we didn't nuke the whole cmd).
    assert "python3" in stripped


def test_strip_quotes_removes_double_quoted_content():
    """Guard pattern inside double quotes must be stripped."""
    raw = 'python3 -c "print(\'systemctl restart hermes-gateway\')"'
    stripped = _strip_quotes(raw)
    assert "systemctl restart hermes-gateway" not in stripped
    assert "python3" in stripped


def test_strip_quotes_removes_heredoc_body_unquoted():
    """Heredoc body (<<EOF ... EOF) must be stripped."""
    raw = "cat <<EOF\nsystemctl --user restart hermes-gateway.service\nEOF"
    stripped = _strip_quotes(raw)
    assert "systemctl --user restart hermes-gateway.service" not in stripped


def test_strip_quotes_removes_heredoc_body_quoted_marker():
    """Heredoc body with quoted marker (<<'EOF' ... EOF) must be stripped."""
    raw = "cat << 'EOF'\nsystemctl --user restart hermes-gateway.service\nEOF"
    stripped = _strip_quotes(raw)
    assert "systemctl --user restart hermes-gateway.service" not in stripped


def test_strip_quotes_removes_heredoc_body_indented_marker():
    """Heredoc body with indented closing marker (<<-'EOF' ... EOF) must be stripped."""
    raw = "cat <<-'EOF'\n    systemctl restart hermes-gateway.service\n    EOF"
    stripped = _strip_quotes(raw)
    assert "systemctl restart hermes-gateway.service" not in stripped


def test_strip_quotes_preserves_real_unquoted_command():
    """A real, unquoted systemctl restart command must NOT be stripped."""
    raw = "systemctl --user restart hermes-gateway.service"
    stripped = _strip_quotes(raw)
    assert "systemctl --user restart hermes-gateway.service" in stripped


# ---------------------------------------------------------------------------
# Guard integration: _contains_gateway_lifecycle_command(_strip_quotes(...))
# ---------------------------------------------------------------------------

def test_guard_fires_on_real_systemctl_restart():
    """A real `systemctl ... restart ... hermes` command must still be blocked."""
    cmd = "systemctl --user restart hermes-gateway.service"
    assert _contains_gateway_lifecycle_command(_strip_quotes(cmd)) is True


def test_guard_fires_on_hermes_gateway_restart():
    """A real `hermes gateway restart` command must still be blocked."""
    cmd = "hermes gateway restart"
    assert _contains_gateway_lifecycle_command(_strip_quotes(cmd)) is True


def test_guard_fires_on_profiled_hermes_gateway_restart():
    """Profile flags must not bypass the in-gateway lifecycle hard block."""
    cmd = "hermes --profile coder gateway restart"
    assert _contains_gateway_lifecycle_command(_strip_quotes(cmd)) is True


def test_guard_fires_on_module_gateway_restart():
    """The Python module entrypoint is equivalent to `hermes gateway restart`."""
    cmd = "python -m hermes_cli.main --profile coder gateway restart"
    assert _contains_gateway_lifecycle_command(_strip_quotes(cmd)) is True


def test_guard_fires_on_script_gateway_restart():
    """Direct script entrypoints must be blocked like the installed CLI."""
    cmd = "python3 ./hermes_cli/main.py --profile coder gateway stop"
    assert _contains_gateway_lifecycle_command(_strip_quotes(cmd)) is True


def test_guard_does_not_fire_on_quoted_python_dash_c():
    """Python -c with the pattern inside a quoted string must NOT be blocked."""
    cmd = 'python3 -c "print(\'systemctl restart hermes-gateway\')"'
    assert _contains_gateway_lifecycle_command(_strip_quotes(cmd)) is False


def test_guard_does_not_fire_on_heredoc_body():
    """Heredoc body mentioning the pattern must NOT be blocked."""
    cmd = "cat << 'EOF'\nsystemctl --user restart hermes-gateway.service\nEOF"
    assert _contains_gateway_lifecycle_command(_strip_quotes(cmd)) is False


def test_guard_does_not_fire_on_echo_with_quoted_argument():
    """echo argument quoting the pattern must NOT be blocked."""
    cmd = "echo 'do not run: systemctl restart hermes-gateway'"
    assert _contains_gateway_lifecycle_command(_strip_quotes(cmd)) is False
