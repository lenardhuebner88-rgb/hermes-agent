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


def derive_gate_streak(records: list[dict]) -> dict:
    """Derive the consecutive-green-nights streak from gate records.

    A *night* (one UTC ``date``) is green only if **every** record for that
    date is ``pass`` — a single ``fail`` makes the whole night red. The
    streak counts back from the most recent recorded night and stops at the
    first red night. Gaps (nights with no record) are simply absent; the
    streak is measured over recorded nights.
    """
    per_date: dict[str, str] = {}
    latest_ts: Optional[str] = None
    latest_epoch = None
    latest_result: Optional[str] = None
    for rec in records:
        date = str(rec.get("date"))
        result = "pass" if str(rec.get("result")).lower() == "pass" else "fail"
        # any fail flips the night red
        if per_date.get(date) == "fail":
            pass
        elif result == "fail":
            per_date[date] = "fail"
        else:
            per_date.setdefault(date, "pass")
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
            latest_result = result

    dates_desc = sorted(per_date.keys(), reverse=True)
    streak = 0
    for date in dates_desc:
        if per_date[date] == "pass":
            streak += 1
        else:
            break

    green_nights = sum(1 for v in per_date.values() if v == "pass")
    fail_nights = sum(1 for v in per_date.values() if v == "fail")
    return {
        "streak": streak,
        "green_nights": green_nights,
        "fail_nights": fail_nights,
        "total_recorded_nights": len(per_date),
        "last_result": latest_result,
        "last_ts": latest_ts,
    }


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


def _night_cause(fails: list[tuple[Optional[int], Optional[dict]]]) -> Optional[dict]:
    """Representative cause for one red night: the EARLIEST fail record's
    ``first_fail`` (by epoch; records without an epoch sort last). A night that
    failed with no first_fail payload at all returns ``None``."""
    for _ep, ff, _leaker_only in sorted(fails, key=_night_sort_key):
        cause = _first_fail_cause(ff)
        if cause is not None:
            return cause
    return None


def _night_sort_key(
    item: tuple[Optional[int], Optional[dict], bool]
) -> tuple[bool, int]:
    ep = item[0]
    return (ep is None, ep if ep is not None else 0)


def _night_cause_and_purity(
    fails: list[tuple[Optional[int], Optional[dict], bool]]
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
    has_leaker_only = any(flag for _ep, _ff, flag in fails)
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

    The returned dict — ``{gate, fingerprint, detail, red_nights, dates}`` — is
    pure detection; the volatile ``red_nights``/``detail`` are for logging only.
    The stable ``gate`` + ``fingerprint`` are what the idempotent ingest keys on.
    """
    per_date: dict[str, dict] = {}
    for rec in records:
        date = str(rec.get("date") or "")
        if not date:
            continue
        slot = per_date.setdefault(date, {"red": False, "fails": []})
        if str(rec.get("result")).lower() != "pass":
            slot["red"] = True
            slot["fails"].append(
                (_record_epoch(rec), rec.get("first_fail"), bool(rec.get("leaker_only")))
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

    return {
        "gate": target_cause["gate"] if target_cause else "unknown",
        "fingerprint": target_fp,
        "detail": target_cause["detail"] if target_cause else "",
        "red_nights": len(matched),
        "dates": matched,
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
    red AND >= ``min_reds`` of the last ``window`` recorded nights are red —
    regardless of whether the first_fail cause changed between nights. The
    changing-cause case is exactly what the same-cause path deliberately skips,
    leaving the operator with a persistent red head and no triage item.

    Returns ``None`` (idle) when:
      - the head night is green (no red head to triage), or
      - fewer than ``min_reds`` reds in the window (isolated flake — AC-2 guard).

    When triggered, returns::

        {
            "gate": <str>,            # the head night's gate
            "red_files": set[str],    # currently-red test files from head first_fail
            "fingerprint": <str>,     # sha1 of the sorted red_files set (dedup key)
            "red_count": <int>,       # reds in the window
            "window": <int>,          # M
            "dates": [<str>, ...],    # dates of the red nights in the window
        }

    The ``fingerprint`` is a pure function of the CURRENT red file set (not the
    historical causes), so re-runs / follow-up nights with the same red file set
    produce the same fingerprint and the ingest dedups — no spam (AC-2).
    """
    if not records:
        return None
    recent = list(records[-window:]) if window > 0 else list(records)
    if not recent:
        return None
    head = recent[-1]
    if str(head.get("result", "")).lower() != "fail":
        return None
    reds = [r for r in recent if str(r.get("result", "")).lower() == "fail"]
    if len(reds) < min_reds:
        return None
    # ``detail`` and ``gate`` live under the record's ``first_fail`` payload
    # (mirrors derive_consecutive_red_cause, which reads first_fail["detail"]).
    # Reading them top-level would always miss → empty file set + "unknown" gate.
    head_first_fail = head.get("first_fail") or {}
    head_files = _extract_failing_test_files(head_first_fail.get("detail"))
    # Fingerprint the CURRENT red file set — the dedup axis. Two runs whose head
    # night shows the same red files produce the same fingerprint, so the ingest
    # reports already_ingested instead of opening a second chain (AC-2). When the
    # red file set changes (a new test broke, a different subset is red), the
    # fingerprint changes and a fresh triage chain opens — correct: the operator
    # SHOULD see a new item for a new failure pattern.
    fingerprint_source = "|".join(sorted(head_files)) if head_files else (
        str(head_first_fail.get("gate") or "unknown")
    )
    fingerprint = hashlib.sha1(fingerprint_source.encode("utf-8")).hexdigest()
    red_dates = [str(r.get("date") or r.get("epoch") or "?") for r in reds]
    return {
        "gate": str(head_first_fail.get("gate") or "unknown"),
        "red_files": head_files,
        "fingerprint": fingerprint,
        "red_count": len(reds),
        "window": window,
        "dates": red_dates,
    }


# ---------------------------------------------------------------------------
# DB-derived metrics
# ---------------------------------------------------------------------------

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


def _autonomy_metric(conn: sqlite3.Connection) -> dict:
    """Autonomie-% ↔ counter 'should_have_escalated_but_didnt'.

    Autonomous = a done task that finished with ``consecutive_failures = 0``
    AND never raised an ``operator_escalation`` event. The paired counter is
    the subset of those "autonomous" tasks that nonetheless carry a
    non-transient ``heiler_classification`` (real-bug / bad-spec / conflict):
    the system saw a real problem and still didn't escalate.
    """
    escalated = _tasks_with_operator_escalation(conn)
    flagged = _tasks_with_nontransient_heiler(conn)
    rows = conn.execute(
        "SELECT id, consecutive_failures FROM tasks WHERE status = 'done'"
    ).fetchall()
    total_done = len(rows)
    autonomous = 0
    should_have = 0
    for r in rows:
        tid = r["id"]
        cf = r["consecutive_failures"] or 0
        if cf == 0 and tid not in escalated:
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
    inside the window.
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
    row = conn.execute(
        "SELECT COUNT(DISTINCT task_id) AS n FROM task_events "
        "WHERE kind = ? AND created_at >= ?",
        (kb.OPERATOR_ESCALATION_EVENT, now - window),
    ).fetchone()
    escalations = int(row["n"] or 0) if row else 0

    # Uses the auto-retry defaults (== live config: failure_limit/backoff/
    # retry_limit), so the settled set matches what the dispatcher sweep
    # computes. If those config values ever diverge from the defaults, this
    # headline could mis-count boundary cases briefly (it is a measurement that
    # converges within a tick, not a gate) — feed the config in here if exactness
    # matters then.
    silent = len(kb.silent_block_task_ids(conn, now=now))
    return {
        "escalations_per_week": escalations,
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
    return {
        "coverage_pct": coverage_pct,
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
    elif recent > prior:
        trend = "up"
        pct_change = round(100.0 * (recent - prior) / prior, 1) if prior else None
    elif recent < prior:
        trend = "down"
        pct_change = round(100.0 * (recent - prior) / prior, 1) if prior else None
    else:
        trend = "flat"
        pct_change = 0.0

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


def _green_gate_metric(gate_records: list[dict]) -> dict:
    """Green-Gate-Streak ↔ counter 'fail_nights'.

    Headline = consecutive green nights from the ledger. Counter = total
    recorded red nights (the streak's antagonist).
    """
    streak = derive_gate_streak(gate_records)
    return {
        "streak": streak["streak"],
        "green_nights": streak["green_nights"],
        "total_recorded_nights": streak["total_recorded_nights"],
        "last_result": streak["last_result"],
        "last_ts": streak["last_ts"],
        "counter": {
            "name": "fail_nights",
            "value": streak["fail_nights"],
            "description": (
                "recorded red gate nights — the streak's antagonist"
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
    ]
    return "\n".join(lines)
