"""Fail-soft, fork-owned usage facts observer for Hermes agent runs."""

from __future__ import annotations

import json
import logging
import os
import threading
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from functools import wraps
from typing import Any, Mapping, Optional

from agent.usage_pricing import resolve_billing_route
from hermes_cli.usage_facts_db import (
    increment_tool_call,
    purge_expired_traces,
    record_llm_call,
    record_tool_result,
    record_trace,
    upsert_run_facts,
)

logger = logging.getLogger(__name__)

_MAX_CORRELATIONS = 4096
_STATE_LOCK = threading.RLock()
_CONTEXTS: OrderedDict[str, "_RunContext"] = OrderedDict()
_PENDING_TOOLS: dict[tuple[str, int, str], int] = defaultdict(int)
_PURGE_LOCK = threading.Lock()
_PURGE_ATTEMPTED = False


@dataclass
class _RunContext:
    run_id: str
    requested_provider: Optional[str] = None
    requested_model: Optional[str] = None
    lane: Optional[str] = None
    last_call_index: Optional[int] = None


def _fail_soft_hook(function):
    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> None:
        try:
            function(*args, **kwargs)
        except Exception:
            logger.debug("board_facts hook failed", exc_info=True)
        return None

    return wrapped


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _integer(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if value is None:
        return {}
    for method_name in ("model_dump", "dict"):
        method = getattr(value, method_name, None)
        if callable(method):
            try:
                result = method()
                if isinstance(result, Mapping):
                    return dict(result)
            except Exception:
                pass
    result = getattr(value, "__dict__", None)
    return dict(result) if isinstance(result, Mapping) else {}


def _request_body(kwargs: Mapping[str, Any]) -> dict[str, Any]:
    request = _mapping(kwargs.get("request"))
    body = request.get("body")
    if isinstance(body, Mapping):
        return dict(body)
    return {}


def _first(*values: Any) -> Any:
    return next((value for value in values if value is not None), None)


def _usage_bucket(usage: Mapping[str, Any], name: str) -> Optional[int]:
    """Keep an explicit provider zero but suppress a normalizer default zero."""
    if usage.get(f"{name}_observed") is False:
        return None
    return _integer(usage.get(name))


def _legacy_thinking_effort(thinking: Mapping[str, Any]) -> Optional[str]:
    budget = _integer(thinking.get("budget_tokens"))
    return {
        4_000: "low",
        8_000: "medium",
        16_000: "high",
        32_000: "xhigh",
    }.get(budget)


def _tool_output_chars(result: Any) -> int:
    """Count Unicode characters in the actual post_tool_call result payload."""
    if result is None:
        return 0
    if isinstance(result, str):
        return len(result)
    rendered = json.dumps(
        result,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return len(rendered)


def _purge_expired_traces_once() -> None:
    global _PURGE_ATTEMPTED
    with _PURGE_LOCK:
        if _PURGE_ATTEMPTED:
            return
        _PURGE_ATTEMPTED = True
    purge_expired_traces()


def _correlation_keys(kwargs: Mapping[str, Any]) -> list[str]:
    keys: list[str] = []
    for name in ("api_request_id", "turn_id", "task_id", "session_id"):
        value = _text(kwargs.get(name))
        if value is not None:
            keys.append(f"{name}:{value}")
    return keys


def _remember_context(ctx: _RunContext, kwargs: Mapping[str, Any]) -> None:
    with _STATE_LOCK:
        for key in _correlation_keys(kwargs):
            _CONTEXTS[key] = ctx
            _CONTEXTS.move_to_end(key)
        while len(_CONTEXTS) > _MAX_CORRELATIONS:
            _CONTEXTS.popitem(last=False)


def _existing_context(
    kwargs: Mapping[str, Any],
) -> tuple[Optional[_RunContext], Optional[str]]:
    with _STATE_LOCK:
        for name in ("api_request_id", "turn_id", "task_id", "session_id"):
            value = _text(kwargs.get(name))
            if value is None:
                continue
            key = f"{name}:{value}"
            ctx = _CONTEXTS.get(key)
            if ctx is not None:
                _CONTEXTS.move_to_end(key)
                return ctx, name
    return None, None


def _resolve_context(kwargs: Mapping[str, Any]) -> Optional[_RunContext]:
    existing, matched_on = _existing_context(kwargs)
    body = _request_body(kwargs)
    explicit_run_id = _text(kwargs.get("task_run_id")) or _text(
        os.environ.get("HERMES_KANBAN_RUN_ID")
    )
    direct_run_id = _first(
        _text(kwargs.get("turn_id")),
        _text(kwargs.get("api_request_id")),
        _text(kwargs.get("task_id")),
        _text(kwargs.get("session_id")),
    )
    if explicit_run_id is not None:
        reuse_existing = (
            existing is not None and existing.run_id == explicit_run_id
        )
        run_id = explicit_run_id
    elif existing is not None and matched_on in {"api_request_id", "turn_id"}:
        reuse_existing = True
        run_id = existing.run_id
    else:
        # A session/task mapping from an older turn is too broad to define a
        # new run. Prefer the current turn/request identity when available.
        reuse_existing = existing is not None and direct_run_id is None
        run_id = direct_run_id or (
            existing.run_id if existing is not None else None
        )
    if run_id is None:
        return None

    requested_provider = _first(
        _text(kwargs.get("requested_provider")),
        existing.requested_provider if reuse_existing else None,
        _text(kwargs.get("provider")),
    )
    requested_model = _first(
        _text(kwargs.get("requested_model")),
        existing.requested_model if reuse_existing else None,
        _text(body.get("model")),
        _text(kwargs.get("model")),
    )
    lane = _first(
        _text(kwargs.get("lane")),
        existing.lane if reuse_existing else None,
        _text(os.environ.get("HERMES_PROFILE")),
    )
    ctx = existing if reuse_existing else _RunContext(run_id=run_id)
    ctx.run_id = run_id
    ctx.requested_provider = requested_provider
    ctx.requested_model = requested_model
    ctx.lane = lane
    _remember_context(ctx, kwargs)
    return ctx


def _call_index(kwargs: Mapping[str, Any], ctx: _RunContext) -> int:
    explicit = _integer(kwargs.get("call_index"))
    if explicit is None:
        explicit = _integer(kwargs.get("api_call_count"))
    with _STATE_LOCK:
        if explicit is not None and explicit >= 0:
            ctx.last_call_index = explicit
        elif ctx.last_call_index is None:
            ctx.last_call_index = 1
        return ctx.last_call_index


def _sampling_fields(kwargs: Mapping[str, Any]) -> dict[str, Any]:
    body = _request_body(kwargs)
    reasoning_map = _mapping(body.get("reasoning"))
    extra_body = _mapping(body.get("extra_body"))
    extra_reasoning_map = _mapping(extra_body.get("reasoning"))
    thinking_map = _mapping(body.get("thinking"))
    output_config = _mapping(body.get("output_config"))
    return {
        "serving_tier": _first(
            _text(kwargs.get("serving_tier")),
            _text(kwargs.get("service_tier")),
            _text(body.get("serving_tier")),
            _text(body.get("service_tier")),
        ),
        "reasoning_effort": _first(
            _text(kwargs.get("reasoning_effort")),
            _text(body.get("reasoning_effort")),
            _text(reasoning_map.get("effort")),
            _text(extra_reasoning_map.get("effort")),
            _text(output_config.get("effort")),
            _text(thinking_map.get("effort")),
            _legacy_thinking_effort(thinking_map),
        ),
        "temperature": _first(
            _number(kwargs.get("temperature")),
            _number(body.get("temperature")),
        ),
        "top_p": _first(
            _number(kwargs.get("top_p")),
            _number(body.get("top_p")),
        ),
    }


def _usage_fields(kwargs: Mapping[str, Any]) -> dict[str, Any]:
    usage = _mapping(kwargs.get("usage"))
    response = _mapping(kwargs.get("response"))
    response_usage = _mapping(response.get("usage"))
    merged_usage = {**response_usage, **usage}

    duration_ms = _number(kwargs.get("duration_ms"))
    if duration_ms is None:
        api_duration = _number(kwargs.get("api_duration"))
        duration_ms = api_duration * 1000 if api_duration is not None else None

    prompt_tokens = _integer(merged_usage.get("prompt_tokens"))
    return {
        "input_tokens": _integer(merged_usage.get("input_tokens")),
        "output_tokens": _integer(merged_usage.get("output_tokens")),
        "cache_read_tokens": _usage_bucket(
            merged_usage, "cache_read_tokens"
        ),
        "cache_write_tokens": _usage_bucket(
            merged_usage, "cache_write_tokens"
        ),
        "reasoning_tokens": _usage_bucket(
            merged_usage, "reasoning_tokens"
        ),
        "total_tokens": _integer(merged_usage.get("total_tokens")),
        "finish_reason": _first(
            _text(kwargs.get("finish_reason")),
            _text(response.get("finish_reason")),
        ),
        "error_type": _text(kwargs.get("error_type")),
        "first_token_ms": _number(kwargs.get("first_token_ms")),
        "duration_ms": duration_ms,
        "context_window_used": _first(
            _integer(kwargs.get("context_window_used")),
            prompt_tokens,
        ),
    }


def _actual_route_fields(
    kwargs: Mapping[str, Any],
    ctx: _RunContext,
) -> tuple[dict[str, Any], dict[str, Any]]:
    body = _request_body(kwargs)
    response = _mapping(kwargs.get("response"))
    actual_provider = _text(kwargs.get("actual_provider")) or _text(kwargs.get("provider"))
    actual_model = _first(
        _text(kwargs.get("actual_model")),
        _text(kwargs.get("response_model")),
        _text(response.get("model")),
        _text(kwargs.get("model")),
    )
    requested_model = _first(
        _text(kwargs.get("requested_model")),
        ctx.requested_model,
        _text(body.get("model")),
    )
    requested_provider = _first(
        _text(kwargs.get("requested_provider")),
        ctx.requested_provider,
        actual_provider,
    )

    # The hook payload does not expose AIAgent._fallback_chain/_fallback_index.
    # Model-name differences are not a valid proxy (providers often version
    # response.model), so this stays unknown until a real chain position exists.
    fallback_depth = None

    billing_mode = None
    if actual_model is not None or requested_model is not None:
        route = resolve_billing_route(
            actual_model or requested_model or "",
            provider=actual_provider or requested_provider,
            base_url=_text(kwargs.get("base_url")),
        )
        billing_mode = route.billing_mode

    model_source = _text(kwargs.get("model_source"))
    if model_source is None:
        if _text(kwargs.get("response_model")) or _text(response.get("model")):
            model_source = "provider_response"
        elif actual_model is not None:
            model_source = "request"

    call_fields = {
        "provider": actual_provider,
        "model": actual_model,
        "requested_model": requested_model,
        "model_source": model_source,
        **_sampling_fields(kwargs),
    }
    run_fields = {
        "provider": actual_provider,
        "model": actual_model,
        "requested_provider": requested_provider,
        "requested_model": requested_model,
        "model_source": model_source,
        "fallback_depth": fallback_depth,
        "lane": ctx.lane,
        "billing_mode": billing_mode,
        **_sampling_fields(kwargs),
    }
    return call_fields, run_fields


def _fact_source(kwargs: Mapping[str, Any]) -> str:
    if any(
        kwargs.get(key) is not None
        for key in (
            "usage",
            "response",
            "finish_reason",
            "api_duration",
            "duration_ms",
            "response_model",
            "error_type",
        )
    ):
        return "measured"
    if any(kwargs.get(key) is not None for key in ("model", "provider", "request")):
        return "derived"
    return "unknown"


def _record_messages(
    run_id: str,
    call_index: int,
    messages: Any,
) -> None:
    if not isinstance(messages, list):
        return
    for message in messages:
        mapped = _mapping(message)
        if mapped:
            role = _text(mapped.get("role")) or "message"
            record_trace(run_id, call_index, role, mapped)
        else:
            record_trace(run_id, call_index, "message", message)


def _request_messages(kwargs: Mapping[str, Any]) -> Any:
    for candidate in (
        kwargs.get("request_messages"),
        kwargs.get("messages"),
        _request_body(kwargs).get("messages"),
        _request_body(kwargs).get("input"),
    ):
        if isinstance(candidate, list):
            return candidate
    return None


def _assistant_trace(kwargs: Mapping[str, Any]) -> Any:
    assistant = kwargs.get("assistant_message")
    if assistant is not None:
        mapped = _mapping(assistant)
        return mapped or assistant
    response = _mapping(kwargs.get("response"))
    assistant = response.get("assistant_message")
    return assistant


@_fail_soft_hook
def on_pre_llm_call(**kwargs: Any) -> None:
    """Capture legacy request-shaped pre_llm_call events only."""
    if not isinstance(kwargs.get("messages"), list):
        return
    on_pre_llm_request(**kwargs)


@_fail_soft_hook
def on_pre_llm_request(**kwargs: Any) -> None:
    ctx = _resolve_context(kwargs)
    if ctx is None:
        return
    call_index = _call_index(kwargs, ctx)
    call_fields, run_fields = _actual_route_fields(kwargs, ctx)
    run_fields["source"] = _fact_source(kwargs)
    record_llm_call(
        ctx.run_id,
        call_index,
        call_fields,
        run_fields=run_fields,
    )
    _record_messages(ctx.run_id, call_index, _request_messages(kwargs))
    _purge_expired_traces_once()


@_fail_soft_hook
def on_post_llm_call(**kwargs: Any) -> None:
    if not any(
        key in kwargs
        for key in (
            "api_request_id",
            "api_call_count",
            "provider",
            "response",
            "usage",
            "finish_reason",
            "api_duration",
            "duration_ms",
            "response_model",
        )
    ):
        # Current Hermes also emits a turn-finalization post_llm_call. The
        # request-scoped post_api_request already owns the per-call facts;
        # replaying a broad turn event would overwrite its actual route.
        return
    ctx = _resolve_context(kwargs)
    if ctx is None:
        return
    call_index = _call_index(kwargs, ctx)
    call_fields, run_fields = _actual_route_fields(kwargs, ctx)
    usage_fields = _usage_fields(kwargs)
    call_fields.update(usage_fields)
    response = _mapping(kwargs.get("response"))
    call_fields["response_id"] = _first(
        _text(kwargs.get("response_id")),
        _text(response.get("id")),
    )
    run_fields.update(
        {
            key: value
            for key, value in usage_fields.items()
            if key
            in {
                "finish_reason",
                "error_type",
                "first_token_ms",
                "duration_ms",
                "context_window_used",
            }
        }
    )
    run_fields["context_window_limit"] = _integer(
        kwargs.get("context_window_limit")
    )
    run_fields["source"] = _fact_source(kwargs)
    record_llm_call(
        ctx.run_id,
        call_index,
        call_fields,
        run_fields=run_fields,
    )
    assistant = _assistant_trace(kwargs)
    if assistant is not None:
        record_trace(ctx.run_id, call_index, "assistant", assistant)


@_fail_soft_hook
def on_api_request_error(**kwargs: Any) -> None:
    """Capture the already-emitted API error without inventing missing usage."""
    error = _mapping(kwargs.get("error"))
    enriched = dict(kwargs)
    enriched["error_type"] = _text(error.get("type"))
    ctx = _resolve_context(enriched)
    if ctx is None:
        return
    call_index = _call_index(enriched, ctx)
    _, run_fields = _actual_route_fields(enriched, ctx)
    run_fields.update(
        {
            "error_type": _text(error.get("type")),
            "source": _fact_source(enriched),
        }
    )
    upsert_run_facts(ctx.run_id, run_fields)
    record_trace(
        ctx.run_id,
        call_index,
        "api_error",
        {
            "type": _text(error.get("type")),
            "message": error.get("message"),
            "status_code": kwargs.get("status_code"),
            "reason": kwargs.get("reason"),
        },
    )


def _tool_key(
    ctx: _RunContext,
    call_index: int,
    kwargs: Mapping[str, Any],
) -> tuple[str, int, str]:
    identity = _first(
        _text(kwargs.get("tool_call_id")),
        _text(kwargs.get("tool_name")),
        "tool",
    )
    return (ctx.run_id, call_index, str(identity))


@_fail_soft_hook
def on_pre_tool_call(**kwargs: Any) -> None:
    ctx = _resolve_context(kwargs)
    if ctx is None:
        return
    call_index = _call_index(kwargs, ctx)
    key = _tool_key(ctx, call_index, kwargs)
    with _STATE_LOCK:
        _PENDING_TOOLS[key] += 1
    increment_tool_call(
        ctx.run_id,
        call_index,
        run_fields={"lane": ctx.lane, "source": "measured"},
    )
    record_trace(
        ctx.run_id,
        call_index,
        "tool_args",
        {
            "tool_name": _text(kwargs.get("tool_name")),
            "tool_call_id": _text(kwargs.get("tool_call_id")),
            "args": kwargs.get("args"),
        },
    )


@_fail_soft_hook
def on_post_tool_call(**kwargs: Any) -> None:
    ctx = _resolve_context(kwargs)
    if ctx is None:
        return
    call_index = _call_index(kwargs, ctx)
    key = _tool_key(ctx, call_index, kwargs)
    with _STATE_LOCK:
        pending = _PENDING_TOOLS.get(key, 0)
        if pending > 1:
            _PENDING_TOOLS[key] = pending - 1
        elif pending == 1:
            _PENDING_TOOLS.pop(key, None)
    error_type = _text(kwargs.get("error_type"))
    output_chars = (
        _tool_output_chars(kwargs.get("result"))
        if "result" in kwargs
        else None
    )
    if pending == 0:
        increment_tool_call(
            ctx.run_id,
            call_index,
            error_type=error_type,
            tool_output_chars=output_chars,
            run_fields={"lane": ctx.lane, "source": "measured"},
        )
    elif error_type is not None or output_chars is not None:
        record_tool_result(
            ctx.run_id,
            call_index,
            error_type=error_type,
            tool_output_chars=output_chars,
            run_fields={
                "lane": ctx.lane,
                "error_type": error_type,
                "source": "measured",
            },
        )
    record_trace(
        ctx.run_id,
        call_index,
        "tool_result",
        {
            "tool_name": _text(kwargs.get("tool_name")),
            "tool_call_id": _text(kwargs.get("tool_call_id")),
            "status": _text(kwargs.get("status")),
            "error_type": error_type,
            "error_message": kwargs.get("error_message"),
            "result": kwargs.get("result"),
        },
    )


def register(ctx: Any) -> None:
    """Register handlers on Hermes' existing request, error, and tool hooks."""
    ctx.register_hook("pre_api_request", on_pre_llm_request)
    ctx.register_hook("post_api_request", on_post_llm_call)
    ctx.register_hook("api_request_error", on_api_request_error)
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("post_llm_call", on_post_llm_call)
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    ctx.register_hook("post_tool_call", on_post_tool_call)
