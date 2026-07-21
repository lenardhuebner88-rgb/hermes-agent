"""Distilled vision-flywheel metrics + the green-gate streak ledger.

Two repo-side CLIs back the vision flywheel's "is the system actually
trustworthy?" question (Nordstern, ``00-Canon/vision.md``):

* ``hermes vision metrics-snapshot`` precomputes a small, *distilled*
  metrics file at ``~/.hermes/state/vision-metrics.json`` — NOT a raw
  ``kanban.db`` dump. Each headline metric ships with a paired *counter*
  metric (the skeptic's number that keeps the headline honest, e.g.
  Autonomie-% ↔ "hätte-eskalieren-sollen-tat-nicht").

* ``hermes vision record-gate-result pass|fail`` appends a structured
  record to a green-gate ledger. The nightly ``green-gate-heartbeat``
  writes nothing on a green run today, so the green-gate *streak*
  (consecutive green nights) is not derivable from the DB alone — this
  ledger makes it derivable.

The runtime wiring (a cron / heartbeat that *calls* these) is an operator
step and lives outside this module. Everything here is pure CLI logic.

State-path resolution honours ``HERMES_VISION_STATE_DIR`` so tests (and
sandboxes) write to a temp dir instead of the live state.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import sqlite3
import statistics
import time
from pathlib import Path
from typing import Callable, Optional

from agent.redact import redact_sensitive_text
from hermes_cli import kanban_db as kb

DAY_SECONDS = 86_400
# v2: cost_per_task averages metered (>0) tasks only + explicit `coverage`
# breakdown (subscription-$0 no longer pollutes the average or the counter).
SCHEMA_VERSION = 2
METRICS_FILENAME = "vision-metrics.json"
GATE_LEDGER_FILENAME = "green-gate-ledger.jsonl"

GATE_RESULTS = ("pass", "fail")

# Canonical gate names a red heartbeat run can attribute a first failure to.
# Not enforced (forward-compat: a future gate or the toolchain-missing edge can
# carry its own label) — used for documentation and normalization expectations.
GATE_NAMES = ("python", "tsc", "vitest", "build")

# AC-2: the captured stderr tail is capped so each red ledger entry stays
# bounded. 2 KiB keeps the first-failure cause readable without letting a
# pathological log balloon the append-only ledger.
GATE_FIRST_FAIL_MAX_BYTES = 2048

# Bound the demoted-leaker list stored on a red ledger entry (operator
# visibility, AC-2) so a pathological red night can't balloon the append-only
# ledger: at most this many entries, each a single redacted/length-capped line.
GATE_LEAKERS_MAX = 25
GATE_LEAKER_ENTRY_MAX = 200

# Heiler classes that indicate a *real* problem was detected (not a transient
# blip). A task counted "autonomous" that nonetheless carries one of these is
# the autonomy metric's skeptic counter: the system saw something real and
# still never escalated to the operator. ``unclassified`` stays out here like
# ``capacity``: it is an opaque/default operational bucket, not a known defect.
_NON_TRANSIENT_HEILER_CLASSES = (
    kb.HEILER_CLASS_REAL_BUG,
    kb.HEILER_CLASS_BAD_SPEC,
    kb.HEILER_CLASS_CONFLICT,
)

# ESCALATION-OPERATOR-GATE-DECLASSIFY-S1: terminal NON-error Heiler classes — a
# deliberate operator gate (the operator must release/answer) or a pure
# observability signal (budget capacity, an operator-intent supersede/green-run).
# None is a product defect, so the ``error`` escalation rate (the AC-1 "escalation
# rate relieved of the false-positive operator gates") excludes them. ``unclassified``
# is intentionally NOT here: an opaque failure is an unknown that may still be a
# real error, so it stays counted as a (potential) error escalation.
_NON_ERROR_HEILER_CLASSES = (
    kb.HEILER_CLASS_OPERATOR_GATED,
    kb.HEILER_CLASS_OPERATOR_INTENT,
    kb.HEILER_CLASS_CAPACITY,
)


# ---------------------------------------------------------------------------
# State-path resolution (env-overridable for tests / sandboxes)
# ---------------------------------------------------------------------------

def vision_state_dir() -> Path:
    """Return the directory the vision artifacts live in.

    ``HERMES_VISION_STATE_DIR`` overrides the default
    ``<root>/state`` (the same ``state/`` dir the kanban dispatcher
    heartbeat uses). Tests set the override to a temp dir so they never
    touch the live state.
    """
    override = os.environ.get("HERMES_VISION_STATE_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    from hermes_constants import get_default_hermes_root

    return get_default_hermes_root() / "state"


def metrics_snapshot_path() -> Path:
    return vision_state_dir() / METRICS_FILENAME


def gate_ledger_path() -> Path:
    return vision_state_dir() / GATE_LEDGER_FILENAME


# Flaky de-flake accountability (GATE-FLAKY-RETRY-HONESTY-S1): the set of
# per-file de-flake keys the strategist has already filed a HELD de-flake task
# for. Read by the counter-metric so ``flaky_neutralized_without_filed_deflake_task``
# is derivable at snapshot time; written by ``strategist.propose_deflake``.
DEFLAKE_FILED_FILENAME = "green-gate-deflake-filed.json"


def deflake_filed_path() -> Path:
    return vision_state_dir() / DEFLAKE_FILED_FILENAME


def read_deflake_filed(path: Optional[Path] = None) -> set[str]:
    """Read the set of flaky-file keys that already have a filed HELD de-flake
    task (written by :func:`strategist.propose_deflake`). Missing or corrupt
    state resolves to an empty set — the counter-metric then reports every
    current flaky file as still-unfiled (fail-loud, never a silent zero)."""
    target = path or deflake_filed_path()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return set()
    if isinstance(data, list):
        return {str(k) for k in data if k}
    return set()


def write_deflake_filed(keys, path: Optional[Path] = None) -> Path:
    """Persist the filed de-flake key set (sorted JSON list). Returns the path."""
    target = path or deflake_filed_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(sorted({str(k) for k in keys if k}), ensure_ascii=False),
        encoding="utf-8",
    )
    return target


# ---------------------------------------------------------------------------
# Green-gate ledger: record + read + streak derivation
# ---------------------------------------------------------------------------

def _parse_ts(ts: Optional[str], *, now: Optional[int]) -> _dt.datetime:
    """Resolve a timestamp to an aware UTC datetime.

    Precedence: explicit ISO ``ts`` → explicit ``now`` epoch → wall clock.
    A naive ISO string is interpreted as UTC.
    """
    if ts:
        cleaned = ts.strip().replace("Z", "+00:00")
        dt = _dt.datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        return dt.astimezone(_dt.timezone.utc)
    epoch = int(now) if now is not None else int(time.time())
    return _dt.datetime.fromtimestamp(epoch, tz=_dt.timezone.utc)


def _build_first_fail(gate: Optional[str], detail: Optional[str]) -> dict:
    """Build the redacted, bounded ``first_fail`` payload for a red record.

    ``gate`` is normalized (lowercased/stripped). ``detail`` (the heartbeat's
    first non-empty ``fails[]`` entry — a gate label plus a short stderr tail)
    is run through the existing response redaction so no secret reaches the
    on-disk ledger, then capped to :data:`GATE_FIRST_FAIL_MAX_BYTES`.

    Redaction runs on the *full* string before capping so a credential can
    never survive by being split across the cap boundary; the kept slice is
    the tail (the most-relevant end of a failing log). Returns ``{}`` when
    neither field carries content.
    """
    first_fail: dict = {}
    if gate:
        cleaned = str(gate).strip().lower()
        if cleaned:
            first_fail["gate"] = cleaned
    if detail:
        # force=True: the ledger persists to disk, so it must never carry raw
        # secrets regardless of the operator's global redaction preference.
        redacted = redact_sensitive_text(str(detail), force=True)
        raw = redacted.encode("utf-8")
        if len(raw) > GATE_FIRST_FAIL_MAX_BYTES:
            redacted = raw[-GATE_FIRST_FAIL_MAX_BYTES:].decode("utf-8", "ignore")
        if redacted:
            first_fail["detail"] = redacted
    return first_fail


def _build_leakers(leakers: Optional[list]) -> list[str]:
    """Redact + bound the demoted-leaker list for a red ledger entry.

    Each entry (``"<gate>: <file>"``) is run through the same forced redaction
    as the first-failure detail (the ledger persists to disk), flattened to one
    line, length-capped, and the list itself is capped to :data:`GATE_LEAKERS_MAX`
    so the operator still sees *which* files were demoted without the entry
    ballooning the append-only ledger.
    """
    if not leakers:
        return []
    out: list[str] = []
    for item in list(leakers)[:GATE_LEAKERS_MAX]:
        redacted = redact_sensitive_text(str(item), force=True)
        redacted = " ".join(redacted.split())  # flatten newlines / runs
        if len(redacted) > GATE_LEAKER_ENTRY_MAX:
            redacted = redacted[:GATE_LEAKER_ENTRY_MAX]
        if redacted:
            out.append(redacted)
    return out


def record_gate_result(
    result: str,
    *,
    ts: Optional[str] = None,
    now: Optional[int] = None,
    path: Optional[Path] = None,
    first_fail_gate: Optional[str] = None,
    first_fail_detail: Optional[str] = None,
    leakers: Optional[list] = None,
    leaker_only: bool = False,
    head_sha: Optional[str] = None,
) -> dict:
    """Append one structured green-gate record to the ledger.

    ``result`` must be ``pass`` or ``fail``. ``ts`` is an optional ISO-8601
    timestamp for the gate run (defaults to ``now`` / the wall clock). The
    record carries the UTC ``date`` so the streak can be counted per night.

    On a ``fail`` an optional first-failure payload (``first_fail_gate`` +
    ``first_fail_detail``) is attached as a ``first_fail`` field so each red
    entry carries a machine-readable cause (which gate, a redacted/capped
    stderr tail) the strategist / StrategistView can surface.

    Cause-purity (GREEN-GATE-LEAKER-CAUSE-PURITY-S1): ``leakers`` is the list of
    failing files the isolation rerun demoted (they passed alone) — stored as a
    bounded, redacted ``leakers`` field for operator visibility, never as a
    cause. ``leaker_only`` (every reported fail was a leaker) is recorded as a
    flag and SUPPRESSES the ``first_fail`` cause: the night stays red (result is
    still ``fail``, the streak still 0) but carries no product cause for the
    autoheal loop to act on. The red *verdict* is never suppressed — only the
    cause attribution is cleaned (AC-2).

    Commit attribution (S3 chronic-red-refinement): ``head_sha`` (optional) is
    the repo HEAD commit the gate ran at. Stored on EVERY record — pass or
    fail, not just fails — because ``derive_persistent_red_triage`` /
    ``derive_consecutive_red_cause`` need the nearest EARLIER sha-carrying
    record (any result) to bracket a ``"<prev_sha>..<sha>"`` suspect range for
    a red night; a pass-only night still needs to contribute its sha as the
    "prev" anchor for the next red one. No git call happens here — the caller
    (the nightly heartbeat) already knows its own HEAD.

    ``pass`` records never carry ``first_fail``/``leakers``/``leaker_only`` —
    pass behaviour is unchanged — and a ``fail`` without any payload is also
    unchanged (backward-compatible). Returns the record that was written.
    """
    normalized = str(result).strip().lower()
    if normalized not in GATE_RESULTS:
        raise ValueError(
            f"result must be one of {GATE_RESULTS}, got {result!r}"
        )
    dt = _parse_ts(ts, now=now)
    record = {
        "result": normalized,
        "ts": dt.isoformat(),
        "epoch": int(dt.timestamp()),
        "date": dt.date().isoformat(),
    }
    cleaned_sha = str(head_sha).strip() if head_sha else ""
    if cleaned_sha:
        record["head_sha"] = cleaned_sha
    if normalized == "fail":
        if leaker_only:
            # whole night was harness noise -> red, but no product cause
            record["leaker_only"] = True
        else:
            first_fail = _build_first_fail(first_fail_gate, first_fail_detail)
            if first_fail:
                record["first_fail"] = first_fail
        cleaned_leakers = _build_leakers(leakers)
        if cleaned_leakers:
            record["leakers"] = cleaned_leakers
    target = path or gate_ledger_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def read_gate_records(path: Optional[Path] = None) -> list[dict]:
    """Read every well-formed record from the green-gate ledger (in order)."""
    target = path or gate_ledger_path()
    if not target.exists():
        return []
    records: list[dict] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("date") and obj.get("result"):
            records.append(obj)
    return records


# ---------------------------------------------------------------------------
# Night disposition: green / red / NEUTRAL (leaker-only)
#
# GATE-LEAKER-STREAK-HONESTY-V2: a night whose ONLY gate failures were
# test-isolation *leakers* (every fail record flagged ``leaker_only`` — the file
# passed when re-run alone, so the failure was concurrency/harness noise, not a
# product regression) is NEUTRAL. Neutral is the honest middle between green and
# red: it must neither build trust (NOT a green night, does NOT advance the green
# streak) nor dissolve it (does NOT count as a red night, does NOT extend the
# release-brake red streak, is NOT counted in the persistent-red triage, does
# NOT resolve a standing red-streak hold). It is transparent to all three
# consumers and surfaces only as low-severity leaker debt. A night with ANY
# surviving fail — a product ``first_fail`` OR an unattributed red — stays fully
# RED (symmetry: leaker demotion may never hide a real regression).
# ---------------------------------------------------------------------------

NIGHT_GREEN = "green"
NIGHT_RED = "red"
NIGHT_NEUTRAL = "neutral"


def classify_gate_nights(records: list[dict]) -> dict[str, str]:
    """Classify each UTC ``date`` in the ledger as green / red / neutral.

    - :data:`NIGHT_RED`: the night has at least one *surviving* fail — a fail
      record that is NOT flagged ``leaker_only`` (a product ``first_fail`` or an
      unattributed red). A real regression can never be demoted to neutral.
    - :data:`NIGHT_NEUTRAL` (leaker-only): the night has fail record(s) and
      EVERY one of them is a ``leaker_only`` harness-noise fail. Such a night is
      transparent to the green streak, the red streak and the triage red-count;
      it surfaces only as low-severity leaker debt.
    - :data:`NIGHT_GREEN`: every record for the date is ``pass``.

    Records without a ``date`` are ignored (they cannot be attributed to a
    night). The classification is global (window-independent), so every consumer
    derives the SAME disposition for the same night — the symmetry the v2 spec
    requires across streak, triage and release brake.
    """
    has_survivor: dict[str, bool] = {}
    has_leaker: dict[str, bool] = {}
    seen: dict[str, bool] = {}
    for rec in records:
        date = str(rec.get("date") or "")
        if not date:
            continue
        seen[date] = True
        if str(rec.get("result", "")).lower() == "pass":
            continue
        if bool(rec.get("leaker_only")):
            has_leaker[date] = True
        else:
            has_survivor[date] = True
    out: dict[str, str] = {}
    for date in seen:
        if has_survivor.get(date):
            out[date] = NIGHT_RED
        elif has_leaker.get(date):
            out[date] = NIGHT_NEUTRAL
        else:
            out[date] = NIGHT_GREEN
    return out


def _latest_record_result(records: list[dict]) -> tuple[Optional[str], Optional[str]]:
    """(result, ts) of the newest record by epoch — for the raw ``last_result``
    surface (unchanged by neutral semantics: it reports what the most recent run
    literally did, while the streak below skips neutral nights)."""
    latest_ts: Optional[str] = None
    latest_epoch = None
    latest_result: Optional[str] = None
    for rec in records:
        epoch = rec.get("epoch")
        if epoch is None:
            try:
                epoch = int(
                    _dt.datetime.fromisoformat(
                        str(rec.get("ts", "")).replace("Z", "+00:00")
                    ).timestamp()
                )
            except (ValueError, TypeError):
                epoch = None
        if epoch is not None and (latest_epoch is None or epoch >= latest_epoch):
            latest_epoch = epoch
            latest_ts = rec.get("ts")
            latest_result = "pass" if str(rec.get("result")).lower() == "pass" else "fail"
    return latest_result, latest_ts


def derive_gate_streak(records: list[dict]) -> dict:
    """Derive the consecutive-green-nights streak from gate records.

    A *night* (one UTC ``date``) is green only if **every** record for that
    date is ``pass``; a surviving ``fail`` makes the whole night red. The streak
    counts back from the most recent recorded night and stops at the first red
    night. Gaps (nights with no record) are simply absent; the streak is
    measured over recorded nights.

    Neutral (leaker-only) nights (:func:`classify_gate_nights`) are TRANSPARENT:
    they neither advance the green streak nor break it, and they are NOT counted
    as green nights — they are tallied separately as ``neutral_nights`` (the
    leaker-debt channel). ``fail_nights`` counts RED nights only (product /
    unattributed reds), so leaker debt never masquerades as a red night either.
    """
    nights = classify_gate_nights(records)
    latest_result, latest_ts = _latest_record_result(records)

    streak = 0
    for date in sorted(nights.keys(), reverse=True):
        disp = nights[date]
        if disp == NIGHT_NEUTRAL:
            continue  # transparent — neither breaks nor advances the green streak
        if disp == NIGHT_GREEN:
            streak += 1
        else:  # NIGHT_RED
            break

    green_nights = sum(1 for v in nights.values() if v == NIGHT_GREEN)
    fail_nights = sum(1 for v in nights.values() if v == NIGHT_RED)
    neutral_nights = sum(1 for v in nights.values() if v == NIGHT_NEUTRAL)
    return {
        "streak": streak,
        "green_nights": green_nights,
        "fail_nights": fail_nights,
        "neutral_nights": neutral_nights,
        "total_recorded_nights": len(nights),
        "last_result": latest_result,
        "last_ts": latest_ts,
    }


def red_streak_from_head(records: list[dict]) -> int:
    """Consecutive RED nights counting back from the most recently recorded
    night — the complementary axis to :func:`derive_gate_streak`'s green
    streak. A night (one UTC ``date``) is red iff ANY record for that date is
    ``fail`` (same rule). Returns 0 when there are no recorded nights or the
    head night is green.

    Used by ``release.pause_on_red_streak`` (``auto_release.maybe_auto_release``,
    S3): a robust check straight over the ledger records rather than the
    precomputed ``vision-metrics.json`` snapshot, which can be stale or
    missing (e.g. before the first ``metrics-snapshot`` run of the day).

    Neutral (leaker-only) nights (:func:`classify_gate_nights`) are TRANSPARENT:
    a leaker-only night neither extends the red streak nor resets it (v2:
    ``verlaengert den Rot-Streak nicht und loest deren Hold nicht``). Counting
    back from the head, a neutral night is skipped so a standing hold is
    preserved — leaker debt can neither build the hold nor dissolve it.
    """
    nights = classify_gate_nights(records)
    streak = 0
    for date in sorted(nights.keys(), reverse=True):
        disp = nights[date]
        if disp == NIGHT_NEUTRAL:
            continue  # transparent — neither extends nor resets the red streak
        if disp == NIGHT_RED:
            streak += 1
        else:  # NIGHT_GREEN
            break
    return streak


# ---------------------------------------------------------------------------
# Flaky de-flake debt (GATE-FLAKY-RETRY-HONESTY-S1)
#
# A ``leakers`` entry on a fail record means that test file FAILED in the
# parallel suite but PASSED on the isolated rerun (fail->pass) — a confirmed
# flake. :func:`derive_gate_streak` already makes such a night streak-NEUTRAL
# (honest: a flake must not reset the green streak). But a neutral night that is
# otherwise never acted on lets a genuinely flaky test accrue as invisible
# leaker debt forever — "dauerhaft gruen-gerechnet". This turns that silent debt
# into accountable, actionable signal:
#
#   * every distinct flaky test FILE the isolation rerun demoted (across
#     leaker-only AND partially-leaky red nights) is a de-flake CANDIDATE; the
#     strategist files exactly one HELD de-flake PlanSpec per file (deduped), so
#     a flake is never silently swallowed — the counter-metric
#     ``flaky_neutralized_without_filed_deflake_task`` must reach 0;
#   * a file flaky-neutralized on >= RECURRING_FLAKE_MIN_NIGHTS DISTINCT nights
#     without a fix ESCALATES into the recurring-flake counter (a stuck flake
#     the operator should prioritise) rather than being permanently green-counted.
#
# A file that FAILS alone too (fail->fail) is NOT a leaker — it is a reproduced
# product failure that stays a red first_fail and never reaches this list. The
# symmetry (a real regression can never be de-flaked away) is preserved by the
# upstream isolation step, not re-derived here.
# ---------------------------------------------------------------------------

RECURRING_FLAKE_MIN_NIGHTS = 3


def flaky_file_key(gate: str, file: str) -> str:
    """Stable per-file de-flake identity (gate token + sha1(file) digest).

    Kept here (not in strategist) so the counter-metric's filed-set membership
    check and the strategist lever key are the SAME function — a flaky file the
    strategist ingests under key K is exactly the K the metric looks up in the
    filed set. Deterministic (sha1); the gate token is upper-alnum so it round-
    trips through a PlanSpec ``slice`` / filename unchanged."""
    digest = hashlib.sha1(str(file).encode("utf-8")).hexdigest()[:8]
    token = re.sub(r"[^A-Z0-9]+", "-", str(gate).upper()).strip("-") or "UNKNOWN"
    return f"GATE-DEFLAKE-{token}-{digest}"


def _split_leaker_entry(entry: str) -> tuple[str, str]:
    """Split a stored ``"<gate>: <file>"`` leaker entry into ``(gate, file)``.

    Robust to the redaction/whitespace-flatten pass the ledger applies: the gate
    is the token before the first ``": "``; the rest is the file path. An entry
    with no separator is a bare file under an ``"unknown"`` gate."""
    s = " ".join(str(entry or "").split())
    if ": " in s:
        gate, _, file = s.partition(": ")
        return (gate.strip().lower() or "unknown"), file.strip()
    return "unknown", s.strip()


def derive_flaky_deflake_candidates(
    records: list[dict],
    *,
    recurring_min_nights: int = RECURRING_FLAKE_MIN_NIGHTS,
) -> list[dict]:
    """Per flaky test FILE, the de-flake debt derived from the ledger.

    Collects every ``leakers`` entry across all fail records (leaker-only AND
    partially-leaky red nights), deduped per file by DATE (a file listed twice
    the same night counts as one flaky night). Returns one dict per file::

        {"file", "gate", "key", "nights", "dates": [...], "recurring": bool}

    ``recurring`` is ``True`` when the file was flaky-neutralized on
    >= ``recurring_min_nights`` DISTINCT nights (AC-2c escalation). Sorted by
    (most nights first, then file) so the worst offenders lead. A fail record
    without a ``leakers`` list contributes nothing; pass records never do."""
    by_file: dict[str, dict] = {}
    for rec in records:
        if str(rec.get("result", "")).lower() != "fail":
            continue
        leakers = rec.get("leakers")
        if not isinstance(leakers, list):
            continue
        date = str(rec.get("date") or "")
        for entry in leakers:
            gate, file = _split_leaker_entry(entry)
            if not file:
                continue
            slot = by_file.setdefault(file, {"file": file, "gate": gate, "dates": set()})
            if date:
                slot["dates"].add(date)
            # First concrete (non-unknown) gate seen wins — a redacted/odd entry
            # must not overwrite a good attribution.
            if slot["gate"] == "unknown" and gate != "unknown":
                slot["gate"] = gate
    threshold = max(1, int(recurring_min_nights))
    out: list[dict] = []
    for file, slot in by_file.items():
        dates = sorted(slot["dates"])
        nights = len(dates)
        out.append(
            {
                "file": file,
                "gate": slot["gate"],
                "key": flaky_file_key(slot["gate"], file),
                "nights": nights,
                "dates": dates,
                "recurring": nights >= threshold,
            }
        )
    out.sort(key=lambda c: (-c["nights"], c["file"]))
    return out


# ---------------------------------------------------------------------------
# Recurring red-cause detection (GREEN-GATE-AUTOHEAL-LOOP-S1)
#
# The streak above answers "how many green nights in a row?". This answers the
# complementary, actionable question the autoheal loop needs: "is the most
# recent run red, AND has it been red for >= N consecutive recorded nights with
# the SAME first_fail cause?". Only then is an operator-gated fix-PlanSpec worth
# opening — a single red night is noise the nightly retry may clear; a repeated
# *same-cause* red night is a real, stuck defect that would otherwise sit on
# green_gate_streak=0 unnoticed until the weekly strategist run.
# ---------------------------------------------------------------------------

# Default: open a fix-PlanSpec only once the same cause has failed this many
# consecutive recorded nights (AC-1 / AC-2: >= 2, never on a single red night).
GATE_FIX_MIN_NIGHTS = 2

# Legacy-night log backfill (GREEN-GATE-AUTOHEAL-LEGACY-NIGHT-S1): the nightly
# heartbeat writes one per-run dir ``<GREEN_GATE_LOG_DIR>/YYYYMMDD-HHMMSS/`` with
# a ``<gate>.log`` each. A red night that predates the first_fail format carries
# no cause in the ledger; to decide whether it shares an attributed head's cause
# we read the TAIL of that night's gate log (the failure summary sits at the end)
# and compare failing-test signatures. Both reads are bounded so a pathological
# log can never balloon the walk.

# Persistent-red N-of-M triage (GREEN-GATE-PERSISTENT-RED-TRIAGE-S1):
# When the head is red AND >=N of the last M recorded nights are red (regardless
# of whether the first_fail cause is the same), open exactly one HELD
# triage-PlanSpec listing the currently-red test files. This is orthogonal to
# the same-cause path above: it catches *changing-cause* persistent reds that
# same-cause dedup would miss.
GATE_TRIAGE_MIN_REDS = 2  # AC-2: never fire on a single isolated flake-night
GATE_TRIAGE_WINDOW = 3  # examine the last M nights (2 of 3)
GREEN_GATE_LOG_DIR_ENV = "GREEN_GATE_LOG_DIR"
GATE_LOG_BACKFILL_MAX_BYTES = 256 * 1024  # tail we read from a legacy gate log
GATE_BACKFILL_MAX_FILES = 200  # cap failing-file tokens parsed from one log


def _record_epoch(rec: dict) -> Optional[int]:
    """Best-effort unix epoch for a ledger record (``epoch`` then ``ts``)."""
    epoch = rec.get("epoch")
    if epoch is not None:
        try:
            return int(epoch)
        except (TypeError, ValueError):
            return None
    try:
        return int(
            _dt.datetime.fromisoformat(
                str(rec.get("ts", "")).replace("Z", "+00:00")
            ).timestamp()
        )
    except (ValueError, TypeError):
        return None


def _prev_sha_index(records: list[dict]) -> list[Optional[str]]:
    """For each ledger record (by position), the nearest EARLIER record's
    ``head_sha`` — any date, any result (pass or fail) — or ``None`` when no
    sha-carrying record precedes it.

    Pure ledger derivation (S3 commit attribution): no git call happens here,
    only a linear scan of whatever ``head_sha`` the caller already recorded.
    A ``pass`` record's sha counts too — it is the correct "prev" anchor for a
    red night that follows a green one.
    """
    out: list[Optional[str]] = []
    prev: Optional[str] = None
    for rec in records:
        out.append(prev)
        sha = str(rec.get("head_sha") or "").strip()
        if sha:
            prev = sha
    return out


def _normalize_cause_detail(detail: str) -> str:
    """Normalize a first-failure detail tail into a STABLE cause token.

    The on-disk ``first_fail.detail`` is a redacted stderr tail that varies
    night to night even for the same root cause (line numbers, counts, paths,
    timestamps). To group "same cause" reliably — and to keep the fingerprint
    safe to embed in a PlanSpec (no YAML/markdown/rubric-tripping characters) —
    everything except lowercase letters, underscore and spaces is dropped (so
    digits, paths, ``<``/``>``/``...`` and quotes never survive), then runs of
    whitespace collapse and the result is bounded. Two nights whose failing
    test/error name matches therefore yield the identical token.
    """
    s = str(detail).lower()
    s = re.sub(r"[^a-z_ ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:160]


def _first_fail_cause(first_fail: Optional[dict]) -> Optional[dict]:
    """Derive the stable ``{gate, fingerprint, detail}`` cause from a record's
    ``first_fail`` payload, or ``None`` when there is no usable payload."""
    if not isinstance(first_fail, dict) or not first_fail:
        return None
    gate = str(first_fail.get("gate") or "").strip().lower() or "unknown"
    detail = str(first_fail.get("detail") or "")
    norm = _normalize_cause_detail(detail)
    fingerprint = f"{gate}|{norm}" if norm else gate
    return {"gate": gate, "fingerprint": fingerprint, "detail": detail}


def _night_cause(
    fails: list[tuple[Optional[int], Optional[dict], bool, int]]
) -> Optional[dict]:
    """Representative cause for one red night: the EARLIEST fail record's
    ``first_fail`` (by epoch; records without an epoch sort last). A night that
    failed with no first_fail payload at all returns ``None``.

    Each ``fails`` entry is ``(epoch, first_fail, leaker_only, record_index)``
    — ``record_index`` (S3) is the fail record's position in the ledger list
    the caller derived from, used elsewhere to look up its ``head_sha`` /
    ``suspect_range``; unused here, kept for a single shared tuple shape.
    """
    for _ep, ff, _leaker_only, _idx in sorted(fails, key=_night_sort_key):
        cause = _first_fail_cause(ff)
        if cause is not None:
            return cause
    return None


def _night_sort_key(
    item: tuple[Optional[int], Optional[dict], bool, int]
) -> tuple[bool, int]:
    ep = item[0]
    return (ep is None, ep if ep is not None else 0)


def _night_cause_and_purity(
    fails: list[tuple[Optional[int], Optional[dict], bool, int]]
) -> tuple[Optional[dict], bool]:
    """Cause + ``pure_leaker`` flag for one red night.

    ``pure_leaker`` is ``True`` when the night has fail records, none yields a
    product ``first_fail`` cause, and at least one was flagged ``leaker_only``
    (every reported fail passed alone — harness noise). Such a night must never
    contribute a healable cause: it is red (the streak stays 0) but there is
    nothing for the autoheal loop to fix. A genuinely *unattributed* red (no
    payload, not leaker-flagged) is NOT pure_leaker — it still coalesces as the
    ``"unknown"`` sentinel cause, preserving the existing behaviour.
    """
    cause = _night_cause(fails)
    if cause is not None:
        return cause, False
    has_leaker_only = any(flag for _ep, _ff, flag, _idx in fails)
    return None, (len(fails) > 0 and has_leaker_only)


# ---------------------------------------------------------------------------
# Legacy-night log backfill helpers (GREEN-GATE-AUTOHEAL-LEGACY-NIGHT-S1)
# ---------------------------------------------------------------------------

# Failing-test files appear ONLY in failure context: the run_tests.sh summary
# block (``  tests/x.py  (k test failed)``) and pytest ``FAILED tests/x.py::…``
# lines. Matching just these two shapes means the thousands of PASSED lines in a
# full log (``✓ tests/x.py (…)``) never pollute the signature.
_FAIL_SUMMARY_FILE_RE = re.compile(
    r"^\s+(tests/[\w./-]+\.py)\s+\(\d+\s+test", re.MULTILINE
)
_PYTEST_FAILED_RE = re.compile(r"^FAILED\s+(tests/[\w./-]+\.py)\b", re.MULTILINE)


def _extract_failing_test_files(text: Optional[str]) -> set[str]:
    """Pull the set of FAILING test-file paths out of a run_tests.sh log/detail.

    Only failure-context lines feed the set (see the regexes above), so a full
    nightly log's passing lines are never mistaken for failures. Bounded to
    :data:`GATE_BACKFILL_MAX_FILES` tokens. Returns an empty set when nothing
    failure-shaped is present (the caller then declines to backfill)."""
    if not text:
        return set()
    files: set[str] = set()
    for rx in (_FAIL_SUMMARY_FILE_RE, _PYTEST_FAILED_RE):
        for match in rx.finditer(text):
            files.add(match.group(1))
            if len(files) >= GATE_BACKFILL_MAX_FILES:
                return files
    return files


def _same_recurring_cause(prev_files: set[str], head_files: set[str]) -> bool:
    """True iff a log-confirmed un-attributed night shares the head's cause.

    Sound + conservative: one failing-file set must be a SUBSET of the other —
    the recurring core is identical and only the boundary churns (a test the head
    newly broke, or newly fixed). A predecessor that failed on a DISJOINT or only
    partially-overlapping set is a *different* cause and breaks the chain (AC-2).
    Both sets must be non-empty: we never 'confirm' a shared cause from a log we
    could extract no failure signature from (empty ⊆ anything would falsely heal).
    """
    if not prev_files or not head_files:
        return False
    return prev_files <= head_files or head_files <= prev_files


def _green_gate_log_root() -> Path:
    """Root dir the nightly green-gate heartbeat writes per-run logs to.

    Mirrors the heartbeat's ``GREEN_GATE_LOG_DIR`` env (default
    ``<hermes_root>/logs/green-gate``) so the autoheal backfill reads exactly the
    logs the heartbeat produced."""
    override = os.environ.get(GREEN_GATE_LOG_DIR_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    from hermes_constants import get_default_hermes_root

    return get_default_hermes_root() / "logs" / "green-gate"


def _read_night_gate_log(root: Path, date: str, *, gate: str = "python") -> Optional[str]:
    """Best-effort read of the per-night green-gate ``<gate>.log`` for ``date``.

    The heartbeat names each run dir ``YYYYMMDD-HHMMSS``; this locates the dir(s)
    whose date-part equals ``date`` (the latest run that night wins) and returns
    the TAIL of ``<gate>.log`` (bounded to :data:`GATE_LOG_BACKFILL_MAX_BYTES` —
    the failure summary lives at the end). Returns ``None`` when nothing is found
    or readable, so the caller treats the night as *unconfirmed* and declines to
    backfill (idle stays the safe default — never a guessed heal)."""
    compact = str(date or "").replace("-", "")
    if not compact:
        return None
    try:
        if not root.exists():
            return None
        candidates = sorted(
            (p for p in root.glob(f"{compact}-*") if p.is_dir()), reverse=True
        )
    except OSError:
        return None
    for run_dir in candidates:
        try:
            raw = (run_dir / f"{gate}.log").read_bytes()
        except OSError:
            continue
        if len(raw) > GATE_LOG_BACKFILL_MAX_BYTES:
            raw = raw[-GATE_LOG_BACKFILL_MAX_BYTES:]
        return raw.decode("utf-8", "ignore")
    return None


def default_night_log_reader(
    log_root: Optional[Path] = None, *, gate: str = "python"
) -> Callable[[str, list], Optional[str]]:
    """A night-log reader bound to the green-gate log root, for the backfill in
    :func:`derive_consecutive_red_cause`. Reads the per-night ``<gate>.log``;
    returns ``None`` for any night whose log is missing/unreadable."""
    root = log_root or _green_gate_log_root()

    def _reader(date: str, _fails: list) -> Optional[str]:
        return _read_night_gate_log(root, date, gate=gate)

    return _reader


def derive_consecutive_red_cause(
    records: list[dict],
    *,
    min_nights: int = GATE_FIX_MIN_NIGHTS,
    night_log_reader: Optional[Callable[[str, list], Optional[str]]] = None,
) -> Optional[dict]:
    """Detect a recurring *same-cause* red streak at the head of the ledger.

    A *night* (one UTC ``date``) is red if any record for that date is ``fail``
    (mirrors :func:`derive_gate_streak`). Walking back from the most recent
    recorded night, this returns the shared cause iff:

    * the most recent recorded night is red, AND
    * the last ``min_nights`` (>= the default 2) consecutive recorded nights are
      ALL red with the SAME first_fail fingerprint.

    The first green night, or the first red night with a *different* fingerprint,
    ends the run. A red night that carries no first_fail payload is treated as
    the sentinel fingerprint ``"unknown"`` (so repeated unattributed reds still
    coalesce, but never merge with an attributed cause). Returns ``None`` when no
    such recurring cause exists — the signal the autoheal loop reads as "nothing
    to open" (idle is the correct, no-spam outcome).

    Legacy-night log backfill (GREEN-GATE-AUTOHEAL-LEGACY-NIGHT-S1): when an
    ATTRIBUTED head is directly preceded by a red but un-attributed night (one
    that predates the first_fail format, like the live 06-20/06-21 case), the
    ``"unknown"`` sentinel would otherwise break the chain at length 1. If a
    ``night_log_reader`` is supplied, the predecessor's on-disk gate log is read
    and the night is adopted into the chain ONLY when its log proves it shares the
    head's failing-test signature (:func:`_same_recurring_cause`) — bounded, and
    purely for the HELD detection. A predecessor whose log shows a different cause
    (or no signature) still ends the run. Without a reader the behaviour is
    unchanged (pure ledger), and an UN-attributed head keeps the legacy
    ``"unknown"`` coalescing untouched.

    The returned dict — ``{gate, fingerprint, detail, red_nights, dates,
    suspect_ranges}`` — is pure detection; the volatile ``red_nights``/
    ``detail``/``suspect_ranges`` are for logging only. The stable ``gate`` +
    ``fingerprint`` are what the idempotent ingest keys on.

    Commit attribution (S3): ``suspect_ranges`` is
    ``[{"date": <str>, "range": "<prev_sha>..<sha>" | None}, ...]`` — one entry
    per matched date (same order as ``dates``). ``sha`` is the ``head_sha`` of
    that night's representative fail record (the same earliest-fail record
    :func:`_night_cause` reads for the cause); ``prev_sha`` is the nearest
    EARLIER ledger record's ``head_sha`` (any date, any result). Either side
    missing (older ledger entries predate the ``head_sha`` field) yields
    ``range: None`` — graceful, no git call, pure ledger derivation.
    """
    per_date: dict[str, dict] = {}
    for idx, rec in enumerate(records):
        date = str(rec.get("date") or "")
        if not date:
            continue
        slot = per_date.setdefault(date, {"red": False, "fails": []})
        if str(rec.get("result")).lower() != "pass":
            slot["red"] = True
            slot["fails"].append(
                (_record_epoch(rec), rec.get("first_fail"), bool(rec.get("leaker_only")), idx)
            )

    if not per_date:
        return None

    dates_desc = sorted(per_date.keys(), reverse=True)
    head = per_date[dates_desc[0]]
    if not head["red"]:
        return None  # gate is currently green at the head — nothing to heal

    target_cause, head_pure_leaker = _night_cause_and_purity(head["fails"])
    if head_pure_leaker:
        # head night is red but pure test-isolation noise — nothing to heal
        # (the whole point: a leaker must never mint a fix-PlanSpec).
        return None
    target_fp = target_cause["fingerprint"] if target_cause else "unknown"
    # Only an ATTRIBUTED head can adopt an un-attributed predecessor via log
    # backfill; an un-attributed head keeps the legacy "unknown" coalescing.
    head_files = (
        _extract_failing_test_files(target_cause["detail"])
        if (night_log_reader is not None and target_cause is not None)
        else set()
    )

    matched: list[str] = []
    for date in dates_desc:
        slot = per_date[date]
        if not slot["red"]:
            break
        cause, pure_leaker = _night_cause_and_purity(slot["fails"])
        if pure_leaker:
            break  # a harness-noise night is not part of a real recurring cause
        if cause is not None:
            if cause["fingerprint"] != target_fp:
                break
            matched.append(date)
            continue
        # Un-attributed red night (no first_fail payload).
        if target_fp == "unknown":
            # legacy behaviour: unattributed reds coalesce under the sentinel
            matched.append(date)
            continue
        # Head is attributed but this older night carries no cause. Backfill from
        # its on-disk gate log: adopt it ONLY when the log proves it shares the
        # head's failing-test signature (HELD-only, bounded). Otherwise the chain
        # ends here exactly as before — never a guessed merge.
        if head_files and night_log_reader is not None:
            prev_files = _extract_failing_test_files(night_log_reader(date, slot["fails"]))
            if _same_recurring_cause(prev_files, head_files):
                matched.append(date)
                continue
        break

    if len(matched) < max(1, int(min_nights)):
        return None

    prev_sha_at = _prev_sha_index(records)
    suspect_ranges: list[dict] = []
    for date in matched:
        fails = per_date[date]["fails"]
        rng = None
        if fails:
            rep = sorted(fails, key=_night_sort_key)[0]
            rep_idx = rep[3]
            sha = str(records[rep_idx].get("head_sha") or "").strip() or None
            prev_sha = prev_sha_at[rep_idx]
            if sha and prev_sha:
                rng = f"{prev_sha}..{sha}"
        suspect_ranges.append({"date": date, "range": rng})

    return {
        "gate": target_cause["gate"] if target_cause else "unknown",
        "fingerprint": target_fp,
        "detail": target_cause["detail"] if target_cause else "",
        "red_nights": len(matched),
        "dates": matched,
        "suspect_ranges": suspect_ranges,
    }


def derive_persistent_red_triage(
    records: list[dict],
    *,
    min_reds: int = GATE_TRIAGE_MIN_REDS,
    window: int = GATE_TRIAGE_WINDOW,
) -> Optional[dict]:
    """N-of-M persistent-red triage trigger (GREEN-GATE-PERSISTENT-RED-TRIAGE-S1).

    Orthogonal to :func:`derive_consecutive_red_cause` (which requires the SAME
    first_fail fingerprint on consecutive nights): this fires when the head is
    red AND >= ``min_reds`` of the last ``window`` recorded nights are red on
    the **same head/anchor gate** — regardless of whether the first_fail cause
    changed between nights of that gate. The changing-cause case is exactly
    what the same-cause path deliberately skips, leaving the operator with a
    persistent red head and no triage item.

    Gate-local honesty (2026-07-21 RCA): red nights of a *different* gate must
    never pad the N-of-M count or supply the anchor file set. A Vitest head
    without extractable ``tests/*.py`` paths therefore cannot fall back onto an
    older Python night and mint a false ``GATE-TRIAGE-PYTHON-*`` PlanSpec.

    Leaker-only nights (GATE-LEAKER-STREAK-HONESTY-V2) are NEUTRAL and fully
    transparent here: they are excluded from the window, never counted toward
    ``min_reds``, and a neutral head (the most recent recorded night is a
    leaker-only red) never opens a triage — the "head" is the most recent
    *meaningful* (green/red) night. Test-isolation debt can therefore neither
    mint a triage PlanSpec on its own nor pad an N-of-M count.

    Returns ``None`` (idle) when:
      - the most recent meaningful night is green (no red head to triage), or
      - there is no meaningful night at all (empty / all leaker-only), or
      - fewer than ``min_reds`` red nights of the head gate in the window
        (isolated flake / mixed-gate window — AC-2 / gate-local guard).

    When triggered, returns::

        {
            "gate": <str>,                  # the ANCHOR night's gate (head's own,
                                             # or same-gate unknown fallback — see below)
            "red_files": set[str],          # the ANCHOR night's failing test files
                                             # (head night's, or — only when head gate
                                             # is un-attributed 'unknown' — the most
                                             # recent SAME-GATE window night that HAS
                                             # concrete files)
            "red_files_window_union": set[str],  # UNION across every same-gate red
                                             # night in the window (additive; dashboards)
            "red_files_by_night": [
                {"date": <str>, "files": [<str>, ...], "suspect_range": <str|None>},
                ...
            ],                               # one entry per same-gate red night in the
                                              # window, oldest-to-newest order
            "fingerprint": <str>,     # sha1 of the ANCHOR night's red-file set (dedup key)
            "red_count": <int>,       # same-gate reds in the window
            "window": <int>,          # M
            "dates": [<str>, ...],    # dates of the same-gate red nights in the window
        }

    Actionable-anchor (unknown head only): the operator-facing ``red_files``
    and ``gate`` — and the fingerprint that keys the PlanSpec — anchor on the
    HEAD night when it carries concrete failing files. When the head gate is
    un-attributed ``unknown`` and its own file set is empty, the anchor falls
    back to the most RECENT **same-gate** red night in the window that DOES
    carry concrete files. An *attributed* head (e.g. ``vitest``) with no
    extractable product files keeps its own gate and empty file set — never
    steals files or gate identity from a different older gate.

    ``red_files``-honesty (S3): with chronic drift (a DIFFERENT test broke each
    red night — the common case per the 2026-07-05 log forensics), the
    pre-S3 behaviour reported only the HEAD night's failing files as
    ``red_files``, which reads as "this ONE set of files has been failing
    repeatedly" even though the underlying cause changed every night. That is
    exactly what the same-cause path (:func:`derive_consecutive_red_cause`)
    is for; this trigger is the changing-cause complement, so its operator
    surface must not silently imply repetition. The per-night breakdown lives
    (additively) in ``red_files_by_night``, and the full window is available as
    ``red_files_window_union`` — for dashboards/digest consumers, NOT the
    idempotent PlanSpec body (a union drifts on every window slide, see below).

    Fingerprint stays anchored to the ANCHOR night's own red-file set only (NOT
    the window union). The union widens/narrows on every window slide even when
    nothing about the underlying failures changed (a night falls out of the
    window on one side, a new one enters on the other), so hashing the union
    would open a fresh triage chain purely from window movement, not new
    information — the "Re-Trigger-Flut" this fix must NOT introduce. The anchor
    keeps the existing, already-tested dedup behaviour: two consecutive runs
    whose anchor night shows the same red files still hit ``already_ingested``;
    an anchor whose OWN red files genuinely change still (correctly) opens a
    fresh chain — new information for the operator.
    """
    if not records:
        return None
    nights = classify_gate_nights(records)
    # Neutral (leaker-only) nights are transparent: excluded from the window and
    # from the head check (GATE-LEAKER-STREAK-HONESTY-V2). Only green/red nights
    # are "meaningful"; the window is the last ``window`` meaningful nights.
    meaningful_dates = [d for d in sorted(nights) if nights[d] != NIGHT_NEUTRAL]
    if not meaningful_dates:
        return None
    head_date = meaningful_dates[-1]
    if nights[head_date] != NIGHT_RED:
        return None  # head is green (or only leaker-noise since) — nothing to triage
    recent_dates = meaningful_dates[-window:] if window > 0 else meaningful_dates
    # Group the RED nights' fail records (index preserved for suspect_range sha
    # lookup). A leaker-only fail that shares a RED night carries no first_fail,
    # so it contributes no files — harmless, and never a phantom cause.
    # NEUTRAL (leaker-only) nights are already excluded from red_dates by the
    # NIGHT classification, so only genuinely-RED nights reach this path.
    red_records_by_date: dict[str, list[tuple[int, dict]]] = {}
    for idx, rec in enumerate(records):
        date = str(rec.get("date") or "")
        if not date or nights.get(date) != NIGHT_RED:
            continue
        if str(rec.get("result", "")).lower() == "pass":
            continue
        red_records_by_date.setdefault(date, []).append((idx, rec))

    def _night_records(date: str) -> list[tuple[int, dict]]:
        return sorted(
            red_records_by_date.get(date, []),
            key=lambda ir: (_record_epoch(ir[1]) is None, _record_epoch(ir[1]) or 0),
        )

    def _night_gate_and_files(date: str) -> tuple[str, set[str]]:
        """Representative gate + extractable files for one red night.

        Gate is the earliest non-empty ``first_fail.gate`` on that night
        (unknown when nothing is attributed). Files are the union of
        extractable failing test paths across that night's fail records.
        """
        files: set[str] = set()
        gate = "unknown"
        for _idx, rec in _night_records(date):
            ff = rec.get("first_fail") or {}
            files |= _extract_failing_test_files(ff.get("detail"))
            g = str(ff.get("gate") or "").strip().lower()
            if g and gate == "unknown":
                gate = g
        return gate, files

    # Head gate first: N-of-M and window surfaces are gate-local to this gate.
    head_gate, head_files = _night_gate_and_files(head_date)

    # ``min_reds`` counts distinct SAME-GATE RED nights only — leaker-only
    # nights are already filtered out, and other-gate reds never pad the count
    # (AC-2 / gate-local). Distinct NIGHTS, not fail records: a single noisy
    # night with multiple fail records is still one.
    window_red_dates = [d for d in recent_dates if nights[d] == NIGHT_RED]
    red_dates = [
        d for d in window_red_dates if _night_gate_and_files(d)[0] == head_gate
    ]
    if len(red_dates) < max(1, int(min_reds)):
        return None

    # Codex review 2026-07-06 finding 2: ``min_reds`` counts distinct red
    # NIGHTS (UTC dates), not fail records — a single noisy night with
    # multiple recorded fails must not satisfy an N-of-M *nights* trigger.
    # Gate-local: only nights that match the head gate participate.
    if len(red_dates) < min_reds:
        return None

    # AC-1 actionability at an unattributed head only. When the HEAD gate is
    # un-attributed ``unknown`` and carries no extractable product files,
    # anchor on the most RECENT *same-gate* red night in the window that DOES
    # carry concrete red files. Attributed heads (e.g. vitest) keep their own
    # gate + empty file set — never steal an older different gate's files.
    # NEUTRAL nights never reach this path (excluded by NIGHT_RED filter above).
    anchor_files, anchor_gate = head_files, head_gate
    if not anchor_files and head_gate == "unknown":
        for date in reversed(red_dates):
            _g, files = _night_gate_and_files(date)
            if files:
                anchor_files = files
                anchor_gate = _g
                break

    fingerprint_source = "|".join(sorted(anchor_files)) if anchor_files else anchor_gate
    fingerprint = hashlib.sha1(fingerprint_source.encode("utf-8")).hexdigest()

    prev_sha_at = _prev_sha_index(records)
    red_files_window_union: set[str] = set()
    red_files_by_night: list[dict] = []
    for date in sorted(red_dates):
        recs = _night_records(date)
        _g, files = _night_gate_and_files(date)
        red_files_window_union |= files
        rng = None
        if recs:
            # suspect_range anchors on the night's earliest record — same
            # representative convention as _night_cause.
            rep_idx = recs[0][0]
            sha = str(records[rep_idx].get("head_sha") or "").strip() or None
            prev_sha = prev_sha_at[rep_idx]
            if sha and prev_sha:
                rng = f"{prev_sha}..{sha}"
        red_files_by_night.append(
            {"date": date, "files": sorted(files), "suspect_range": rng}
        )
    return {
        "gate": anchor_gate,
        "red_files": set(anchor_files),
        "red_files_window_union": red_files_window_union,
        "red_files_by_night": red_files_by_night,
        "fingerprint": fingerprint,
        "red_count": len(red_dates),
        "window": window,
        "dates": sorted(red_dates),
    }


# ---------------------------------------------------------------------------
# DB-derived metrics
# ---------------------------------------------------------------------------

_FAILED_RUN_OUTCOMES = frozenset(
    {
        "blocked",
        "crashed",
        "gave_up",
        "iteration_budget_exhausted",
        "spawn_failed",
        "timed_out",
    }
)


def _tasks_with_operator_escalation(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT DISTINCT task_id FROM task_events WHERE kind = ?",
        (kb.OPERATOR_ESCALATION_EVENT,),
    ).fetchall()
    return {r["task_id"] for r in rows}


def _tasks_with_nontransient_heiler(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT task_id, payload FROM task_events WHERE kind = ?",
        (kb.HEILER_CLASSIFICATION_EVENT,),
    ).fetchall()
    out: set[str] = set()
    for r in rows:
        try:
            payload = json.loads(r["payload"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(payload, dict) and payload.get("class") in _NON_TRANSIENT_HEILER_CLASSES:
            out.add(r["task_id"])
    return out


def _tasks_with_failed_run(conn: sqlite3.Connection) -> set[str]:
    placeholders = ",".join("?" * len(_FAILED_RUN_OUTCOMES))
    rows = conn.execute(
        f"SELECT DISTINCT task_id FROM task_runs WHERE outcome IN ({placeholders})",
        tuple(sorted(_FAILED_RUN_OUTCOMES)),
    ).fetchall()
    return {r["task_id"] for r in rows}


def _autonomy_metric(conn: sqlite3.Connection) -> dict:
    """Autonomie-% ↔ counter 'should_have_escalated_but_didnt'.

    Autonomous = a done task that never raised an ``operator_escalation`` event
    AND has no failed ``task_runs.outcome``. The paired counter is the subset of
    those "autonomous" tasks that nonetheless carry a non-transient
    ``heiler_classification`` (real-bug / bad-spec / conflict): the system saw
    a real problem and still didn't escalate.
    """
    escalated = _tasks_with_operator_escalation(conn)
    flagged = _tasks_with_nontransient_heiler(conn)
    failed = _tasks_with_failed_run(conn)
    rows = conn.execute("SELECT id FROM tasks WHERE status = 'done'").fetchall()
    total_done = len(rows)
    autonomous = 0
    should_have = 0
    for r in rows:
        tid = r["id"]
        if tid not in escalated and tid not in failed:
            autonomous += 1
            if tid in flagged:
                should_have += 1
    pct = round(100.0 * autonomous / total_done, 1) if total_done else None
    return {
        "autonomy_pct": pct,
        "autonomous_done": autonomous,
        "total_done": total_done,
        "counter": {
            "name": "should_have_escalated_but_didnt",
            "value": should_have,
            "description": (
                "done tasks counted autonomous that carry a non-transient "
                "heiler_classification (real-bug/bad-spec/conflict) yet never "
                "raised operator_escalation"
            ),
        },
    }


def _escalation_rate_metric(
    conn: sqlite3.Connection, *, now: int, window_days: int
) -> dict:
    """Eskalations-Rate (per week) ↔ counter 'silent_blocks'.

    Headline = count of distinct tasks with ``operator_escalation`` events
    inside the window. ``error_escalations_per_week`` reports the same rate
    relieved of the false-positive operator gates (held-before-release /
    operator-hold / human-input parks classified ``operator-gated`` or the other
    terminal non-error classes) — the AC-1 "escalation rate entlastet" number,
    while the raw headline still counts every escalated task (AC-2: the
    operator-facing escalation is preserved).
    Counter = *settled* blocked tasks (the self-healing retry lane is done with
    them) that have NO escalation event — blocks that bypass the operator while
    looking like progress from outside (SILENT-BLOCK-GUARD-S1). Transient blocks
    the auto-retry lane is still working are excluded: they are being handled, so
    they are not "silent", and escalating them would flood the operator (AC-2).
    Computed via :func:`kb.silent_block_task_ids`, the same predicate
    :func:`kb.escalate_silent_blocks_sweep` uses to fix the gap, so the metric
    converges to 0 within one dispatcher tick and the two cannot drift.
    """
    window = window_days * DAY_SECONDS
    cutoff = now - window
    row = conn.execute(
        "SELECT COUNT(DISTINCT task_id) AS n FROM task_events "
        "WHERE kind = ? AND created_at >= ?",
        (kb.OPERATOR_ESCALATION_EVENT, cutoff),
    ).fetchone()
    escalations = int(row["n"] or 0) if row else 0

    # ESCALATION-OPERATOR-GATE-DECLASSIFY-S1: the raw headline counts EVERY
    # escalated task (the operator-facing escalation is preserved, AC-2), but a
    # held-before-release / operator-hold gate is not an error. Derive each
    # escalation's Heiler class from its own persisted evidence (the same
    # deterministic function the classifier + sweep use) and report the
    # error-only rate — distinct tasks with at least one escalation whose class
    # is NOT a terminal non-error class — so the escalation rate can be read
    # relieved of the false-positive operator gates.
    error_tasks: set[str] = set()
    for r in conn.execute(
        "SELECT task_id, payload FROM task_events "
        "WHERE kind = ? AND created_at >= ?",
        (kb.OPERATOR_ESCALATION_EVENT, cutoff),
    ).fetchall():
        try:
            payload = json.loads(r["payload"] or "{}")
        except (json.JSONDecodeError, TypeError):
            payload = {}
        cls, _ = kb._classify_escalation_payload(
            payload if isinstance(payload, dict) else {}
        )
        if cls not in _NON_ERROR_HEILER_CLASSES:
            error_tasks.add(r["task_id"])
    error_escalations = len(error_tasks)

    # Uses the auto-retry defaults (== live config: failure_limit/backoff/
    # retry_limit), so the settled set matches what the dispatcher sweep
    # computes. If those config values ever diverge from the defaults, this
    # headline could mis-count boundary cases briefly (it is a measurement that
    # converges within a tick, not a gate) — feed the config in here if exactness
    # matters then.
    silent = len(kb.silent_block_task_ids(conn, now=now))
    return {
        "escalations_per_week": escalations,
        "error_escalations_per_week": error_escalations,
        "window_days": window_days,
        "counter": {
            "name": "silent_blocks",
            "value": silent,
            "description": (
                "settled blocked tasks (self-healer done) with no "
                "operator_escalation event — blocks that bypass the operator; "
                "transient self-healing retries are excluded"
            ),
        },
    }


def _classification_coverage_metric(
    conn: sqlite3.Connection, *, now: int, window_days: int
) -> dict:
    """Klassifikations-Abdeckung ↔ counter 'operator_corrected_pct'.

    Headline = share of ``operator_escalation`` events in the window that
    received a paired ``heiler_classification`` (one referencing the escalation
    via ``escalation_event_id``) WITHIN 24h. The Stratege's ``by_class`` input
    is only trustworthy when this is high — the live ledger had starved to 0%
    (9 escalations/week, 0 classified) before the safety-net sweep.

    Counter = share of *classified* escalations the operator later corrected
    (``heiler_classification_corrected``): the auto-misclassification guardrail,
    held under 10%. 0 corrections → 0.0 (trivially held until a human overrides).
    """
    window = window_days * DAY_SECONDS
    cutoff = now - window

    escalations = conn.execute(
        "SELECT id, created_at FROM task_events "
        "WHERE kind = ? AND created_at >= ?",
        (kb.OPERATOR_ESCALATION_EVENT, cutoff),
    ).fetchall()
    total = len(escalations)

    # escalation_event_id -> earliest classification created_at
    classified_at: dict[int, int] = {}
    for r in conn.execute(
        "SELECT payload, created_at FROM task_events WHERE kind = ?",
        (kb.HEILER_CLASSIFICATION_EVENT,),
    ).fetchall():
        try:
            payload = json.loads(r["payload"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        ref = payload.get("escalation_event_id")
        if ref is None or r["created_at"] is None:
            continue
        try:
            ref = int(ref)
        except (TypeError, ValueError):
            continue
        ca = int(r["created_at"])
        prev = classified_at.get(ref)
        if prev is None or ca < prev:
            classified_at[ref] = ca

    corrected: set[int] = set()
    for r in conn.execute(
        "SELECT payload FROM task_events WHERE kind = ?",
        (kb.HEILER_CLASSIFICATION_CORRECTED_EVENT,),
    ).fetchall():
        try:
            payload = json.loads(r["payload"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(payload, dict) and payload.get("escalation_event_id") is not None:
            try:
                corrected.add(int(payload["escalation_event_id"]))
            except (TypeError, ValueError):
                pass

    covered = 0
    classified_in_window = 0
    corrected_in_window = 0
    for e in escalations:
        eid = int(e["id"])
        eca = int(e["created_at"]) if e["created_at"] is not None else None
        cat = classified_at.get(eid)
        if cat is None:
            continue
        classified_in_window += 1
        if eid in corrected:
            corrected_in_window += 1
        if eca is None or cat <= eca + DAY_SECONDS:
            covered += 1

    coverage_pct = round(100.0 * covered / total, 1) if total else None
    corrected_pct = (
        round(100.0 * corrected_in_window / classified_in_window, 1)
        if classified_in_window else 0.0
    )
    # M2: Klassifikations-Qualitaet — Anteil 'unclassified' an den Klassifikationen
    # im Fenster. Hoch = by_class untrustworthy. Die 24h-coverage oben ist durch den
    # Auto-Sweep trivial saturiert; dies ist das echte Vertrauenssignal fuer den Strategen.
    cls_counts: dict[str, int] = {}
    for r in conn.execute(
        "SELECT payload FROM task_events WHERE kind = ? AND created_at >= ?",
        (kb.HEILER_CLASSIFICATION_EVENT, cutoff),
    ).fetchall():
        try:
            p = json.loads(r["payload"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(p, dict) and p.get("class"):
            cls_counts[p["class"]] = cls_counts.get(p["class"], 0) + 1
    classified_total = sum(cls_counts.values())
    unclassified_share = (
        round(100.0 * cls_counts.get(kb.HEILER_CLASS_UNCLASSIFIED, 0) / classified_total, 1)
        if classified_total else None
    )
    return {
        "coverage_pct": coverage_pct,
        "unclassified_share": unclassified_share,
        "classified_total": classified_total,
        "escalations": total,
        "classified_within_24h": covered,
        "window_days": window_days,
        "counter": {
            "name": "operator_corrected_pct",
            "value": corrected_pct,
            "description": (
                "share of classified escalations the operator later corrected "
                "(heiler_classification_corrected) — the misclassification "
                "guardrail; held under 10%"
            ),
        },
    }


def _cost_per_task_metric(
    conn: sqlite3.Connection, *, now: int, window_days: int
) -> dict:
    """Kosten/Task-Trend ↔ counter 'tasks_without_cost_data'.

    Cost per task = SUM(task_runs.cost_usd) grouped by task. The trend
    averages ONLY tasks that carry a real *metered* cost (summed cost > 0).

    Subscription-included runs are stamped ``$0`` (the COST-VISIBILITY-WORKERS
    backfill: quota burn rides the subscription, not a metered card). Folding
    those zeros into the average drags it toward 0 and manufactures a phantom
    "‑100 % savings" the instant a recent window fills with subscription work —
    the artifact this metric must not produce. A ``$0`` stamp is *no metered
    cost*, not a saving, so subscription-only tasks are excluded from the
    average and surfaced as explicit ``coverage`` instead.

    The paired counter (``tasks_without_cost_data``) is every done task without
    a real metered cost — subscription-``$0`` PLUS NULL. It shrinks ONLY when a
    task gains real metered cost, never merely because a NULL run was stamped
    ``$0``; that keeps the coverage number from dropping by hiding tasks.
    """
    window = window_days * DAY_SECONDS

    # SUM(cost_usd) + COUNT(non-NULL cost_usd) per task across all runs. NULL
    # costs are ignored by SUM/COUNT; a task whose runs are *all* stamped $0
    # (subscription-included) has n_cost > 0 but total == 0.0. Real metered
    # cost is always strictly positive (no API call bills $0), so total > 0
    # cleanly separates metered tasks from subscription-$0 ones.
    cost_rows = conn.execute(
        "SELECT task_id, SUM(cost_usd) AS total, "
        "COUNT(cost_usd) AS n_cost "
        "FROM task_runs GROUP BY task_id"
    ).fetchall()
    metered_by_task: dict[str, float] = {}  # real metered cost (> 0)
    subscription_tasks: set[str] = set()  # stamped, but summed exactly $0
    for r in cost_rows:
        n_cost = r["n_cost"] or 0
        total = r["total"]
        if n_cost <= 0 or total is None:
            continue  # no non-NULL cost rows -> no cost data
        total = float(total)
        if total > 0:
            metered_by_task[r["task_id"]] = total
        else:
            subscription_tasks.add(r["task_id"])
    cost_total = round(sum(metered_by_task.values()), 6)

    done_rows = conn.execute(
        "SELECT id, completed_at FROM tasks WHERE status = 'done'"
    ).fetchall()

    def _avg_in_window(lo: int, hi: int) -> Optional[float]:
        # average over metered (cost > 0) tasks only -> never includes a $0
        # subscription stamp, so the result is always strictly positive when
        # non-None and pct_change can never be the misleading -100%.
        costs = [
            metered_by_task[r["id"]]
            for r in done_rows
            if r["completed_at"] is not None
            and lo <= r["completed_at"] < hi
            and r["id"] in metered_by_task
        ]
        if not costs:
            return None
        return round(sum(costs) / len(costs), 6)

    recent = _avg_in_window(now - window, now + 1)
    prior = _avg_in_window(now - 2 * window, now - window)

    if recent is None or prior is None:
        trend = "n/a"
        pct_change = None
        trend_basis = "insufficient_metered_data"
    elif recent > prior:
        trend = "up"
        pct_change = round(100.0 * (recent - prior) / prior, 1) if prior else None
        trend_basis = "ok"
    elif recent < prior:
        trend = "down"
        pct_change = round(100.0 * (recent - prior) / prior, 1) if prior else None
        trend_basis = "ok"
    else:
        trend = "flat"
        pct_change = 0.0
        trend_basis = "ok"

    total_done = len(done_rows)
    with_metered = sum(1 for r in done_rows if r["id"] in metered_by_task)
    subscription_only = sum(
        1 for r in done_rows if r["id"] in subscription_tasks
    )
    # Every done task without real metered cost: subscription-$0 OR NULL.
    without_cost = total_done - with_metered
    no_cost_data = without_cost - subscription_only
    coverage_pct = (
        round(100.0 * with_metered / total_done, 1) if total_done else None
    )
    return {
        "cost_usd_total": cost_total,
        "tasks_with_cost": with_metered,
        "recent_avg_cost_per_task": recent,
        "prior_avg_cost_per_task": prior,
        "trend": trend,
        "trend_basis": trend_basis,
        "pct_change": pct_change,
        # Explicit coverage so a $0 subscription stamp can never masquerade as
        # either a saving (in the average) or improved coverage (in the count).
        "coverage": {
            "total_done": total_done,
            "with_metered_cost": with_metered,
            "subscription_only": subscription_only,
            "no_cost_data": no_cost_data,
            "coverage_pct": coverage_pct,
        },
        "counter": {
            "name": "tasks_without_cost_data",
            "value": without_cost,
            "description": (
                "done tasks with no real metered cost — subscription-stamped "
                "$0 runs PLUS NULL-cost runs; the cost trend's blind spot. "
                "Shrinks only when a task gains real metered cost, never when "
                "a subscription run is stamped $0"
            ),
        },
    }


def _green_gate_metric(
    gate_records: list[dict], *, deflake_filed: Optional[set] = None
) -> dict:
    """Green-Gate-Streak ↔ counter 'fail_nights' + leaker-debt + flake-debt.

    Headline = consecutive green nights from the ledger. Counter = total
    recorded RED nights (product / unattributed reds — the streak's antagonist).

    Leaker-debt (GATE-LEAKER-STREAK-HONESTY-V2): nights whose ONLY gate failures
    were test-isolation leakers are NEUTRAL — they do not build the streak and
    are not red nights, so they never appear in ``fail_nights``. They are tracked
    in their OWN low-severity, visible channel (``leaker_debt`` + the flat
    ``leaker_debt_nights`` the dashboard tile reads) so the debt stays visible
    without building or dissolving trust.

    Flake-debt (GATE-FLAKY-RETRY-HONESTY-S1): the accountability guardrail on the
    above neutrality. ``flaky_neutralized_without_filed_deflake_task`` counts
    distinct flaky test FILES (:func:`derive_flaky_deflake_candidates`) whose
    per-file de-flake key is NOT yet in the strategist's filed set — it MUST reach
    0 so no flake is silently swallowed (AC-2b). ``recurring_flakes`` escalates
    files flaky over >= :data:`RECURRING_FLAKE_MIN_NIGHTS` nights (AC-2c). The
    filed set is read from disk unless injected (tests)."""
    streak = derive_gate_streak(gate_records)
    neutral_nights = streak.get("neutral_nights", 0)
    candidates = derive_flaky_deflake_candidates(gate_records)
    filed = deflake_filed if deflake_filed is not None else read_deflake_filed()
    unfiled = [c for c in candidates if c["key"] not in filed]
    recurring = [c for c in candidates if c["recurring"]]
    return {
        "streak": streak["streak"],
        "green_nights": streak["green_nights"],
        "neutral_nights": neutral_nights,
        # Flat, numeric mirror the curated dashboard tile pulls by path.
        "leaker_debt_nights": neutral_nights,
        "total_recorded_nights": streak["total_recorded_nights"],
        "last_result": streak["last_result"],
        "last_ts": streak["last_ts"],
        "counter": {
            "name": "fail_nights",
            "value": streak["fail_nights"],
            "description": (
                "recorded red gate nights (product / unattributed reds only; "
                "test-isolation leaker-only nights are neutral, not red) — the "
                "streak's antagonist"
            ),
        },
        "leaker_debt": {
            "name": "leaker_debt_nights",
            "value": neutral_nights,
            "severity": "low",
            "channel": "test-isolation-leaker",
            "description": (
                "nights whose ONLY gate failures were test-isolation leakers — "
                "neutral for streak/triage/release, tracked as low-severity "
                "debt so it stays visible without building or dissolving trust"
            ),
        },
        "flake_debt": {
            "name": "flaky_neutralized_without_filed_deflake_task",
            "value": len(unfiled),
            # A flake with no filed de-flake task IS the silent-swallow the
            # guardrail forbids -> high severity until the strategist files it.
            "severity": "high" if unfiled else "low",
            "flaky_files_total": len(candidates),
            "recurring_flakes": len(recurring),
            "recurring_flake_files": [c["file"] for c in recurring][:GATE_LEAKERS_MAX],
            "unfiled_flake_files": [c["file"] for c in unfiled][:GATE_LEAKERS_MAX],
            "description": (
                "distinct flaky test files (fail->pass on the isolated rerun) "
                "with NO filed HELD de-flake task — MUST be 0 so a neutralized "
                "flake is never silently swallowed; recurring_flakes escalates "
                "files flaky over many nights instead of green-counting them"
            ),
        },
    }


# Authors that count as the human operator. Automatic authors (flow-gate,
# dispatcher, stratege-gutachter, …) must NOT count as operator touches —
# freigabe_released events on freigabe:complete chains are flow-gate-authored.
OPERATOR_AUTHORS: tuple[str, ...] = ("operator", "piet", "piet-via-claude")


def held_strategist_roots(conn: sqlite3.Connection) -> list[dict]:
    """Undecided held strategist proposal ROOTS (OPERATOR-LOAD-S1).

    A root is a ``scheduled`` task ``created_by='strategist-cron'`` with no
    ``freigabe_released``/``freigabe_vetoed`` event yet, whose ``created``
    event does NOT carry ``from_decompose_of`` — chain children are created by
    the same profile, so the decompose marker is the root discriminator.
    Shared by :func:`_operator_load_metric` and the strategist's propose
    back-pressure gate (STRATEGIST-BACKPRESSURE-S1).
    """
    rows = conn.execute(
        """
        SELECT t.id, t.created_at
          FROM tasks t
         WHERE t.status = 'scheduled'
           AND t.created_by = 'strategist-cron'
           AND NOT EXISTS (
                 SELECT 1 FROM task_events e
                  WHERE e.task_id = t.id
                    AND e.kind IN ('freigabe_released', 'freigabe_vetoed')
               )
        """
    ).fetchall()
    roots: list[dict] = []
    for r in rows:
        created = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'created' "
            "ORDER BY id LIMIT 1",
            (r["id"],),
        ).fetchone()
        payload: dict = {}
        if created and created["payload"]:
            try:
                loaded = json.loads(created["payload"])
                if isinstance(loaded, dict):
                    payload = loaded
            except (TypeError, ValueError):
                payload = {}
        if payload.get("from_decompose_of"):
            continue
        roots.append({"id": r["id"], "created_at": r["created_at"]})
    return roots


def _operator_load_metric(
    conn: sqlite3.Connection, *, now: int, window_days: int
) -> dict:
    """Operator-Last ↔ counter 'held_over_7d' (OPERATOR-LOAD-S1).

    The north star is "Piet rarely has to intervene" — this measures the
    operator side, which no other metric does. touches_per_week = operator-
    authored freigabe decisions (event payload ``author``; automatic authors
    like ``flow-gate`` do not count, see :data:`OPERATOR_AUTHORS`) plus
    operator-authored task comments, both inside the window.
    decision_latency_days_median = days from a strategist-cron task's creation
    to its operator freigabe decision, over decisions inside the window.
    The paired skeptic counter is the antagonist of "few touches": held
    strategist roots older than 7 days still undecided — a silent operator is
    only a good sign while the proposal queue isn't rotting.
    """
    cutoff = int(now) - int(window_days) * 86400
    decisions = 0
    latencies: list[float] = []
    rows = conn.execute(
        """
        SELECT e.payload, e.created_at AS decided_at,
               t.created_by, t.created_at AS task_created
          FROM task_events e
          JOIN tasks t ON t.id = e.task_id
         WHERE e.kind IN ('freigabe_released', 'freigabe_vetoed')
           AND e.created_at >= ?
        """,
        (cutoff,),
    ).fetchall()
    for r in rows:
        author = None
        idempotent_replay = False
        if r["payload"]:
            try:
                loaded = json.loads(r["payload"])
                if isinstance(loaded, dict):
                    author = loaded.get("author")
                    # A repeat release call appends a second event marked
                    # {"idempotent": true} — that is not a second operator touch.
                    idempotent_replay = bool(loaded.get("idempotent"))
            except (TypeError, ValueError):
                author = None
        if author not in OPERATOR_AUTHORS or idempotent_replay:
            continue
        decisions += 1
        if r["created_by"] == "strategist-cron" and r["task_created"]:
            latencies.append(
                (int(r["decided_at"]) - int(r["task_created"])) / 86400.0
            )
    placeholders = ",".join("?" * len(OPERATOR_AUTHORS))
    comments = conn.execute(
        f"SELECT COUNT(*) FROM task_comments "
        f"WHERE created_at >= ? AND author IN ({placeholders})",
        (cutoff, *OPERATOR_AUTHORS),
    ).fetchone()[0]
    latency = round(statistics.median(latencies), 2) if latencies else None
    held = held_strategist_roots(conn)
    held_over = sum(
        1
        for h in held
        if h["created_at"] and int(now) - int(h["created_at"]) > 7 * 86400
    )
    return {
        "touches_per_week": decisions + comments,
        "freigabe_decisions": decisions,
        "operator_comments": comments,
        "decision_latency_days_median": latency,
        "held_open": len(held),
        "window_days": window_days,
        "counter": {
            "name": "held_over_7d",
            "value": held_over,
            "description": (
                "held strategist proposals older than 7 days still undecided "
                "— the antagonist of 'few operator touches'"
            ),
        },
    }


def compute_metrics_snapshot(
    conn: sqlite3.Connection,
    *,
    now: Optional[int] = None,
    window_days: int = 7,
    gate_records: Optional[list[dict]] = None,
) -> dict:
    """Compute the distilled metrics snapshot (pure read; no writes).

    ``gate_records`` defaults to the on-disk green-gate ledger; pass an
    explicit list to compute the streak from supplied records (tests).
    """
    ts = int(now) if now is not None else int(time.time())
    if gate_records is None:
        gate_records = read_gate_records()
    generated = _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated.isoformat(),
        "generated_epoch": ts,
        "window_days": window_days,
        "metrics": {
            "autonomy": _autonomy_metric(conn),
            "cost_per_task": _cost_per_task_metric(
                conn, now=ts, window_days=window_days
            ),
            "escalation_rate": _escalation_rate_metric(
                conn, now=ts, window_days=window_days
            ),
            "classification_coverage": _classification_coverage_metric(
                conn, now=ts, window_days=window_days
            ),
            "green_gate_streak": _green_gate_metric(gate_records),
            "operator_load": _operator_load_metric(
                conn, now=ts, window_days=window_days
            ),
        },
    }


def write_metrics_snapshot(
    *,
    conn: Optional[sqlite3.Connection] = None,
    board: Optional[str] = None,
    now: Optional[int] = None,
    window_days: int = 7,
) -> tuple[Path, dict]:
    """Compute and atomically write the snapshot to ``metrics_snapshot_path``.

    Opens its own DB connection when ``conn`` is None. Returns the written
    path and the snapshot dict.
    """
    owns_conn = conn is None
    if owns_conn:
        conn = kb.connect(board=board)
    try:
        snapshot = compute_metrics_snapshot(
            conn, now=now, window_days=window_days
        )
    finally:
        if owns_conn:
            conn.close()

    path = metrics_snapshot_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)
    return path, snapshot


def render_snapshot_summary(snapshot: dict) -> str:
    """One-screen human summary of a snapshot (for the CLI's stdout)."""
    m = snapshot.get("metrics", {})
    a = m.get("autonomy", {})
    c = m.get("cost_per_task", {})
    e = m.get("escalation_rate", {})
    cc = m.get("classification_coverage", {})
    g = m.get("green_gate_streak", {})
    o = m.get("operator_load", {})
    lines = [
        f"vision metrics @ {snapshot.get('generated_at')}",
        (
            f"  autonomy:        {a.get('autonomy_pct')}%  "
            f"({a.get('autonomous_done')}/{a.get('total_done')} done)  "
            f"↔ {a.get('counter', {}).get('name')}="
            f"{a.get('counter', {}).get('value')}"
        ),
        (
            f"  cost/task:       total ${c.get('cost_usd_total')}  "
            f"trend={c.get('trend')}  "
            f"↔ {c.get('counter', {}).get('name')}="
            f"{c.get('counter', {}).get('value')}"
        ),
        (
            f"  escalations/wk:  {e.get('escalations_per_week')}  "
            f"(error={e.get('error_escalations_per_week')})  "
            f"↔ {e.get('counter', {}).get('name')}="
            f"{e.get('counter', {}).get('value')}"
        ),
        (
            f"  classify cover:  {cc.get('coverage_pct')}%  "
            f"({cc.get('classified_within_24h')}/{cc.get('escalations')} "
            f"≤24h)  ↔ {cc.get('counter', {}).get('name')}="
            f"{cc.get('counter', {}).get('value')}"
        ),
        (
            f"  green-gate:      streak={g.get('streak')} nights  "
            f"↔ {g.get('counter', {}).get('name')}="
            f"{g.get('counter', {}).get('value')}"
        ),
        (
            f"  operator load:   {o.get('touches_per_week')} touches/wk  "
            f"(held={o.get('held_open')}, latency="
            f"{o.get('decision_latency_days_median')}d)  "
            f"↔ {o.get('counter', {}).get('name')}="
            f"{o.get('counter', {}).get('value')}"
        ),
    ]
    return "\n".join(lines)
