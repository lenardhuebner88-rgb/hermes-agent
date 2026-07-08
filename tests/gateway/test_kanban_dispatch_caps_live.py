"""Tests for live dispatch-cap resolution (Risiko-Tab "Parallele Worker pro
Profil" lever, 2026-07-08).

The gateway dispatcher used to capture max_spawn / max_in_progress /
max_in_progress_per_profile / serialize_by_repo / max_concurrent_per_repo
ONCE at boot and reuse them as a tick closure, so a dashboard write to
``kanban.max_in_progress_per_profile`` / ``kanban.max_concurrent_per_repo``
was a silent no-op until a gateway restart. ``_read_dispatch_caps`` is now
called every tick, reading the current config — mirrors
``test_kanban_auto_decompose_live.py`` for ``_resolve_auto_decompose_settings``.
"""

from __future__ import annotations

from gateway.kanban_watchers import _DispatchCaps, _read_dispatch_caps


def test_defaults_when_kanban_key_absent():
    caps, warnings = _read_dispatch_caps(lambda: {"kanban": {}})
    assert caps == _DispatchCaps(
        max_spawn=None,
        max_in_progress=None,
        max_in_progress_per_profile=None,
        serialize_by_repo=True,
        max_concurrent_per_repo=1,
    )
    assert warnings == []


def test_max_in_progress_per_profile_and_max_concurrent_per_repo_respected():
    """The coupled Risiko-Tab lever writes both fields with the same N —
    confirm the helper resolves both, independent of each other and of the
    global max_in_progress."""
    caps, warnings = _read_dispatch_caps(
        lambda: {
            "kanban": {
                "max_in_progress": 5,
                "max_in_progress_per_profile": 2,
                "serialize_by_repo": True,
                "max_concurrent_per_repo": 2,
            }
        }
    )
    assert caps.max_in_progress == 5
    assert caps.max_in_progress_per_profile == 2
    assert caps.max_concurrent_per_repo == 2
    assert caps.serialize_by_repo is True
    assert warnings == []


def test_serialize_by_repo_string_false_coerced():
    caps, _ = _read_dispatch_caps(
        lambda: {"kanban": {"serialize_by_repo": "false"}}
    )
    assert caps.serialize_by_repo is False


def test_below_one_values_ignored_with_warning():
    caps, warnings = _read_dispatch_caps(
        lambda: {
            "kanban": {
                "max_spawn": 0,
                "max_in_progress": -1,
                "max_in_progress_per_profile": 0,
                "max_concurrent_per_repo": 0,
            }
        }
    )
    assert caps.max_spawn is None
    assert caps.max_in_progress is None
    assert caps.max_in_progress_per_profile is None
    # max_concurrent_per_repo falls back to the safe default (1), unlike the
    # other caps which fall back to "no override" (None) — same as boot.
    # `0` is falsy so `raw or 1` coerces it to 1 BEFORE the <1 check (existing
    # semantics, preserved verbatim) — no warning for this one input.
    assert caps.max_concurrent_per_repo == 1
    assert len(warnings) == 3


def test_malformed_values_fall_back_with_warning():
    caps, warnings = _read_dispatch_caps(
        lambda: {
            "kanban": {
                "max_in_progress_per_profile": "lots",
                "max_concurrent_per_repo": "many",
            }
        }
    )
    assert caps.max_in_progress_per_profile is None
    assert caps.max_concurrent_per_repo == 1
    assert len(warnings) == 2


def test_config_read_error_returns_none_to_retain_last_known():
    """A transient config READ failure must never widen concurrency. It returns
    ``(None, warnings)`` — the signal for the tick loop to RETAIN its last-known
    caps rather than reset to unbounded (max_in_progress/per_profile=None would
    drop the global/per-profile cap for that tick). Codex-caught fail-safe."""

    def _boom():
        raise RuntimeError("config read failed")

    caps, warnings = _read_dispatch_caps(_boom)
    assert caps is None
    assert len(warnings) == 1
    assert "retaining last-known" in warnings[0]


def test_non_dict_config_fails_safe():
    caps, _ = _read_dispatch_caps(lambda: None)
    assert caps.max_in_progress_per_profile is None
    assert caps.max_concurrent_per_repo == 1


def test_live_change_takes_effect_between_ticks():
    """Simulate the Risiko-Tab lever writing config.yaml mid-run: a
    dispatcher tick calling `_read_dispatch_caps` again — WITHOUT a gateway
    restart — must see the new per_profile/per_repo values immediately.
    This is the exact regression the engine fix (per-tick re-read replacing
    a boot-time closure) targets."""
    state = {
        "kanban": {
            "max_in_progress": 3,
            "max_in_progress_per_profile": 1,
            "max_concurrent_per_repo": 1,
        }
    }
    caps_before, _ = _read_dispatch_caps(lambda: state)
    assert caps_before.max_in_progress_per_profile == 1
    assert caps_before.max_concurrent_per_repo == 1

    # Dashboard POST /release-concurrency writes both fields with N=2.
    state["kanban"]["max_in_progress_per_profile"] = 2
    state["kanban"]["max_concurrent_per_repo"] = 2

    caps_after, _ = _read_dispatch_caps(lambda: state)
    assert caps_after.max_in_progress_per_profile == 2
    assert caps_after.max_concurrent_per_repo == 2
    # The global cap is untouched by the coupled lever.
    assert caps_after.max_in_progress == 3
