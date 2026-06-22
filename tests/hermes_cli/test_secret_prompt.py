import pytest

from hermes_cli.secret_prompt import _collect_masked_input, masked_secret_prompt


def _run_collect(chars: str):
    output: list[str] = []
    iterator = iter(chars)

    def read_char() -> str:
        return next(iterator, "")

    def write(text: str) -> None:
        output.append(text)

    value = _collect_masked_input(
        read_char,
        write,
        "API key: ",
    )
    return value, "".join(output)


def test_collect_masked_input_shows_feedback_without_echoing_secret():
    value, output = _run_collect("secret\n")

    assert value == "secret"
    assert output == "API key: ******\r\n"
    assert "secret" not in output


def test_collect_masked_input_handles_backspace():
    value, output = _run_collect("sec\x7fret\r")

    assert value == "seret"
    assert output == "API key: ***\b \b***\r\n"
    assert "secret" not in output


def test_collect_masked_input_raises_keyboard_interrupt():
    output: list[str] = []

    with pytest.raises(KeyboardInterrupt):
        _collect_masked_input(
            lambda: "\x03",
            output.append,
            "API key: ",
        )

    assert "".join(output) == "API key: \r\n"


def test_collect_masked_input_consumes_arrow_key_escape_sequence():
    """\\x1b[A (Up arrow) must not leak '[A' into the secret value."""
    value, output = _run_collect("sec\x1b[Aret\r")

    assert value == "secret"
    assert "secret" not in output


def test_collect_masked_input_consumes_all_arrow_keys():
    for seq, name in [("\x1b[A", "Up"), ("\x1b[B", "Down"), ("\x1b[C", "Right"), ("\x1b[D", "Left")]:
        value, output = _run_collect(f"ab{seq}cd\r")
        assert value == "abcd", f"arrow {name} leaked into secret"
        assert "secret" not in output


def test_collect_masked_input_consumes_delete_key():
    """\\x1b[3~ (Delete) must not leak '[3~' into the secret value."""
    value, output = _run_collect("ab\x1b[3~cd\r")

    assert value == "abcd"
    assert "secret" not in output


def test_collect_masked_input_consumes_home_end_csi():
    """\\x1b[H (Home) and \\x1b[F (End) must be fully consumed."""
    value, output = _run_collect("ab\x1b[Hcd\x1b[Fef\r")

    assert value == "abcdef"
    assert "secret" not in output


def test_collect_masked_input_consumes_ss3_home_end():
    """SS3 sequences (\\x1bOH, \\x1bOF) must be fully consumed."""
    value, output = _run_collect("ab\x1bOHcd\x1bOFef\r")

    assert value == "abcdef"
    assert "secret" not in output


def test_collect_masked_input_consumes_extended_csi_sequence():
    """Extended sequences like \\x1b[1;5A (Ctrl+Up) must be fully consumed."""
    value, output = _run_collect("ab\x1b[1;5Acd\r")

    assert value == "abcd"
    assert "secret" not in output


def test_collect_masked_input_bare_escape_does_not_block():
    """A lone ESC (no following bytes) should be ignored without blocking."""
    value, output = _run_collect("ab\x1b\r")

    assert value == "ab"
    assert "secret" not in output


def test_masked_secret_prompt_falls_back_to_getpass_for_non_tty(monkeypatch):
    class NonTty:
        def isatty(self):
            return False

    monkeypatch.setattr("sys.stdin", NonTty())
    monkeypatch.setattr("sys.stdout", NonTty())
    monkeypatch.setattr("getpass.getpass", lambda prompt: f"value from {prompt}")

    assert masked_secret_prompt("API key: ") == "value from API key: "
