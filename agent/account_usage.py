from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

import httpx

from agent.anthropic_adapter import _is_oauth_token, resolve_anthropic_token
from hermes_cli.auth import AuthError, _read_codex_tokens, resolve_codex_runtime_credentials
from hermes_cli.runtime_provider import resolve_runtime_provider

if TYPE_CHECKING:
    from typing import TypeGuard

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class AccountUsageWindow:
    label: str
    used_percent: Optional[float] = None
    reset_at: Optional[datetime] = None
    detail: Optional[str] = None
    # Stable, language-independent classifier so the frontend never has to guess
    # the window kind from (English) label strings: "session" | "weekly" |
    # "opus_week" | "sonnet_week" | other. None for legacy/unknown windows.
    window_key: Optional[str] = None


@dataclass(frozen=True)
class AccountUsageSnapshot:
    provider: str
    source: str
    fetched_at: datetime
    # When the underlying data point was produced (e.g. Grok log line ts).
    # None ⇒ consumers treat fetched_at as the signal time.
    signal_at: Optional[datetime] = None
    title: str = "Account limits"
    plan: Optional[str] = None
    windows: tuple[AccountUsageWindow, ...] = ()
    details: tuple[str, ...] = ()
    unavailable_reason: Optional[str] = None

    @property
    def available(self) -> bool:
        return bool(self.windows or self.details) and not self.unavailable_reason


def _title_case_slug(value: Optional[str]) -> Optional[str]:
    cleaned = str(value or "").strip()
    if not cleaned:
        return None
    return cleaned.replace("_", " ").replace("-", " ").title()


def _parse_dt(value: Any) -> Optional[datetime]:
    if value in {None, ""}:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _format_reset(dt: Optional[datetime]) -> str:
    if not dt:
        return "unknown"
    local_dt = dt.astimezone()
    delta = dt - _utc_now()
    total_seconds = int(delta.total_seconds())
    if total_seconds <= 0:
        return f"now ({local_dt.strftime('%Y-%m-%d %H:%M %Z')})"
    hours, rem = divmod(total_seconds, 3600)
    minutes = rem // 60
    if hours >= 24:
        days, hours = divmod(hours, 24)
        rel = f"in {days}d {hours}h"
    elif hours > 0:
        rel = f"in {hours}h {minutes}m"
    else:
        rel = f"in {minutes}m"
    return f"{rel} ({local_dt.strftime('%Y-%m-%d %H:%M %Z')})"


def render_account_usage_lines(snapshot: Optional[AccountUsageSnapshot], *, markdown: bool = False) -> list[str]:
    if not snapshot:
        return []
    header = f"📈 {'**' if markdown else ''}{snapshot.title}{'**' if markdown else ''}"
    lines = [header]
    if snapshot.plan:
        lines.append(f"Provider: {snapshot.provider} ({snapshot.plan})")
    else:
        lines.append(f"Provider: {snapshot.provider}")
    for window in snapshot.windows:
        if window.used_percent is None:
            base = f"{window.label}: unavailable"
        else:
            remaining = max(0, round(100 - float(window.used_percent)))
            used = max(0, round(float(window.used_percent)))
            base = f"{window.label}: {remaining}% remaining ({used}% used)"
        if window.reset_at:
            base += f" • resets {_format_reset(window.reset_at)}"
        if window.detail:
            base += f" • {window.detail}"
        lines.append(base)
    for detail in snapshot.details:
        lines.append(detail)
    if snapshot.unavailable_reason:
        lines.append(f"Unavailable: {snapshot.unavailable_reason}")
    return lines


def _fmt_usd(d: float) -> str:
    return f"${d:,.2f}"


def _is_finite_num(v: Any) -> TypeGuard[float]:
    """True iff v is a real numeric value (int or float, not bool, not NaN/Inf).

    Typed as a ``TypeGuard[float]`` so the type checker narrows ``v`` to a real
    number in the positive branch — callers can then do arithmetic / pass it to
    ``_fmt_usd`` without a None-operand warning.
    """
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def build_nous_credits_snapshot(account_info) -> Optional[AccountUsageSnapshot]:
    """Map a NousPortalAccountInfo into an AccountUsageSnapshot for /usage.

    Shows dollar magnitudes (subscription / top-up / total) + renewal date + a
    portal CTA. When the portal supplies a subscription denominator
    (``monthly_credits``), also emits a subscription-usage window so the renderer
    shows a real ``% used`` gauge; when it's absent (older portals) the view
    gracefully degrades to magnitudes-only. Returns None when there's no usable
    account info to show (fail-open: caller just shows nothing).
    """
    try:
        from hermes_cli.nous_account import nous_portal_topup_url

        if account_info is None or not getattr(account_info, "logged_in", False):
            return None

        access = getattr(account_info, "paid_service_access_info", None)
        sub = getattr(account_info, "subscription", None)

        windows: list[AccountUsageWindow] = []
        details: list[str] = []

        # Subscription usage gauge — only when the portal supplies a positive
        # monthly_credits denominator AND a finite remaining balance that does
        # not exceed the cap. Money math is on float dollars (allowed: numeric
        # account fields, NOT a server-provided *_usd string). used = cap -
        # remaining; clamp [0,100] so a debt balance (remaining < 0) reads 100%.
        # Excluded on purpose:
        #   - non-finite values (NaN/Infinity slip past isinstance and json.loads
        #     parses bare NaN/Infinity by default) → would render "$nan"/"$inf"
        #     and a falsely-confident gauge;
        #   - remaining > cap (rollover balance spanning the period) → monthly_credits
        #     is no longer a meaningful denominator, and "$X of $Y left" with X>Y
        #     reads as a contradiction. Both fall back to the magnitudes lines.
        if sub is not None:
            monthly_credits = getattr(sub, "monthly_credits", None)
            sub_remaining = getattr(sub, "credits_remaining", None)
            if (
                _is_finite_num(monthly_credits)
                and monthly_credits > 0
                and _is_finite_num(sub_remaining)
                and sub_remaining <= monthly_credits
            ):
                used = monthly_credits - sub_remaining
                used_pct = max(0.0, min(100.0, used / monthly_credits * 100.0))
                windows.append(
                    AccountUsageWindow(
                        label="Subscription",
                        used_percent=used_pct,
                        detail=f"{_fmt_usd(sub_remaining)} of {_fmt_usd(monthly_credits)} left",
                    )
                )

        if access is not None:
            sub_credits = getattr(access, "subscription_credits_remaining", None)
            if _is_finite_num(sub_credits):
                details.append(f"Subscription credits: {_fmt_usd(sub_credits)}")
            purchased = getattr(access, "purchased_credits_remaining", None)
            if _is_finite_num(purchased):
                details.append(f"Top-up credits: {_fmt_usd(purchased)}")
            total_usable = getattr(access, "total_usable_credits", None)
            if _is_finite_num(total_usable):
                details.append(f"Total usable: {_fmt_usd(total_usable)}")

        if sub is not None:
            rollover = getattr(sub, "rollover_credits", None)
            if _is_finite_num(rollover) and rollover > 0:
                details.append(f"Rollover: {_fmt_usd(rollover)}")
            period_end = getattr(sub, "current_period_end", None)
            if period_end:
                details.append(f"Renews: {period_end}")

        paid = getattr(account_info, "paid_service_access", None)
        if paid is False:
            details.append("Status: access depleted — top up to restore")

        if not windows and not details:
            return None

        details.append(f"Top up: {nous_portal_topup_url(account_info)}")
        details.append("(or run /credits)")

        plan = getattr(sub, "plan", None) if sub is not None else None
        return AccountUsageSnapshot(
            provider="nous",
            source="portal-account",
            fetched_at=_utc_now(),
            title="Nous credits",
            plan=plan,
            windows=tuple(windows),
            details=tuple(details),
        )
    except (AttributeError, TypeError):
        return None


def nous_credits_lines(*, markdown: bool = False, timeout: float = 10.0) -> list[str]:
    """Return rendered Nous-credits /usage lines, or [] when there's nothing to show.

    Account-independent of any live agent: gated on "a Nous account is logged in"
    (a cheap local auth-state check), then a wall-clock-bounded portal fetch. Shared
    by the CLI ``_show_usage`` and the TUI ``session.usage`` RPC so both surfaces show
    the same block regardless of session API-call count or resume state. Fail-open:
    any auth/portal hiccup or timeout returns [] (the caller shows nothing).

    Dev override: when HERMES_DEV_CREDITS_FIXTURE selects a fixture state, /usage
    renders from that fixture instead of the real portal (so the block + gauge are
    testable without a live account). Throwaway scaffolding.
    """
    # Dev fixture short-circuit — render /usage from the injected state, no portal.
    try:
        from agent.credits_tracker import dev_fixture_credits_state

        fixture = dev_fixture_credits_state()
    except Exception:
        fixture = None
    if fixture is not None:
        snapshot = _snapshot_from_credits_state(fixture)
        return render_account_usage_lines(snapshot, markdown=markdown)

    try:
        from hermes_cli.auth import get_provider_auth_state

        tok = (get_provider_auth_state("nous") or {}).get("access_token")
        if not (isinstance(tok, str) and tok.strip()):
            return []
    except Exception:
        return []
    try:
        import concurrent.futures

        from hermes_cli.nous_account import get_nous_portal_account_info

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            account = pool.submit(
                get_nous_portal_account_info, force_fresh=True
            ).result(timeout=timeout)
        snapshot = build_nous_credits_snapshot(account)
        return render_account_usage_lines(snapshot, markdown=markdown)
    except Exception:
        # Fail-open (caller shows nothing), but leave a breadcrumb so a dead
        # /usage credits block is diagnosable in agent.log without a dev flag.
        logger.debug("credits ▸ /usage portal fetch/render failed (fail-open)", exc_info=True)
        return []


def _snapshot_from_credits_state(state) -> Optional[AccountUsageSnapshot]:
    """Map a header-shaped CreditsState (e.g. a dev fixture) to the /usage snapshot.

    Renders the same magnitudes + monthly-grant % window the portal path produces,
    so HERMES_DEV_CREDITS_FIXTURE can exercise /usage without a live account. The
    *_usd strings are mock display values here (not server balance to compute on);
    the % comes from CreditsState.used_fraction (micros math). Fail-open → None.
    """
    try:
        if state is None:
            return None

        windows: list[AccountUsageWindow] = []
        details: list[str] = []

        uf = getattr(state, "used_fraction", None)
        if isinstance(uf, (int, float)) and math.isfinite(uf):
            cap_usd = getattr(state, "subscription_limit_usd", None)
            sub_usd = getattr(state, "subscription_usd", None)
            detail = None
            if sub_usd and cap_usd:
                detail = f"${sub_usd} of ${cap_usd} left"
            windows.append(
                AccountUsageWindow(
                    label="Subscription",
                    used_percent=max(0.0, min(100.0, uf * 100.0)),
                    detail=detail,
                )
            )

        sub_usd = getattr(state, "subscription_usd", None)
        if sub_usd:
            details.append(f"Subscription credits: ${sub_usd}")
        purchased_usd = getattr(state, "purchased_usd", None)
        if purchased_usd:
            details.append(f"Top-up credits: ${purchased_usd}")
        remaining_usd = getattr(state, "remaining_usd", None)
        if remaining_usd:
            details.append(f"Total usable: ${remaining_usd}")
        if getattr(state, "paid_access", True) is False:
            details.append("Status: access depleted — top up to restore")

        if not windows and not details:
            return None

        details.append("(dev fixture — HERMES_DEV_CREDITS_FIXTURE)")
        return AccountUsageSnapshot(
            provider="nous",
            source="dev-fixture",
            fetched_at=_utc_now(),
            title="Nous credits",
            windows=tuple(windows),
            details=tuple(details),
        )
    except (AttributeError, TypeError):
        return None


@dataclass(frozen=True)
class CreditsView:
    """Surface-agnostic data for the ``/credits`` command.

    One portal fetch, one parse — consumed identically by the CLI panel, the
    gateway button, and any other money surface. Fail-open: when not logged in
    or the portal is unreachable, ``logged_in`` is False / ``topup_url`` is None
    and callers degrade gracefully.
    """

    logged_in: bool
    balance_lines: tuple[str, ...] = ()
    identity_line: Optional[str] = None
    topup_url: Optional[str] = None
    depleted: bool = False


def build_credits_view(*, markdown: bool = False, timeout: float = 10.0) -> CreditsView:
    """Build the /credits view: balance block + identity line + top-up URL.

    Reuses the same account fetch + snapshot + URL builder as the /usage credits
    block, so the numbers always match. The balance block is the rendered
    snapshot MINUS its trailing top-up/command-hint lines (the /credits surface
    supplies its own affordance). Fail-open → ``CreditsView(logged_in=False)``.
    """
    not_logged_in = CreditsView(logged_in=False)
    try:
        from hermes_cli.auth import get_provider_auth_state

        tok = (get_provider_auth_state("nous") or {}).get("access_token")
        if not (isinstance(tok, str) and tok.strip()):
            return not_logged_in
    except Exception:
        return not_logged_in

    try:
        import concurrent.futures

        from hermes_cli.nous_account import (
            get_nous_portal_account_info,
            nous_portal_topup_url,
        )

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            account = pool.submit(get_nous_portal_account_info, force_fresh=True).result(
                timeout=timeout
            )
    except Exception:
        logger.debug("credits ▸ /credits portal fetch failed (fail-open)", exc_info=True)
        return not_logged_in

    if account is None or not getattr(account, "logged_in", False):
        return not_logged_in

    snapshot = build_nous_credits_snapshot(account)
    # Balance lines = the snapshot block minus the two trailing affordance lines
    # ("Top up: <url>" + "(or run /credits)") that build_nous_credits_snapshot
    # appends for the /usage surface. /credits renders its own button/panel.
    balance_lines: list[str] = []
    if snapshot is not None:
        rendered = render_account_usage_lines(snapshot, markdown=markdown)
        balance_lines = [
            line
            for line in rendered
            if not line.lstrip().startswith("Top up:")
            and not line.lstrip().startswith("(or run")
        ]

    # Identity line — shown before any open (roadmap §4.4).
    email = getattr(account, "email", None)
    org_name = getattr(account, "org_name", None)
    who: list[str] = []
    if email:
        who.append(str(email))
    if org_name:
        who.append(f"org {org_name}")
    identity_line = ("Topping up as " + " / ".join(who)) if who else None

    return CreditsView(
        logged_in=True,
        balance_lines=tuple(balance_lines),
        identity_line=identity_line,
        topup_url=nous_portal_topup_url(account),
        depleted=getattr(account, "paid_service_access", None) is False,
    )


def _codex_backend_urls(base_url: str) -> tuple[str, str, str]:
    """Resolve the Codex backend endpoints (usage, reset-credits list, consume).

    Mirrors the Codex CLI's PathStyle split (codex-rs backend-client): base URLs
    containing ``/backend-api`` use the ChatGPT ``/wham/...`` paths; everything
    else uses ``/api/codex/...``.
    """
    normalized = (base_url or "").strip().rstrip("/")
    if not normalized:
        normalized = "https://chatgpt.com/backend-api/codex"
    if normalized.endswith("/codex"):
        normalized = normalized[: -len("/codex")]
    prefix = normalized + ("/wham" if "/backend-api" in normalized else "/api/codex")
    return (
        prefix + "/usage",
        prefix + "/rate-limit-reset-credits",
        prefix + "/rate-limit-reset-credits/consume",
    )


def _resolve_codex_usage_url(base_url: str) -> str:
    return _codex_backend_urls(base_url)[0]


def _resolve_codex_usage_credentials(
    base_url: Optional[str],
    api_key: Optional[str],
) -> tuple[str, str, Optional[str]]:
    """Resolve Codex quota credentials from the native runtime path.

    Prefer explicit live-agent credentials, then the legacy singleton OAuth
    state, then the credential pool.  Hermes's native OAuth setup now stores
    device-code logins in the pool, so quota diagnostics must not depend only
    on the older singleton store.
    """
    explicit_key = str(api_key or "").strip()
    if explicit_key:
        return explicit_key, str(base_url or "").strip(), None

    # Tier 2: the native runtime resolver. It ALREADY falls back to the
    # credential pool when the singleton is empty (see
    # ``resolve_codex_runtime_credentials`` — issue #32992), so in a pool-only
    # setup this returns a usable ``source="credential_pool"`` token.
    #
    # Only ``AuthError`` ("no creds" / rate-limited) is caught so tier 3 can
    # run: a broad ``except Exception`` would (a) mask a transient refresh /
    # network failure and silently hand back a DIFFERENT pool account's usage,
    # and (b) hide genuine programming errors. A refresh/network error must
    # propagate — the outer ``fetch_account_usage`` guard fails open (shows
    # nothing this turn) rather than reporting the wrong account.
    #
    # The ``account_id`` (for the ``ChatGPT-Account-Id`` header) is read
    # best-effort: a partial/missing singleton token store must not sink an
    # otherwise-usable resolver credential and force a header-less pool fallback.
    try:
        creds = resolve_codex_runtime_credentials(refresh_if_expiring=True)
        account_id: Optional[str] = None
        try:
            token_data = _read_codex_tokens()
            tokens = token_data.get("tokens") or {}
            account_id = str(tokens.get("account_id", "") or "").strip() or None
        except AuthError:
            # Pool-only creds carry no singleton account_id; header is optional.
            logger.debug("codex ▸ /usage account_id read failed (best-effort)", exc_info=True)
        return creds["api_key"], str(creds.get("base_url", "") or "").strip(), account_id
    except AuthError:
        logger.debug("codex ▸ /usage runtime resolver returned no creds; trying pool", exc_info=True)

    # Tier 3: direct pool select. Reached only when the resolver itself raises
    # AuthError (e.g. singleton missing AND its own pool read found nothing at
    # resolve time, but a pool entry is usable now). Pool credentials have no
    # account_id concept, so the ChatGPT-Account-Id header is intentionally
    # omitted here.
    from agent.credential_pool import load_pool

    pool = load_pool("openai-codex")
    entry = pool.select()
    if entry is None:
        raise RuntimeError("No available openai-codex credential in credential pool")
    return entry.runtime_api_key, str(entry.runtime_base_url or base_url or "").strip(), None


def _codex_window_kind(window: dict, fallback_label: str, fallback_key: str) -> tuple[str, str]:
    """Classify a Codex rate-limit window by its real length as (label, window_key).

    OpenAI historically shipped a 5-hour primary window, but Pro plans now report a
    single rolling 7-day window as ``primary_window`` (``limit_window_seconds``
    604800). Classify only well-evidenced duration bands — up to a day is a session
    window (5 h = 18000 s), multiple days is a weekly window (7 d = 604800 s) — and
    fall back to the legacy position-based default for a missing, boolean, or
    ambiguous in-between length, so an unknown window is never mislabelled.
    """
    seconds = window.get("limit_window_seconds")
    if isinstance(seconds, (int, float)) and not isinstance(seconds, bool):
        if seconds <= 86400:  # ≤ 24 h → session-class
            return ("Session", "session")
        if seconds >= 2 * 86400:  # ≥ 2 days → weekly-class
            return ("Weekly", "weekly")
    return (fallback_label, fallback_key)


def _fetch_codex_account_usage(
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Optional[AccountUsageSnapshot]:
    """Fetch Codex/ChatGPT quota. Fail-soft with specific reasons (Kimi pattern).

    Credential resolution: AuthError / empty token → unavailable snapshot.
    Transient non-AuthError failures (e.g. refresh 503) still propagate so the
    outer ``fetch_account_usage`` guard returns None rather than a wrong-account
    pool snapshot — see ``_resolve_codex_usage_credentials``.
    """
    try:
        token, resolved_base_url, account_id = _resolve_codex_usage_credentials(
            base_url, api_key
        )
    except AuthError:
        return AccountUsageSnapshot(
            provider="openai-codex",
            source="usage_api",
            fetched_at=_utc_now(),
            unavailable_reason="Codex credentials not available.",
        )
    if not str(token or "").strip():
        return AccountUsageSnapshot(
            provider="openai-codex",
            source="usage_api",
            fetched_at=_utc_now(),
            unavailable_reason="Codex credentials not available.",
        )
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "codex-cli",
    }
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    try:
        with httpx.Client(timeout=15.0) as client:
            response = client.get(
                _resolve_codex_usage_url(resolved_base_url), headers=headers
            )
            status_code = getattr(response, "status_code", None)
            if status_code is not None and status_code >= 400:
                if status_code in (401, 403):
                    return AccountUsageSnapshot(
                        provider="openai-codex",
                        source="usage_api",
                        fetched_at=_utc_now(),
                        unavailable_reason="ChatGPT/Codex token rejected.",
                    )
                return AccountUsageSnapshot(
                    provider="openai-codex",
                    source="usage_api",
                    fetched_at=_utc_now(),
                    unavailable_reason=f"Codex usage API error ({status_code}).",
                )
            response.raise_for_status()
    except Exception as exc:
        logger.debug("Codex usage API request failed", exc_info=True)
        return AccountUsageSnapshot(
            provider="openai-codex",
            source="usage_api",
            fetched_at=_utc_now(),
            unavailable_reason=f"Codex usage API unreachable ({type(exc).__name__}).",
        )
    try:
        payload = response.json() or {}
    except Exception as exc:
        return AccountUsageSnapshot(
            provider="openai-codex",
            source="usage_api",
            fetched_at=_utc_now(),
            unavailable_reason=f"Invalid Codex usage response ({type(exc).__name__}).",
        )
    rate_limit = payload.get("rate_limit") or {}
    windows: list[AccountUsageWindow] = []
    for key, fallback_label, fallback_wkey in (
        ("primary_window", "Session", "session"),
        ("secondary_window", "Weekly", "weekly"),
    ):
        window = rate_limit.get(key) or {}
        used = window.get("used_percent")
        if used is None:
            continue
        label, wkey = _codex_window_kind(window, fallback_label, fallback_wkey)
        windows.append(
            AccountUsageWindow(
                label=label,
                used_percent=float(used),
                reset_at=_parse_dt(window.get("reset_at")),
                window_key=wkey,
            )
        )
    details: list[str] = []
    reset_credits = payload.get("rate_limit_reset_credits") or {}
    banked = reset_credits.get("available_count")
    if isinstance(banked, (int, float)) and int(banked) > 0:
        count = int(banked)
        plural = "s" if count != 1 else ""
        details.append(
            f"You have {count} reset{plural} banked - use /usage reset to activate"
        )
    credits = payload.get("credits") or {}
    if credits.get("has_credits"):
        balance = credits.get("balance")
        if isinstance(balance, (int, float)):
            details.append(f"Credits balance: ${float(balance):.2f}")
        elif credits.get("unlimited"):
            details.append("Credits balance: unlimited")
    return AccountUsageSnapshot(
        provider="openai-codex",
        source="usage_api",
        fetched_at=_utc_now(),
        plan=_title_case_slug(payload.get("plan_type")),
        windows=tuple(windows),
        details=tuple(details),
    )


@dataclass(frozen=True)
class CodexResetRedeemResult:
    """Outcome of a `/usage reset` attempt against the Codex backend."""

    status: str  # reset | nothing_to_reset | no_credit | already_redeemed |
    #              not_exhausted | no_credits_banked | unavailable
    message: str
    available_count: int = 0
    windows_reset: int = 0

    @property
    def redeemed(self) -> bool:
        return self.status == "reset"


# Client-side guard threshold: a rate-limit window only counts as exhausted
# when it is fully used. Below this, redeeming a banked reset wastes most of
# its value, so we block and point at --force instead.
_CODEX_WINDOW_EXHAUSTED_PERCENT = 100.0


def redeem_codex_reset_credit(
    *,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    force: bool = False,
) -> CodexResetRedeemResult:
    """Redeem one banked Codex rate-limit reset credit (`/usage reset`).

    Flow (mirrors the Codex CLI's reset-credits picker, codex-rs
    ``backend-client``):

    1. ``GET .../usage`` — read the current windows + banked credit count.
    2. Guard: zero banked credits → refuse. No window fully used and not
       ``force`` → refuse with a warning (a banked reset restores the WHOLE
       5h + weekly allowance; burning it early wastes it). The backend has
       the same protection (``nothing_to_reset`` doesn't consume the
       credit), but failing fast client-side gives a clearer message.
    3. ``POST .../rate-limit-reset-credits/consume`` with a fresh UUID
       idempotency key (``redeem_request_id``). No ``credit_id`` — the
       backend picks the next available credit, exactly like the CLI's
       default "Full reset" option.

    Never raises: every failure mode returns a ``CodexResetRedeemResult``
    with a user-renderable message.
    """
    import uuid

    try:
        token, resolved_base_url, account_id = _resolve_codex_usage_credentials(base_url, api_key)
    except Exception:
        return CodexResetRedeemResult(
            status="unavailable",
            message="No Codex credentials available. Run `hermes auth` to sign in with your ChatGPT account.",
        )
    usage_url, _credits_url, consume_url = _codex_backend_urls(resolved_base_url)
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "codex-cli",
    }
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id

    try:
        with httpx.Client(timeout=15.0) as client:
            usage_resp = client.get(usage_url, headers=headers)
            usage_resp.raise_for_status()
            payload = usage_resp.json() or {}

            reset_credits = payload.get("rate_limit_reset_credits") or {}
            raw_count = reset_credits.get("available_count")
            available = int(raw_count) if isinstance(raw_count, (int, float)) else 0
            if available <= 0:
                return CodexResetRedeemResult(
                    status="no_credits_banked",
                    message="No banked reset credits on this account — nothing to redeem.",
                )

            rate_limit = payload.get("rate_limit") or {}
            worst_used: Optional[float] = None
            for key in ("primary_window", "secondary_window"):
                used = (rate_limit.get(key) or {}).get("used_percent")
                if isinstance(used, (int, float)):
                    worst_used = max(worst_used or 0.0, float(used))
            exhausted = worst_used is not None and worst_used >= _CODEX_WINDOW_EXHAUSTED_PERCENT
            if not exhausted and not force:
                usage_note = (
                    f"your busiest window is only {worst_used:.0f}% used"
                    if worst_used is not None
                    else "your current usage could not be confirmed as exhausted"
                )
                plural = "s" if available != 1 else ""
                return CodexResetRedeemResult(
                    status="not_exhausted",
                    message=(
                        f"⚠️ Not redeeming: {usage_note}. A banked reset restores your FULL "
                        f"5h + weekly limits, so spending it now would waste most of it. "
                        f"You have {available} reset{plural} banked. "
                        f"Use `/usage reset --force` to redeem anyway."
                    ),
                    available_count=available,
                )

            consume_resp = client.post(
                consume_url,
                headers={**headers, "Content-Type": "application/json"},
                json={"redeem_request_id": str(uuid.uuid4())},
            )
            consume_resp.raise_for_status()
            body = consume_resp.json() or {}
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code in (401, 403):
            return CodexResetRedeemResult(
                status="unavailable",
                message=(
                    "Codex backend rejected the request (HTTP "
                    f"{code}). Reset credits require ChatGPT-account (OAuth) auth — "
                    "run `hermes auth` and sign in with your ChatGPT account."
                ),
            )
        return CodexResetRedeemResult(
            status="unavailable",
            message=f"Codex backend error (HTTP {code}) — try again shortly.",
        )
    except Exception as exc:
        return CodexResetRedeemResult(
            status="unavailable",
            message=f"Could not reach the Codex backend: {exc}",
        )

    code = str(body.get("code", "") or "").strip().lower()
    windows_reset = body.get("windows_reset")
    windows_reset = int(windows_reset) if isinstance(windows_reset, (int, float)) else 0
    remaining = max(0, available - 1)
    plural = "s" if remaining != 1 else ""
    if code == "reset":
        return CodexResetRedeemResult(
            status="reset",
            message=(
                f"✅ Reset redeemed — your usage limits have been reset. "
                f"{remaining} banked reset{plural} remaining."
            ),
            available_count=remaining,
            windows_reset=windows_reset,
        )
    if code == "nothing_to_reset":
        return CodexResetRedeemResult(
            status="nothing_to_reset",
            message=(
                "Backend reports nothing to reset — your limits aren't exhausted. "
                "The credit was NOT spent."
            ),
            available_count=available,
        )
    if code == "no_credit":
        return CodexResetRedeemResult(
            status="no_credit",
            message="Backend reports no available reset credit on this account.",
        )
    if code == "already_redeemed":
        return CodexResetRedeemResult(
            status="already_redeemed",
            message="This redemption was already processed — no additional credit was spent.",
            available_count=remaining,
        )
    return CodexResetRedeemResult(
        status="unavailable",
        message=f"Unexpected response from the Codex backend: {body!r}",
    )


def _format_anthropic_plan(
    subscription_type: Optional[str], rate_limit_tier: Optional[str]
) -> Optional[str]:
    """Render a Claude subscription label like ``"Max 20×"`` from the raw fields.

    ``subscription_type`` is the base plan (``max`` / ``pro`` / ``free`` / ``team``);
    ``rate_limit_tier`` (e.g. ``default_claude_max_20x``) carries the multiplier and,
    as a fallback, the base plan. Returns None when neither yields anything usable.
    """
    base: Optional[str] = None
    if isinstance(subscription_type, str) and subscription_type.strip():
        base = subscription_type.strip().title()
    multiplier: Optional[str] = None
    if isinstance(rate_limit_tier, str) and rate_limit_tier.strip():
        tier = rate_limit_tier.strip().lower()
        for part in tier.replace("-", "_").split("_"):
            if len(part) >= 2 and part.endswith("x") and part[:-1].isdigit():
                multiplier = f"{part[:-1]}×"
                break
        if base is None:
            if "max" in tier:
                base = "Max"
            elif "pro" in tier:
                base = "Pro"
            elif "team" in tier:
                base = "Team"
    if base and multiplier:
        return f"{base} {multiplier}"
    return base or multiplier


def _resolve_anthropic_plan_label() -> Optional[str]:
    """Best-effort Claude subscription tier for display (e.g. ``"Max 20×"``).

    The OAuth usage API carries no plan/tier field, so read it from the local
    Claude Code credentials (the same store ``resolve_anthropic_token`` uses):
    ``~/.claude/.credentials.json`` → ``claudeAiOauth.{subscriptionType,rateLimitTier}``,
    falling back to ``~/.claude.json`` → ``oauthAccount.organizationRateLimitTier``.
    Fail-soft: any missing file or parse error returns None (the card just omits
    the plan line, exactly as before this change).
    """
    subscription_type: Optional[str] = None
    rate_limit_tier: Optional[str] = None
    try:
        cred_path = Path.home() / ".claude" / ".credentials.json"
        if cred_path.exists():
            data = json.loads(cred_path.read_text(encoding="utf-8"))
            data = data if isinstance(data, dict) else {}
            oauth = data.get("claudeAiOauth")
            if isinstance(oauth, dict):
                subscription_type = oauth.get("subscriptionType") or None
                rate_limit_tier = oauth.get("rateLimitTier") or None
    except (json.JSONDecodeError, OSError, ValueError):
        pass
    if rate_limit_tier is None or subscription_type is None:
        try:
            cfg_path = Path.home() / ".claude.json"
            if cfg_path.exists():
                data = json.loads(cfg_path.read_text(encoding="utf-8"))
                data = data if isinstance(data, dict) else {}
                account = data.get("oauthAccount")
                if isinstance(account, dict):
                    rate_limit_tier = rate_limit_tier or account.get(
                        "organizationRateLimitTier"
                    ) or account.get("userRateLimitTier") or None
                    subscription_type = subscription_type or account.get("seatTier") or None
        except (json.JSONDecodeError, OSError, ValueError):
            pass
    return _format_anthropic_plan(subscription_type, rate_limit_tier)


def _fetch_anthropic_account_usage() -> Optional[AccountUsageSnapshot]:
    """Fetch Anthropic OAuth usage. Fail-soft with specific reasons (Kimi pattern)."""
    token = (resolve_anthropic_token() or "").strip()
    if not token:
        return AccountUsageSnapshot(
            provider="anthropic",
            source="oauth_usage_api",
            fetched_at=_utc_now(),
            unavailable_reason="Anthropic OAuth token not configured.",
        )
    if not _is_oauth_token(token):
        return AccountUsageSnapshot(
            provider="anthropic",
            source="oauth_usage_api",
            fetched_at=_utc_now(),
            unavailable_reason="Anthropic account limits are only available for OAuth-backed Claude accounts.",
        )
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "claude-code/2.1.0",
    }
    try:
        with httpx.Client(timeout=15.0) as client:
            response = client.get(
                "https://api.anthropic.com/api/oauth/usage", headers=headers
            )
            status_code = getattr(response, "status_code", None)
            if status_code is not None and status_code >= 400:
                if status_code in (401, 403):
                    return AccountUsageSnapshot(
                        provider="anthropic",
                        source="oauth_usage_api",
                        fetched_at=_utc_now(),
                        unavailable_reason="Anthropic OAuth token rejected (expired?).",
                    )
                return AccountUsageSnapshot(
                    provider="anthropic",
                    source="oauth_usage_api",
                    fetched_at=_utc_now(),
                    unavailable_reason=f"Anthropic usage API error ({status_code}).",
                )
            response.raise_for_status()
    except Exception as exc:
        logger.debug("Anthropic usage API request failed", exc_info=True)
        return AccountUsageSnapshot(
            provider="anthropic",
            source="oauth_usage_api",
            fetched_at=_utc_now(),
            unavailable_reason=f"Anthropic usage API unreachable ({type(exc).__name__}).",
        )
    try:
        payload = response.json() or {}
    except Exception as exc:
        return AccountUsageSnapshot(
            provider="anthropic",
            source="oauth_usage_api",
            fetched_at=_utc_now(),
            unavailable_reason=f"Invalid Anthropic usage response ({type(exc).__name__}).",
        )
    windows: list[AccountUsageWindow] = []
    mapping = (
        ("five_hour", "Current session", "session"),
        ("seven_day", "Current week", "weekly"),
        ("seven_day_opus", "Opus week", "opus_week"),
        ("seven_day_sonnet", "Sonnet week", "sonnet_week"),
    )
    for key, label, wkey in mapping:
        window = payload.get(key) or {}
        util = window.get("utilization")
        if util is None:
            continue
        used = float(util) * 100 if float(util) <= 1 else float(util)
        windows.append(
            AccountUsageWindow(
                label=label,
                used_percent=used,
                reset_at=_parse_dt(window.get("resets_at")),
                window_key=wkey,
            )
        )
    # Newer OAuth payloads additionally carry a structured ``limits`` list that
    # includes model-scoped weekly caps (``weekly_scoped``) the flat top-level
    # fields don't expose. Surface them as secondary ("other") windows so a tight
    # model-scoped cap shows up in the details instead of being silently dropped.
    limits = payload.get("limits")
    if isinstance(limits, list):
        for entry in limits:
            if not isinstance(entry, dict) or entry.get("kind") != "weekly_scoped":
                continue
            # An explicitly inactive scoped cap is not currently in force — skip it
            # (missing/None counts as active: fail-open towards showing).
            if entry.get("is_active") is False:
                continue
            raw_pct = entry.get("percent")
            if isinstance(raw_pct, bool) or not isinstance(raw_pct, (int, float)):
                continue
            # ``limits[].percent`` is already in percentage points (4/82/94 in the
            # live payload) — unlike top-level ``utilization``, which can be a 0..1
            # fraction. Use it directly; the *100-if-<=1 heuristic would wrongly turn
            # a legitimate percent=1 into 100 %.
            scoped_used = max(0.0, min(100.0, float(raw_pct)))
            model_name: Optional[str] = None
            scope = entry.get("scope")
            if isinstance(scope, dict):
                model = scope.get("model")
                if isinstance(model, dict):
                    display = model.get("display_name")
                    if isinstance(display, str) and display.strip():
                        model_name = display.strip()
            windows.append(
                AccountUsageWindow(
                    label="Modell-Limit",
                    used_percent=scoped_used,
                    reset_at=_parse_dt(entry.get("resets_at")),
                    detail=model_name,
                    window_key="scoped_week",
                )
            )
    details: list[str] = []
    extra = payload.get("extra_usage") or {}
    if extra.get("is_enabled"):
        used_credits = extra.get("used_credits")
        monthly_limit = extra.get("monthly_limit")
        currency = extra.get("currency") or "USD"
        if isinstance(used_credits, (int, float)) and isinstance(monthly_limit, (int, float)):
            details.append(
                f"Extra usage: {used_credits:.2f} / {monthly_limit:.2f} {currency}"
            )
    return AccountUsageSnapshot(
        provider="anthropic",
        source="oauth_usage_api",
        fetched_at=_utc_now(),
        plan=_resolve_anthropic_plan_label(),
        windows=tuple(windows),
        details=tuple(details),
    )


def _fetch_openrouter_account_usage(base_url: Optional[str], api_key: Optional[str]) -> Optional[AccountUsageSnapshot]:
    runtime = resolve_runtime_provider(
        requested="openrouter",
        explicit_base_url=base_url,
        explicit_api_key=api_key,
    )
    token = str(runtime.get("api_key", "") or "").strip()
    if not token:
        return None
    normalized = str(runtime.get("base_url", "") or "").rstrip("/")
    credits_url = f"{normalized}/credits"
    key_url = f"{normalized}/key"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    with httpx.Client(timeout=10.0) as client:
        credits_resp = client.get(credits_url, headers=headers)
        credits_resp.raise_for_status()
        credits = (credits_resp.json() or {}).get("data") or {}
        try:
            key_resp = client.get(key_url, headers=headers)
            key_resp.raise_for_status()
            key_data = (key_resp.json() or {}).get("data") or {}
        except Exception:
            key_data = {}
    total_credits = float(credits.get("total_credits") or 0.0)
    total_usage = float(credits.get("total_usage") or 0.0)
    details = [f"Credits balance: ${max(0.0, total_credits - total_usage):.2f}"]
    windows: list[AccountUsageWindow] = []
    limit = key_data.get("limit")
    limit_remaining = key_data.get("limit_remaining")
    limit_reset = str(key_data.get("limit_reset") or "").strip()
    usage = key_data.get("usage")
    if (
        isinstance(limit, (int, float))
        and float(limit) > 0
        and isinstance(limit_remaining, (int, float))
        and 0 <= float(limit_remaining) <= float(limit)
    ):
        limit_value = float(limit)
        remaining_value = float(limit_remaining)
        used_percent = ((limit_value - remaining_value) / limit_value) * 100
        detail_parts = [f"${remaining_value:.2f} of ${limit_value:.2f} remaining"]
        if limit_reset:
            detail_parts.append(f"resets {limit_reset}")
        windows.append(
            AccountUsageWindow(
                label="API key quota",
                used_percent=used_percent,
                detail=" • ".join(detail_parts),
            )
        )
    if isinstance(usage, (int, float)):
        usage_parts = [f"API key usage: ${float(usage):.2f} total"]
        for value, label in (
            (key_data.get("usage_daily"), "today"),
            (key_data.get("usage_weekly"), "this week"),
            (key_data.get("usage_monthly"), "this month"),
        ):
            if isinstance(value, (int, float)) and float(value) > 0:
                usage_parts.append(f"${float(value):.2f} {label}")
        details.append(" • ".join(usage_parts))
    return AccountUsageSnapshot(
        provider="openrouter",
        source="credits_api",
        fetched_at=_utc_now(),
        windows=tuple(windows),
        details=tuple(details),
    )


def _positive_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _format_token_total(total: int) -> str:
    return f"{int(total):,}"


def _format_run_count(count: int) -> str:
    return f"{count:,} run" + ("" if count == 1 else "s")


def _numeric(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value in {None, ""}:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _used_percent_from_quota(bucket: Any) -> Optional[float]:
    if not isinstance(bucket, dict):
        return None
    explicit = _numeric(bucket.get("used_percent"))
    if explicit is not None:
        return max(0.0, min(100.0, explicit))
    used = _numeric(bucket.get("used") or bucket.get("total_tokens"))
    limit = _numeric(bucket.get("limit") or bucket.get("cap"))
    if used is None or limit is None or limit <= 0:
        return None
    return max(0.0, min(100.0, (used / limit) * 100.0))


def _remaining_detail(bucket: Any) -> Optional[str]:
    if not isinstance(bucket, dict):
        return None
    remaining = bucket.get("remaining")
    limit = bucket.get("limit") or bucket.get("cap")
    if remaining in {None, ""} or limit in {None, ""}:
        return None
    return f"{remaining}/{limit} verbleibend"


def _normalize_kimi_tag(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    for prefix in ("LEVEL_", "TYPE_", "PLAN_"):
        if text.upper().startswith(prefix):
            text = text[len(prefix) :]
            break
    return _title_case_slug(text)


def _fetch_kimi_account_usage() -> Optional[AccountUsageSnapshot]:
    """Fetch Kimi Coding usage from the live Moonshot/Kimi usages API.

    Live shape verified 2026-07-09::

        GET https://api.kimi.com/coding/v1/usages
        {
          "usage": {"limit": "100", "used": "22", "remaining": "78", "resetTime": "..."},
          "limits": [{"window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
                      "detail": {"limit": "100", "used": "17", "remaining": "83", "resetTime": "..."}}],
          "user": {"membership": {"level": "LEVEL_BASIC"}},
          "subType": "TYPE_PURCHASE"
        }

    Missing/unrecognized data is fail-open so the dashboard never invents limits.
    """
    import os

    api_key = os.environ.get("KIMI_API_KEY", "").strip()
    if not api_key:
        return AccountUsageSnapshot(
            provider="kimi",
            source="usage_api",
            fetched_at=_utc_now(),
            unavailable_reason="Kimi API key not configured (KIMI_API_KEY).",
        )

    url = "https://api.kimi.com/coding/v1/usages"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    try:
        with httpx.Client(timeout=15.0) as client:
            response = client.get(url, headers=headers)
            status_code = getattr(response, "status_code", None)
            if status_code is not None and status_code >= 400:
                if status_code in (401, 403):
                    return AccountUsageSnapshot(
                        provider="kimi",
                        source="usage_api",
                        fetched_at=_utc_now(),
                        unavailable_reason="Kimi API key rejected.",
                    )
                return AccountUsageSnapshot(
                    provider="kimi",
                    source="usage_api",
                    fetched_at=_utc_now(),
                    unavailable_reason=f"Kimi usage API error ({status_code}).",
                )
            response.raise_for_status()
    except Exception as exc:
        logger.debug("Kimi usage API request failed", exc_info=True)
        return AccountUsageSnapshot(
            provider="kimi",
            source="usage_api",
            fetched_at=_utc_now(),
            unavailable_reason=f"Kimi usage API unreachable ({type(exc).__name__}).",
        )

    try:
        payload = response.json() or {}
    except Exception as exc:
        return AccountUsageSnapshot(
            provider="kimi",
            source="usage_api",
            fetched_at=_utc_now(),
            unavailable_reason=f"Invalid Kimi usage response ({type(exc).__name__}).",
        )
    if not isinstance(payload, dict):
        return AccountUsageSnapshot(
            provider="kimi",
            source="usage_api",
            fetched_at=_utc_now(),
            unavailable_reason="Kimi usage response is not an object.",
        )

    windows: list[AccountUsageWindow] = []
    details: list[str] = []

    # Weekly/current account bucket from the live API: payload["usage"].
    weekly = payload.get("usage")
    weekly = weekly if isinstance(weekly, dict) else {}
    weekly_used = _used_percent_from_quota(weekly)
    if weekly_used is not None:
        windows.append(
            AccountUsageWindow(
                label="Diese Woche",
                used_percent=float(weekly_used),
                reset_at=_parse_dt(weekly.get("resetTime") or weekly.get("resets_at") or weekly.get("reset_at")),
                detail=_remaining_detail(weekly),
                window_key="weekly",
            )
        )

    # 5-hour sliding window from the live API: payload["limits"][0]["detail"].
    session: dict[str, Any] = {}
    limits = payload.get("limits")
    if isinstance(limits, list) and limits:
        first_limit = limits[0]
        if isinstance(first_limit, dict):
            detail = first_limit.get("detail")
            if isinstance(detail, dict):
                session = detail
    session_used = _used_percent_from_quota(session)
    if session_used is not None:
        windows.append(
            AccountUsageWindow(
                label="5-Std-Fenster",
                used_percent=float(session_used),
                reset_at=_parse_dt(session.get("resetTime") or session.get("resets_at") or session.get("reset_at")),
                detail=_remaining_detail(session),
                window_key="session",
            )
        )

    plan_parts: list[str] = []
    user = payload.get("user")
    membership = user.get("membership", {}) if isinstance(user, dict) else {}
    for raw in (
        membership.get("level") if isinstance(membership, dict) else None,
        payload.get("subType"),
    ):
        part = _normalize_kimi_tag(raw)
        if part and part not in plan_parts:
            plan_parts.append(part)
    plan = " · ".join(plan_parts) if plan_parts else None

    # Optional magnitude details for non-dashboard consumers/details disclosure.
    total_quota = payload.get("totalQuota")
    if isinstance(total_quota, dict):
        detail = _remaining_detail(total_quota)
        if detail:
            details.append(f"Gesamt-Quota: {detail}")
    parallel = payload.get("parallel")
    if isinstance(parallel, dict) and parallel.get("limit") not in {None, ""}:
        details.append(f"Parallel: {parallel.get('limit')}")

    if not windows and not details:
        return AccountUsageSnapshot(
            provider="kimi",
            source="usage_api",
            fetched_at=_utc_now(),
            unavailable_reason="Kimi usage data empty or unrecognized shape.",
        )

    return AccountUsageSnapshot(
        provider="kimi",
        source="usage_api",
        fetched_at=_utc_now(),
        title="Kimi",
        plan=plan,
        windows=tuple(windows),
        details=tuple(details),
    )


_XAI_BILLING_MARKER = "billing: fetched credits config"
_XAI_LOG_TAIL_BYTES = 4 * 1024 * 1024  # last 4 MiB only


def _fetch_xai_account_usage(log_path: Optional[Path] = None) -> Optional[AccountUsageSnapshot]:
    """Fetch Grok/xAI plan usage from the local Grok CLI billing log.

    Pure local file read (no network). Scans ~/.grok/logs/unified.jsonl for the
    latest ``billing: fetched credits config`` line and maps
    ``ctx.config.creditUsagePercent`` into an AccountUsageSnapshot.
    Fail-soft: missing/unreadable log or no valid billing line returns a
    snapshot with unavailable_reason set; never raises.
    """
    path = log_path if log_path is not None else Path.home() / ".grok" / "logs" / "unified.jsonl"

    def _unavailable(reason: str) -> AccountUsageSnapshot:
        return AccountUsageSnapshot(
            provider="xai",
            source="grok_cli_log",
            fetched_at=_utc_now(),
            unavailable_reason=reason,
        )

    try:
        if not path.is_file():
            return _unavailable(
                "Grok-CLI-Log nicht gefunden (~/.grok/logs/unified.jsonl)."
            )
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > _XAI_LOG_TAIL_BYTES:
                fh.seek(size - _XAI_LOG_TAIL_BYTES)
                raw = fh.read()
                # Mid-line seek: drop the partial first line.
                nl = raw.find(b"\n")
                if nl != -1:
                    raw = raw[nl + 1 :]
            else:
                raw = fh.read()
    except OSError:
        return _unavailable(
            "Grok-CLI-Log nicht gefunden (~/.grok/logs/unified.jsonl)."
        )

    text = raw.decode("utf-8", errors="replace")

    def _parse_record(line: str) -> Optional[dict]:
        if _XAI_BILLING_MARKER not in line:
            return None
        try:
            record_payload = json.loads(line)
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
        if not isinstance(record_payload, dict):
            return None
        record_ctx = record_payload.get("ctx")
        if not isinstance(record_ctx, dict):
            return None
        record_config = record_ctx.get("config")
        if not isinstance(record_config, dict):
            return None
        return {"payload": record_payload, "ctx": record_ctx, "config": record_config}

    def _period_start(record: dict) -> Any:
        current = record["config"].get("currentPeriod")
        if isinstance(current, dict):
            return current.get("start")
        return None

    def _valid_pct(record: dict) -> Optional[float]:
        pct = record["config"].get("creditUsagePercent")
        # bool is a subclass of int — reject it.
        if isinstance(pct, bool) or not isinstance(pct, (int, float)):
            return None
        return float(pct)

    records = [
        record
        for record in (_parse_record(line) for line in reversed(text.splitlines()))
        if record is not None
    ]
    if not records:
        return _unavailable("Keine Billing-Daten im Grok-CLI-Log.")

    # Plan/reset/signal always come from the NEWEST billing line (the current
    # period). The percentage is bound to that same period: at a period boundary
    # the newest line (just-started period, new unified-billing format) can omit
    # ``creditUsagePercent`` — accept a percentage only from the newest line's own
    # period, otherwise report an honest "unknown" instead of resurrecting the
    # previous period's stale value (often 100 % with an already-passed reset).
    reference = records[0]
    reference_start = _period_start(reference)
    credit_pct: Optional[float] = None
    if reference_start is None:
        # No reliable period anchor: trust only the newest record — never fall back
        # to an arbitrarily old percentage (that is exactly the stale path the
        # period binding exists to prevent).
        credit_pct = _valid_pct(reference)
    else:
        for record in records:
            if _period_start(record) != reference_start:
                continue
            candidate = _valid_pct(record)
            if candidate is not None:
                credit_pct = candidate
                break

    ctx = reference["ctx"]
    config = reference["config"]
    payload = reference["payload"]

    plan_raw = ctx.get("subscriptionTier")
    plan: Optional[str] = None
    if plan_raw not in {None, ""}:
        cleaned = str(plan_raw).strip()
        if cleaned:
            plan = cleaned

    period_end: Any = None
    current_period = config.get("currentPeriod")
    if isinstance(current_period, dict):
        period_end = current_period.get("end")

    windows = (
        AccountUsageWindow(
            label="Diese Woche",
            used_percent=credit_pct,
            reset_at=_parse_dt(period_end),
            window_key="weekly",
        ),
    )

    details: list[str] = []
    ts = _parse_dt(payload.get("ts"))
    if ts is not None:
        details.append(
            f"Stand: {ts.astimezone().strftime('%Y-%m-%d %H:%M %Z')}"
        )

    def _nested_val(obj: Any) -> Optional[float]:
        if not isinstance(obj, dict):
            return None
        raw_val = obj.get("val")
        if raw_val in {None, ""}:
            return None
        try:
            return float(raw_val)
        except (TypeError, ValueError):
            return None

    on_demand_cap = _nested_val(config.get("onDemandCap"))
    on_demand_used = _nested_val(config.get("onDemandUsed"))
    if on_demand_cap is not None and on_demand_cap > 0:
        used_disp = int(on_demand_used) if on_demand_used is not None and on_demand_used == int(on_demand_used) else (on_demand_used if on_demand_used is not None else 0)
        cap_disp = int(on_demand_cap) if on_demand_cap == int(on_demand_cap) else on_demand_cap
        details.append(f"On-Demand: {used_disp}/{cap_disp}")

    prepaid = _nested_val(config.get("prepaidBalance"))
    if prepaid is not None and prepaid > 0:
        prepaid_disp = int(prepaid) if prepaid == int(prepaid) else prepaid
        details.append(f"Prepaid-Guthaben: {prepaid_disp}")

    return AccountUsageSnapshot(
        provider="xai",
        source="grok_cli_log",
        fetched_at=_utc_now(),
        signal_at=ts,
        title="Grok",
        plan=plan,
        windows=windows,
        details=tuple(details),
    )


def fetch_account_usage(
    provider: Optional[str],
    *,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Optional[AccountUsageSnapshot]:
    normalized = str(provider or "").strip().lower()
    if normalized in {"", "auto", "custom"}:
        return None
    try:
        if normalized == "openai-codex":
            return _fetch_codex_account_usage(base_url=base_url, api_key=api_key)
        if normalized == "anthropic":
            return _fetch_anthropic_account_usage()
        if normalized == "openrouter":
            return _fetch_openrouter_account_usage(base_url, api_key)
        if normalized in {"kimi", "moonshot"}:
            return _fetch_kimi_account_usage()
        if normalized in {"xai", "grok"}:
            return _fetch_xai_account_usage()
    except Exception:
        return None
    return None
