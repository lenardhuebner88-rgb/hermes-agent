"""Buzz approval bridge for operator-held Kanban release gates.

The bridge posts one approval request, polls the relay's stored reaction
state through ``buzz reactions get``, and invokes the existing release-gate
CLI only for the configured emoji from an allowlisted public key.  The Buzz
private key is inherited by the subprocess from ``BUZZ_PRIVATE_KEY`` and is
never read, copied, or logged here.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import re
import subprocess
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Mapping, Protocol

from hermes_cli.config import load_config

logger = logging.getLogger(__name__)

DEFAULT_APPROVERS = (
    "447eedf4a7fa32f4444396dbd511c59aa7521cd003396113fe121882fcdb20cd",
)
DEFAULT_EMOJI = "✅"
DEFAULT_POLL_INTERVAL_SECONDS = 10.0
DEFAULT_TIMEOUT_SECONDS = 3600.0
DEFAULT_BUZZ_BINARY = "/mnt/data/services/buzz/target/release/buzz"
_HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class Runner(Protocol):
    def run(self, args: list[str]) -> CommandResult: ...


class SubprocessRunner:
    """Run fixed argument vectors without a shell or environment inspection."""

    def run(self, args: list[str]) -> CommandResult:
        completed = subprocess.run(
            args,
            capture_output=True,
            text=True,
            check=False,
        )
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)


@dataclass(frozen=True)
class ReleaseGateConfig:
    approvers: frozenset[str]
    channel_id: str
    emoji: str
    poll_interval_seconds: float
    timeout_seconds: float


@dataclass(frozen=True)
class BridgeOutcome:
    status: str
    event_id: str
    release_gate_called: bool


class BuzzBridgeError(RuntimeError):
    """Safe bridge failure whose message never includes subprocess output."""


def _positive_number(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number) or number <= 0:
        return default
    return number


def _normalized_approvers(value: Any) -> frozenset[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return frozenset(DEFAULT_APPROVERS)
    approvers = {
        candidate.lower()
        for item in value
        if isinstance(item, str)
        for candidate in (item.strip(),)
        if _HEX64_RE.fullmatch(candidate)
    }
    return frozenset(approvers or DEFAULT_APPROVERS)


def load_release_gate_config(config: Mapping[str, Any] | None = None) -> ReleaseGateConfig:
    """Normalize ``kanban.release_gate`` with a fail-closed approver default."""

    root = config if isinstance(config, Mapping) else load_config()
    kanban = root.get("kanban") if isinstance(root, Mapping) else None
    kanban = kanban if isinstance(kanban, Mapping) else {}
    raw = kanban.get("release_gate")
    raw = raw if isinstance(raw, Mapping) else {}
    return ReleaseGateConfig(
        approvers=_normalized_approvers(raw.get("approvers")),
        channel_id=str(raw.get("channel_id") or "").strip(),
        emoji=str(raw.get("emoji") or DEFAULT_EMOJI).strip() or DEFAULT_EMOJI,
        poll_interval_seconds=_positive_number(
            raw.get("poll_interval_seconds"), DEFAULT_POLL_INTERVAL_SECONDS
        ),
        timeout_seconds=_positive_number(
            raw.get("timeout_seconds"), DEFAULT_TIMEOUT_SECONDS
        ),
    )


class BuzzReleaseGateBridge:
    def __init__(
        self,
        config: ReleaseGateConfig,
        *,
        runner: Runner | None = None,
        buzz_binary: str = DEFAULT_BUZZ_BINARY,
        hermes_binary: str = "hermes",
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.runner = runner or SubprocessRunner()
        self.buzz_binary = buzz_binary
        self.hermes_binary = hermes_binary
        self._monotonic = monotonic
        self._sleep = sleep
        self.event_id: str | None = None
        self._release_gate_called = False
        self._logged_rejections: set[str] = set()

    def post_request(
        self,
        *,
        task_id: str,
        chain: str,
        merge_commit: str,
        hold_reason: str,
    ) -> str:
        if not self.config.channel_id:
            raise BuzzBridgeError("kanban.release_gate.channel_id is required")
        content = (
            "🔐 **Release-Gate-Freigabe erforderlich**\n"
            f"- Task-ID: `{task_id}`\n"
            f"- Kette: `{chain}`\n"
            f"- Merge-Commit: `{merge_commit}`\n"
            f"- Halte-Grund: {hold_reason}\n"
            f"Mit {self.config.emoji} freigeben."
        )
        result = self.runner.run(
            [
                self.buzz_binary,
                "messages",
                "send",
                "--channel",
                self.config.channel_id,
                "--content",
                content,
            ]
        )
        if result.returncode != 0:
            raise BuzzBridgeError(
                f"buzz messages send failed with exit code {result.returncode}"
            )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise BuzzBridgeError("buzz messages send returned invalid JSON") from exc
        event_id = payload.get("event_id") if isinstance(payload, dict) else None
        if not isinstance(event_id, str) or _HEX64_RE.fullmatch(event_id) is None:
            raise BuzzBridgeError("buzz messages send returned no valid event_id")
        self.event_id = event_id.lower()
        return self.event_id

    def _stored_reactions(self, event_id: str) -> list[dict[str, Any]]:
        result = self.runner.run(
            [self.buzz_binary, "reactions", "get", "--event", event_id]
        )
        if result.returncode != 0:
            raise BuzzBridgeError(
                f"buzz reactions get failed with exit code {result.returncode}"
            )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise BuzzBridgeError("buzz reactions get returned invalid JSON") from exc
        reactions = payload.get("reactions") if isinstance(payload, dict) else None
        if not isinstance(reactions, list):
            raise BuzzBridgeError("buzz reactions get returned no reactions list")
        return [item for item in reactions if isinstance(item, dict)]

    def _authorized_reaction_seen(self, event_id: str) -> bool:
        for reaction in self._stored_reactions(event_id):
            if reaction.get("emoji") != self.config.emoji:
                continue
            pubkeys = reaction.get("pubkeys")
            if not isinstance(pubkeys, list):
                continue
            for value in pubkeys:
                if not isinstance(value, str) or _HEX64_RE.fullmatch(value) is None:
                    continue
                pubkey = value.lower()
                if pubkey in self.config.approvers:
                    return True
                if pubkey not in self._logged_rejections:
                    self._logged_rejections.add(pubkey)
                    logger.info(
                        "buzz release gate: ignored configured emoji because "
                        "reacting pubkey is not in the approver allowlist"
                    )
        return False

    def _release_gate(self, task_id: str) -> bool:
        if self._release_gate_called:
            return True
        self._release_gate_called = True
        result = self.runner.run(
            [self.hermes_binary, "kanban", "release-gate", task_id]
        )
        if result.returncode != 0:
            logger.error(
                "buzz release gate: hermes kanban release-gate failed with exit code %s",
                result.returncode,
            )
            return False
        return True

    def run(
        self,
        *,
        task_id: str,
        chain: str,
        merge_commit: str,
        hold_reason: str,
    ) -> BridgeOutcome:
        event_id = self.post_request(
            task_id=task_id,
            chain=chain,
            merge_commit=merge_commit,
            hold_reason=hold_reason,
        )
        deadline = self._monotonic() + self.config.timeout_seconds
        while True:
            if self._authorized_reaction_seen(event_id):
                released = self._release_gate(task_id)
                return BridgeOutcome(
                    status="approved" if released else "release_failed",
                    event_id=event_id,
                    release_gate_called=True,
                )
            now = self._monotonic()
            if now >= deadline:
                return BridgeOutcome(
                    status="timed_out",
                    event_id=event_id,
                    release_gate_called=False,
                )
            self._sleep(min(self.config.poll_interval_seconds, deadline - now))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--chain", required=True)
    parser.add_argument("--merge-commit", required=True)
    parser.add_argument("--hold-reason", required=True)
    args = parser.parse_args(argv)
    try:
        outcome = BuzzReleaseGateBridge(load_release_gate_config()).run(
            task_id=args.task_id,
            chain=args.chain,
            merge_commit=args.merge_commit,
            hold_reason=args.hold_reason,
        )
    except BuzzBridgeError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps(asdict(outcome), sort_keys=True))
    return 0 if outcome.status == "approved" else 1


if __name__ == "__main__":
    raise SystemExit(main())
