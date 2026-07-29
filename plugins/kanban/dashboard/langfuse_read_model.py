"""Sanitized, fail-soft Langfuse aggregates for the Hermes control surface."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from hermes_cli.urllib_security import open_credentialed_url

CONTRACT_VERSION = "langfuse-hermes-read.v1"
_PAGE_SIZE = 100
_MAX_PAGES = 20
_CACHE_TTL_SECONDS = 45
_MAX_STALE_SECONDS = 15 * 60
_REQUEST_TIMEOUT_SECONDS = 1.5
_FETCH_BUDGET_SECONDS = 4.0


@dataclass
class _CacheEntry:
    payload: dict[str, Any]
    fetched_monotonic: float


@dataclass(frozen=True, slots=True)
class _PageScan:
    rows: list[dict[str, Any]]
    truncated: bool
    reason: str | None
    pages: int


_CACHE_LOCK = threading.Condition(threading.Lock())
_CACHE: dict[tuple[int, str], _CacheEntry] = {}
_IN_FLIGHT: set[tuple[int, str]] = set()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _metadata(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(decoded) if isinstance(decoded, Mapping) else {}
    return {}


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[min(rank, len(ordered) - 1)]


def _metric(
    value: float | int | None,
    *,
    denominator: int,
    observed: int,
    source: str,
    captured_at: str,
) -> dict[str, Any]:
    return {
        "value": value,
        "denominator": denominator,
        "observed": observed,
        "coverage": observed / denominator if denominator else None,
        "source": source,
        "captured_at": captured_at,
    }


def _credentials(env: Mapping[str, str]) -> tuple[str, str] | None:
    host = (
        env.get("HERMES_LANGFUSE_BASE_URL")
        or env.get("HERMES_LANGFUSE_HOST")
        or ""
    ).rstrip("/")
    public_key = env.get("HERMES_LANGFUSE_PUBLIC_KEY", "")
    secret_key = env.get("HERMES_LANGFUSE_SECRET_KEY", "")
    if not host or not public_key or not secret_key:
        return None
    parsed = urllib.parse.urlparse(host)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    authorization = base64.b64encode(
        f"{public_key}:{secret_key}".encode("utf-8")
    ).decode("ascii")
    return host, f"Basic {authorization}"


def _cache_identity(env: Mapping[str, str]) -> str:
    """Scope cached projections to the configured Langfuse project.

    The digest changes on host/key rotation without retaining credentials in a
    cache key or exposing them in the returned payload.
    """
    material = "\0".join(
        (
            env.get("HERMES_LANGFUSE_BASE_URL")
            or env.get("HERMES_LANGFUSE_HOST")
            or "",
            env.get("HERMES_LANGFUSE_PUBLIC_KEY", ""),
            env.get("HERMES_LANGFUSE_SECRET_KEY", ""),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _request_json(
    url: str,
    authorization: str,
    *,
    timeout: float = _REQUEST_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": authorization,
            "User-Agent": "HermesControl/1.0",
        },
        method="GET",
    )
    try:
        with open_credentialed_url(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Langfuse HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise RuntimeError("Langfuse unreachable") from exc
    try:
        decoded = json.loads(raw or b"{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("Langfuse returned invalid JSON") from exc
    if not isinstance(decoded, dict):
        raise RuntimeError("Langfuse returned a non-object payload")
    return decoded


def _page_data(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data", [])
    if isinstance(data, Mapping):
        data = data.get("data", data.get("observations", data.get("traces", [])))
    if not isinstance(data, list):
        raise RuntimeError("Langfuse page has no data list")
    return [dict(row) for row in data if isinstance(row, Mapping)]


def _all_pages(
    host: str,
    authorization: str,
    path: str,
    params: Mapping[str, Any],
    *,
    deadline_monotonic: float,
) -> _PageScan:
    rows: list[dict[str, Any]] = []
    for page in range(1, _MAX_PAGES + 1):
        remaining = deadline_monotonic - time.monotonic()
        if remaining <= 0:
            return _PageScan(rows, True, "deadline", page - 1)
        query = urllib.parse.urlencode(
            {**params, "page": page, "limit": _PAGE_SIZE},
            doseq=True,
        )
        payload = _request_json(
            f"{host}{path}?{query}",
            authorization,
            timeout=min(_REQUEST_TIMEOUT_SECONDS, max(0.05, remaining)),
        )
        page_rows = _page_data(payload)
        rows.extend(page_rows)
        meta = payload.get("meta")
        total_pages = (
            meta.get("totalPages")
            if isinstance(meta, Mapping)
            else None
        )
        if (
            not page_rows
            or len(page_rows) < _PAGE_SIZE
            or (total_pages is not None and page >= int(total_pages))
        ):
            return _PageScan(rows, False, None, page)
    return _PageScan(rows, True, "page_limit", _MAX_PAGES)


def _observation_is_worker(observation: Mapping[str, Any]) -> bool:
    metadata = _metadata(observation.get("metadata"))
    return bool(
        metadata.get("kanban_task_id")
        or metadata.get("task_run_id")
        or metadata.get("kanban_run_id")
        or metadata.get("worker_usage_backfill")
        or (
            metadata.get("task_id")
            and metadata.get("correlation_source")
        )
        or metadata.get("origin") == "hermes_agent"
    )


def _observation_latency_ms(row: Mapping[str, Any]) -> float | None:
    start = _parse_time(row.get("startTime"))
    end = _parse_time(row.get("endTime"))
    if start is not None and end is not None and end >= start:
        return (end - start).total_seconds() * 1000
    raw = _number(row.get("latency"))
    return raw * 1000 if raw is not None else None


def _observation_ttft_ms(row: Mapping[str, Any]) -> float | None:
    start = _parse_time(row.get("startTime"))
    first = _parse_time(row.get("completionStartTime"))
    if start is not None and first is not None and first >= start:
        return (first - start).total_seconds() * 1000
    raw = _number(row.get("timeToFirstToken"))
    return raw * 1000 if raw is not None else None


def _observation_cost(row: Mapping[str, Any]) -> float | None:
    direct = _number(
        row.get("calculatedTotalCost", row.get("totalCost"))
    )
    if direct is not None:
        return direct
    details = row.get("costDetails")
    return _number(details.get("total")) if isinstance(details, Mapping) else None


def _usage_total(row: Mapping[str, Any]) -> float | None:
    details = row.get("usageDetails")
    if not isinstance(details, Mapping):
        return None
    total = _number(details.get("total"))
    if total is not None:
        return total
    values = [
        _number(details.get(name))
        for name in (
            "input",
            "output",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        )
    ]
    known = [value for value in values if value is not None]
    return sum(known) if known else None


def _build_remote_snapshot(
    *,
    days: int,
    env: Mapping[str, str],
) -> dict[str, Any]:
    credentials = _credentials(env)
    now = _utc_now()
    if credentials is None:
        return _absent_payload(
            days=days,
            state="absent",
            reason="credentials_missing",
            generated_at=_iso(now),
        )
    host, authorization = credentials
    since = now - timedelta(days=days)
    scan = _all_pages(
        host,
        authorization,
        "/api/public/observations",
        {"type": "GENERATION", "fromStartTime": _iso(since)},
        deadline_monotonic=time.monotonic() + _FETCH_BUDGET_SECONDS,
    )
    selected = [
        row
        for row in scan.rows
        if _observation_is_worker(row)
    ]
    payload = _aggregate(selected, days=days, generated_at=_iso(now))
    payload["coverage"].update({
        "scanned_generations": len(scan.rows),
        "scan_pages": scan.pages,
        "scan_truncated": scan.truncated,
    })
    if scan.truncated:
        payload["state"] = "partial"
        payload["reason"] = f"scan_{scan.reason}"
    return payload


def _aggregate(
    observations: list[dict[str, Any]],
    *,
    days: int,
    generated_at: str,
) -> dict[str, Any]:
    captured_times = [
        parsed
        for row in observations
        if (parsed := _parse_time(row.get("endTime") or row.get("startTime")))
        is not None
    ]
    captured_at = (
        _iso(max(captured_times)) if captured_times else generated_at
    )
    total = len(observations)
    latency = [
        value
        for row in observations
        if (value := _observation_latency_ms(row)) is not None
    ]
    ttft = [
        value
        for row in observations
        if (value := _observation_ttft_ms(row)) is not None
    ]
    cost = [
        value
        for row in observations
        if (value := _observation_cost(row)) is not None
    ]
    tokens = [
        value
        for row in observations
        if (value := _usage_total(row)) is not None
    ]
    by_model: dict[str, list[dict[str, Any]]] = defaultdict(list)
    unknown_model = 0
    for row in observations:
        model = str(row.get("providedModelName") or row.get("model") or "").strip()
        if not model:
            unknown_model += 1
            model = "nicht_zuordenbar"
        by_model[model].append(row)

    models: list[dict[str, Any]] = []
    for name, rows in by_model.items():
        model_latency = [
            value
            for row in rows
            if (value := _observation_latency_ms(row)) is not None
        ]
        model_ttft = [
            value
            for row in rows
            if (value := _observation_ttft_ms(row)) is not None
        ]
        model_cost = [
            value
            for row in rows
            if (value := _observation_cost(row)) is not None
        ]
        models.append(
            {
                "name": name,
                "calls": len(rows),
                "known_cost_usd": sum(model_cost) if model_cost else None,
                "cost_coverage": len(model_cost) / len(rows) if rows else None,
                "latency_p50_ms": _percentile(model_latency, 0.50),
                "latency_p95_ms": _percentile(model_latency, 0.95),
                "latency_coverage": len(model_latency) / len(rows) if rows else None,
                "ttft_p50_ms": _percentile(model_ttft, 0.50),
                "ttft_p95_ms": _percentile(model_ttft, 0.95),
                "ttft_coverage": len(model_ttft) / len(rows) if rows else None,
            }
        )
    models.sort(key=lambda row: (-int(row["calls"]), str(row["name"])))

    return {
        "contract_version": CONTRACT_VERSION,
        "available": True,
        "state": "fresh",
        "reason": None,
        "window_days": days,
        "generated_at": generated_at,
        "captured_at": captured_at,
        "cache": {"ttl_seconds": _CACHE_TTL_SECONDS, "age_seconds": 0},
        "metrics": {
            "generation_calls": _metric(
                total,
                denominator=total,
                observed=total,
                source="langfuse.generations",
                captured_at=captured_at,
            ),
            "known_cost_usd": _metric(
                sum(cost) if cost else None,
                denominator=total,
                observed=len(cost),
                source="langfuse.generations.cost",
                captured_at=captured_at,
            ),
            "latency_p50_ms": _metric(
                _percentile(latency, 0.50),
                denominator=total,
                observed=len(latency),
                source="langfuse.generations.time",
                captured_at=captured_at,
            ),
            "latency_p95_ms": _metric(
                _percentile(latency, 0.95),
                denominator=total,
                observed=len(latency),
                source="langfuse.generations.time",
                captured_at=captured_at,
            ),
            "ttft_p50_ms": _metric(
                _percentile(ttft, 0.50),
                denominator=total,
                observed=len(ttft),
                source="langfuse.generations.completionStartTime",
                captured_at=captured_at,
            ),
            "ttft_p95_ms": _metric(
                _percentile(ttft, 0.95),
                denominator=total,
                observed=len(ttft),
                source="langfuse.generations.completionStartTime",
                captured_at=captured_at,
            ),
            "tokens_total": _metric(
                sum(tokens) if tokens else None,
                denominator=total,
                observed=len(tokens),
                source="langfuse.generations.usageDetails",
                captured_at=captured_at,
            ),
        },
        "coverage": {
            "worker_traces": total,
            "model_known": total - unknown_model,
            "model_unknown": unknown_model,
            "latency_known": len(latency),
            "ttft_known": len(ttft),
            "cost_known": len(cost),
            "tokens_known": len(tokens),
        },
        "models": models,
    }


def _absent_payload(
    *,
    days: int,
    state: str,
    reason: str,
    generated_at: str,
) -> dict[str, Any]:
    return {
        "contract_version": CONTRACT_VERSION,
        "available": False,
        "state": state,
        "reason": reason,
        "window_days": days,
        "generated_at": generated_at,
        "captured_at": None,
        "cache": {"ttl_seconds": _CACHE_TTL_SECONDS, "age_seconds": None},
        "metrics": {},
        "coverage": {},
        "models": [],
    }


def _with_cache_age(
    payload: Mapping[str, Any],
    *,
    age_seconds: int,
    state: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    result = dict(payload)
    result["cache"] = {
        "ttl_seconds": _CACHE_TTL_SECONDS,
        "age_seconds": max(0, age_seconds),
    }
    if state is not None:
        result["state"] = state
    if reason is not None:
        result["reason"] = reason
    return result


def get_langfuse_snapshot(
    *,
    days: int = 7,
    env: Mapping[str, str] | None = None,
    force_refresh: bool = False,
    allow_refresh: bool = True,
) -> dict[str, Any]:
    """Return one sanitized snapshot with TTL, single-flight and stale fallback."""
    safe_days = max(1, min(30, int(days)))
    selected_env = env if env is not None else os.environ
    cache_key = (safe_days, _cache_identity(selected_env))
    now_mono = time.monotonic()
    with _CACHE_LOCK:
        cached = _CACHE.get(cache_key)
        if cached is not None:
            age = now_mono - cached.fetched_monotonic
            if not force_refresh and age < _CACHE_TTL_SECONDS:
                return _with_cache_age(
                    cached.payload,
                    age_seconds=int(age),
                )
        if not allow_refresh:
            if cached is not None:
                age = now_mono - cached.fetched_monotonic
                return _with_cache_age(
                    cached.payload,
                    age_seconds=int(age),
                    state=(
                        "stale"
                        if age >= _CACHE_TTL_SECONDS
                        and cached.payload.get("available")
                        else None
                    ),
                )
            return _absent_payload(
                days=safe_days,
                state="unknown",
                reason="refresh_disabled",
                generated_at=_iso(_utc_now()),
            )
        if cache_key in _IN_FLIGHT:
            wait_deadline = time.monotonic() + _FETCH_BUDGET_SECONDS + 0.5
            while cache_key in _IN_FLIGHT:
                remaining = wait_deadline - time.monotonic()
                if remaining <= 0:
                    break
                _CACHE_LOCK.wait(timeout=remaining)
            cached = _CACHE.get(cache_key)
            if cached is not None:
                age = time.monotonic() - cached.fetched_monotonic
                return _with_cache_age(
                    cached.payload,
                    age_seconds=int(age),
                    state=(
                        "stale"
                        if age >= _CACHE_TTL_SECONDS
                        and cached.payload.get("available")
                        else None
                    ),
                )
            if cache_key in _IN_FLIGHT:
                return _absent_payload(
                    days=safe_days,
                    state="partial",
                    reason="refresh_in_flight",
                    generated_at=_iso(_utc_now()),
                )
        _IN_FLIGHT.add(cache_key)

    try:
        payload = _build_remote_snapshot(
            days=safe_days,
            env=selected_env,
        )
        # A bounded negative cache prevents every Scorecard/Statistik poll from
        # retrying a stopped or unconfigured Langfuse instance. It expires
        # after the same short TTL and is scoped to the credential identity.
        with _CACHE_LOCK:
            _CACHE[cache_key] = _CacheEntry(payload, time.monotonic())
        return payload
    except Exception as exc:
        with _CACHE_LOCK:
            cached = _CACHE.get(cache_key)
        if cached is not None and cached.payload.get("available"):
            age = time.monotonic() - cached.fetched_monotonic
            if age <= _MAX_STALE_SECONDS:
                return _with_cache_age(
                    cached.payload,
                    age_seconds=int(age),
                    state="stale",
                    reason=f"refresh_failed:{type(exc).__name__}",
                )
        absent = _absent_payload(
            days=safe_days,
            state="absent",
            reason=f"read_failed:{type(exc).__name__}",
            generated_at=_iso(_utc_now()),
        )
        with _CACHE_LOCK:
            _CACHE[cache_key] = _CacheEntry(absent, time.monotonic())
        return absent
    finally:
        with _CACHE_LOCK:
            _IN_FLIGHT.discard(cache_key)
            _CACHE_LOCK.notify_all()


def clear_cache() -> None:
    """Test seam; the dashboard never needs to mutate cache state directly."""
    with _CACHE_LOCK:
        _CACHE.clear()
        _IN_FLIGHT.clear()
        _CACHE_LOCK.notify_all()


__all__ = [
    "CONTRACT_VERSION",
    "clear_cache",
    "get_langfuse_snapshot",
]
