"""Fork-owned: per-agent work evidence read from the systemd user journal.

``ActiveState`` only says systemd holds the unit up. Measured on 2026-08-05 it
said ``active`` for all eight ``buzz-agent@*`` units while four of them had not
made a single tool call for hours — one since the previous morning. The honest
signal for "this agent is actually working" is the ACP tool-call stream the
agent logs, so that is what this module counts.

Nothing here reads Buzz's source, its config, or any private key; it only reads
the journal of units the fork already starts and restarts.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Iterable, Protocol

_UNIT_STEM_RE = re.compile(r"^buzz-agent@([a-z0-9-]+)\.service$")

# Matched by `journalctl -g`, which greps the *rendered* message.
#
# It must not span the tracing target: the agent logs with ANSI colour on even
# under systemd, so the rendered line reads
# `ESC[2macp::toolESC[0mESC[2m:ESC[0m tool_call: …`. The literal substring
# "acp::tool: tool_call" therefore does not exist, and grepping for it returns
# zero matches and exit 0 — indistinguishable from an idle agent. The message
# body itself is uncoloured, so anchoring inside it is safe.
#
# The trailing colon is load-bearing: it excludes `tool_call_update:`, which
# outnumbers real tool calls roughly six to one (2781 vs 462 over six hours).
JOURNAL_WORK_PATTERN = "tool_call:"

# Windows are bounded on purpose. The same query over 24 h returns 120k lines
# and takes 33 s; 60 min returns ~286 lines in 0.15 s. An agent quiet for
# longer than the window reports "nothing in this window" — never "never".
DEFAULT_WINDOW_SECONDS = 3600
DEFAULT_RECENT_SECONDS = 300


class CommandResult(Protocol):
    returncode: int
    stdout: str
    stderr: str


class Runner(Protocol):
    def run(self, args: list[str], *, timeout: float | None = None) -> CommandResult: ...


@dataclass(frozen=True)
class AgentHeartbeat:
    """What the journal proves about one agent inside the queried window."""

    stem: str
    tool_calls_window: int
    tool_calls_recent: int
    last_tool_call_at: float | None

    @property
    def working(self) -> bool:
        return self.tool_calls_recent > 0

    def as_payload(self, *, now: float | None = None) -> dict[str, Any]:
        """The wire shape.

        ``last_tool_call_seconds_ago`` carries the age rather than leaving the
        client to subtract two clocks: the ISO stamp is local server time with
        an offset, and a phone in another zone — or merely a few minutes off —
        would render a confident, wrong "worked 3 hours ago". The age is as
        stale as the counts beside it and no staler, which is the accuracy the
        card actually needs. The stamp stays for reading and debugging.
        """
        reference = time.time() if now is None else now
        return {
            "tool_calls_window": self.tool_calls_window,
            "tool_calls_recent": self.tool_calls_recent,
            "last_tool_call_at": (
                None
                if self.last_tool_call_at is None
                else _iso(self.last_tool_call_at)
            ),
            "last_tool_call_seconds_ago": (
                None
                if self.last_tool_call_at is None
                else max(0, int(reference - self.last_tool_call_at))
            ),
            "working": self.working,
        }


def _iso(epoch_seconds: float) -> str:
    """Local-time ISO 8601 with an offset, so the app never has to guess a zone."""
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(epoch_seconds))


def stem_for_unit(unit: str) -> str | None:
    match = _UNIT_STEM_RE.fullmatch(unit)
    return match.group(1) if match is not None else None


def journal_command(stems: Iterable[str], *, window_seconds: int) -> list[str]:
    """One journalctl call for every agent, machine-readable and bounded.

    ``--output-fields=_SYSTEMD_USER_UNIT`` is the field that carries the agent
    unit. ``_SYSTEMD_UNIT`` looks like the obvious choice and is what a
    system-scope journal would use, but for ``--user`` units it holds
    ``user@1000.service`` for every line — using it silently merges all eight
    agents into one bucket that then looks uniformly busy.

    ``MESSAGE`` is deliberately *not* requested. Because the agent's lines carry
    ANSI escapes, journalctl emits the field as a JSON array of byte values
    rather than a string, so anything expecting text gets a list of integers.
    ``-g`` has already done the matching server-side; counting lines needs only
    the unit and the timestamp.
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
        "--output-fields=_SYSTEMD_USER_UNIT",
        "-g",
        JOURNAL_WORK_PATTERN,
    ]
    return args


def parse_journal(
    stdout: str,
    *,
    now: float,
    recent_seconds: int = DEFAULT_RECENT_SECONDS,
) -> dict[str, AgentHeartbeat]:
    """Fold journalctl's JSON lines into one heartbeat per stem.

    Only stems that actually appear are returned; the caller fills in the rest
    as zero, because "did not appear in the window" is a real answer and an
    absent stem must not be confused with a failed query.
    """
    counts: dict[str, int] = {}
    recent: dict[str, int] = {}
    last: dict[str, float] = {}
    recent_floor = now - recent_seconds

    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        stem = stem_for_unit(str(entry.get("_SYSTEMD_USER_UNIT", "")))
        if stem is None:
            continue
        raw_timestamp = entry.get("__REALTIME_TIMESTAMP")
        try:
            # journalctl reports microseconds since the epoch, as a string.
            seconds = int(raw_timestamp) / 1_000_000
        except (TypeError, ValueError):
            continue
        counts[stem] = counts.get(stem, 0) + 1
        if seconds >= recent_floor:
            recent[stem] = recent.get(stem, 0) + 1
        if seconds > last.get(stem, 0.0):
            last[stem] = seconds

    return {
        stem: AgentHeartbeat(
            stem=stem,
            tool_calls_window=count,
            tool_calls_recent=recent.get(stem, 0),
            last_tool_call_at=last.get(stem),
        )
        for stem, count in counts.items()
    }


class HeartbeatReader:
    """Reads one bounded journal window for a set of agent stems."""

    def __init__(
        self,
        runner: Runner,
        *,
        window_seconds: int = DEFAULT_WINDOW_SECONDS,
        recent_seconds: int = DEFAULT_RECENT_SECONDS,
        timeout: float = 10.0,
        clock: Any = time.time,
    ) -> None:
        self.runner = runner
        self.window_seconds = window_seconds
        self.recent_seconds = recent_seconds
        self.timeout = timeout
        self._clock = clock

    def read(self, stems: list[str]) -> tuple[dict[str, AgentHeartbeat], str | None]:
        """Return heartbeats per stem plus an error code, never both silently empty.

        A failed query and a genuinely quiet hour both produce zero journal
        lines. They must not render the same way, so a non-zero exit returns an
        error code and *no* heartbeats — the caller is then obliged to show
        "unknown" rather than a reassuring "idle".
        """
        if not stems:
            return {}, None
        result = self.runner.run(
            journal_command(stems, window_seconds=self.window_seconds),
            timeout=self.timeout,
        )
        if result.returncode != 0:
            return {}, "journal_unavailable"
        found = parse_journal(
            result.stdout,
            now=self._clock(),
            recent_seconds=self.recent_seconds,
        )
        return {
            stem: found.get(
                stem,
                AgentHeartbeat(
                    stem=stem,
                    tool_calls_window=0,
                    tool_calls_recent=0,
                    last_tool_call_at=None,
                ),
            )
            for stem in stems
        }, None
