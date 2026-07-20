"""Config-driven field definitions for the /control Stats tab.

The dashboard's stats rendering — provider labels, usage-window labels/kinds, and
subscription lanes — used to be hardcoded across the React frontend
(``StatistikView``/``AccountUsageTile``/``accountUsage``). It now lives in
``config/stats_fields.yaml`` at the repo root and is served to the frontend via
``GET /api/stats-config`` (see :func:`hermes_cli.web_server`).

Loading is **fail-soft**: a missing or malformed file falls back to the built-in
:data:`DEFAULT_STATS_CONFIG`, and empty sections are backfilled with defaults, so a
bad operator edit can never blank out the stats tab. Reads are TTL+mtime cached — the
file is re-read at most once per :data:`_CACHE_TTL_S`, and immediately when its mtime
changes (so an edit reflects on the very next request).
"""
from __future__ import annotations

import copy
import threading
import time
from pathlib import Path
from typing import Any, Optional

import yaml

from hermes_cli.config import get_project_root

# Built-in fallback — kept in lockstep with config/stats_fields.yaml. This is the
# single Python source of truth for the field set when the YAML is absent/malformed.
DEFAULT_STATS_CONFIG: dict[str, Any] = {
    "version": 1,
    "providers": [
        {"id": "anthropic", "label": "Claude", "lane": "claude", "visible": True},
        {"id": "openai-codex", "label": "ChatGPT / Codex", "lane": "chatgpt", "visible": True},
        {"id": "kimi", "label": "Kimi", "lane": "kimi", "visible": True},
        {"id": "xai", "label": "Grok", "lane": None, "visible": True},
        {"id": "openrouter", "label": "OpenRouter", "lane": None, "visible": True},
    ],
    "windows": [
        {"key": "session", "label": "5-Std-Fenster", "kind": "session"},
        {"key": "weekly", "label": "Diese Woche", "kind": "weekly"},
        {"key": "opus_week", "label": "Opus-Woche", "kind": "other"},
        {"key": "sonnet_week", "label": "Sonnet-Woche", "kind": "other"},
        {"key": "scoped_week", "label": "Modell-Limit", "kind": "other"},
    ],
    "subscription_lanes": [
        {"key": "chatgpt", "label": "ChatGPT/Codex Abo", "visible": True},
        {"key": "claude", "label": "Claude Max Abo", "visible": True},
        {"key": "kimi", "label": "Kimi Abo", "visible": True},
    ],
}

_WINDOW_KINDS = ("session", "weekly", "other")
_CACHE_TTL_S = 30.0

_lock = threading.Lock()
# (monotonic_ts, file_mtime, config) — file_mtime is -1.0 when the file is absent.
_cache: Optional[tuple[float, float, dict[str, Any]]] = None


def stats_config_path() -> Path:
    """Absolute path to the stats field config (``<repo_root>/config/stats_fields.yaml``)."""
    return get_project_root() / "config" / "stats_fields.yaml"


def _coerce_str(value: Any) -> Optional[str]:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return None


def _normalize_provider(raw: Any) -> Optional[dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    pid = _coerce_str(raw.get("id"))
    if not pid:
        return None
    return {
        "id": pid,
        "label": _coerce_str(raw.get("label")) or pid,
        "lane": _coerce_str(raw.get("lane")),  # None when absent/blank → API-billed
        "visible": bool(raw.get("visible", True)),
    }


def _normalize_window(raw: Any) -> Optional[dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    key = _coerce_str(raw.get("key"))
    if not key:
        return None
    kind = _coerce_str(raw.get("kind")) or "other"
    if kind not in _WINDOW_KINDS:
        kind = "other"
    return {"key": key, "label": _coerce_str(raw.get("label")) or key, "kind": kind}


def _normalize_lane(raw: Any) -> Optional[dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    key = _coerce_str(raw.get("key"))
    if not key:
        return None
    return {"key": key, "label": _coerce_str(raw.get("label")) or key, "visible": bool(raw.get("visible", True))}


def _normalize(raw: Any) -> dict[str, Any]:
    """Validate + normalize a parsed config; drop malformed entries, backfill empties.

    Each section that ends up empty (malformed or omitted) is backfilled from
    :data:`DEFAULT_STATS_CONFIG` so the tab never renders with no fields.
    """
    if not isinstance(raw, dict):
        return copy.deepcopy(DEFAULT_STATS_CONFIG)
    providers = [p for p in (_normalize_provider(x) for x in (raw.get("providers") or [])) if p]
    windows = [w for w in (_normalize_window(x) for x in (raw.get("windows") or [])) if w]
    lanes = [lane for lane in (_normalize_lane(x) for x in (raw.get("subscription_lanes") or [])) if lane]
    version = raw.get("version", 1)
    if not isinstance(version, int):
        version = 1
    return {
        "version": version,
        "providers": providers or copy.deepcopy(DEFAULT_STATS_CONFIG["providers"]),
        "windows": windows or copy.deepcopy(DEFAULT_STATS_CONFIG["windows"]),
        "subscription_lanes": lanes or copy.deepcopy(DEFAULT_STATS_CONFIG["subscription_lanes"]),
    }


def load_stats_config(*, force: bool = False) -> dict[str, Any]:
    """Return the stats field config, TTL+mtime cached, fail-soft to defaults.

    A missing or malformed YAML file yields :data:`DEFAULT_STATS_CONFIG` rather than
    raising. ``force=True`` bypasses the cache (used by tests).
    """
    global _cache
    path = stats_config_path()
    now = time.monotonic()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = -1.0  # file absent

    if not force:
        with _lock:
            cached = _cache
        if cached is not None:
            ts, cached_mtime, cfg = cached
            if now - ts < _CACHE_TTL_S and cached_mtime == mtime:
                return cfg

    if mtime < 0:
        cfg = copy.deepcopy(DEFAULT_STATS_CONFIG)
    else:
        try:
            with path.open("r", encoding="utf-8") as handle:
                raw = yaml.safe_load(handle)
            cfg = _normalize(raw)
        except Exception:
            cfg = copy.deepcopy(DEFAULT_STATS_CONFIG)

    with _lock:
        _cache = (now, mtime, cfg)
    return cfg


def _reset_cache_for_tests() -> None:
    """Clear the module cache (test-only helper)."""
    global _cache
    with _lock:
        _cache = None
