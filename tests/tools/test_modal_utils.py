"""Characterization tests for the pure shell-wrapping helpers in
``tools.environments.modal_utils``.

Covers the two side-effect-free command wrappers (the surrounding
``BaseModalExecutionEnvironment`` machinery is covered by
``tests/tools/test_managed_modal_environment.py``):

* ``wrap_modal_sudo_pipe`` — feeds a sudo password via a ``printf`` pipe.
* ``wrap_modal_stdin_heredoc`` — appends stdin as a collision-free heredoc.
"""
from __future__ import annotations

from unittest.mock import patch

from tools.environments.modal_utils import (
    wrap_modal_stdin_heredoc,
    wrap_modal_sudo_pipe,
)

# ─── wrap_modal_sudo_pipe ───────────────────────────────────────────────────


def test_wrap_modal_sudo_pipe_basic():
    assert wrap_modal_sudo_pipe("cmd", "pw") == "printf '%s\\n' pw | cmd"


def test_wrap_modal_sudo_pipe_quotes_password_with_special_chars():
    # A password with a space must be shell-quoted (shlex wraps in single quotes).
    assert (
        wrap_modal_sudo_pipe("cmd", "p@ss w0rd")
        == "printf '%s\\n' 'p@ss w0rd' | cmd"
    )


def test_wrap_modal_sudo_pipe_strips_trailing_whitespace():
    assert wrap_modal_sudo_pipe("cmd", "pw\n\n") == "printf '%s\\n' pw | cmd"


def test_wrap_modal_sudo_pipe_empty_password_is_quoted_empty():
    assert wrap_modal_sudo_pipe("cmd", "") == "printf '%s\\n' '' | cmd"


def test_wrap_modal_sudo_pipe_command_appended_verbatim():
    # The command itself is NOT quoted — it is the caller's full shell line.
    assert (
        wrap_modal_sudo_pipe("sudo -S apt-get update", "pw")
        == "printf '%s\\n' pw | sudo -S apt-get update"
    )


# ─── wrap_modal_stdin_heredoc ───────────────────────────────────────────────


def _split_heredoc(out: str):
    lines = out.split("\n")
    return lines[0], "\n".join(lines[1:-1]), lines[-1]


def test_wrap_modal_stdin_heredoc_structure():
    out = wrap_modal_stdin_heredoc("run", "line1\nline2")
    first, body, marker = _split_heredoc(out)
    # First line: `<command> << '<marker>'` with an 8-hex-digit marker.
    assert first.startswith("run << 'HERMES_EOF_")
    assert first.endswith("'")
    # Body is the stdin data verbatim; final line repeats the marker.
    assert body == "line1\nline2"
    assert marker == first[len("run << '"):-1]
    assert len(marker) == len("HERMES_EOF_") + 8


def test_wrap_modal_stdin_heredoc_marker_absent_from_data():
    data = "some stdin payload"
    out = wrap_modal_stdin_heredoc("run", data)
    _, _, marker = _split_heredoc(out)
    # The chosen marker must not occur inside the data (else the heredoc would
    # terminate early).
    assert marker not in data


class _FakeUUID:
    def __init__(self, hexstr: str):
        self.hex = hexstr


def test_wrap_modal_stdin_heredoc_regenerates_marker_on_collision():
    # Data already contains the first candidate marker → the loop must draw a
    # new one and use the second candidate.
    data = "payload with HERMES_EOF_deadbeef embedded"
    seq = [_FakeUUID("deadbeef1111"), _FakeUUID("cafebabe2222")]
    with patch("tools.environments.modal_utils.uuid.uuid4", side_effect=seq):
        out = wrap_modal_stdin_heredoc("run", data)
    first, _, marker = _split_heredoc(out)
    assert first == "run << 'HERMES_EOF_cafebabe'"
    assert marker == "HERMES_EOF_cafebabe"
    assert "HERMES_EOF_deadbeef'" not in first  # the colliding one was rejected
