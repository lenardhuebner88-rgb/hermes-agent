"""Fork-owned: *what* each agent is doing, read from the systemd user journal.

:mod:`hermes_cli.buzz_agent_heartbeat` answers "is this agent working" by
counting matching journal lines. It deliberately never asks for ``MESSAGE``,
because ANSI colour makes journalctl emit that field as an array of byte values
instead of a string — so the counting path stays cheap and text-free.

This module pays that cost on purpose, because the count is not the answer the
operator wants. Measured on 2026-08-05 the same journal carried, per line:

    tool_call: Terminal (execute)
    tool_call: Edit (edit)
    tool_call: terminal: buzz canvas get --channel 7abf1e2a-6629-… (execute)
    tool_call_update: toolu_01Du2UAj9dWfDhJQ8VLoFXLA → in_progress

That is the difference between "12 Tool-Calls in 1 Stunde" and "codex läuft seit
40 s auf ``rg -n dispatch_once``". Nothing here reads Buzz's source, its config
or any key; it reads the journal of units the fork already starts.

Two properties are load-bearing and tested:

* **A parse failure is never silent.** :class:`Diagnostics` carries how many
  lines were seen against how many were understood. A format change upstream
  therefore surfaces as "480 Zeilen gesehen, 0 verstanden", not as an idle fleet.
* **Command lines are masked before they leave this process.** Agents run real
  shell commands, and a token pasted into one would otherwise travel to a phone.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol

from hermes_cli.buzz_agent_heartbeat import stem_for_unit

# Matches both `tool_call:` and `tool_call_update:`. The heartbeat module
# anchors on the trailing colon to *exclude* the updates; here they are wanted,
# because an update is what proves a long call is still alive rather than hung.
JOURNAL_ACTIVITY_PATTERN = "tool_call"

# A tighter window than the heartbeat's hour: this query decodes every message
# body, and the timeline a card shows is minutes deep, not hours.
DEFAULT_ACTIVITY_WINDOW_SECONDS = 1800
DEFAULT_CALLS_PER_AGENT = 20

# Labels come pre-truncated by the agent at ~80 characters, but a heredoc's
# first line can be arbitrary. Clamp again so one pathological line cannot
# dominate a payload.
MAX_LABEL_CHARS = 160

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_CALL_RE = re.compile(r"tool_call:\s*(?P<label>.+?)\s*$")
_UPDATE_RE = re.compile(
    r"tool_call_update:\s*(?P<call_id>\S+)\s*(?:→|->)\s*(?P<status>\S+)"
)
# `Terminal (execute)` / `terminal: python: from hermes_tools import terminal (execute)`
# — the kind is the *last* parenthesised group, and only when it is a bare word.
_KIND_RE = re.compile(r"\s*\((?P<kind>[a-z_]+)\)\s*$")

# Statuses that end a call. Anything else (`in_progress`, `?`) keeps it open.
_TERMINAL_STATUSES = frozenset({"completed", "failed", "error", "cancelled", "canceled"})

# Order matters. `Authorization: Bearer <token>` must be caught by the bearer
# rule first: the key/value rule would otherwise consume the word "Bearer" as
# the value, mask that, and leave the actual token in the clear.
#
# The key rule allows a prefix on the keyword (`HERMES_TOKEN`, `PG_PASSWORD`)
# because `\btoken\b` does not match inside an underscore-joined name — a word
# boundary sits at neither end. It requires a real `=` or `:` separator; masking
# on whitespace alone turned `hermes auth status` into `hermes auth ***`.
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{4,}"), "masked_bearer"),
    (
        re.compile(
            r"(?i)([A-Za-z0-9_.\-]*(?:token|password|passwd|secret|api[_-]?key|authorization))"
            r"(\s*[:=]\s*)([^\s'\"]{4,})"
        ),
        "masked_kv",
    ),
    (re.compile(r"(?i)(--(?:password|token|secret|api[_-]?key)[= ])(\S{4,})"), "masked_kv2"),
    (re.compile(r"\b(?:sk|rk)-[A-Za-z0-9_\-]{8,}"), "masked_token"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{8,}"), "masked_token"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{8,}"), "masked_token"),
    (re.compile(r"\bnsec1[02-9ac-hj-np-z]{20,}"), "masked_token"),
)

MASK = "***"


class CommandResult(Protocol):
    returncode: int
    stdout: str
    stderr: str


class Runner(Protocol):
    def run(self, args: list[str], *, timeout: float | None = None) -> CommandResult: ...


@dataclass(frozen=True)
class ToolCall:
    """One tool invocation as the journal recorded it."""

    at: float
    label: str
    kind: str

    def as_payload(self, *, now: float) -> dict[str, Any]:
        return {
            "at": _iso(self.at),
            "seconds_ago": max(0, int(now - self.at)),
            "label": self.label,
            "kind": self.kind,
        }


@dataclass
class AgentTimeline:
    """Every tool call one agent made inside the window, newest last."""

    stem: str
    calls: list[ToolCall] = field(default_factory=list)
    last_signal_at: float | None = None
    last_terminal_at: float | None = None

    @property
    def latest(self) -> ToolCall | None:
        """The newest call in the window. A fact, not an inference."""
        return self.calls[-1] if self.calls else None

    @property
    def open_since(self) -> float | None:
        """When the newest call started, *if* nothing has reported finishing since.

        This is a heuristic and the payload says so. The journal puts no call id
        on the ``tool_call:`` line and no label on the ``tool_call_update:``
        line, so the two streams cannot be joined by key — and calls genuinely
        overlap: three updates have been observed completing in the same second
        while further calls opened between them. Ordering is all there is.

        So the claim made here is deliberately weak: "no completion has been
        logged since this call started". A card may render that as *läuft*; it
        may not render a duration as if the call were known to still be running,
        because with overlapping calls that number would be confidently wrong.
        """
        latest = self.latest
        if latest is None:
            return None
        if self.last_terminal_at is not None and self.last_terminal_at >= latest.at:
            return None
        return latest.at

    def as_payload(self, *, now: float, limit: int = DEFAULT_CALLS_PER_AGENT) -> dict[str, Any]:
        latest = self.latest
        open_since = self.open_since
        return {
            "stem": self.stem,
            "calls_in_window": len(self.calls),
            "latest": None if latest is None else latest.as_payload(now=now),
            # True means "nothing has reported completing since it started" —
            # not "this call is proven to be running". See `open_since`.
            "looks_open": open_since is not None,
            "open_for_seconds": (
                None if open_since is None else max(0, int(now - open_since))
            ),
            # A signal younger than the last call means the agent is still
            # reporting progress on it — the difference between "busy" and "hung".
            "last_signal_seconds_ago": (
                None
                if self.last_signal_at is None
                else max(0, int(now - self.last_signal_at))
            ),
            "recent": [
                call.as_payload(now=now) for call in reversed(self.calls[-limit:])
            ],
        }


@dataclass(frozen=True)
class Diagnostics:
    """Proof that an empty result is a quiet fleet and not a broken parser."""

    lines_seen: int
    lines_understood: int

    @property
    def suspicious(self) -> bool:
        """Lines arrived and none of them parsed — a format change, not silence."""
        return self.lines_seen > 0 and self.lines_understood == 0

    def as_payload(self) -> dict[str, Any]:
        return {
            "lines_seen": self.lines_seen,
            "lines_understood": self.lines_understood,
            "suspicious": self.suspicious,
        }


def _iso(epoch_seconds: float) -> str:
    """Local-time ISO 8601 with an offset, so the app never has to guess a zone."""
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(epoch_seconds))


def decode_message(raw: Any) -> str | None:
    """journalctl's ``MESSAGE``, whichever of its two shapes it arrives in.

    A line without ANSI escapes comes back as a string; a coloured one — which
    is every line these agents write — comes back as an array of byte values.
    Reading only the first shape is the trap this whole module exists to avoid:
    it yields zero messages with exit code 0.
    """
    if isinstance(raw, str):
        return raw
    if isinstance(raw, (list, tuple)):
        try:
            return bytes(bytearray(int(b) & 0xFF for b in raw)).decode("utf-8", "replace")
        except (TypeError, ValueError):
            return None
    return None


def mask_secrets(text: str) -> str:
    """Blank anything that looks like a credential inside a logged command."""
    masked = text
    for pattern, mode in _SECRET_PATTERNS:
        if mode == "masked_kv":
            masked = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", masked)
        elif mode == "masked_kv2":
            masked = pattern.sub(lambda m: f"{m.group(1)}{MASK}", masked)
        elif mode == "masked_bearer":
            masked = pattern.sub(f"Bearer {MASK}", masked)
        else:
            masked = pattern.sub(MASK, masked)
    return masked


def split_kind(label: str) -> tuple[str, str]:
    """``"Terminal (execute)"`` → ``("Terminal", "execute")``.

    A label without a trailing kind keeps its full text and reports ``unknown``;
    that happens for the first physical line of a multi-line heredoc, which is
    still the most informative thing the journal has about that call.
    """
    match = _KIND_RE.search(label)
    if match is None:
        return label.strip(), "unknown"
    return label[: match.start()].strip(), match.group("kind")


def activity_command(stems: Iterable[str], *, window_seconds: int) -> list[str]:
    """One journalctl call for every agent, message bodies included.

    ``--output-fields`` must name ``MESSAGE`` explicitly: without it journalctl
    honours the field filter and the body never arrives, which reads as a fleet
    that made no calls at all.
    """
    args = ["journalctl", "--user"]
    for stem in stems:
        args += ["-u", f"buzz-agent@{stem}.service"]
    args += [
        "--since",
        f"-{int(window_seconds)}s",
        "--no-pager",
        "-o",
        "json",
        "--output-fields=_SYSTEMD_USER_UNIT,MESSAGE",
        "-g",
        JOURNAL_ACTIVITY_PATTERN,
    ]
    return args


def parse_activity(stdout: str) -> tuple[dict[str, AgentTimeline], Diagnostics]:
    """Fold journalctl's JSON lines into one timeline per stem.

    Only stems that appear are returned. The caller fills in the rest, because
    "made no call in this window" is a real answer that must not be confused
    with a query that failed.
    """
    timelines: dict[str, AgentTimeline] = {}
    seen = 0
    understood = 0

    for raw_line in stdout.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        seen += 1
        try:
            entry = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        stem = stem_for_unit(str(entry.get("_SYSTEMD_USER_UNIT", "")))
        if stem is None:
            continue
        try:
            at = int(entry.get("__REALTIME_TIMESTAMP")) / 1_000_000
        except (TypeError, ValueError):
            continue
        message = decode_message(entry.get("MESSAGE"))
        if message is None:
            continue
        plain = _ANSI_RE.sub("", message)

        update = _UPDATE_RE.search(plain)
        if update is not None:
            understood += 1
            timeline = timelines.setdefault(stem, AgentTimeline(stem=stem))
            timeline.last_signal_at = max(timeline.last_signal_at or 0.0, at)
            if update.group("status").strip().lower() in _TERMINAL_STATUSES:
                timeline.last_terminal_at = max(timeline.last_terminal_at or 0.0, at)
            continue

        call = _CALL_RE.search(plain)
        if call is None:
            continue
        understood += 1
        label, kind = split_kind(call.group("label"))
        timeline = timelines.setdefault(stem, AgentTimeline(stem=stem))
        timeline.calls.append(
            ToolCall(at=at, label=mask_secrets(label)[:MAX_LABEL_CHARS], kind=kind)
        )
        timeline.last_signal_at = max(timeline.last_signal_at or 0.0, at)

    for timeline in timelines.values():
        # journalctl emits in time order, but a merged multi-unit query has been
        # seen to interleave by a few milliseconds; `current` depends on the last
        # element really being the newest.
        timeline.calls.sort(key=lambda call: call.at)

    return timelines, Diagnostics(lines_seen=seen, lines_understood=understood)


class ActivityReader:
    """Reads one bounded journal window and caches it, because journalctl is not free.

    A phone polling every eight seconds must not spawn a journal scan every
    eight seconds across eight units; the TTL collapses a burst of clients onto
    one read. The cached payload carries its own age, so a stale read is
    labelled rather than hidden.
    """

    def __init__(
        self,
        runner: Runner,
        *,
        window_seconds: int = DEFAULT_ACTIVITY_WINDOW_SECONDS,
        timeout: float = 15.0,
        ttl_seconds: float = 5.0,
        clock: Any = time.time,
    ) -> None:
        self.runner = runner
        self.window_seconds = window_seconds
        self.timeout = timeout
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._cache: tuple[float, dict[str, AgentTimeline], Diagnostics] | None = None
        self._cache_key: tuple[str, ...] = ()

    def read(
        self, stems: list[str]
    ) -> tuple[dict[str, AgentTimeline], Diagnostics, str | None]:
        """Timelines per stem, diagnostics, and an error code — never all three empty."""
        if not stems:
            return {}, Diagnostics(0, 0), None
        key = tuple(sorted(stems))
        now = self._clock()
        if (
            self._cache is not None
            and self._cache_key == key
            and now - self._cache[0] < self.ttl_seconds
        ):
            _, cached, diagnostics = self._cache
            return self._fill(cached, stems), diagnostics, None

        result = self.runner.run(
            activity_command(key, window_seconds=self.window_seconds),
            timeout=self.timeout,
        )
        if result.returncode != 0:
            # An empty journal and an unreadable one both yield nothing. Only
            # the exit code tells them apart, so it is the only thing trusted here.
            return {}, Diagnostics(0, 0), "journal_unavailable"
        timelines, diagnostics = parse_activity(result.stdout)
        self._cache = (now, timelines, diagnostics)
        self._cache_key = key
        return self._fill(timelines, stems), diagnostics, None

    @staticmethod
    def _fill(
        timelines: dict[str, AgentTimeline], stems: list[str]
    ) -> dict[str, AgentTimeline]:
        return {stem: timelines.get(stem, AgentTimeline(stem=stem)) for stem in stems}
