"""Shared nightly budget for every Autoresearch lane.

One focused surface (plan 2026-07-10 ARB-S2) with three jobs:

* **Daily ledger** — an Europe/Berlin day-scoped, atomically persisted JSON
  ledger beside the skill-audit state. Both systemd nightlies (AR3 + V2) share
  it, so together they stay under ``daily_token_limit`` tokens and
  ``daily_model_call_limit`` model calls per day. Every call is recorded with a
  conservative pre-call reservation that is reconciled against provider usage
  after the call; missing usage keeps the estimate (``usage_source:
  "estimated"``) and is never stored as a measured zero.
* **Subscription guard** — quota thresholds against
  ``agent.account_usage.fetch_account_usage("openai-codex")``: weekly >= 50%
  skips expensive models (Luna/Terra/Sol), weekly >= 70% skips all lanes,
  session >= 60% stops further lanes in the same window. An unknown usage API
  fails closed for expensive models and leaves mini lanes bounded by the
  ledger only (``unknown_usage_policy: mini_only``).
* **ROI cooldown** — three consecutive *healthy* zero-yield runs of a lane set
  a reversible seven-day lane cooldown in a state file. Errors, expected
  skips, quota skips and budget skips never count; config.yaml is never
  mutated.

All non-secret operating values come from ``config.yaml`` under
``autoresearch`` (deep-merged defaults in ``hermes_cli/config.py``); no new
behaviour environment variable is introduced. The ledger persists no prompts,
file contents, credentials, account ids or provider headers.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import fcntl
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from zoneinfo import ZoneInfo

_REPO = Path(__file__).resolve().parents[1]
_DEFAULT_AUDIT = _REPO / ".hermes" / "skill-audit"
LEDGER_FILENAME = "autoresearch-budget-ledger.json"
COOLDOWN_FILENAME = "autoresearch-lane-cooldowns.json"
QUOTA_PROVIDER = "openai-codex"

# Marker match on the model name: the expensive GPT-5.6 tiers per the
# 2026-07-10 plan. Mini/others are the bounded cheap path.
EXPENSIVE_MODEL_MARKERS = ("luna", "terra", "sol")

# Kept days of ledger evidence (rollback rule: budget evidence is never deleted
# same-day; old days age out after two weeks).
_LEDGER_KEEP_DAYS = 14

_LEDGER_ENTRY_KEYS = (
    "at", "lane", "model", "estimated_tokens", "usage_source",
    "input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens",
    "total_tokens",
)


class BudgetExhausted(RuntimeError):
    """The shared daily autoresearch budget refuses another model call."""


@dataclass(frozen=True)
class BudgetConfig:
    timezone: str = "Europe/Berlin"
    daily_token_limit: int = 100_000
    daily_model_call_limit: int = 30
    weekly_expensive_skip_percent: float = 50.0
    weekly_all_skip_percent: float = 70.0
    session_stop_percent: float = 60.0
    unknown_usage_policy: str = "mini_only"


@dataclass(frozen=True)
class QuotaDecision:
    allow_expensive: bool
    allow_any: bool
    stop_session: bool
    reason: str
    source: str  # "usage_api" | "unknown"
    session_percent: float | None = None
    weekly_percent: float | None = None


def _coerce_int(value: Any, default: int) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError):
        return default
    return out if out > 0 else default


def _coerce_percent(value: Any, default: float) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if 0 < out <= 100 else default


def load_budget_config(config: Mapping[str, Any] | None = None) -> BudgetConfig:
    """Validated budget values from ``autoresearch.budget`` in config.yaml."""
    if config is None:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly()
    raw = ((config.get("autoresearch") or {}).get("budget") or {})
    if not isinstance(raw, Mapping):
        raw = {}
    defaults = BudgetConfig()
    tz_name = str(raw.get("timezone") or defaults.timezone).strip() or defaults.timezone
    try:
        ZoneInfo(tz_name)
    except Exception:
        tz_name = defaults.timezone
    policy = str(raw.get("unknown_usage_policy") or defaults.unknown_usage_policy).strip()
    if policy not in ("mini_only", "block_all"):
        policy = defaults.unknown_usage_policy
    return BudgetConfig(
        timezone=tz_name,
        daily_token_limit=_coerce_int(raw.get("daily_token_limit"), defaults.daily_token_limit),
        daily_model_call_limit=_coerce_int(
            raw.get("daily_model_call_limit"), defaults.daily_model_call_limit
        ),
        weekly_expensive_skip_percent=_coerce_percent(
            raw.get("weekly_expensive_skip_percent"), defaults.weekly_expensive_skip_percent
        ),
        weekly_all_skip_percent=_coerce_percent(
            raw.get("weekly_all_skip_percent"), defaults.weekly_all_skip_percent
        ),
        session_stop_percent=_coerce_percent(
            raw.get("session_stop_percent"), defaults.session_stop_percent
        ),
        unknown_usage_policy=policy,
    )


def lane_budget_value(config: Mapping[str, Any] | None, lane: str, key: str, default: int) -> int:
    """One validated cap from ``autoresearch.lanes.<lane>.budget.<key>``."""
    try:
        raw = (((config or {}).get("autoresearch") or {}).get("lanes") or {})
        value = ((raw.get(lane) or {}).get("budget") or {}).get(key)
    except AttributeError:
        return default
    return _coerce_int(value, default)


def is_expensive_model(model: str | None) -> bool:
    name = str(model or "").lower()
    return any(marker in name for marker in EXPENSIVE_MODEL_MARKERS)


def _audit_dir() -> Path:
    override = os.environ.get("HERMES_AUTORESEARCH_AUDIT_DIR")
    return Path(override) if override else _DEFAULT_AUDIT


def ledger_path() -> Path:
    return _audit_dir() / LEDGER_FILENAME


def cooldown_path() -> Path:
    return _audit_dir() / COOLDOWN_FILENAME


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    try:
        tmp.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def _read_json(path: Path) -> dict:
    """Tolerant read for OPTIONAL state (cooldowns): garbage degrades to {}."""
    try:
        if not path.exists() or path.stat().st_size == 0:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _read_json_fail_closed(path: Path) -> dict:
    """Strict read for the BUDGET ledger. A missing/empty file is a fresh
    day-zero state; an unreadable, unparsable or wrong-typed file must NOT
    silently reset the budget — that would re-open 30 calls/100k tokens."""
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BudgetExhausted(
            f"autoresearch budget ledger unreadable (fail-closed): {type(exc).__name__}: {exc}"
        ) from exc
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise BudgetExhausted(
            f"autoresearch budget ledger corrupt (fail-closed): {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise BudgetExhausted(
            "autoresearch budget ledger corrupt (fail-closed): top level is not an object"
        )
    return data


def estimate_call_tokens(
    messages: Iterable[Mapping[str, Any]] | None,
    max_tokens: int,
    *,
    tools: Any = None,
) -> int:
    """Conservative reservation: prompt content AND tool schemas at ~3 chars
    per token, a per-message overhead, plus the full output allowance. This is
    a reservation, not a tokenizer — measured provider usage reconciles it
    after the call. Never below 1 so a real call can never reserve zero."""
    chars = 0
    n_messages = 0
    for message in messages or ():
        n_messages += 1
        if not isinstance(message, Mapping):
            chars += len(str(message))
            continue
        content = message.get("content")
        if isinstance(content, str):
            chars += len(content)
        elif isinstance(content, (list, tuple)):
            chars += sum(len(str(part)) for part in content)
        tool_calls = message.get("tool_calls")
        if tool_calls:
            with contextlib.suppress(Exception):
                chars += len(json.dumps(tool_calls, default=str))
    if tools:
        with contextlib.suppress(Exception):
            chars += len(json.dumps(tools, default=str))
    return max(1, chars // 3 + 16 * n_messages + max(0, int(max_tokens or 0)))


def _usage_total(usage: Any) -> int:
    total = getattr(usage, "total_tokens", None)
    if total is None and isinstance(usage, Mapping):
        total = usage.get("total_tokens")
    try:
        return int(total or 0)
    except (TypeError, ValueError):
        return 0


def _usage_field(usage: Any, *names: str) -> int:
    for name in names:
        value = getattr(usage, name, None)
        if value is None and isinstance(usage, Mapping):
            value = usage.get(name)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    return 0


class DailyLedger:
    """Europe/Berlin day-scoped call/token ledger shared by both nightlies."""

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        config: BudgetConfig | None = None,
        clock: Callable[[], _dt.datetime] | None = None,
    ) -> None:
        self.path = Path(path) if path else ledger_path()
        self.config = config or load_budget_config()
        self._clock = clock

    # -- time -----------------------------------------------------------
    def _now(self) -> _dt.datetime:
        if self._clock is not None:
            return self._clock()
        return _dt.datetime.now(ZoneInfo(self.config.timezone))

    def day_key(self) -> str:
        now = self._now()
        if now.tzinfo is not None:
            now = now.astimezone(ZoneInfo(self.config.timezone))
        return now.date().isoformat()

    # -- persistence ----------------------------------------------------
    @contextlib.contextmanager
    def _locked(self):
        """Exclusive inter-process lock spanning read-modify-write, so two
        concurrent lanes cannot both pass the check on the same stale state
        (lost-update / double-spend)."""
        lock_path = self.path.with_name(self.path.name + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "w", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def _load(self) -> dict:
        payload = _read_json_fail_closed(self.path)
        days = payload.get("days")
        if not isinstance(days, dict):
            if payload:
                raise BudgetExhausted(
                    "autoresearch budget ledger corrupt (fail-closed): 'days' is not an object"
                )
            days = {}
        return {"timezone": self.config.timezone, "days": days}

    def _day(self, payload: dict) -> dict:
        day = payload["days"].setdefault(self.day_key(), {})
        day.setdefault("calls", [])
        day.setdefault("quota_snapshots", [])
        return day

    def _save(self, payload: dict) -> None:
        keep = sorted(payload["days"])[-_LEDGER_KEEP_DAYS:]
        payload["days"] = {k: payload["days"][k] for k in keep}
        _atomic_write_json(self.path, payload)

    # -- read side ------------------------------------------------------
    def entries_today(self) -> list[dict]:
        payload = self._load()
        calls = self._day(payload)["calls"]
        return [dict(entry) for entry in calls if isinstance(entry, dict)]

    def calls_today(self) -> int:
        return len(self.entries_today())

    def tokens_today(self) -> int:
        return sum(int(entry.get("total_tokens") or 0) for entry in self.entries_today())

    # -- write side -----------------------------------------------------
    def _check_payload(self, payload: dict, estimated_tokens: int) -> None:
        calls = self._day(payload)["calls"]
        n_calls = len([entry for entry in calls if isinstance(entry, dict)])
        if n_calls + 1 > self.config.daily_model_call_limit:
            raise BudgetExhausted(
                "autoresearch daily budget exhausted: "
                f"{n_calls} model calls today >= limit {self.config.daily_model_call_limit}"
            )
        tokens = sum(int(entry.get("total_tokens") or 0) for entry in calls if isinstance(entry, dict))
        estimate = max(1, int(estimated_tokens or 0))
        if tokens + estimate > self.config.daily_token_limit:
            raise BudgetExhausted(
                "autoresearch daily budget exhausted: "
                f"{tokens} tokens today + ~{estimate} reserved > limit {self.config.daily_token_limit}"
            )

    def check_call(self, estimated_tokens: int) -> None:
        """Read-only pre-check (call 31+ or crossing the daily token limit).
        Writers must use :meth:`reserve_call`, which re-checks under the lock."""
        self._check_payload(self._load(), estimated_tokens)

    @staticmethod
    def _entry_fields(*, estimate: int, usage: Any) -> dict:
        measured_total = _usage_total(usage)
        if usage is not None and measured_total > 0:
            return {
                "usage_source": "measured",
                "input_tokens": _usage_field(usage, "input_tokens", "prompt_tokens"),
                "cached_tokens": _usage_field(usage, "cache_read_tokens", "cached_tokens"),
                "output_tokens": _usage_field(usage, "output_tokens", "completion_tokens"),
                "reasoning_tokens": _usage_field(usage, "reasoning_tokens"),
                "total_tokens": measured_total,
            }
        return {
            "usage_source": "estimated",
            "input_tokens": 0,
            "cached_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "total_tokens": estimate,
        }

    def reserve_call(self, *, lane: str, model: str, estimated_tokens: int) -> dict:
        """Atomically check the daily limits and persist a conservative
        reservation BEFORE the provider is contacted. Check and write happen
        under one exclusive lock, so concurrent lanes cannot double-spend.
        Returns a reservation handle for :meth:`settle_call`."""
        estimate = max(1, int(estimated_tokens or 0))
        with self._locked():
            payload = self._load()
            self._check_payload(payload, estimate)
            entry = self._entry_fields(estimate=estimate, usage=None)
            entry.update({
                "at": self._now().isoformat(timespec="seconds"),
                "lane": str(lane),
                "model": str(model or ""),
                "estimated_tokens": estimate,
            })
            assert set(entry) <= set(_LEDGER_ENTRY_KEYS)
            day = self.day_key()
            calls = self._day(payload)["calls"]
            calls.append(entry)
            index = len(calls) - 1
            self._save(payload)
        return {"day": day, "index": index, "lane": str(lane), "estimate": estimate}

    def settle_call(self, reservation: Mapping[str, Any], *, usage: Any = None,
                    model: str | None = None) -> dict:
        """Reconcile a reservation with the provider's measured usage. Missing
        or zero usage keeps the conservative estimate (``estimated``)."""
        estimate = max(1, int(reservation.get("estimate") or 0))
        with self._locked():
            payload = self._load()
            days = payload["days"]
            day = str(reservation.get("day") or self.day_key())
            calls = (days.get(day) or {}).get("calls") or []
            index = int(reservation.get("index", -1))
            if not (0 <= index < len(calls)) or calls[index].get("lane") != reservation.get("lane"):
                # Reservation aged out (e.g. day pruned) — keep the evidence
                # by appending instead of silently dropping it.
                entry = self._entry_fields(estimate=estimate, usage=usage)
                entry.update({
                    "at": self._now().isoformat(timespec="seconds"),
                    "lane": str(reservation.get("lane") or ""),
                    "model": str(model or ""),
                    "estimated_tokens": estimate,
                })
                self._day(payload)["calls"].append(entry)
                self._save(payload)
                return entry
            entry = calls[index]
            entry.update(self._entry_fields(estimate=estimate, usage=usage))
            if model:
                entry["model"] = str(model)
            self._save(payload)
            return dict(entry)

    def record_call(
        self,
        *,
        lane: str,
        model: str,
        estimated_tokens: int,
        usage: Any = None,
    ) -> dict:
        """Persist one completed model call in a single atomic step (used when
        reserve/settle are not split around the provider call)."""
        reservation = self.reserve_call(lane=lane, model=model, estimated_tokens=estimated_tokens)
        return self.settle_call(reservation, usage=usage, model=model)

    def record_quota_snapshot(
        self,
        phase: str,
        *,
        session_percent: float | None,
        weekly_percent: float | None,
    ) -> None:
        with self._locked():
            payload = self._load()
            self._day(payload)["quota_snapshots"].append({
                "at": self._now().isoformat(timespec="seconds"),
                "phase": str(phase),
                "session_percent": session_percent,
                "weekly_percent": weekly_percent,
            })
            self._save(payload)


def run_usage_summary(entries: Iterable[Mapping[str, Any]]) -> dict:
    """Aggregate ledger entries of one run. Zero calls stay a *real* zero
    (measured); any estimated/unknown entry taints the aggregate honestly."""
    total = 0
    sources: set[str] = set()
    for entry in entries:
        total += int(entry.get("total_tokens") or 0)
        sources.add(str(entry.get("usage_source") or "unknown"))
    if not sources or sources == {"measured"}:
        source = "measured"
    elif "unknown" in sources:
        source = "unknown"
    else:
        source = "estimated"
    return {"tokens": total, "usage_source": source}


# ---------------------------------------------------------------------------
# Subscription guard
# ---------------------------------------------------------------------------
def fetch_quota_snapshot(provider: str = QUOTA_PROVIDER):
    """Best-effort account usage snapshot; ``None`` means unknown → fail
    closed for expensive models."""
    try:
        from agent.account_usage import fetch_account_usage

        return fetch_account_usage(provider)
    except Exception:
        return None


def _window_percent(snapshot: Any, key: str) -> float | None:
    for window in getattr(snapshot, "windows", ()) or ():
        if getattr(window, "window_key", None) == key:
            value = getattr(window, "used_percent", None)
            return float(value) if value is not None else None
    return None


def evaluate_quota(snapshot: Any, config: BudgetConfig) -> QuotaDecision:
    """Map an account usage snapshot onto the plan's skip thresholds."""
    session = _window_percent(snapshot, "session") if snapshot is not None else None
    weekly = _window_percent(snapshot, "weekly") if snapshot is not None else None
    available = bool(snapshot is not None and getattr(snapshot, "available", False))
    if not available or weekly is None:
        allow_any = config.unknown_usage_policy == "mini_only"
        return QuotaDecision(
            allow_expensive=False,
            allow_any=allow_any,
            stop_session=False,
            reason="usage API unknown: expensive models fail closed, mini bounded by daily ledger",
            source="unknown",
            session_percent=session,
            weekly_percent=weekly,
        )
    allow_any = weekly < config.weekly_all_skip_percent
    allow_expensive = allow_any and weekly < config.weekly_expensive_skip_percent
    stop_session = session is not None and session >= config.session_stop_percent
    if not allow_any:
        reason = f"weekly usage {weekly:.0f}% >= {config.weekly_all_skip_percent:.0f}%"
    elif stop_session:
        reason = f"session usage {session:.0f}% >= {config.session_stop_percent:.0f}%"
    elif not allow_expensive:
        reason = f"weekly usage {weekly:.0f}% >= {config.weekly_expensive_skip_percent:.0f}%"
    else:
        reason = ""
    return QuotaDecision(
        allow_expensive=allow_expensive,
        allow_any=allow_any,
        stop_session=stop_session,
        reason=reason,
        source="usage_api",
        session_percent=session,
        weekly_percent=weekly,
    )


def quota_block_reason(decision: QuotaDecision, model: str | None) -> str | None:
    """``None`` when the lane may run this model; otherwise the expected-skip
    reason (classified as ``quota_skipped``, never an infra failure)."""
    if not decision.allow_any:
        return f"quota skip: {decision.reason}"
    if decision.stop_session:
        return f"quota skip: {decision.reason}"
    if not decision.allow_expensive:
        if not str(model or "").strip():
            # Fail-closed: while expensive models are blocked, a lane whose
            # effective model cannot be resolved must not slip through.
            return "quota skip: effective lane model unknown (fail-closed while expensive models are blocked)"
        if is_expensive_model(model):
            base = decision.reason or "expensive models blocked"
            return f"quota skip: {base} blocks expensive model {model}"
    return None


# ---------------------------------------------------------------------------
# Ledger-enforced model call (shared by all four lanes)
# ---------------------------------------------------------------------------
def guarded_llm_call(
    *,
    lane: str,
    call: Callable[..., Any],
    messages: list,
    max_tokens: int,
    ledger: DailyLedger | None = None,
    **kwargs: Any,
) -> tuple[Any, dict]:
    """Atomically reserve → call → reconcile. Raises :class:`BudgetExhausted`
    before the provider is contacted when the shared daily ledger is spent.
    The reservation is persisted BEFORE the call and stands (as ``estimated``)
    even when the call itself crashes — provider-side spend is never lost."""
    led = ledger or DailyLedger()
    estimate = estimate_call_tokens(messages, max_tokens, tools=kwargs.get("tools"))
    reservation = led.reserve_call(
        lane=lane, model=str(kwargs.get("model") or ""), estimated_tokens=estimate
    )
    try:
        resp = call(messages=messages, max_tokens=max_tokens, **kwargs)
    except Exception:
        with contextlib.suppress(Exception):
            led.settle_call(reservation, usage=None)
        raise
    raw_usage = getattr(resp, "usage", None)
    if raw_usage is None and isinstance(resp, Mapping):
        raw_usage = resp.get("usage")
    usage = None
    if raw_usage is not None:
        try:
            from agent.usage_pricing import normalize_usage

            canonical = normalize_usage(raw_usage)
            usage = canonical if canonical.total_tokens > 0 else None
        except Exception:
            usage = None
        if usage is None and _usage_total(raw_usage) > 0:
            # Provider shapes the canonical extractor does not know still
            # count as measured — read the fields directly.
            usage = raw_usage
    model = str(getattr(resp, "model", "") or "")
    if not model and isinstance(resp, Mapping):
        model = str(resp.get("model") or "")
    entry = led.settle_call(reservation, usage=usage, model=model or None)
    return resp, entry


# ---------------------------------------------------------------------------
# ROI cooldown (reversible, state-only)
# ---------------------------------------------------------------------------
def _roi_settings(config: Mapping[str, Any] | None = None) -> tuple[int, int]:
    if config is None:
        try:
            from hermes_cli.config import load_config_readonly

            config = load_config_readonly()
        except Exception:
            config = {}
    raw = ((config.get("autoresearch") or {}).get("roi") or {})
    if not isinstance(raw, Mapping):
        raw = {}
    runs = _coerce_int(raw.get("zero_yield_runs_before_cooldown"), 3)
    days = _coerce_int(raw.get("cooldown_days"), 7)
    return runs, days


def record_lane_run_for_cooldown(
    lane: str,
    *,
    outcome: str,
    yielded: int,
    healthy_calls: int,
    config: Mapping[str, Any] | None = None,
    path: Path | str | None = None,
    clock: Callable[[], _dt.datetime] | None = None,
) -> dict:
    """Track healthy zero-yield streaks per lane.

    Counts only a *healthy* zero-yield run: outcome ``clean`` with at least
    one healthy model call and no yield. Yield resets the streak. Errors,
    expected skips, quota skips and budget skips leave the streak untouched.
    On the configured streak a reversible ``cooldown_until`` (+7 days) is set.
    """
    streak_limit, cooldown_days = _roi_settings(config)
    state_path = Path(path) if path else cooldown_path()
    now = clock() if clock is not None else _dt.datetime.now(_dt.timezone.utc)
    state = _read_json(state_path)
    lanes = state.setdefault("lanes", {})
    entry = lanes.setdefault(str(lane), {"zero_yield_streak": 0, "cooldown_until": None})

    outcome_name = str(outcome or "").strip()
    if outcome_name == "yielded" or int(yielded or 0) > 0:
        entry["zero_yield_streak"] = 0
    elif outcome_name == "clean" and int(healthy_calls or 0) > 0:
        entry["zero_yield_streak"] = int(entry.get("zero_yield_streak") or 0) + 1
        if entry["zero_yield_streak"] >= streak_limit:
            until = now + _dt.timedelta(days=cooldown_days)
            entry["cooldown_until"] = until.isoformat(timespec="seconds")
            entry["zero_yield_streak"] = 0
    # every other outcome (errors, expected/quota/budget skips, clean without
    # healthy calls) neither counts nor resets
    entry["last_outcome"] = outcome_name
    entry["updated_at"] = now.isoformat(timespec="seconds")
    _atomic_write_json(state_path, state)
    return dict(entry)


def lane_cooldown_until(
    lane: str,
    *,
    path: Path | str | None = None,
    clock: Callable[[], _dt.datetime] | None = None,
) -> str | None:
    """ISO timestamp while the lane's reversible cooldown is active, else None."""
    state_path = Path(path) if path else cooldown_path()
    now = clock() if clock is not None else _dt.datetime.now(_dt.timezone.utc)
    entry = ((_read_json(state_path).get("lanes") or {}).get(str(lane)) or {})
    raw = entry.get("cooldown_until")
    if not raw:
        return None
    try:
        until = _dt.datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    if until.tzinfo is None:
        until = until.replace(tzinfo=_dt.timezone.utc)
    reference = now if now.tzinfo is not None else now.replace(tzinfo=_dt.timezone.utc)
    return str(raw) if reference < until else None
