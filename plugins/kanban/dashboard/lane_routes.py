"""Lane catalog, routing, smoke, and persistence dashboard routes."""

from __future__ import annotations

# extension_runtime.load_api_extension injects the parent API context.

# --- Lanes (night-sprint F1) — switchable profile→routing presets ---


_LANE_CLAUDE_CLI_MODELS: tuple[dict[str, Any], ...] = (
    {"id": "claude-fable-5", "label": "Claude Fable 5", "runtime": "claude-cli", "group": "Claude (Max-Abo)", "provider": None, "locked": False},
    {"id": "claude-opus-4-8", "label": "Claude Opus 4.8", "runtime": "claude-cli", "group": "Claude (Max-Abo)", "provider": None, "locked": False},
    {"id": "claude-sonnet-5", "label": "Claude Sonnet 5", "runtime": "claude-cli", "group": "Claude (Max-Abo)", "provider": None, "locked": False},
    {"id": "claude-sonnet-4-6", "label": "Claude Sonnet 4.6", "runtime": "claude-cli", "group": "Claude (Max-Abo)", "provider": None, "locked": False},
    {"id": "claude-haiku-4-5", "label": "Claude Haiku 4.5", "runtime": "claude-cli", "group": "Claude (Max-Abo)", "provider": None, "locked": False},
)
_LANE_CLAUDE_CLI_MODEL_IDS = {str(item["id"]) for item in _LANE_CLAUDE_CLI_MODELS}


def reasoning_support_for(provider: str | None, model_id: str) -> list[str]:
    """Return the reasoning-effort values Hermes can actually transport."""
    provider = (provider or "").strip().lower()
    model_id = (model_id or "").strip().lower()
    if provider in {"openai-codex", "openai"} and model_id.startswith("gpt-5"):
        return ["minimal", "low", "medium", "high"]
    if model_id.startswith("claude") or provider == "anthropic":
        return ["low", "medium", "high"]
    if provider == "moonshotai" or "kimi" in model_id:
        return ["low", "medium", "high"]
    if provider == "google" or "gemini" in model_id:
        return ["low", "medium", "high"]
    if provider == "openrouter":
        return ["low", "medium", "high"]
    return []


def _lane_model_metadata(provider: str | None, model: str) -> dict[str, float | int | None]:
    metadata: dict[str, float | int | None] = {
        "price_in_per_mtok_usd": None,
        "price_out_per_mtok_usd": None,
        "context_window": None,
    }
    if not provider:
        return metadata
    try:
        from agent.models_dev import get_model_capabilities, get_model_info

        capabilities = get_model_capabilities(provider, model)
        info = get_model_info(provider, model)
        if capabilities is not None:
            metadata["context_window"] = capabilities.context_window
        if info is not None and info.has_cost_data():
            metadata["price_in_per_mtok_usd"] = info.cost_input
            metadata["price_out_per_mtok_usd"] = info.cost_output
    except Exception:
        log.exception("lanes: failed to load models.dev metadata for %s/%s", provider, model)
    return metadata


def _lane_provider_label(provider_id: str, provider_row: dict[str, Any] | None = None) -> str:
    provider_id = (provider_id or "").strip()
    if provider_row is not None:
        for key in ("name", "label", "display_name"):
            value = provider_row.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    if not provider_id:
        return "API-Modelle"
    known = {
        "openai-codex": "OpenAI Codex",
        "openrouter": "OpenRouter",
        "kimi-coding": "Kimi Coding",
        "kimi-coding-cn": "Kimi Coding CN",
        "google": "Google Gemini",
        "anthropic": "Anthropic",
        "nous": "Nous",
    }
    return known.get(provider_id, provider_id)


def _lane_model_label(model_id: str) -> str:
    if not model_id:
        return model_id
    compact = model_id.rsplit("/", 1)[-1].replace("-", " ").replace("_", " ")
    return " ".join(part.upper() if part.lower() in {"gpt", "k2"} else part.capitalize() for part in compact.split())


def _append_lane_model_option(
    out: list[dict[str, Any]],
    seen: set[tuple[str, str | None, str]],
    *,
    model: str,
    runtime: str,
    group: str,
    provider: str | None = None,
    label: str | None = None,
    locked: bool = False,
    source: str | None = None,
    authenticated: bool = False,
    configured: bool = False,
) -> None:
    model = (model or "").strip()
    if not model:
        return
    if runtime == "hermes" and model in _LANE_CLAUDE_CLI_MODEL_IDS:
        return
    provider = provider.strip() if isinstance(provider, str) and provider.strip() else None
    key = (model, provider, runtime)
    if key in seen:
        return
    seen.add(key)
    row = {
        "id": model,
        "label": label or _lane_model_label(model),
        "runtime": runtime,
        "group": group,
        "provider": provider,
        "locked": locked,
        "authenticated": authenticated,
        "configured": configured,
        **_lane_model_metadata(provider, model),
        "reasoning_support": reasoning_support_for(provider, model),
        "probe": _lane_model_probe_for(provider, model),
    }
    if source:
        row["source"] = source
    out.append(row)


def _append_openrouter_extra_model_options(
    out: list[dict[str, Any]],
    seen: set[tuple[str, str | None, str]],
) -> None:
    """Add locally admitted OpenRouter models from config.yaml."""
    try:
        from hermes_cli.model_catalog import get_configured_provider_extra_models

        model_ids = get_configured_provider_extra_models("openrouter")
    except Exception:
        log.exception("lanes: failed to load configured OpenRouter extra models")
        return
    for model_id in model_ids:
        _append_lane_model_option(
            out,
            seen,
            model=model_id,
            runtime="hermes",
            group="OpenRouter",
            provider="openrouter",
            label=model_id,
            source="config",
        )


# Last good inventory-sourced model rows. The live inventory occasionally
# returns empty (provider API blip, mobile network, auth refresh in flight);
# this snapshot keeps the Lanes dropdown AND the /persist validator stable so a
# model that was valid moments ago is not suddenly rejected.
_LANE_INVENTORY_CACHE: list[dict] = []


def _lane_model_catalog(profiles: list[dict], active_lane: Optional[dict] = None) -> list[dict]:
    """Provider-aware model list for Lanes.

    Hermes-runtime rows come from the shared inventory/model-catalog substrate
    used by the main picker. Claude-CLI rows are explicit Cloud Max choices;
    selecting them must route through ``claude -p`` rather than an API provider.
    ``active_lane`` (a ``kanban_db.get_active_lane``/``list_lanes`` row) adds a
    resilience source for models the lane currently pins per profile — see the
    lane-pinned block below.
    """
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str | None, str]] = set()

    for item in _LANE_CLAUDE_CLI_MODELS:
        _append_lane_model_option(
            out,
            seen,
            model=str(item["id"]),
            label=str(item["label"]),
            runtime="claude-cli",
            group=str(item["group"]),
            provider=None,
            locked=bool(item.get("locked")),
            source="claude-cli",
        )

    try:
        from hermes_cli.inventory import build_models_payload, load_picker_context

        payload = build_models_payload(
            load_picker_context(),
            include_unconfigured=True,
            picker_hints=True,
            capabilities=True,
            max_models=200,
        )
        for provider_row in payload.get("providers") or []:
            if not isinstance(provider_row, dict):
                continue
            provider = str(provider_row.get("slug") or provider_row.get("id") or "").strip()
            if not provider:
                continue
            authenticated = provider_row.get("authenticated")
            configured = provider_row.get("configured")
            has_models = bool(provider_row.get("models"))
            if authenticated is False and configured is False and not has_models:
                continue
            group = _lane_provider_label(provider, provider_row)
            for model in provider_row.get("models") or []:
                if not isinstance(model, str) or not model.strip():
                    continue
                _append_lane_model_option(
                    out,
                    seen,
                    model=model,
                    runtime="hermes",
                    group=group,
                    provider=provider,
                    label=model,
                    source="inventory",
                    authenticated=bool(authenticated),
                    configured=bool(configured),
                )
    except Exception:
        log.exception("lanes: failed to build dynamic model catalog")

    # Resilience: a fresh build that yielded no inventory rows (transient
    # provider/network failure) must not strip API models the operator just
    # picked — reuse the last good snapshot; refresh it whenever live succeeds.
    global _LANE_INVENTORY_CACHE
    inventory_rows = [dict(r) for r in out if r.get("source") == "inventory"]
    if inventory_rows:
        _LANE_INVENTORY_CACHE = inventory_rows
    elif _LANE_INVENTORY_CACHE:
        for r in _LANE_INVENTORY_CACHE:
            _append_lane_model_option(
                out,
                seen,
                model=str(r.get("id") or ""),
                runtime=str(r.get("runtime") or "hermes"),
                group=str(r.get("group") or "API-Modelle"),
                provider=r.get("provider"),
                label=str(r.get("label") or r.get("id") or ""),
                source="inventory-cache",
                authenticated=bool(r.get("authenticated")),
                configured=bool(r.get("configured")),
            )

    _append_openrouter_extra_model_options(out, seen)

    # Profile defaults are live config and must stay visible even if the
    # curated catalog does not know them yet.
    for prof in profiles:
        try:
            model = (prof.get("default_model") or "").strip()
            if not model:
                continue
            runtime = "claude-cli" if prof.get("worker_runtime") == "claude-cli" else "hermes"
            if runtime == "hermes" and model in _LANE_CLAUDE_CLI_MODEL_IDS:
                continue
            group = "Claude (Max-Abo)" if runtime == "claude-cli" else "API-Modelle"
            _append_lane_model_option(
                out,
                seen,
                model=model,
                runtime=runtime,
                group=group,
                provider=prof.get("default_provider") if runtime == "hermes" else None,
                label=model,
                locked=runtime == "claude-cli",
                source="profile-default",
            )
        except Exception:
            continue

    # Resilience: a model the ACTIVE LANE currently pins per profile must stay
    # representable even if it fell out of every other catalog source
    # (provider outage, removed from extra_models, general catalog drift) —
    # otherwise a single stale pin 400s the ENTIRE /persist call, including
    # unrelated corrections riding along in the same payload (2026-06-27
    # incident: a metered lane could not be turned off via the dashboard
    # because its own current pin was unrepresentable).
    for entry in ((active_lane or {}).get("profiles") or {}).values():
        try:
            model = (entry.get("model") or "").strip()
            if not model:
                continue
            runtime = "claude-cli" if entry.get("worker_runtime") == "claude-cli" else "hermes"
            if runtime == "hermes" and model in _LANE_CLAUDE_CLI_MODEL_IDS:
                continue
            group = "Claude (Max-Abo)" if runtime == "claude-cli" else "API-Modelle"
            _append_lane_model_option(
                out,
                seen,
                model=model,
                runtime=runtime,
                group=group,
                provider=entry.get("provider") if runtime == "hermes" else None,
                label=model,
                locked=runtime == "claude-cli",
                source="lane-pinned",
            )
        except Exception:
            continue
    return out


_LANE_PROFILE_CACHE_TTL_S = 30.0
_lane_profile_cache: Optional[tuple[float, list[dict]]] = None


def _scan_lane_profiles() -> list[dict]:
    """Direct profile-dir scan for the Lanes UI dropdowns.

    Deliberately NOT ``list_profiles()``: that helper additionally rglobs
    every profile's skills/ tree (~100 files per profile) and probes
    gateway pids — measured at ~5s per call with 11 profiles, which made
    GET /lanes time out on mobile. The editor only needs name, runtime,
    default model and description, all of which live in two small YAML
    files per profile (same seams the dispatcher uses).
    """
    import yaml
    from hermes_cli.profiles import _PROFILE_ID_RE, _get_profiles_root, read_profile_meta

    out: list[dict] = []
    root = _get_profiles_root()
    if not root.is_dir():
        return out
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name == "default" or not _PROFILE_ID_RE.match(entry.name):
            continue
        runtime = "hermes"
        claude_model = None
        model = None
        try:
            cfg_path = entry / "config.yaml"
            if cfg_path.is_file():
                with open(cfg_path, "r", encoding="utf-8") as fh:
                    cfg = yaml.safe_load(fh) or {}
                if isinstance(cfg, dict):
                    if cfg.get("worker_runtime") == "claude-cli":
                        runtime = "claude-cli"
                    cm = cfg.get("claude_model")
                    if isinstance(cm, str) and cm.strip():
                        claude_model = cm.strip()
                    model_cfg = cfg.get("model")
                    provider = None
                    if isinstance(model_cfg, str):
                        model = model_cfg
                    elif isinstance(model_cfg, dict):
                        model = model_cfg.get("default") or model_cfg.get("model")
                        provider = model_cfg.get("provider")
                    agent_cfg = cfg.get("agent")
                    raw_reasoning_effort = (
                        agent_cfg.get("reasoning_effort")
                        if isinstance(agent_cfg, dict)
                        else None
                    )
                    reasoning_effort = (
                        raw_reasoning_effort.strip()
                        if isinstance(raw_reasoning_effort, str) and raw_reasoning_effort.strip()
                        else None
                    )
                    from hermes_cli.fallback_config import get_fallback_chain
                    fallback_providers = get_fallback_chain(cfg)
                else:
                    provider = None
                    fallback_providers = []
                    reasoning_effort = None
            else:
                provider = None
                fallback_providers = []
                reasoning_effort = None
        except Exception:
            provider = None
            fallback_providers = []
            reasoning_effort = None
            pass
        out.append({
            "name": entry.name,
            "worker_runtime": runtime,
            "default_model": claude_model if runtime == "claude-cli" else model,
            "default_provider": None if runtime == "claude-cli" else (
                provider.strip() if isinstance(provider, str) and provider.strip() else None
            ),
            "fallback_providers": [] if runtime == "claude-cli" else fallback_providers,
            "reasoning_effort": reasoning_effort,
            "reasoning_support": reasoning_support_for(
                None if runtime == "claude-cli" else provider,
                claude_model if runtime == "claude-cli" else (model or ""),
            ),
            "description": read_profile_meta(entry).get("description", ""),
            "locked": runtime == "claude-cli",
            "locked_reason": "Claude-CLI / claude -p excluded from this slice" if runtime == "claude-cli" else None,
        })
    return out


def _lane_profile_catalog() -> list[dict]:
    """Profile names + config defaults for the Lanes UI dropdowns.

    Fail-soft: any error yields an empty list — the UI then falls back to
    free-text profile entry. Cached for a short TTL because the catalog
    only changes when someone edits a profile's config.yaml, while the
    Lanes tab refetches after every mutation.
    """
    global _lane_profile_cache
    now = time.monotonic()
    if _lane_profile_cache is not None and now - _lane_profile_cache[0] < _LANE_PROFILE_CACHE_TTL_S:
        return _lane_profile_cache[1]
    try:
        out = _scan_lane_profiles()
    except Exception:
        return []
    _lane_profile_cache = (now, out)
    return out


def _annotate_lane_model_relevance(
    models: list[dict],
    profiles: list[dict],
    lanes: list[dict],
) -> None:
    """Add operator-relevance flags to every GET /lanes model row."""
    used_providers = {
        str(profile.get("default_provider") or "").strip()
        for profile in profiles
        if str(profile.get("default_provider") or "").strip()
    }
    lane_providers: set[str] = set()
    lane_models: set[str] = set()
    for lane in lanes:
        for entry in ((lane.get("profiles") or {}).values()):
            if not isinstance(entry, dict):
                continue
            provider = str(entry.get("provider") or "").strip()
            model = str(entry.get("model") or "").strip()
            if provider:
                lane_providers.add(provider)
            if model:
                lane_models.add(model)
            for fallback in entry.get("fallback_providers") or []:
                if not isinstance(fallback, dict):
                    continue
                fallback_provider = str(fallback.get("provider") or "").strip()
                fallback_model = str(fallback.get("model") or "").strip()
                if fallback_provider:
                    lane_providers.add(fallback_provider)
                if fallback_model:
                    lane_models.add(fallback_model)

    admitted_by_provider: dict[str, set[str]] = {}
    try:
        from hermes_cli.model_catalog import get_configured_provider_extra_models

        for provider in {
            str(row.get("provider") or "").strip()
            for row in models
            if str(row.get("provider") or "").strip()
        }:
            admitted_by_provider[provider] = set(
                get_configured_provider_extra_models(provider)
            )
    except Exception:
        log.exception("lanes: failed to load admitted model metadata")

    for row in models:
        provider = str(row.get("provider") or "").strip()
        model = str(row.get("id") or "").strip()
        used_in_profiles = bool(provider and provider in used_providers)
        admitted = bool(
            provider and model and model in admitted_by_provider.get(provider, set())
        )
        row["used_in_profiles"] = used_in_profiles
        row["admitted"] = admitted
        row["sinnvoll"] = bool(
            row.get("runtime") == "claude-cli"
            or used_in_profiles
            or admitted
            or (provider and provider in lane_providers)
            or (model and model in lane_models)
        )


@lane_routes.get("/lanes")
def list_lanes_endpoint(
    board: Optional[str] = Query(None, description="Kanban board slug (omit for current)"),
):
    """F1: all lane presets (seeding api-standard/max-abo on first contact)
    plus the profile catalog for the editor dropdowns."""
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        lanes = kanban_db.list_lanes(conn)
        active_lane = next((l for l in lanes if l["active"]), None)
        profiles = _lane_profile_catalog()
        models = _lane_model_catalog(profiles, active_lane)
        _annotate_lane_model_relevance(models, profiles, lanes)
        profiles = [
            {**p, "kanban_spawn_health": _profile_spawn_health(p, profiles, models)}
            for p in profiles
        ]
        return {
            "lanes": lanes,
            "count": len(lanes),
            "active_id": next((l["id"] for l in lanes if l["active"]), None),
            "profiles": profiles,
            "models": models,
        }
    finally:
        conn.close()


class LaneBody(BaseModel):
    name: Optional[ShortText] = None
    profiles: Optional[dict] = None


class LaneSpawnCheckBody(BaseModel):
    profile: ShortText
    worker_runtime: Literal["hermes", "claude-cli"]
    model: Optional[ShortText] = None


class LaneOpenRouterModelImportBody(BaseModel):
    raw_text: Optional[FreeText] = None
    model_ids: Optional[list[ShortText]] = None


_OPENROUTER_MODEL_ID_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._:+-]*(?::[A-Za-z0-9._+-]+)?$"
)
_OPENROUTER_IMPORT_LIMIT = 25
LANES_AUTH_SMOKE_ROLE_LIMIT = 12

class LaneAuthSmokeBody(BaseModel):
    lane_id: Optional[ShortText] = None
    roles: Optional[list[ShortText]] = None
    timeout_seconds: Optional[int] = 45


class ModelProbeResult(BaseModel):
    provider: str
    model: str
    profile: str
    status: Literal[
        "ok",
        "fallback",
        "auth_error",
        "quota_or_rate_limit",
        "timeout",
        "config_error",
        "error",
        "skipped",
    ]
    duration_ms: int
    observed_provider: str | None
    observed_model: str | None
    error_class: str | None
    reason: str | None
    at: int


class LaneModelProbeBody(BaseModel):
    provider: ShortText
    model: ShortText
    profile: ShortText = "coder"
    timeout_seconds: int = Field(default=45, ge=1, le=120)


class LaneCatalogProbeModel(BaseModel):
    provider: ShortText
    model: ShortText


class LaneCatalogProbeBody(BaseModel):
    models: list[LaneCatalogProbeModel] = Field(min_length=1, max_length=16)
    profile: Optional[ShortText] = None
    timeout_seconds: int = Field(default=45, ge=1, le=120)
    limit: int = Field(default=8, ge=1, le=16)


_LANE_MODEL_PROBE_CACHE_CAP = 200
_lane_model_probe_cache: dict[tuple[str, str], ModelProbeResult] = {}
_lane_model_probe_cache_loaded = False


def _lane_model_probe_cache_path() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "cache" / "lanes_model_probes.json"


def _load_lane_model_probe_cache() -> None:
    global _lane_model_probe_cache_loaded
    if _lane_model_probe_cache_loaded:
        return
    _lane_model_probe_cache_loaded = True
    path = _lane_model_probe_cache_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
        rows = raw if isinstance(raw, list) else []
        parsed = [ModelProbeResult.model_validate(row) for row in rows]
        parsed.sort(key=lambda row: row.at)
        for row in parsed[-_LANE_MODEL_PROBE_CACHE_CAP:]:
            _lane_model_probe_cache[(row.provider, row.model)] = row
    except Exception:
        log.exception("lanes: failed to load model probe cache")
        _lane_model_probe_cache.clear()


def _write_lane_model_probe_cache() -> None:
    from utils import atomic_json_write

    rows = sorted(_lane_model_probe_cache.values(), key=lambda row: row.at)
    atomic_json_write(
        _lane_model_probe_cache_path(),
        [row.model_dump() for row in rows[-_LANE_MODEL_PROBE_CACHE_CAP:]],
        indent=2,
    )


def _cache_lane_model_probe(result: ModelProbeResult) -> None:
    _load_lane_model_probe_cache()
    key = (result.provider, result.model)
    _lane_model_probe_cache.pop(key, None)
    _lane_model_probe_cache[key] = result
    while len(_lane_model_probe_cache) > _LANE_MODEL_PROBE_CACHE_CAP:
        oldest_key = min(
            _lane_model_probe_cache,
            key=lambda item: _lane_model_probe_cache[item].at,
        )
        _lane_model_probe_cache.pop(oldest_key)
    _write_lane_model_probe_cache()


def _lane_model_probe_for(provider: str | None, model: str) -> dict[str, Any] | None:
    if not provider:
        return None
    _load_lane_model_probe_cache()
    result = _lane_model_probe_cache.get((provider, model))
    return result.model_dump() if result is not None else None


_LANES_PROVIDER_RE = re.compile(r"provider=([A-Za-z0-9_.:/-]+)")
_LANES_MODEL_RE = re.compile(r"model=([^\s,]+)")
_LANES_SESSION_RE = re.compile(r"session=([A-Za-z0-9_.:-]+)|\[([A-Za-z0-9_.:-]+)\]")
_LANES_AUTH_RE = re.compile(
    r"\b(401|403)\b|missing bearer|unauthorized|invalid api|empty API key|authentication",
    re.IGNORECASE,
)
_LANES_QUOTA_RE = re.compile(
    r"\b(402|429)\b|quota|rate limit|RESOURCE_EXHAUSTED",
    re.IGNORECASE,
)
_LANES_TIMEOUT_RE = re.compile(r"timeout|timed out", re.IGNORECASE)
_LANES_SECRET_TEXT_RE = re.compile(
    r"(?i)(authorization:\s*(?:bearer|basic)\s+)[^\s,;]+|"
    r"(bearer\s+)[^\s,;]+|"
    r"(token\s+)[^\s,;]+|"
    r"([\"']?(?:api[_-]?key|token|secret)[\"']?\s*[:=]\s*[\"']?)[^\"'\s,;]+[\"']?|"
    r"([A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|KEY)=)[^\s,;]+|"
    r"((?:sk-proj|sk|ghp)[_-][A-Za-z0-9_-]+)"
)


def _parse_lanes_auth_smoke_log(
    lines: list[str],
    *,
    session_id: Optional[str] = None,
) -> dict[str, object]:
    scoped_lines = [line for line in lines if session_id and session_id in line] if session_id else list(lines)
    if session_id and not scoped_lines:
        scoped_lines = list(lines)

    observed_provider: Optional[str] = None
    observed_model: Optional[str] = None
    parsed_session_id: Optional[str] = session_id
    fallback_activated = False
    error_class: Optional[str] = None

    for line in scoped_lines:
        if "fallback" in line.lower():
            fallback_activated = True
        provider_match = _LANES_PROVIDER_RE.search(line)
        if provider_match:
            observed_provider = provider_match.group(1)
        model_match = _LANES_MODEL_RE.search(line)
        if model_match:
            observed_model = model_match.group(1)
        session_match = _LANES_SESSION_RE.search(line)
        if session_match and not parsed_session_id:
            parsed_session_id = session_match.group(1) or session_match.group(2)
        if _LANES_AUTH_RE.search(line):
            error_class = "auth_error"
        elif _LANES_QUOTA_RE.search(line):
            error_class = "quota_or_rate_limit"
        elif _LANES_TIMEOUT_RE.search(line):
            error_class = "timeout"

    return {
        "observed_provider": observed_provider,
        "observed_model": observed_model,
        "fallback_activated": fallback_activated,
        "error_class": error_class,
        "session_id": parsed_session_id,
    }


def _redact_lanes_auth_smoke_text(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        for prefix in match.groups():
            if prefix:
                lowered = prefix.lower()
                if lowered.startswith(("sk-", "sk_", "sk-proj", "ghp_", "ghp-")):
                    return "<redacted>"
                return f"{prefix}<redacted>"
        return "<redacted>"

    return _LANES_SECRET_TEXT_RE.sub(repl, text)


def _build_lanes_auth_smoke_command(
    *,
    python_bin: str,
    profile: str,
    provider: str,
    model: str,
    token: str,
) -> list[str]:
    return [
        python_bin,
        "-m",
        "hermes_cli.main",
        "--profile",
        profile,
        "chat",
        "-q",
        f"Reply exactly {token}",
        "--max-turns",
        "1",
        "-Q",
        "--ignore-rules",
        "--source",
        "lanes-auth-smoke",
        "--provider",
        provider,
        "--model",
        model,
    ]


def _derive_lanes_auth_smoke_status(
    *,
    returncode: int | str,
    response_exact: bool,
    requested_provider: str,
    requested_model: str,
    observed_provider: Optional[str],
    observed_model: Optional[str],
    fallback_activated: bool,
    error_class: Optional[str],
) -> str:
    if error_class:
        return error_class
    if returncode == "timeout":
        return "timeout"
    if fallback_activated:
        return "fallback"
    if observed_provider and observed_provider != requested_provider:
        return "fallback"
    if observed_model and observed_model != requested_model:
        return "fallback"
    if returncode == 0 and response_exact and observed_provider == requested_provider and observed_model == requested_model:
        return "ok"
    return "error"


def _explain_lanes_auth_smoke_result(
    *,
    status: str,
    requested_provider: str,
    requested_model: str,
    observed_provider: Optional[str],
    observed_model: Optional[str],
    response_exact: bool,
    fallback_activated: bool,
    error_class: Optional[str],
) -> str:
    observed = f"{observed_provider or '-'}/{observed_model or '-'}"
    parts = [
        f"requested {requested_provider or '-'}/{requested_model or '-'}",
        f"observed {observed}",
        "exact response" if response_exact else "response was not exact",
    ]
    if fallback_activated or status == "fallback":
        parts.append("fallback activated")
    if error_class:
        parts.append(f"error_class={error_class}")
    return "; ".join(parts)


def _summarize_lanes_auth_smoke(
    results: list[dict[str, object]],
    *,
    total_role_count: int,
    checked_role_count: int,
    truncated: bool,
) -> dict[str, object]:
    blocking_statuses = {"auth_error", "quota_or_rate_limit", "timeout", "config_error", "error"}
    blocking_roles = [
        str(item.get("role") or item.get("profile") or "unknown")
        for item in results
        if item.get("status") in blocking_statuses
    ]
    fallback_roles = [
        str(item.get("role") or item.get("profile") or "unknown")
        for item in results
        if bool(item.get("fallback_activated")) or item.get("status") == "fallback"
    ]
    skipped_roles = [
        str(item.get("role") or item.get("profile") or "unknown")
        for item in results
        if item.get("status") == "skipped"
    ]
    ok_count = sum(1 for item in results if item.get("status") == "ok")

    if not results:
        decision = "blocked"
        next_action = "Keine Rollen geprüft; Lane-Konfiguration oder Profilkatalog prüfen."
    elif blocking_roles:
        decision = "blocked"
        first = blocking_roles[0]
        next_action = f"{first.capitalize()} zuerst reparieren oder bewusst auf ein funktionierendes Modell umstellen."
    elif fallback_roles:
        decision = "restricted"
        first = fallback_roles[0]
        next_action = f"{first.capitalize()} verwendet Fallback; requested/observed Route prüfen."
    elif skipped_roles or truncated or checked_role_count < total_role_count:
        decision = "restricted"
        next_action = "Nicht geprüfte oder übersprungene Rollen vor Live-Freigabe bewusst bewerten."
    else:
        decision = "ready"
        next_action = "Lane kann nach kontrolliertem Dashboard-Respawn erneut produktiv verifiziert werden."

    return {
        "decision": decision,
        "safe_to_activate": decision == "ready",
        "ok_count": ok_count,
        "blocking_roles": blocking_roles,
        "fallback_roles": fallback_roles,
        "skipped_roles": skipped_roles,
        "checked_role_count": checked_role_count,
        "total_role_count": total_role_count,
        "truncated": truncated,
        "recommended_next_action": next_action,
    }


def _select_lanes_auth_smoke_roles(
    lane: dict[str, object],
    requested_roles: list[str] | None,
    catalog: list[dict],
) -> list[dict[str, object]]:
    lane_profiles = lane.get("profiles") if isinstance(lane, dict) else {}
    if not isinstance(lane_profiles, dict):
        lane_profiles = {}
    catalog_by_name = {
        str(item.get("name") or ""): item
        for item in catalog
        if str(item.get("name") or "").strip()
    }

    requested = [str(role).strip() for role in (requested_roles or []) if str(role).strip()]
    if requested:
        names = requested
    else:
        names = list(catalog_by_name)
        names.extend(name for name in lane_profiles if name not in catalog_by_name)

    results: list[dict[str, object]] = []
    seen: set[str] = set()
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        lane_entry = lane_profiles.get(name) if isinstance(lane_profiles.get(name), dict) else {}
        catalog_entry = catalog_by_name.get(name, {})
        runtime = str(lane_entry.get("worker_runtime") or catalog_entry.get("worker_runtime") or "hermes")
        provider = lane_entry.get("provider")
        if provider is None:
            provider = catalog_entry.get("default_provider")
        model = lane_entry.get("model")
        if model is None:
            model = catalog_entry.get("default_model")
        results.append({
            "role": name,
            "profile": name,
            "runtime": runtime,
            "provider": str(provider or ""),
            "model": str(model or ""),
        })
    return results


def _lanes_auth_smoke_profile_log_path(profile: str) -> Path:
    try:
        from hermes_cli.profiles import resolve_profile_env

        return Path(resolve_profile_env(profile)) / "logs" / "agent.log"
    except Exception:
        return Path.home() / ".hermes" / "profiles" / profile / "logs" / "agent.log"


def _count_lanes_auth_smoke_log_lines(path: Path) -> int:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return sum(1 for _ in handle)
    except FileNotFoundError:
        return 0


def _read_lanes_auth_smoke_log_lines(path: Path, start_line: int) -> list[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read().splitlines()[start_line:]
    except FileNotFoundError:
        return []


def _normalize_openrouter_import_token(value: object) -> str:
    token = str(value or "").strip().strip("`'\"[]{}()")
    if token.lower().startswith("openrouter:"):
        token = token.split(":", 1)[1].strip()
    return token


def _parse_openrouter_import_tokens(payload: LaneOpenRouterModelImportBody) -> list[str]:
    raw: list[str] = []
    if payload.model_ids:
        raw.extend(str(item) for item in payload.model_ids)
    if payload.raw_text:
        raw.extend(re.split(r"[\s,;]+", payload.raw_text))
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        token = _normalize_openrouter_import_token(item)
        if not token or token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out


def _openrouter_extra_models_from_config() -> list[str]:
    try:
        from hermes_cli.model_catalog import get_configured_provider_extra_models

        return get_configured_provider_extra_models("openrouter")
    except Exception:
        log.exception("lanes: failed to read OpenRouter extra_models from config")
        return []


def _write_openrouter_extra_models_to_config(model_ids: list[str]) -> None:
    from hermes_cli.config import get_config_path

    config_path = get_config_path()
    try:
        from utils import atomic_roundtrip_yaml_update

        atomic_roundtrip_yaml_update(
            config_path,
            "model_catalog.providers.openrouter.extra_models",
            model_ids,
        )
        return
    except ModuleNotFoundError as exc:
        if exc.name != "ruamel":
            raise

    from hermes_cli.config import read_raw_config
    from utils import atomic_yaml_write

    cfg = read_raw_config()
    model_catalog = cfg.setdefault("model_catalog", {})
    if not isinstance(model_catalog, dict):
        model_catalog = {}
        cfg["model_catalog"] = model_catalog
    providers = model_catalog.setdefault("providers", {})
    if not isinstance(providers, dict):
        providers = {}
        model_catalog["providers"] = providers
    openrouter = providers.setdefault("openrouter", {})
    if not isinstance(openrouter, dict):
        openrouter = {}
        providers["openrouter"] = openrouter
    openrouter["extra_models"] = model_ids
    atomic_yaml_write(config_path, cfg, sort_keys=False)


def _admit_openrouter_extra_models(model_ids: list[str]) -> tuple[list[str], list[str]]:
    existing = _openrouter_extra_models_from_config()
    seen = set(existing)
    merged = list(existing)
    added: list[str] = []
    for model_id in model_ids:
        if model_id in seen:
            continue
        seen.add(model_id)
        merged.append(model_id)
        added.append(model_id)
    if added:
        from hermes_cli.model_catalog import reset_cache as reset_model_catalog_cache

        _write_openrouter_extra_models_to_config(merged)
        reset_model_catalog_cache()
    return added, merged


def _smoke_openrouter_model_id(model_id: str) -> tuple[bool, str]:
    """Run a minimal OpenRouter completion through Hermes runtime plumbing."""
    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider
        from run_agent import AIAgent

        runtime = resolve_runtime_provider(requested="openrouter", target_model=model_id)
        agent = AIAgent(
            api_key=runtime.get("api_key"),
            base_url=runtime.get("base_url"),
            provider=runtime.get("provider"),
            api_mode=runtime.get("api_mode"),
            model=model_id,
            enabled_toolsets=[],
            quiet_mode=True,
            platform="dashboard",
            credential_pool=runtime.get("credential_pool"),
            max_iterations=1,
            max_tokens=8,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=None,
        )
        agent.tools = []
        agent.valid_tool_names = set()
        response = (agent.chat("Reply with OK.") or "").strip()
        if not response:
            return False, "Smoke produced no response"
        return True, "Smoke ok"
    except Exception as exc:  # noqa: BLE001 - expose sanitized provider failure
        try:
            from hermes_cli.error_sanitize import safe_detail

            return False, safe_detail(exc, "OpenRouter smoke failed", log=log)
        except Exception:
            log.exception("OpenRouter smoke failed")
            return False, "OpenRouter smoke failed"


def _lane_model_runtime(
    model: Optional[str],
    profiles: list[dict],
    models: Optional[list[dict]] = None,
) -> Optional[str]:
    """Return the curated runtime for ``model`` when known.

    Unknown models are intentionally fail-soft; profile defaults can be added
    by ``_lane_model_catalog`` and genuinely custom provider ids still need the
    real worker path to decide whether they work.
    """
    model = (model or "").strip()
    if not model:
        return None
    catalog = models if models is not None else _lane_model_catalog(profiles)
    for item in catalog:
        if item.get("id") == model:
            runtime = item.get("runtime")
            return runtime if runtime in {"hermes", "claude-cli"} else None
    if model.startswith("claude-"):
        return "claude-cli"
    return None


def _profile_spawn_health(profile: dict, profiles: list[dict], models: list[dict]) -> dict:
    """Spawn-Health eines Katalog-Profils für GET /lanes.

    Gleiche Prüf-Seams wie POST /lanes/spawn-check (Model↔Runtime-Widerspruch,
    claude-Binary), aber auf den Katalog-Defaults des Profils — das Frontend
    erwartet das Feld pro Profil und disabled sonst die Triage-Eskalation.
    """
    runtime = profile.get("worker_runtime") or "hermes"
    model = profile.get("default_model")
    model_runtime = _lane_model_runtime(model, profiles, models)
    if model_runtime and model_runtime != runtime:
        return {
            "status": "unhealthy",
            "reason": f"Model {model!r} belongs to {model_runtime}, but profile runtime is {runtime}",
        }
    if runtime == "claude-cli" and not _claude_worker_available():
        return {
            "status": "unhealthy",
            "reason": "`claude` executable is not available for claude-cli workers",
        }
    return {"status": "healthy", "reason": None}


def _claude_worker_available() -> bool:
    import shutil

    binary = kanban_db._claude_worker_bin()
    if os.path.sep in binary:
        return os.path.exists(binary)
    return shutil.which(binary) is not None


@lane_routes.post("/lanes/spawn-check")
def lane_spawn_check_endpoint(payload: LaneSpawnCheckBody):
    """Read-only Lane worker/model health check for the dashboard.

    This mirrors the dispatcher's lane seams without creating a task or
    touching the board: profile must exist in the lean lane catalog, the
    selected model must not contradict the selected worker runtime, and the
    claude-cli path must have an executable available.
    """
    profiles = _lane_profile_catalog()
    models = _lane_model_catalog(profiles)
    profile = next((p for p in profiles if p.get("name") == payload.profile), None)
    dispatcher_path = payload.worker_runtime
    resolved_model = payload.model or (profile or {}).get("default_model") or None

    if profile is None:
        return {
            "status": "unhealthy",
            "reason": f"Profile {payload.profile!r} is not in the lane catalog",
            "dispatcher_path": dispatcher_path,
            "resolved_model": resolved_model,
        }

    model_runtime = _lane_model_runtime(resolved_model, profiles, models)
    if model_runtime and model_runtime != dispatcher_path:
        return {
            "status": "unhealthy",
            "reason": f"Model {resolved_model!r} belongs to {model_runtime}, but selected worker runtime is {dispatcher_path}",
            "dispatcher_path": dispatcher_path,
            "resolved_model": resolved_model,
        }

    if dispatcher_path == "claude-cli":
        if not _claude_worker_available():
            return {
                "status": "unhealthy",
                "reason": "`claude` executable is not available for claude-cli workers",
                "dispatcher_path": dispatcher_path,
                "resolved_model": resolved_model,
            }
        return {
            "status": "healthy",
            "reason": "Claude CLI worker executable is available",
            "dispatcher_path": dispatcher_path,
            "resolved_model": resolved_model,
        }

    return {
        "status": "healthy",
        "reason": "Hermes worker profile is available",
        "dispatcher_path": dispatcher_path,
        "resolved_model": resolved_model,
    }

def _extract_lanes_auth_smoke_session_id(text: str, lines: list[str], *, token: str) -> Optional[str]:
    for line in lines:
        if token not in line and "lanes-auth-smoke" not in line:
            continue
        match = _LANES_SESSION_RE.search(line)
        if match:
            return match.group(1) or match.group(2)
    match = _LANES_SESSION_RE.search(text)
    if match:
        return match.group(1) or match.group(2)
    for line in reversed(lines):
        match = _LANES_SESSION_RE.search(line)
        if match:
            return match.group(1) or match.group(2)
    return None


def _lanes_auth_smoke_response_exact(stdout: str, token: str) -> bool:
    for line in stdout.splitlines():
        cleaned = line.strip().strip("`\"'")
        if cleaned == token:
            return True
    return stdout.strip().strip("`\"'") == token


def _run_single_lanes_auth_smoke(role: dict[str, object], *, timeout_seconds: int) -> dict[str, object]:
    import secrets
    import subprocess
    import sys

    profile = str(role.get("profile") or role.get("role") or "").strip()
    provider = str(role.get("provider") or "").strip()
    model = str(role.get("model") or "").strip()
    runtime = str(role.get("runtime") or "hermes").strip()
    role_name = str(role.get("role") or profile or "unknown")

    base = {
        "role": role_name,
        "profile": profile,
        "runtime": runtime,
        "requested_provider": provider,
        "requested_model": model,
        "observed_provider": None,
        "observed_model": None,
        "response_exact": False,
        "fallback_activated": False,
        "auth_ok": False,
        "session_id": None,
    }
    if runtime != "hermes":
        return {
            **base,
            "status": "skipped",
            "error_class": None,
            "reason": "unsupported runtime for auth smoke",
        }
    if not profile or not provider or not model:
        return {
            **base,
            "status": "config_error",
            "error_class": "missing_profile_provider_or_model",
            "reason": "missing profile, provider, or model",
        }

    agent_root = Path(__file__).resolve().parents[3]
    log_path = _lanes_auth_smoke_profile_log_path(profile)
    before_lines = _count_lanes_auth_smoke_log_lines(log_path)
    token = f"lanes-auth-smoke-{profile}-{secrets.token_hex(3)}"
    command = _build_lanes_auth_smoke_command(
        python_bin=sys.executable,
        profile=profile,
        provider=provider,
        model=model,
        token=token,
    )
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=str(agent_root),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
        )
        returncode: int | str = completed.returncode
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
    except subprocess.TimeoutExpired as exc:
        returncode = "timeout"
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else "timeout"

    new_lines = _read_lanes_auth_smoke_log_lines(log_path, before_lines)
    session_id = _extract_lanes_auth_smoke_session_id(stdout + "\n" + stderr, new_lines, token=token)
    parsed = _parse_lanes_auth_smoke_log(new_lines, session_id=session_id)
    response_exact = _lanes_auth_smoke_response_exact(stdout, token)
    status = _derive_lanes_auth_smoke_status(
        returncode=returncode,
        response_exact=response_exact,
        requested_provider=provider,
        requested_model=model,
        observed_provider=parsed["observed_provider"],
        observed_model=parsed["observed_model"],
        fallback_activated=bool(parsed["fallback_activated"]),
        error_class=parsed["error_class"],
    )
    reason = _explain_lanes_auth_smoke_result(
        status=status,
        requested_provider=provider,
        requested_model=model,
        observed_provider=parsed["observed_provider"],
        observed_model=parsed["observed_model"],
        response_exact=response_exact,
        fallback_activated=bool(parsed["fallback_activated"]),
        error_class=parsed["error_class"],
    )

    return {
        **base,
        "observed_provider": parsed["observed_provider"],
        "observed_model": parsed["observed_model"],
        "response_exact": response_exact,
        "fallback_activated": parsed["fallback_activated"],
        "auth_ok": status == "ok",
        "status": status,
        "error_class": parsed["error_class"],
        "duration_ms": int((time.monotonic() - started) * 1000),
        "session_id": parsed["session_id"] or session_id,
        "reason": reason,
        "observed_response": _redact_lanes_auth_smoke_text(stdout.strip()[:300]),
        "stderr_preview": _redact_lanes_auth_smoke_text(stderr.strip()[:300]),
    }


@lane_routes.post("/lanes/auth-smoke")
def lane_auth_smoke_endpoint(
    payload: LaneAuthSmokeBody,
    board: Optional[str] = Query(None, description="Kanban board slug (omit for current)"),
):
    board = _resolve_board(board)
    timeout_seconds = min(max(int(payload.timeout_seconds or 45), 5), 60)
    conn = _conn(board=board)
    try:
        lanes = kanban_db.list_lanes(conn)
        lane = None
        if payload.lane_id:
            lane = next((item for item in lanes if item.get("id") == payload.lane_id), None)
        else:
            lane = next((item for item in lanes if item.get("active")), None)
        if lane is None:
            raise HTTPException(status_code=404, detail="lane_not_found")
    finally:
        conn.close()

    profiles = _lane_profile_catalog()
    requested_roles = payload.roles or []
    all_roles = _select_lanes_auth_smoke_roles(lane, requested_roles, profiles)
    truncated = len(all_roles) > LANES_AUTH_SMOKE_ROLE_LIMIT
    roles = all_roles[:LANES_AUTH_SMOKE_ROLE_LIMIT]
    results = [
        _run_single_lanes_auth_smoke(role, timeout_seconds=timeout_seconds)
        for role in roles
    ]
    summary = _summarize_lanes_auth_smoke(
        results,
        total_role_count=len(all_roles),
        checked_role_count=len(results),
        truncated=truncated,
    )
    return {
        "ok": bool(results) and all(item.get("status") in {"ok", "skipped"} for item in results),
        "lane_id": lane.get("id"),
        "source": "lanes-auth-smoke",
        "scope": {
            "requested_roles": requested_roles,
            "checked_role_count": len(results),
            "total_role_count": len(all_roles),
            "truncated": truncated,
            "role_limit": LANES_AUTH_SMOKE_ROLE_LIMIT,
        },
        "summary": summary,
        "results": results,
    }


def _run_lanes_model_probe(
    *,
    provider: str,
    model: str,
    profile: str,
    timeout_seconds: int,
) -> ModelProbeResult:
    smoke = _run_single_lanes_auth_smoke(
        {
            "role": profile,
            "profile": profile,
            "provider": provider,
            "model": model,
            "runtime": "hermes",
        },
        timeout_seconds=timeout_seconds,
    )
    status = str(smoke.get("status") or "error")
    allowed_statuses = {
        "ok",
        "fallback",
        "auth_error",
        "quota_or_rate_limit",
        "timeout",
        "config_error",
        "error",
        "skipped",
    }
    if status not in allowed_statuses:
        status = "error"
    return ModelProbeResult(
        provider=provider,
        model=model,
        profile=profile,
        status=status,
        duration_ms=max(int(smoke.get("duration_ms") or 0), 0),
        observed_provider=smoke.get("observed_provider"),
        observed_model=smoke.get("observed_model"),
        error_class=smoke.get("error_class"),
        reason=smoke.get("reason"),
        at=int(time.time()),
    )


@lane_routes.post("/lanes/model-probe")
def lane_model_probe_endpoint(payload: LaneModelProbeBody):
    try:
        result = _run_lanes_model_probe(
            provider=payload.provider.strip(),
            model=payload.model.strip(),
            profile=payload.profile.strip(),
            timeout_seconds=payload.timeout_seconds,
        )
        _cache_lane_model_probe(result)
        return result
    except HTTPException:
        raise
    except Exception as exc:
        from hermes_cli.error_sanitize import safe_detail

        raise HTTPException(
            status_code=500,
            detail=safe_detail(exc, "Model probe failed", log=log),
        ) from exc


@lane_routes.post("/lanes/catalog-probe")
def lane_catalog_probe_endpoint(payload: LaneCatalogProbeBody):
    try:
        selected = payload.models[:payload.limit]
        profile = (payload.profile or "coder").strip()
        results: list[ModelProbeResult] = []
        for target in selected:
            result = _run_lanes_model_probe(
                provider=target.provider.strip(),
                model=target.model.strip(),
                profile=profile,
                timeout_seconds=payload.timeout_seconds,
            )
            _cache_lane_model_probe(result)
            results.append(result)
        return {
            "results": results,
            "truncated": len(payload.models) > len(selected),
        }
    except HTTPException:
        raise
    except Exception as exc:
        from hermes_cli.error_sanitize import safe_detail

        raise HTTPException(
            status_code=500,
            detail=safe_detail(exc, "Catalog probe failed", log=log),
        ) from exc


@lane_routes.post("/lanes/openrouter-models/import")
def lane_openrouter_model_import_endpoint(payload: LaneOpenRouterModelImportBody):
    """Smoke pasted OpenRouter model IDs and admit successful ones to config."""
    tokens = _parse_openrouter_import_tokens(payload)
    if len(tokens) > _OPENROUTER_IMPORT_LIMIT:
        raise HTTPException(
            status_code=400,
            detail=f"At most {_OPENROUTER_IMPORT_LIMIT} model IDs can be smoked at once",
        )

    results: list[dict[str, str]] = []
    smoke_ok: list[str] = []
    for token in tokens:
        if not _OPENROUTER_MODEL_ID_RE.fullmatch(token):
            results.append({
                "id": token,
                "status": "invalid",
                "reason": "Expected an OpenRouter model id like vendor/model",
            })
            continue
        ok, reason = _smoke_openrouter_model_id(token)
        if ok:
            smoke_ok.append(token)
            results.append({"id": token, "status": "smoke_ok", "reason": reason})
        else:
            results.append({"id": token, "status": "failed", "reason": reason})

    added, configured = _admit_openrouter_extra_models(smoke_ok) if smoke_ok else ([], _openrouter_extra_models_from_config())
    added_set = set(added)
    for row in results:
        if row["status"] != "smoke_ok":
            continue
        if row["id"] in added_set:
            row["status"] = "admitted"
            row["reason"] = "Smoke ok; added to config"
        else:
            row["status"] = "already_configured"
            row["reason"] = "Smoke ok; already present in config"

    return {
        "results": results,
        "admitted": added,
        "configured": configured,
    }


@lane_routes.post("/lanes")
def create_lane_endpoint(
    payload: LaneBody,
    board: Optional[str] = Query(None, description="Kanban board slug (omit for current)"),
):
    """F1: create a lane preset (inactive until explicitly activated)."""
    if not payload.name:
        raise HTTPException(status_code=400, detail="name is required")
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        try:
            lane = kanban_db.create_lane(
                conn, name=payload.name, profiles=payload.profiles,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"lane": lane}
    finally:
        conn.close()


@lane_routes.put("/lanes/{lane_id}")
def update_lane_endpoint(
    lane_id: str,
    payload: LaneBody,
    board: Optional[str] = Query(None, description="Kanban board slug (omit for current)"),
):
    """F1: rename a lane and/or replace its profile mapping."""
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        try:
            lane = kanban_db.update_lane(
                conn, lane_id, name=payload.name, profiles=payload.profiles,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if lane is None:
            raise HTTPException(status_code=404, detail=f"lane {lane_id} not found")
        return {"lane": lane}
    finally:
        conn.close()


@lane_routes.delete("/lanes/{lane_id}")
def delete_lane_endpoint(
    lane_id: str,
    board: Optional[str] = Query(None, description="Kanban board slug (omit for current)"),
):
    """F1: delete a lane. The active lane is protected (409)."""
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        try:
            ok = kanban_db.delete_lane(conn, lane_id)
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))
        if not ok:
            raise HTTPException(status_code=404, detail=f"lane {lane_id} not found")
        return {"deleted": lane_id}
    finally:
        conn.close()


@lane_routes.post("/lanes/{lane_id}/activate")
def activate_lane_endpoint(
    lane_id: str,
    board: Optional[str] = Query(None, description="Kanban board slug (omit for current)"),
):
    """F1: make this the single active lane. Takes effect from the next
    worker spawn — the dispatcher hot-reads the active lane per spawn."""
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        lane = kanban_db.activate_lane(conn, lane_id)
        if lane is None:
            raise HTTPException(status_code=404, detail=f"lane {lane_id} not found")
        return {"lane": lane}
    finally:
        conn.close()


class LanePersistFallbackEntry(BaseModel):
    provider: ShortText
    model: ShortText


class LanePersistProfileEntry(BaseModel):
    worker_runtime: Literal["hermes", "claude-cli"]
    provider: Optional[ShortText] = None
    model: ShortText
    # ``None`` preserves the pre-K31 behaviour for older API clients that omit
    # this field. An explicit empty list is materially different: it clears the
    # profile fallback chain and the active-lane override.
    fallback_providers: Optional[list[LanePersistFallbackEntry]] = None
    reasoning_effort: str | None = None


class LanePersistBody(BaseModel):
    profiles: dict[str, LanePersistProfileEntry]


@lane_routes.post("/lanes/persist")
def persist_lane_models_endpoint(
    payload: LanePersistBody,
    board: Optional[str] = Query(None, description="Kanban board slug (omit for current)"),
):
    """Persist complete lane selections to profile configs and the active lane.

    The operation is all-or-nothing across every requested profile. Profile
    configs are snapshotted before the first write and atomically restored if a
    later config write or the active-lane mirror fails. Older clients that omit
    ``fallback_providers`` keep their existing chain; an explicit ``[]`` clears
    it deterministically.
    """
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        from hermes_cli import profiles as profiles_mod
        from utils import atomic_replace, atomic_roundtrip_yaml_update

        catalog_profiles = _lane_profile_catalog()
        known_profiles = {p["name"] for p in catalog_profiles}
        active_lane_for_catalog = kanban_db.get_active_lane(conn)
        models = _lane_model_catalog(catalog_profiles, active_lane_for_catalog)
        known_models = {m["id"] for m in models}

        unknown_profiles = [name for name in payload.profiles if name not in known_profiles]
        if unknown_profiles:
            raise HTTPException(
                status_code=400,
                detail={"error": "unknown profiles", "profiles": unknown_profiles},
            )

        bad_models: list[dict[str, str]] = []
        bad_runtime_models: list[dict[str, str]] = []
        bad_claude_providers: list[dict[str, str]] = []
        bad_reasoning_profiles: list[str] = []
        catalog_profiles_by_name = {profile["name"]: profile for profile in catalog_profiles}
        for name, entry in payload.profiles.items():
            if entry.model not in known_models:
                bad_models.append({"profile": name, "model": entry.model})
                continue
            model_runtime = _lane_model_runtime(entry.model, catalog_profiles, models)
            if model_runtime and model_runtime != entry.worker_runtime:
                bad_runtime_models.append(
                    {
                        "profile": name,
                        "model": entry.model,
                        "expected_runtime": model_runtime,
                        "worker_runtime": entry.worker_runtime,
                    }
                )
            if entry.worker_runtime == "claude-cli" and entry.provider:
                bad_claude_providers.append({"profile": name, "provider": str(entry.provider)})
            if entry.reasoning_effort not in {None, ""}:
                profile = catalog_profiles_by_name[name]
                model_row = next(
                    (
                        row
                        for row in models
                        if row.get("id") == entry.model
                        and row.get("runtime") == entry.worker_runtime
                    ),
                    None,
                )
                target_provider = (
                    entry.provider
                    or (model_row or {}).get("provider")
                    or profile.get("default_provider")
                )
                target_model = entry.model or profile.get("default_model") or ""
                if entry.reasoning_effort not in reasoning_support_for(
                    target_provider,
                    target_model,
                ):
                    bad_reasoning_profiles.append(name)
        if bad_models:
            raise HTTPException(
                status_code=400,
                detail={"error": "unknown models", "models": bad_models},
            )
        if bad_runtime_models:
            raise HTTPException(
                status_code=400,
                detail={"error": "model runtime mismatch", "models": bad_runtime_models},
            )
        if bad_claude_providers:
            raise HTTPException(
                status_code=400,
                detail={"error": "claude-cli provider must be empty", "profiles": bad_claude_providers},
            )
        if bad_reasoning_profiles:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "unsupported reasoning effort",
                    "profiles": bad_reasoning_profiles,
                },
            )

        lanes = kanban_db.list_lanes(conn)
        active_id = next((lane["id"] for lane in lanes if lane["active"]), None)
        active_lane = next((lane for lane in lanes if lane["id"] == active_id), None)
        active_profiles = (active_lane or {}).get("profiles") or {}

        config_snapshots: dict[str, tuple[Path, bool, bytes, int | None]] = {}
        lane_profiles: dict[str, dict[str, Any]] = {}
        for name, entry in payload.profiles.items():
            canon = profiles_mod.normalize_profile_name(name)
            config_path = profiles_mod.get_profile_dir(canon) / "config.yaml"
            exists = config_path.exists()
            config_snapshots[name] = (
                config_path,
                exists,
                config_path.read_bytes() if exists else b"",
                config_path.stat().st_mode if exists else None,
            )
            existing = active_profiles.get(name) or {}
            fallback_rows = (
                existing.get("fallback_providers") or []
                if entry.fallback_providers is None
                else [row.model_dump() for row in entry.fallback_providers]
            )
            lane_profiles[name] = {
                "worker_runtime": entry.worker_runtime,
                "provider": (
                    None
                    if entry.worker_runtime == "claude-cli"
                    else (entry.provider or existing.get("provider"))
                ),
                "model": entry.model,
                "fallback_providers": fallback_rows,
            }

        def rollback_profile_configs() -> list[str]:
            errors: list[str] = []
            for profile_name, (config_path, existed, contents, mode) in config_snapshots.items():
                try:
                    if not existed:
                        config_path.unlink(missing_ok=True)
                        continue
                    config_path.parent.mkdir(parents=True, exist_ok=True)
                    fd, tmp_name = tempfile.mkstemp(
                        prefix=f".{config_path.name}.lane-rollback-",
                        dir=str(config_path.parent),
                    )
                    tmp_path = Path(tmp_name)
                    try:
                        with os.fdopen(fd, "wb") as handle:
                            handle.write(contents)
                            handle.flush()
                            os.fsync(handle.fileno())
                        if mode is not None:
                            os.chmod(tmp_path, mode)
                        atomic_replace(tmp_path, config_path)
                    finally:
                        tmp_path.unlink(missing_ok=True)
                except Exception as rollback_exc:
                    log.exception("lanes/persist: rollback failed for %s", profile_name)
                    errors.append(f"{profile_name}: {rollback_exc}")
            return errors

        failed_profile = "__active_lane__"
        try:
            for name, entry in payload.profiles.items():
                failed_profile = name
                config_path = config_snapshots[name][0]
                if entry.worker_runtime == "claude-cli":
                    atomic_roundtrip_yaml_update(config_path, "claude_model", entry.model)
                    atomic_roundtrip_yaml_update(config_path, "worker_runtime", "claude-cli")
                else:
                    atomic_roundtrip_yaml_update(config_path, "model.default", entry.model)
                    # An absent provider preserves an operator-pinned provider.
                    if entry.provider:
                        atomic_roundtrip_yaml_update(config_path, "model.provider", entry.provider)
                    atomic_roundtrip_yaml_update(config_path, "worker_runtime", "hermes")
                if entry.fallback_providers is not None:
                    atomic_roundtrip_yaml_update(
                        config_path,
                        "fallback_providers",
                        [row.model_dump() for row in entry.fallback_providers],
                    )
                if entry.reasoning_effort is not None:
                    atomic_roundtrip_yaml_update(
                        config_path,
                        "agent.reasoning_effort",
                        entry.reasoning_effort,
                    )

            failed_profile = "__active_lane__"
            if lane_profiles and active_id is not None and active_lane is not None:
                merged_profiles = dict(active_profiles)
                merged_profiles.update(lane_profiles)
                kanban_db.update_lane(conn, active_id, profiles=merged_profiles)
            _invalidate_lane_profile_caches()
        except Exception as exc:
            log.exception("lanes/persist: transaction failed at %s", failed_profile)
            rollback_errors = rollback_profile_configs()
            if rollback_errors:
                raise HTTPException(
                    status_code=500,
                    detail={
                        "error": "lane persist rollback failed",
                        "cause": str(exc),
                        "rollback_errors": rollback_errors,
                    },
                ) from exc
            return {
                "written": [],
                "failed": [
                    {
                        "profile": failed_profile,
                        "error": f"{exc}; transaction rolled back",
                    },
                ],
                "lanes": kanban_db.list_lanes(conn),
                "active_id": active_id,
            }

        return {
            "written": list(payload.profiles),
            "failed": [],
            "lanes": kanban_db.list_lanes(conn),
            "active_id": active_id,
        }
    finally:
        conn.close()


def _invalidate_lane_profile_caches() -> None:
    global _lane_profile_cache
    _lane_profile_cache = None
    try:
        web_server = sys.modules.get("hermes_cli.web_server")
        invalidate = getattr(web_server, "_invalidate_profiles_cache", None)
        if invalidate is not None:
            invalidate()
    except Exception:
        log.debug("lanes: dashboard profile cache invalidation unavailable", exc_info=True)



__all__ = tuple(
    name
    for name in globals()
    if name not in _API_CONTEXT_NAMES
    and name != "_API_CONTEXT_NAMES"
    and not name.startswith("__")
)
