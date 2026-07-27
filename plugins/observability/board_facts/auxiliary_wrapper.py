"""Install fail-soft usage wrappers around Hermes auxiliary LLM calls."""

from __future__ import annotations

import hashlib
import inspect
import logging
import sys
import time
import uuid
from dataclasses import dataclass
from functools import wraps
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Optional

from agent.usage_pricing import normalize_usage

logger = logging.getLogger(__name__)

_WRAPPER_MARKER = "_hermes_auxiliary_usage_wrapper"
_ORIGINAL_ATTR = "_hermes_auxiliary_original"
_OBSERVER_ATTR = "_hermes_auxiliary_observer"
_UNKNOWN_AUX_TASK = "unknown"
_MAX_AUX_TASK_LABEL_CHARS = 128


@dataclass(frozen=True)
class _Observer:
    pre: Callable[..., Any]
    post: Callable[..., Any]
    error: Callable[..., Any]


@dataclass(frozen=True)
class AuxiliaryWrapperInstallation:
    """The installed wrappers and the number of stale module aliases rebound."""

    call_llm: Callable[..., Any]
    async_call_llm: Callable[..., Any]
    rebound_references: int


def is_auxiliary_wrapper(function: Any) -> bool:
    """Return whether *function* is one of this module's installed wrappers."""
    return bool(getattr(function, _WRAPPER_MARKER, False))


def _optional_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        text = str(value).strip()
    except Exception:
        return None
    return text or None


def _aux_task_label(value: Any) -> str:
    text = _optional_text(value)
    if text is None:
        return _UNKNOWN_AUX_TASK
    if len(text) <= _MAX_AUX_TASK_LABEL_CHARS:
        return text
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:12]
    prefix_length = _MAX_AUX_TASK_LABEL_CHARS - len(digest) - 1
    return f"{text[:prefix_length]}#{digest}"


def _bound_arguments(
    original: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: Mapping[str, Any],
) -> dict[str, Any]:
    """Resolve the real runtime arguments, including expanded ``**kwargs``."""
    try:
        return dict(inspect.signature(original).bind_partial(*args, **kwargs).arguments)
    except Exception:
        # Instrumentation must not pre-empt the wrapped function's own argument
        # validation. This fallback still covers both supported task forms.
        fallback = dict(kwargs)
        if args:
            fallback.setdefault("task", args[0])
        return fallback


def _base_event(
    original: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: Mapping[str, Any],
) -> dict[str, Any]:
    bound = _bound_arguments(original, args, kwargs)
    aux_task = _aux_task_label(bound.get("task"))
    request_id = uuid.uuid4().hex
    call_kind = (
        "stream_unmeasured"
        if bool(bound.get("stream"))
        else "aux"
    )
    return {
        "api_request_id": request_id,
        "origin": "hermes_aux",
        "call_kind": call_kind,
        "aux_task": aux_task,
        "requested_provider": _optional_text(bound.get("provider")),
        "requested_model": _optional_text(bound.get("model")),
        # Retention policy: persist only bounded routing metadata. Never pass
        # the real messages here; compression prompts contain full transcripts.
        "request_messages": [
            {
                "role": "aux_metadata",
                "content": {
                    "api_request_id": request_id,
                    "aux_task": aux_task,
                },
            }
        ],
    }


def _safe_emit(wrapper: Callable[..., Any], phase: str, event: Mapping[str, Any]) -> None:
    try:
        observer = getattr(wrapper, _OBSERVER_ATTR)
        callback = getattr(observer, phase)
        callback(**dict(event))
    except Exception:
        logger.debug(
            "board_facts auxiliary %s instrumentation failed",
            phase,
            exc_info=True,
        )


def _started_ns() -> Optional[int]:
    try:
        return time.perf_counter_ns()
    except Exception:
        logger.debug("board_facts auxiliary timer start failed", exc_info=True)
        return None


def _duration_ms(started_ns: Optional[int]) -> Optional[float]:
    if started_ns is None:
        return None
    try:
        return max(0.0, (time.perf_counter_ns() - started_ns) / 1_000_000)
    except Exception:
        logger.debug("board_facts auxiliary timer finish failed", exc_info=True)
        return None


def _objectify(value: Any) -> Any:
    if isinstance(value, Mapping):
        return SimpleNamespace(
            **{str(key): _objectify(item) for key, item in value.items()}
        )
    return value


def _has_usage_field(usage: Any, name: str) -> bool:
    if isinstance(usage, Mapping):
        return name in usage and usage.get(name) is not None
    return hasattr(usage, name) and getattr(usage, name, None) is not None


def _normalized_usage(response: Any) -> Optional[dict[str, Any]]:
    raw_usage = getattr(response, "usage", None)
    if raw_usage is None:
        return None
    usage = _objectify(raw_usage)
    if _has_usage_field(raw_usage, "prompt_tokens") or _has_usage_field(
        raw_usage, "completion_tokens"
    ):
        canonical = normalize_usage(usage)
    elif _has_usage_field(raw_usage, "cache_read_input_tokens") or _has_usage_field(
        raw_usage, "cache_creation_input_tokens"
    ):
        canonical = normalize_usage(usage, provider="anthropic")
    else:
        canonical = normalize_usage(usage, api_mode="codex_responses")
    return {
        "input_tokens": canonical.input_tokens,
        "output_tokens": canonical.output_tokens,
        "cache_read_tokens": canonical.cache_read_tokens,
        "cache_write_tokens": canonical.cache_write_tokens,
        "reasoning_tokens": canonical.reasoning_tokens,
        "cache_read_tokens_observed": canonical.cache_read_tokens_observed,
        "cache_write_tokens_observed": canonical.cache_write_tokens_observed,
        "reasoning_tokens_observed": canonical.reasoning_tokens_observed,
        "total_tokens": canonical.total_tokens,
    }


def _finish_reason(response: Any) -> Optional[str]:
    try:
        choices = getattr(response, "choices", None) or ()
        return _optional_text(getattr(choices[0], "finish_reason", None))
    except Exception:
        return None


def _success_event_fields(
    response: Any,
    duration_ms: Optional[float],
) -> dict[str, Any]:
    response_model = _optional_text(getattr(response, "model", None))
    response_id = _optional_text(getattr(response, "id", None))
    response_metadata = {
        key: value
        for key, value in {
            "id": response_id,
            "model": response_model,
        }.items()
        if value is not None
    }
    fields: dict[str, Any] = {
        "response_model": response_model,
        "response": response_metadata,
        "finish_reason": _finish_reason(response),
    }
    if duration_ms is not None:
        fields["duration_ms"] = duration_ms
        fields["wall_ms"] = round(duration_ms)
    usage = _normalized_usage(response)
    if usage is not None:
        fields["usage"] = usage
    return fields


def _emit_success(
    wrapper: Callable[..., Any],
    base_event: Mapping[str, Any],
    response: Any,
    duration_ms: Optional[float],
) -> None:
    try:
        event = {**base_event, **_success_event_fields(response, duration_ms)}
    except Exception:
        logger.debug(
            "board_facts auxiliary response instrumentation failed",
            exc_info=True,
        )
        return
    _safe_emit(wrapper, "post", event)


def _emit_error(
    wrapper: Callable[..., Any],
    base_event: Mapping[str, Any],
    error: BaseException,
    duration_ms: Optional[float],
) -> None:
    error_type = type(error).__name__
    event = {
        **base_event,
        "error_type": error_type,
        # Do not retain exception messages: provider exceptions can echo prompts.
        "error": {"type": error_type},
    }
    if duration_ms is not None:
        event["duration_ms"] = duration_ms
        event["wall_ms"] = round(duration_ms)
    _safe_emit(wrapper, "error", event)


def _make_sync_wrapper(
    original: Callable[..., Any],
    observer: _Observer,
) -> Callable[..., Any]:
    @wraps(original)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            event = _base_event(original, args, kwargs)
        except Exception:
            logger.debug(
                "board_facts auxiliary pre-instrumentation failed",
                exc_info=True,
            )
            event = None
        if event is not None:
            _safe_emit(wrapped, "pre", event)

        started_ns = _started_ns()
        try:
            response = original(*args, **kwargs)
        except BaseException as error:
            if event is not None:
                try:
                    _emit_error(wrapped, event, error, _duration_ms(started_ns))
                except Exception:
                    logger.debug(
                        "board_facts auxiliary error instrumentation failed",
                        exc_info=True,
                    )
            raise

        if event is None:
            return response
        if event["call_kind"] == "stream_unmeasured":
            # Return the exact iterator object without attribute access,
            # iteration, materialization, wrapping, or duration claims.
            _safe_emit(wrapped, "post", event)
            return response
        _emit_success(wrapped, event, response, _duration_ms(started_ns))
        return response

    setattr(wrapped, _WRAPPER_MARKER, True)
    setattr(wrapped, _ORIGINAL_ATTR, original)
    setattr(wrapped, _OBSERVER_ATTR, observer)
    return wrapped


def _make_async_wrapper(
    original: Callable[..., Any],
    observer: _Observer,
) -> Callable[..., Any]:
    @wraps(original)
    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            event = _base_event(original, args, kwargs)
        except Exception:
            logger.debug(
                "board_facts async auxiliary pre-instrumentation failed",
                exc_info=True,
            )
            event = None
        if event is not None:
            _safe_emit(wrapped, "pre", event)

        started_ns = _started_ns()
        try:
            response = await original(*args, **kwargs)
        except BaseException as error:
            if event is not None:
                try:
                    _emit_error(wrapped, event, error, _duration_ms(started_ns))
                except Exception:
                    logger.debug(
                        "board_facts async auxiliary error instrumentation failed",
                        exc_info=True,
                    )
            raise

        if event is not None:
            _emit_success(wrapped, event, response, _duration_ms(started_ns))
        return response

    setattr(wrapped, _WRAPPER_MARKER, True)
    setattr(wrapped, _ORIGINAL_ATTR, original)
    setattr(wrapped, _OBSERVER_ATTR, observer)
    return wrapped


def _wrapper_for(
    current: Callable[..., Any],
    observer: _Observer,
    *,
    asynchronous: bool,
) -> tuple[Callable[..., Any], Callable[..., Any]]:
    if is_auxiliary_wrapper(current):
        setattr(current, _OBSERVER_ATTR, observer)
        return current, getattr(current, _ORIGINAL_ATTR)
    maker = _make_async_wrapper if asynchronous else _make_sync_wrapper
    return maker(current, observer), current


def _sweep_module_references(
    replacements: tuple[
        tuple[Callable[..., Any], Callable[..., Any]],
        ...,
    ],
) -> int:
    rebound = 0
    for module in tuple(sys.modules.values()):
        if module is None:
            continue
        try:
            namespace = vars(module)
        except Exception:
            continue
        for name, value in tuple(namespace.items()):
            for original, wrapper in replacements:
                if value is original:
                    namespace[name] = wrapper
                    rebound += 1
                    break
    return rebound


def install_auxiliary_wrappers(
    *,
    pre_callback: Callable[..., Any],
    post_callback: Callable[..., Any],
    error_callback: Callable[..., Any],
    auxiliary_module: Any = None,
) -> AuxiliaryWrapperInstallation:
    """Patch the aux client and every already-imported direct module alias."""
    if auxiliary_module is None:
        from agent import auxiliary_client as auxiliary_module

    observer = _Observer(
        pre=pre_callback,
        post=post_callback,
        error=error_callback,
    )
    sync_wrapper, sync_original = _wrapper_for(
        auxiliary_module.call_llm,
        observer,
        asynchronous=False,
    )
    async_wrapper, async_original = _wrapper_for(
        auxiliary_module.async_call_llm,
        observer,
        asynchronous=True,
    )
    auxiliary_module.call_llm = sync_wrapper
    auxiliary_module.async_call_llm = async_wrapper
    rebound = _sweep_module_references(
        (
            (sync_original, sync_wrapper),
            (async_original, async_wrapper),
        )
    )
    return AuxiliaryWrapperInstallation(
        call_llm=sync_wrapper,
        async_call_llm=async_wrapper,
        rebound_references=rebound,
    )
