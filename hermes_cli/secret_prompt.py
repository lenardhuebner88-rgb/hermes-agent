"""Secret input prompts with masked typing feedback."""

from __future__ import annotations

import getpass
import os
import sys
from collections.abc import Callable


_BACKSPACE_CHARS = {"\b", "\x7f"}
_ENTER_CHARS = {"\r", "\n"}
_EOF_CHARS = {"\x04", "\x1a"}

# Terminator bytes for CSI (\x1b[...) and SS3 (\x1b O...) escape sequences.
# A CSI/SS3 sequence ends at the first byte in the range 0x40–0x7E.
_CSI_TERMINATORS = set(chr(c) for c in range(0x40, 0x7F))

# Introducer bytes that follow \x1b to form a multi-byte escape sequence.
_ESCAPE_INTRODUCERS = frozenset({"[", "O"})


def _drain_escape_sequence(read_char: Callable[[], str]) -> str | None:
    """Consume the remaining bytes of a terminal escape sequence.

    Called after an initial ``\\x1b`` has been read.  Terminals send
    multi-byte sequences for navigation keys (arrows, Delete, Home, End)
    such as ``\\x1b[A`` (Up) or ``\\x1b[3~`` (Delete).  Only swallowing the
    leading ``\\x1b`` leaves the suffix (``[A``, ``3~``) to be treated as
    secret text, corrupting the entered value.

    If the byte following ``\\x1b`` is not a CSI/SS3 introducer (``[`` or
    ``O``), it's likely the next legitimate keystroke (e.g. a lone Escape
    followed by Enter).  In that case the byte is returned to the caller
    for normal processing.  ``None`` is returned when all sequence bytes
    were consumed.
    """
    nxt = read_char()
    if nxt == "":
        # Bare ESC with nothing queued behind it — nothing to return.
        return None
    if nxt in _ESCAPE_INTRODUCERS:
        # CSI (ESC [) or SS3 (ESC O) sequence — consume until a terminator.
        # The introducer ('[' or 'O') itself is in the 0x40–0x7E range but
        # is NOT a terminator; start scanning from the byte after it.
        nxt = read_char()
        while nxt != "":
            if nxt in _CSI_TERMINATORS:
                return None
            nxt = read_char()
        return None
    # Not part of a recognised escape sequence — hand it back to the caller.
    return nxt


def _collect_masked_input(
    read_char: Callable[[], str],
    write: Callable[[str], object],
    prompt: str,
    *,
    mask: str = "*",
) -> str:
    """Read one secret line while writing a mask character per typed char."""
    value: list[str] = []
    write(prompt)

    pending: str | None = None
    while True:
        if pending is not None:
            ch = pending
            pending = None
        else:
            ch = read_char()
        if ch == "":
            write("\r\n")
            raise EOFError
        if ch in _ENTER_CHARS:
            write("\r\n")
            return "".join(value)
        if ch == "\x03":
            write("\r\n")
            raise KeyboardInterrupt
        if ch in _EOF_CHARS:
            write("\r\n")
            raise EOFError
        if ch in _BACKSPACE_CHARS:
            if value:
                value.pop()
                write("\b \b")
            continue
        if ch == "\x1b":
            # Consume the full escape-prefixed sequence so navigation bytes
            # don't become secret text. If the byte after ESC isn't a
            # sequence introducer, it's returned for normal processing.
            leftover = _drain_escape_sequence(read_char)
            if leftover is not None:
                pending = leftover
            continue

        value.append(ch)
        if mask:
            write(mask)


def masked_secret_prompt(prompt: str, *, mask: str = "*") -> str:
    """Prompt for a secret while showing masked typing feedback.

    Falls back to ``getpass.getpass`` when stdin/stdout are not interactive or
    when raw terminal handling is unavailable.
    """
    stdin = sys.stdin
    stdout = sys.stdout

    if not _stream_is_tty(stdin) or not _stream_is_tty(stdout):
        return getpass.getpass(prompt)

    if os.name == "nt":
        try:
            return _masked_secret_prompt_windows(prompt, mask=mask)
        except (KeyboardInterrupt, EOFError):
            raise
        except Exception:
            return getpass.getpass(prompt)

    try:
        return _masked_secret_prompt_posix(prompt, mask=mask)
    except (KeyboardInterrupt, EOFError):
        raise
    except Exception:
        return getpass.getpass(prompt)


def _stream_is_tty(stream) -> bool:
    try:
        return bool(stream.isatty())
    except Exception:
        return False


def _masked_secret_prompt_windows(prompt: str, *, mask: str) -> str:
    import msvcrt

    def read_char() -> str:
        ch = msvcrt.getwch()
        if ch in {"\x00", "\xe0"}:
            msvcrt.getwch()
            return "\x1b"
        return ch

    def write(text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    return _collect_masked_input(read_char, write, prompt, mask=mask)


def _masked_secret_prompt_posix(prompt: str, *, mask: str) -> str:
    import termios
    import tty

    fd = sys.stdin.fileno()
    old_attrs = termios.tcgetattr(fd)

    def read_char() -> str:
        return sys.stdin.read(1)

    def write(text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    try:
        tty.setraw(fd)
        return _collect_masked_input(read_char, write, prompt, mask=mask)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
