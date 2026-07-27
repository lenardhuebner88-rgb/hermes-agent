"""Fork-owned: honor ``agent.service_tier`` in ``hermes -z`` oneshot runs.

``hermes_cli/oneshot.py`` is upstream-owned, so the logic lives here and is
wired in with one-line calls:

- ``**resolve_oneshot_service_tier_kwargs(cfg, effective_model)`` at the
  ``AIAgent(...)`` construction site, and
- ``apply_oneshot_service_tier_audit(result, agent)`` after the run, so the
  ``--usage-file`` report shows the tier that actually went out on the wire.

Both functions deliberately reuse the real implementations — the CLI's
``cli._parse_service_tier_config`` (lazy import: cli.py is heavy and oneshot
is a hot pipeline path) and ``hermes_cli.models.resolve_fast_mode_overrides`` —
so oneshot matches the interactive CLI (``cli_agent_setup_mixin``) exactly and
can never drift to a third parsing copy.
"""

from __future__ import annotations

from typing import Any, Optional


def resolve_oneshot_service_tier_kwargs(
    cfg: dict, model: Optional[str]
) -> dict[str, Any]:
    """Return ``AIAgent`` kwargs for the profile's ``agent.service_tier``.

    ``model`` must be the FINAL resolved model (after alias/env/provider
    resolution in ``oneshot._run_agent``): ``resolve_fast_mode_overrides``
    gates on the concrete model id, so passing an unresolved one would
    silently drop the tier.

    Returns ``{}`` when unset/disabled/invalid (identical semantics to
    ``cli._parse_service_tier_config``: invalid values warn and are ignored),
    i.e. zero behavior change for profiles without a tier. When set, mirrors
    ``cli_agent_setup_mixin``: ``service_tier`` plus ``request_overrides``
    (``None`` when the model is not fast-eligible, e.g. codex-series).
    """
    agent_cfg = cfg.get("agent") if isinstance(cfg, dict) else None
    if not isinstance(agent_cfg, dict):
        return {}
    raw = agent_cfg.get("service_tier", "")
    if not str(raw or "").strip():
        return {}

    from cli import _parse_service_tier_config

    tier = _parse_service_tier_config(raw)
    if not tier:
        return {}

    from hermes_cli.models import resolve_fast_mode_overrides

    try:
        overrides = resolve_fast_mode_overrides(model)
    except Exception:
        overrides = None
    return {"service_tier": tier, "request_overrides": overrides}


def apply_oneshot_service_tier_audit(result: dict, agent: Any) -> dict:
    """Fill the usage-report ``service_tier`` audit field when it is empty.

    ``agent.turn_finalizer`` only reports tiers requested via
    ``request_overrides.extra_body`` (e.g. OpenAI ``flex``). Fast/priority
    mode lands as top-level request kwargs instead, so without this the
    ``hermes -z --usage-file`` report would still read ``null`` even though
    the tier went out. Reports exactly what the overrides carried
    (``service_tier`` or Anthropic ``speed``); a copy is returned so the
    caller's original dict is never mutated.
    """
    if not isinstance(result, dict) or result.get("service_tier"):
        return result
    overrides = getattr(agent, "request_overrides", None) or {}
    tier = overrides.get("service_tier") or overrides.get("speed")
    if not tier:
        return result
    patched = dict(result)
    patched["service_tier"] = tier
    return patched
