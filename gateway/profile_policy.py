"""HUB/DEFAULT profile-aware policy helpers.

Centralises the Hermes Agent rules that depend on whether the running
gateway is bound to the default Hermes home (``~/.hermes`` or whatever
``HERMES_HOME`` points to when it is the root) versus a named profile
(``<root>/profiles/<name>``) or an isolated worktree
(``<root>/worktrees/<name>``).

Public surface:

* :func:`is_default_hermes_profile_home` — boolean classifier.
* :func:`filter_default_gateway_fallbacks` — strips Minimax entries from
  fallback chains when the runtime is the HUB; leaves named profiles and
  worktrees untouched.
* :func:`collect_profile_policy_findings` — gateway-startup diagnostic.
* Module-level thresholds for token pressure and Discord lag, each
  overridable via environment variable.

The module is import-light and stdlib-only so it can be referenced from
``gateway/run.py``, ``gateway/status.py``, the Discord adapter and the CLI
without dragging in heavy optional dependencies.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Thresholds (env-overridable defaults)
# ---------------------------------------------------------------------------


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "profile_policy: invalid %s=%r — using default %d", name, raw, default
        )
        return default


PRESSURE_WATCH_PCT: int = _int_env("HERMES_PRESSURE_WATCH_PCT", 65)
PRESSURE_CRITICAL_PCT: int = _int_env("HERMES_PRESSURE_CRITICAL_PCT", 85)
PRESSURE_FLOOR_TOKENS: int = _int_env("HERMES_PRESSURE_FLOOR_TOKENS", 20_000)

DISCORD_LAG_WATCH_MS: int = _int_env("HERMES_DISCORD_LAG_WATCH_MS", 500)
DISCORD_LAG_CRITICAL_MS: int = _int_env("HERMES_DISCORD_LAG_CRITICAL_MS", 1000)
# Heartbeat-age zombie-WS detection. Discord normally sends op=11 every ~41s;
# a one-missed-beat gap (~60s) is a soft signal, two missed beats (~120s) is
# a hard signal that the socket is dead even though latency may still report
# a cached value (Review-Finding #11).
DISCORD_HEARTBEAT_AGE_WATCH_SECONDS: int = _int_env(
    "HERMES_DISCORD_HEARTBEAT_AGE_WATCH_SECONDS", 60
)
DISCORD_HEARTBEAT_AGE_CRITICAL_SECONDS: int = _int_env(
    "HERMES_DISCORD_HEARTBEAT_AGE_CRITICAL_SECONDS", 120
)


# Review-Finding #10: the module-level constants above are frozen at import
# time. The ``current_*`` accessors below re-read the env var per call so
# operators can tune thresholds without a full process re-import (for hot-
# reload supervisors, sigup handlers, or tests that don't want importlib
# .reload). Each returns the env value when set, else the import-time
# default — preserving backward compatibility for callers that import the
# bare constants.


def current_pressure_watch_pct() -> int:
    return _int_env("HERMES_PRESSURE_WATCH_PCT", PRESSURE_WATCH_PCT)


def current_pressure_critical_pct() -> int:
    return _int_env("HERMES_PRESSURE_CRITICAL_PCT", PRESSURE_CRITICAL_PCT)


def current_pressure_floor_tokens() -> int:
    return _int_env("HERMES_PRESSURE_FLOOR_TOKENS", PRESSURE_FLOOR_TOKENS)


def current_discord_lag_watch_ms() -> int:
    return _int_env("HERMES_DISCORD_LAG_WATCH_MS", DISCORD_LAG_WATCH_MS)


def current_discord_lag_critical_ms() -> int:
    return _int_env("HERMES_DISCORD_LAG_CRITICAL_MS", DISCORD_LAG_CRITICAL_MS)


def current_discord_heartbeat_age_watch_seconds() -> int:
    return _int_env(
        "HERMES_DISCORD_HEARTBEAT_AGE_WATCH_SECONDS",
        DISCORD_HEARTBEAT_AGE_WATCH_SECONDS,
    )


def current_discord_heartbeat_age_critical_seconds() -> int:
    return _int_env(
        "HERMES_DISCORD_HEARTBEAT_AGE_CRITICAL_SECONDS",
        DISCORD_HEARTBEAT_AGE_CRITICAL_SECONDS,
    )

MINIMAX_PROVIDER_NAMES: frozenset[str] = frozenset(
    {"minimax", "minimax-ai", "minimaxio"}
)
# 'minimax' is intentionally kept as a substring marker — false-positives on
# unrelated models would require an opaque model name containing the literal
# vendor name, which we accept as a rare misclassification in exchange for
# catching every known Minimax model id without an exhaustive enumeration.
# 'm2.7' was previously included but was too short and too generic (e.g.
# 'company-m2.7-bench' would have been falsely stripped — Review-Finding #6).
MINIMAX_MODEL_MARKERS: tuple[str, ...] = ("minimax",)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _as_path(value: Any) -> Optional[Path]:
    if value is None:
        return None
    if isinstance(value, Path):
        return value
    try:
        return Path(str(value))
    except (TypeError, ValueError):
        return None


def _runtime_default_root() -> Optional[Path]:
    """Resolve the canonical Hermes root for the current process."""
    try:
        from hermes_constants import get_default_hermes_root

        return get_default_hermes_root()
    except Exception as exc:  # pragma: no cover — import-only failure path
        logger.warning("profile_policy: get_default_hermes_root unavailable: %s", exc)
        env = os.environ.get("HERMES_HOME", "").strip()
        if env:
            return Path(env)
        return Path.home() / ".hermes"


def _runtime_hermes_home() -> Optional[Path]:
    try:
        from hermes_constants import get_hermes_home

        return get_hermes_home()
    except Exception as exc:  # pragma: no cover
        logger.warning("profile_policy: get_hermes_home unavailable: %s", exc)
        env = os.environ.get("HERMES_HOME", "").strip()
        if env:
            return Path(env)
        return Path.home() / ".hermes"


def _is_under(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


# ---------------------------------------------------------------------------
# is_default_hermes_profile_home
# ---------------------------------------------------------------------------


def is_default_hermes_profile_home(
    default_root: Any = None,
    hermes_home: Any = None,
) -> bool:
    """Return ``True`` when the current Hermes home is the *default* root.

    A path is the HUB iff it equals the resolved default root.  Named
    profiles under ``<root>/profiles/<x>`` (any depth) and isolated
    worktrees under ``<root>/worktrees/<x>`` (any depth) are explicitly
    NOT the HUB.
    """
    root = _as_path(default_root) or _runtime_default_root()
    home = _as_path(hermes_home) or _runtime_hermes_home()
    if root is None or home is None:
        return False

    profiles_dir = root / "profiles"
    worktrees_dir = root / "worktrees"

    if _is_under(home, profiles_dir):
        return False
    if _is_under(home, worktrees_dir):
        return False

    try:
        home_resolved = home.resolve()
        root_resolved = root.resolve()
    except OSError:
        return False

    if home_resolved != root_resolved:
        return False

    # Corrupted-config safety net (Review-Finding #4): if ``get_default_hermes_root``
    # collapsed *root* onto *home* and the path's immediate parent is named
    # ``worktrees`` or ``profiles``, the home is actually a vault-internal
    # profile/worktree masquerading as the HUB. Reject only this exact
    # masquerade — checking arbitrary ancestors (previous behaviour) falsely
    # rejected legitimate deployment paths like ``/srv/profiles/team/.hermes``.
    if home_resolved.parent.name in {"profiles", "worktrees"}:
        return False
    return True


# ---------------------------------------------------------------------------
# Minimax fallback filter
# ---------------------------------------------------------------------------


def _is_minimax_entry(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    provider = str(entry.get("provider") or "").strip().lower()
    if provider in MINIMAX_PROVIDER_NAMES:
        return True
    model = str(entry.get("model") or "").strip().lower()
    if not model:
        return False
    return any(marker in model for marker in MINIMAX_MODEL_MARKERS)


def filter_default_gateway_fallbacks(
    raw: Any,
    *,
    default_root: Any = None,
    hermes_home: Any = None,
) -> Any:
    """Return *raw* with Minimax entries removed when running at the HUB.

    Acts only when :func:`is_default_hermes_profile_home` is ``True``.
    Operates exclusively on the fallback chain that callers pass in —
    the primary ``model.default`` is never read here, so callers can be
    sure it remains unchanged.

    Accepts both shapes observed in ``config.yaml``:

    * ``fallback_providers`` — ``list[dict]``
    * legacy ``fallback_model`` — single ``dict``

    Operator opt-out: setting ``HERMES_HUB_ALLOW_MINIMAX_FALLBACK=1`` keeps
    Minimax entries in the chain even at the HUB.  Use when the HUB primary
    needs a safety net (e.g. ChatGPT-OAuth outages on ``gpt-5.x``).
    """
    if not is_default_hermes_profile_home(
        default_root=default_root, hermes_home=hermes_home
    ):
        return raw

    if os.environ.get("HERMES_HUB_ALLOW_MINIMAX_FALLBACK", "").strip() in {"1", "true", "yes"}:
        return raw

    if raw is None:
        return None
    if isinstance(raw, list):
        return [entry for entry in raw if not _is_minimax_entry(entry)]
    if isinstance(raw, dict):
        return None if _is_minimax_entry(raw) else raw
    return raw


# ---------------------------------------------------------------------------
# Findings (gateway start-up diagnostic)
# ---------------------------------------------------------------------------


def _iter_fallback_entries(cfg: dict[str, Any]) -> Iterable[dict[str, Any]]:
    providers = cfg.get("fallback_providers") if isinstance(cfg, dict) else None
    if isinstance(providers, list):
        for entry in providers:
            if isinstance(entry, dict):
                yield entry
    legacy = cfg.get("fallback_model") if isinstance(cfg, dict) else None
    if isinstance(legacy, dict):
        yield legacy
    elif isinstance(legacy, list):
        for entry in legacy:
            if isinstance(entry, dict):
                yield entry


def collect_profile_policy_findings(
    config: dict[str, Any] | None,
    *,
    default_root: Any = None,
    hermes_home: Any = None,
) -> list[dict[str, Any]]:
    """Return one-shot diagnostic findings about HUB-relevant config issues.

    Currently emits ``default-profile-minimax-fallback-filtered`` when the
    HUB sees Minimax entries in either ``fallback_providers`` or the
    legacy ``fallback_model``.  Findings list is empty for named profiles,
    worktrees, or when no offending entries exist.
    """
    if not isinstance(config, dict):
        return []
    if not is_default_hermes_profile_home(
        default_root=default_root, hermes_home=hermes_home
    ):
        return []

    # Operator opt-in keeps Minimax entries at the HUB — suppress the
    # "will be filtered" finding so the start-up log is honest about
    # what actually happens at runtime.
    if os.environ.get("HERMES_HUB_ALLOW_MINIMAX_FALLBACK", "").strip() in {"1", "true", "yes"}:
        return []

    findings: list[dict[str, Any]] = []
    minimax_seen = any(_is_minimax_entry(entry) for entry in _iter_fallback_entries(config))
    if minimax_seen:
        findings.append(
            {
                "code": "default-profile-minimax-fallback-filtered",
                "message": (
                    "HUB/DEFAULT profile has Minimax entries in fallback configuration; "
                    "they will be filtered at runtime. Move Minimax to a named profile."
                ),
                "severity": "warning",
            }
        )
    return findings


__all__ = [
    "PRESSURE_WATCH_PCT",
    "PRESSURE_CRITICAL_PCT",
    "PRESSURE_FLOOR_TOKENS",
    "DISCORD_LAG_WATCH_MS",
    "DISCORD_LAG_CRITICAL_MS",
    "DISCORD_HEARTBEAT_AGE_WATCH_SECONDS",
    "DISCORD_HEARTBEAT_AGE_CRITICAL_SECONDS",
    "current_pressure_watch_pct",
    "current_pressure_critical_pct",
    "current_pressure_floor_tokens",
    "current_discord_lag_watch_ms",
    "current_discord_lag_critical_ms",
    "current_discord_heartbeat_age_watch_seconds",
    "current_discord_heartbeat_age_critical_seconds",
    "MINIMAX_PROVIDER_NAMES",
    "MINIMAX_MODEL_MARKERS",
    "is_default_hermes_profile_home",
    "filter_default_gateway_fallbacks",
    "collect_profile_policy_findings",
]
