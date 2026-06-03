"""Client-facing exception detail sanitizers."""
from __future__ import annotations

import logging
import re

_log = logging.getLogger(__name__)

_MAX_DETAIL_LEN = 300
_TRACEBACK_MARKER = "Traceback (most recent call last)"
_WINDOWS_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"[A-Za-z]:\\(?:[^\\/:*?\"<>|\s]+\\)*[^\\/:*?\"<>|\s]*"
)
_POSIX_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9:])"
    r"/(?!api(?:/|$))"
    r"(?:"
    r"(?:home|tmp|var|Users)(?:/[^\s'\"\)\]\}>:,;]*)*"
    r"|"
    r"(?:[A-Za-z0-9._-]+/)+[A-Za-z0-9._-][^\s'\"\)\]\}>:,;]*"
    r")"
)
_WHITESPACE_RE = re.compile(r"\s+")


def scrub_detail(text: str) -> str:
    """Return a one-line, path-scrubbed detail safe for client responses."""
    if text is None:
        return ""
    raw = str(text)
    if _TRACEBACK_MARKER in raw or "\n" in raw or "\r" in raw:
        return ""

    clean = _WINDOWS_PATH_RE.sub("<path>", raw)
    clean = _POSIX_PATH_RE.sub("<path>", clean)
    clean = _WHITESPACE_RE.sub(" ", clean).strip()
    if len(clean) > _MAX_DETAIL_LEN:
        clean = clean[:_MAX_DETAIL_LEN].rstrip()
    return clean


def safe_detail(exc: BaseException, generic: str, *, log) -> str:
    """Log full exception detail and return a scrubbed client-facing string."""
    logger = log or _log
    if hasattr(logger, "exception"):
        logger.exception(generic)
    else:
        logger.warning("%s: %s", generic, exc)
    return scrub_detail(str(exc)) or generic
