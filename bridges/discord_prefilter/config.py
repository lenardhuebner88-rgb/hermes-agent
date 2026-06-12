"""Configuration for the Discord pre-filter bridge.

All settings come from environment variables (optionally seeded from a small
env file), in a namespace separate from the main Hermes bot so the two never
share a token or clobber each other.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Pattern, Sequence

from bridges.discord_prefilter.triage import build_noise_matchers


def _truthy(val: Optional[str]) -> bool:
    return (val or "").strip().lower() in {"1", "true", "yes", "on"}


def _parse_user_ids(raw: Optional[str]) -> frozenset:
    """Parse a comma/space/semicolon-separated list of Discord user ids.

    Non-numeric fragments are ignored (a typo must not silently widen the
    allowlist — worst case it narrows it, which fails closed).
    """
    ids = set()
    for fragment in (raw or "").replace(";", ",").replace(" ", ",").split(","):
        fragment = fragment.strip()
        if fragment.isdigit():
            ids.add(int(fragment))
    return frozenset(ids)


def _load_env_file(path: Path) -> None:
    """Seed os.environ from a ``KEY=VALUE`` file without clobbering real env.

    Real environment variables always win; the file only fills gaps. Lines
    that are blank or start with ``#`` are ignored. A surrounding pair of
    quotes around the value is stripped.
    """
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


# Default env-file location (overridable via PREFILTER_ENV_FILE).
DEFAULT_ENV_FILE = Path(
    os.path.expanduser("~/.hermes/bridges/discord_prefilter.env")
)

# Noise patterns applied before any model spawn. Conservative on purpose —
# only the most obvious chatter. Operators extend via PREFILTER_NOISE_PATTERNS
# (newline- or ``|||``-separated regexes).
DEFAULT_NOISE_PATTERNS: List[str] = [
    r"^\s*(gm|gn|good morning|good night)\b",
    r"^\s*(thx|thanks|danke|ty|\+1|👍|🙏)\s*$",
    r"^\s*(lol|haha+|lmao|rofl)\s*$",
]


@dataclass
class PrefilterConfig:
    discord_token: str
    channel_id: int
    model: str
    escalate_enabled: bool = False
    escalate_mode: str = "orchestrator"  # "orchestrator" (forward) | "oneshot" (hermes -z)
    escalate_profile: str = "default"
    escalate_placeholder: str = "🛎️ Echte Aufgabe erkannt — eskaliere an Hermes …"
    # Orchestrator hand-off (escalate_mode="orchestrator"): forward the task to
    # the live Hub Orchestrator's channel with an @mention (its allow-bots policy
    # is "mentions"), so the existing Kanban pipeline picks it up. Additive only.
    orchestrator_channel_id: int = 1500203113867378789
    orchestrator_mention_id: Optional[str] = "1500199614706483210"
    escalate_forward_ack: str = (
        "🛎️ Echte Aufgabe → an den Hub-Orchestrator (#hermes-oc) weitergeleitet."
    )
    noise_patterns: List[str] = field(default_factory=lambda: list(DEFAULT_NOISE_PATTERNS))
    noise_matchers: Sequence[Pattern[str]] = field(default_factory=list)
    claude_bin: Optional[str] = None
    hermes_argv: List[str] = field(default_factory=list)
    triage_timeout_s: int = 45
    escalate_timeout_s: int = 600
    allow_bots: bool = False
    react_on_noise: Optional[str] = None
    # User allowlist (S3): only these Discord user ids reach triage. EMPTY
    # means fail-closed — the bot ignores everyone and logs why. Loaded from
    # PREFILTER_ALLOWED_USERS, falling back to the hub-wide
    # DISCORD_ALLOWED_USERS so one operator setting covers both bots.
    allowed_user_ids: frozenset = frozenset()

    # --- subprocess environments ------------------------------------------

    def claude_env(self) -> dict:
        """Env for ``claude -p`` that FORCES the Max subscription (no API call).

        Stripping the Anthropic API-key vars makes the ``claude`` CLI fall back
        to its subscription OAuth login — the hard "Max only, kein API-Call"
        guarantee for the pre-filter.
        """
        env = dict(os.environ)
        for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
            env.pop(key, None)
        return env

    def hermes_env(self) -> dict:
        """Env for the escalation ``hermes -z`` call (uses the normal default)."""
        return dict(os.environ)

    # --- loader -----------------------------------------------------------

    @classmethod
    def load(cls, env_file: Optional[str] = None) -> "PrefilterConfig":
        env_path = Path(env_file) if env_file else Path(
            os.environ.get("PREFILTER_ENV_FILE", str(DEFAULT_ENV_FILE))
        )
        _load_env_file(env_path)

        token = os.environ.get("PREFILTER_DISCORD_TOKEN", "").strip()
        if not token:
            raise SystemExit(
                "PREFILTER_DISCORD_TOKEN is not set. Put the (reactivated) Kanbanops "
                f"bot token in {env_path} or the environment."
            )

        channel_raw = os.environ.get("PREFILTER_CHANNEL_ID", "").strip()
        if not channel_raw.isdigit():
            raise SystemExit(
                "PREFILTER_CHANNEL_ID must be the numeric Discord channel id of the "
                "dedicated pilot channel."
            )
        channel_id = int(channel_raw)

        model = os.environ.get("PREFILTER_MODEL", "").strip()
        if not model:
            raise SystemExit(
                "PREFILTER_MODEL is not set. Set it to the `claude -p --model` alias "
                'for the pre-filter model ("fable 5").'
            )

        # Noise patterns: default list plus any operator additions.
        extra = os.environ.get("PREFILTER_NOISE_PATTERNS", "")
        patterns = list(DEFAULT_NOISE_PATTERNS)
        if extra:
            sep = "|||" if "|||" in extra else "\n"
            patterns.extend(p.strip() for p in extra.split(sep) if p.strip())

        # Hermes invocation for escalation. Default mirrors the repo's own
        # fallback (_resolve_hermes_argv): run the module via the current
        # interpreter so it works without a `hermes` shim on PATH.
        hermes_argv_raw = os.environ.get("PREFILTER_HERMES_ARGV", "").strip()
        if hermes_argv_raw:
            hermes_argv = hermes_argv_raw.split()
        else:
            hermes_argv = [sys.executable, "-m", "hermes_cli.main"]

        cfg = cls(
            discord_token=token,
            channel_id=channel_id,
            model=model,
            escalate_enabled=_truthy(os.environ.get("PREFILTER_ESCALATE")),
            escalate_mode=os.environ.get("PREFILTER_ESCALATE_MODE", "orchestrator").strip() or "orchestrator",
            escalate_profile=os.environ.get("PREFILTER_ESCALATE_PROFILE", "default").strip() or "default",
            orchestrator_channel_id=int(
                os.environ.get("PREFILTER_ORCHESTRATOR_CHANNEL_ID", "1500203113867378789") or 1500203113867378789
            ),
            orchestrator_mention_id=os.environ.get(
                "PREFILTER_ORCHESTRATOR_MENTION_ID", "1500199614706483210"
            ).strip() or None,
            noise_patterns=patterns,
            claude_bin=os.environ.get("PREFILTER_CLAUDE_BIN", "").strip() or None,
            hermes_argv=hermes_argv,
            triage_timeout_s=int(os.environ.get("PREFILTER_TRIAGE_TIMEOUT_S", "45") or 45),
            escalate_timeout_s=int(os.environ.get("PREFILTER_ESCALATE_TIMEOUT_S", "600") or 600),
            allow_bots=_truthy(os.environ.get("PREFILTER_ALLOW_BOTS")),
            react_on_noise=os.environ.get("PREFILTER_NOISE_REACTION", "").strip() or None,
            allowed_user_ids=_parse_user_ids(
                os.environ.get("PREFILTER_ALLOWED_USERS")
                or os.environ.get("DISCORD_ALLOWED_USERS")
            ),
        )
        cfg.noise_matchers = build_noise_matchers(cfg.noise_patterns)
        return cfg
