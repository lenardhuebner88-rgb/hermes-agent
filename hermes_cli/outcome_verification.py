"""Shared, evidence-first outcome verification for Strategist and Autoresearch.

The module deliberately sits at the edge of the Kanban kernel.  It owns an
additive SQLite schema, but never changes the kernel schema declaration.  A
measurement claim and its corresponding ``task_events`` row are committed in
the same SQLite transaction, which gives concurrent nightly/reconcile
processes one real ownership boundary without creating a second delivery
truth.

Probe contracts are immutable, versioned and allowlisted.  There is no shell,
SQL, network, free-form command, or absolute-path probe surface.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Iterator, Mapping, MutableMapping, Sequence
import uuid


OUTCOME_SCHEMA_VERSION = 1

OUTCOME_APPLICABILITY = frozenset({"applicable", "not_applicable"})
MEASUREMENT_STATUSES = frozenset(
    {"not_started", "pending", "measuring", "measured", "retryable_failure", "exhausted"}
)
OUTCOME_VERDICTS = frozenset(
    {None, "improved", "neutral", "worsened", "unmeasurable", "confounded"}
)
EVIDENCE_GRADES = frozenset({"legacy_observational", "contract_verified"})

CONTRACT_EVENT = "outcome_contract_registered"
MEASUREMENT_STARTED_EVENT = "outcome_measurement_started"
MEASUREMENT_COMPLETED_EVENT = "outcome_measurement_completed"

_INTEGRATION_EVENT_KINDS = (
    "integration_merged",
    "INTEGRATOR_VERIFIED",
    "deployment_verified",
    "deployed",
)
_SHA_KEYS = ("commit_sha", "merged_sha", "deployed_sha", "head_sha", "sha")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)

_SAFE_ROOTS = frozenset(
    {
        "apps",
        "gateway",
        "hermes_cli",
        "plugins",
        "scripts",
        "tests",
        "tools",
        "tui_gateway",
        "ui-tui",
        "web",
    }
)
_SOURCE_PATTERN_RULES: dict[str, re.Pattern[str]] = {
    "bare_except": re.compile(r"(?m)^\s*except\s*:\s*(?:#.*)?$"),
    "silent_except": re.compile(
        r"(?ms)^\s*except(?:\s+Exception)?(?:\s+as\s+\w+)?\s*:\s*"
        r"(?:pass|continue|return(?:\s+None)?)\s*(?:#.*)?$"
    ),
}
_VISION_METRIC_DIRECTIONS: dict[str, int] = {
    "autonomy_pct": 1,
    "escalations_per_week": -1,
    "green_gate_streak.streak": 1,
    "fail_nights": -1,
    "recent_avg_cost_per_task": -1,
    "unclassified_share": -1,
    "error_escalations_per_week": -1,
    "touches_per_week": -1,
    "decision_latency_days_median": -1,
}


class ContractError(ValueError):
    """Raised when a proposed probe would exceed the immutable allowlist."""


@dataclass(frozen=True)
class MeasurementClaim:
    dedupe_key: str
    owner_token: str
    attempt_no: int
    lease_expires_at: int


_LOCK_GUARD = threading.RLock()
_LOCK_STATE = threading.local()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _lock_depths() -> dict[str, tuple[int, Any]]:
    depths = getattr(_LOCK_STATE, "depths", None)
    if depths is None:
        depths = {}
        _LOCK_STATE.depths = depths
    return depths


@contextmanager
def shared_state_lock(path: Path, *, exclusive: bool = True) -> Iterator[None]:
    """Cross-process, re-entrant advisory lock for one persistent state file."""
    target = Path(path)
    lock_path = target.parent / f".{target.name}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    key = str(lock_path.resolve())
    with _LOCK_GUARD:
        depths = _lock_depths()
        current = depths.get(key)
        if current is not None:
            depths[key] = (current[0] + 1, current[1])
            try:
                yield
            finally:
                depth, handle = depths[key]
                if depth == 1:
                    depths.pop(key, None)
                else:
                    depths[key] = (depth - 1, handle)
            return

        handle = lock_path.open("a+", encoding="utf-8")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        depths[key] = (1, handle)
        try:
            yield
        finally:
            depths.pop(key, None)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


def atomic_write_json(path: Path, value: Any, *, lock: bool = True) -> None:
    """Durably replace a JSON projection without fixed-temp collisions."""
    target = Path(path)

    def _write() -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.parent / f".{target.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
        try:
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, target)
            try:
                dir_fd = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass

    if lock:
        with shared_state_lock(target):
            _write()
    else:
        _write()


def locked_json_update(
    path: Path,
    *,
    default: Any,
    transform: Callable[[Any], Any],
) -> Any:
    """Serialize a complete JSON read/modify/write operation across processes."""
    target = Path(path)
    with shared_state_lock(target):
        try:
            current = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            current = default
        updated = transform(current)
        atomic_write_json(target, updated, lock=False)
        return updated


def normalize_outcome_fields(record: Mapping[str, Any], *, source: str) -> dict[str, Any]:
    """Return the canonical four outcome dimensions plus calibration policy.

    Existing canonical fields win.  Legacy Strategist verdict/time facts are
    preserved and explicitly graded as observational.  Terminal Autoresearch
    records without delivery are inapplicable, never silently "neutral".
    """
    src = str(source or "").strip().lower()
    explicit_applicability = record.get("outcome_applicability")
    explicit_status = record.get("measurement_status")
    explicit_verdict = record.get("outcome_verdict")
    explicit_grade = record.get("evidence_grade")

    delivery = str(record.get("delivery_state") or "").strip().lower()
    finding = str(record.get("finding_state") or "").strip().lower()
    decision = str(record.get("decision_state") or "").strip().lower()
    lifecycle_status = str(record.get("status") or "").strip().lower()
    legacy_verdict = record.get("verdict")
    has_contract = bool(record.get("contract_hash") or record.get("probe_contract"))
    terminal_no_delivery = delivery == "none" and (
        finding in {"rejected", "stale"}
        or decision == "dismissed"
        or lifecycle_status in {"skipped", "archived", "failed", "cancelled"}
    )

    if explicit_applicability in OUTCOME_APPLICABILITY:
        applicability = explicit_applicability
    else:
        applicability = "not_applicable" if terminal_no_delivery else "applicable"

    if explicit_status in MEASUREMENT_STATUSES:
        measurement_status = explicit_status
    elif applicability == "not_applicable":
        measurement_status = "exhausted"
    elif lifecycle_status == "measured" or record.get("measured_at") is not None:
        measurement_status = "measured"
    elif lifecycle_status == "shipped":
        measurement_status = "pending" if src == "strategist" or has_contract else "exhausted"
    elif delivery == "integrated":
        measurement_status = "pending" if has_contract else "exhausted"
    elif has_contract:
        measurement_status = "pending"
    else:
        measurement_status = "not_started"

    verdict = (
        explicit_verdict
        if "outcome_verdict" in record and explicit_verdict in OUTCOME_VERDICTS
        else legacy_verdict
    )
    if verdict not in OUTCOME_VERDICTS or applicability == "not_applicable":
        verdict = None
    if measurement_status == "exhausted" and applicability == "applicable" and verdict is None:
        verdict = "unmeasurable"

    if explicit_grade in EVIDENCE_GRADES:
        evidence_grade = explicit_grade
    elif has_contract or (src == "autoresearch" and terminal_no_delivery):
        evidence_grade = "contract_verified"
    else:
        evidence_grade = "legacy_observational"

    calibration_eligible = bool(record.get("calibration_eligible", src != "autoresearch"))
    if src == "autoresearch":
        calibration_eligible = False

    return {
        "outcome_applicability": applicability,
        "measurement_status": measurement_status,
        "outcome_verdict": verdict,
        "evidence_grade": evidence_grade,
        "calibration_eligible": calibration_eligible,
    }


def normalize_strategist_record(record: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(record)
    out.update(normalize_outcome_fields(out, source=str(out.get("outcome_source") or "strategist")))
    out.setdefault("outcome_schema_version", OUTCOME_SCHEMA_VERSION)
    out.setdefault("outcome_source", "strategist")
    return out


def _validated_repo_path(raw: Any, *, tests_only: bool = False) -> str:
    text = str(raw or "").strip().replace("\\", "/")
    path = PurePosixPath(text)
    if not text or path.is_absolute():
        raise ContractError("probe path must be repository-relative")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ContractError("probe path traversal is forbidden")
    if path.parts[0] not in _SAFE_ROOTS:
        raise ContractError(f"probe path root {path.parts[0]!r} is not allowlisted")
    if tests_only and path.parts[0] != "tests":
        raise ContractError("pytest probe targets must live under tests/")
    return path.as_posix()


def _probe_blueprint(proposal: Mapping[str, Any]) -> dict[str, Any]:
    affected = proposal.get("affected_tests")
    if isinstance(affected, Sequence) and not isinstance(affected, (str, bytes)) and affected:
        targets = [_validated_repo_path(item, tests_only=True) for item in list(affected)[:4]]
        return {
            "probe_id": "pytest_target.v1",
            "args": {"targets": targets},
            "comparator": {"metric": "returncode", "rule": "failing_to_passing"},
            "requires_delivery_sha": True,
            "budget": {"max_attempts": 3, "max_samples": 1, "timeout_seconds": 120},
        }

    target = _validated_repo_path(proposal.get("target_path") or proposal.get("target"))
    category_text = " ".join(
        str(proposal.get(key) or "").strip().lower().replace("-", "_")
        for key in ("category", "theme")
    )
    rule = next((name for name in _SOURCE_PATTERN_RULES if name in category_text), None)
    if rule:
        return {
            "probe_id": "source_pattern.v1",
            "args": {"path": target, "pattern_rule": rule},
            "comparator": {"metric": "occurrences", "rule": "lower_is_better"},
            "requires_delivery_sha": True,
            "budget": {"max_attempts": 3, "max_samples": 1, "timeout_seconds": 30},
        }

    return {
        "probe_id": "delivery_evidence.v1",
        "args": {"target": target},
        "comparator": {"metric": "delivery", "rule": "no_benefit_claim"},
        "requires_delivery_sha": True,
        "budget": {"max_attempts": 3, "max_samples": 1, "timeout_seconds": 30},
    }


def build_probe_contract(proposal: Mapping[str, Any], *, repo_root: Path) -> dict[str, Any]:
    """Materialize one canonical contract from the immutable allowlist."""
    root = Path(repo_root)
    if not root.is_absolute():
        raise ContractError("repo_root must be absolute")
    blueprint = _probe_blueprint(proposal)
    payload = {
        "schema_version": OUTCOME_SCHEMA_VERSION,
        "probe_id": blueprint["probe_id"],
        "args": blueprint["args"],
        "comparator": blueprint["comparator"],
        "requires_delivery_sha": blueprint["requires_delivery_sha"],
        "sampling": {"samples": 1, "aggregation": "single_observation"},
        "budget": blueprint["budget"],
    }
    digest = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    payload["contract_hash"] = digest
    payload["contract_id"] = f"outcome:{payload['probe_id']}:{digest[:16]}"
    return payload


def build_vision_metric_contract(metric_key: str, *, direction: int) -> dict[str, Any]:
    """Build the Strategist's fixed snapshot probe contract."""
    key = str(metric_key or "").strip()
    basename = key.rsplit(".", 1)[-1]
    expected = _VISION_METRIC_DIRECTIONS.get(key, _VISION_METRIC_DIRECTIONS.get(basename))
    if expected is None or direction not in {-1, 1} or expected != direction:
        raise ContractError(f"vision metric {key!r} is not in the reviewed direction allowlist")
    payload = {
        "schema_version": OUTCOME_SCHEMA_VERSION,
        "probe_id": "vision_metric_snapshot.v1",
        "args": {"metric_key": key},
        "comparator": {
            "metric": key,
            "rule": "higher_is_better" if direction == 1 else "lower_is_better",
        },
        "requires_delivery_sha": False,
        "sampling": {"samples": 1, "aggregation": "single_snapshot"},
        "budget": {"max_attempts": 1, "max_samples": 1, "timeout_seconds": 5},
    }
    digest = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    payload["contract_hash"] = digest
    payload["contract_id"] = f"outcome:{payload['probe_id']}:{digest[:16]}"
    return payload


def release_fingerprint(
    *, proposal_id: str, contract: Mapping[str, Any], baseline: Mapping[str, Any], target_sha256: str | None
) -> str:
    material = {
        "proposal_id": proposal_id,
        "contract_hash": contract.get("contract_hash"),
        "baseline": baseline,
        "target_sha256": target_sha256,
    }
    return hashlib.sha256(_canonical_json(material).encode("utf-8")).hexdigest()


def capture_probe(contract: Mapping[str, Any], *, repo_root: Path) -> dict[str, Any]:
    """Execute one bounded allowlisted probe without a shell or network."""
    probe_id = contract.get("probe_id")
    args = contract.get("args") if isinstance(contract.get("args"), Mapping) else {}
    started = time.monotonic()
    if probe_id == "source_pattern.v1":
        relative = _validated_repo_path(args.get("path"))
        rule = str(args.get("pattern_rule") or "")
        pattern = _SOURCE_PATTERN_RULES.get(rule)
        if pattern is None:
            raise ContractError(f"unknown source pattern rule: {rule!r}")
        path = (Path(repo_root) / relative).resolve()
        root = Path(repo_root).resolve()
        if root not in path.parents:
            raise ContractError("probe path escaped repository root")
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            return {"ok": False, "metric": "occurrences", "value": None, "error": type(exc).__name__}
        value = len(pattern.findall(text))
        return {
            "ok": True,
            "metric": "occurrences",
            "value": value,
            "sample_count": 1,
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
        }

    if probe_id == "pytest_target.v1":
        targets_raw = args.get("targets")
        if not isinstance(targets_raw, Sequence) or isinstance(targets_raw, (str, bytes)):
            raise ContractError("pytest targets must be a list")
        targets = [_validated_repo_path(item, tests_only=True) for item in list(targets_raw)[:4]]
        timeout = min(120, max(1, int((contract.get("budget") or {}).get("timeout_seconds") or 120)))
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", *targets],
                cwd=str(Path(repo_root)),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout,
                check=False,
                env={**os.environ, "PYTHONHASHSEED": "0", "TZ": "UTC", "LANG": "C.UTF-8"},
            )
            output_digest = hashlib.sha256(completed.stdout.encode("utf-8", errors="replace")).hexdigest()
            return {
                "ok": completed.returncode in {0, 1},
                "metric": "returncode",
                "value": int(completed.returncode),
                "sample_count": 1,
                "output_sha256": output_digest,
                "duration_ms": round((time.monotonic() - started) * 1000, 3),
            }
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "metric": "returncode",
                "value": None,
                "error": "timeout",
                "duration_ms": round((time.monotonic() - started) * 1000, 3),
            }

    if probe_id == "delivery_evidence.v1":
        _validated_repo_path(args.get("target"))
        return {
            "ok": True,
            "metric": "delivery",
            "value": 0,
            "sample_count": 1,
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
        }

    raise ContractError(f"unknown probe_id: {probe_id!r}")


def compare_observations(
    contract: Mapping[str, Any], baseline: Mapping[str, Any], current: Mapping[str, Any]
) -> str:
    if not baseline.get("ok") or not current.get("ok"):
        return "unmeasurable"
    rule = (contract.get("comparator") or {}).get("rule")
    before = baseline.get("value")
    after = current.get("value")
    if rule == "lower_is_better" and isinstance(before, (int, float)) and isinstance(after, (int, float)):
        if after < before:
            return "improved"
        if after > before:
            return "worsened"
        return "neutral"
    if rule == "failing_to_passing" and isinstance(before, int) and isinstance(after, int):
        if before != 0 and after == 0:
            return "improved"
        if before == 0 and after != 0:
            return "worsened"
        return "neutral" if before == after == 0 else "unmeasurable"
    return "unmeasurable"


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the additive outcome ownership tables on the current DB."""
    statements = (
        """
        CREATE TABLE IF NOT EXISTS outcome_contracts (
            proposal_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            contract_id TEXT NOT NULL,
            contract_hash TEXT NOT NULL,
            contract_json TEXT NOT NULL,
            baseline_json TEXT NOT NULL,
            baseline_recorded_at INTEGER NOT NULL,
            release_fingerprint TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'autoresearch',
            created_at INTEGER NOT NULL,
            UNIQUE(task_id, contract_hash)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS outcome_attempts (
            dedupe_key TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            proposal_id TEXT,
            contract_hash TEXT NOT NULL,
            phase TEXT NOT NULL,
            attempt_no INTEGER NOT NULL,
            owner_token TEXT NOT NULL,
            lease_expires_at INTEGER NOT NULL,
            status TEXT NOT NULL,
            observation_json TEXT,
            verdict TEXT,
            cost_usd REAL NOT NULL DEFAULT 0,
            integration_sha TEXT,
            created_at INTEGER NOT NULL,
            completed_at INTEGER,
            UNIQUE(task_id, contract_hash, phase, attempt_no)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_outcome_contracts_task ON outcome_contracts(task_id)",
        "CREATE INDEX IF NOT EXISTS idx_outcome_attempts_task ON outcome_attempts(task_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_outcome_attempts_status ON outcome_attempts(status, lease_expires_at)",
    )
    for statement in statements:
        conn.execute(statement)


def _missing_outcome_schema_objects(conn: sqlite3.Connection) -> list[str]:
    expected = (
        ("table", "outcome_contracts"),
        ("table", "outcome_attempts"),
        ("index", "idx_outcome_contracts_task"),
        ("index", "idx_outcome_attempts_task"),
        ("index", "idx_outcome_attempts_status"),
    )
    missing: list[str] = []
    for object_type, name in expected:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = ? AND name = ?",
            (object_type, name),
        ).fetchone()
        if row is None:
            missing.append(name)
    return missing


@contextmanager
def _immediate_txn(conn: sqlite3.Connection) -> Iterator[None]:
    if conn.in_transaction:
        yield
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


def _append_task_event(
    conn: sqlite3.Connection, task_id: str, kind: str, payload: Mapping[str, Any], *, now: int
) -> int:
    cur = conn.execute(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?, NULL, ?, ?, ?)",
        (task_id, kind, _canonical_json(payload), now),
    )
    return int(cur.lastrowid)


def register_contract(
    conn: sqlite3.Connection,
    *,
    proposal_id: str,
    task_id: str,
    contract: Mapping[str, Any],
    baseline: Mapping[str, Any],
    release_fingerprint: str,
    source: str = "autoresearch",
) -> bool:
    """Persist baseline/contract and its task event exactly once."""
    ensure_schema(conn)
    now = int(time.time())
    contract_hash = str(contract.get("contract_hash") or "")
    contract_id = str(contract.get("contract_id") or "")
    if not contract_hash or not contract_id:
        raise ContractError("contract id/hash are required")
    with _immediate_txn(conn):
        existing = conn.execute(
            "SELECT task_id, contract_hash, baseline_json, release_fingerprint "
            "FROM outcome_contracts WHERE proposal_id = ?",
            (proposal_id,),
        ).fetchone()
        if existing is not None:
            expected = (task_id, contract_hash, _canonical_json(baseline), release_fingerprint)
            actual = (
                existing["task_id"],
                existing["contract_hash"],
                existing["baseline_json"],
                existing["release_fingerprint"],
            )
            if actual != expected:
                raise ContractError("immutable outcome contract differs from the registered baseline")
            return False
        conn.execute(
            "INSERT INTO outcome_contracts "
            "(proposal_id, task_id, contract_id, contract_hash, contract_json, baseline_json, "
            "baseline_recorded_at, release_fingerprint, source, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                proposal_id,
                task_id,
                contract_id,
                contract_hash,
                _canonical_json(contract),
                _canonical_json(baseline),
                now,
                release_fingerprint,
                source,
                now,
            ),
        )
        _append_task_event(
            conn,
            task_id,
            CONTRACT_EVENT,
            {
                "schema_version": OUTCOME_SCHEMA_VERSION,
                "source": source,
                "proposal_id": proposal_id,
                "contract_id": contract_id,
                "contract_hash": contract_hash,
                "baseline_recorded_at": now,
                "release_fingerprint": release_fingerprint,
            },
            now=now,
        )
    return True


def claim_measurement_attempt(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    proposal_id: str | None,
    contract_hash: str,
    phase: str,
    attempt_no: int,
    lease_seconds: int = 300,
) -> MeasurementClaim | None:
    ensure_schema(conn)
    phase_value = str(phase or "").strip().lower()
    if phase_value not in {"replay", "canary", "forward", "shadow", "backfill"}:
        raise ValueError("unknown measurement phase")
    if attempt_no < 1:
        raise ValueError("attempt_no must be positive")
    now = int(time.time())
    lease = now + min(900, max(30, int(lease_seconds)))
    material = f"{task_id}\0{contract_hash}\0{phase_value}\0{attempt_no}"
    dedupe_key = hashlib.sha256(material.encode("utf-8")).hexdigest()
    owner_token = uuid.uuid4().hex
    with _immediate_txn(conn):
        cur = conn.execute(
            "INSERT OR IGNORE INTO outcome_attempts "
            "(dedupe_key, task_id, proposal_id, contract_hash, phase, attempt_no, owner_token, "
            "lease_expires_at, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'measuring', ?)",
            (
                dedupe_key,
                task_id,
                proposal_id,
                contract_hash,
                phase_value,
                attempt_no,
                owner_token,
                lease,
                now,
            ),
        )
        if cur.rowcount != 1:
            return None
        _append_task_event(
            conn,
            task_id,
            MEASUREMENT_STARTED_EVENT,
            {
                "schema_version": OUTCOME_SCHEMA_VERSION,
                "dedupe_key": dedupe_key,
                "proposal_id": proposal_id,
                "contract_hash": contract_hash,
                "phase": phase_value,
                "attempt": attempt_no,
                "lease_expires_at": lease,
            },
            now=now,
        )
    return MeasurementClaim(dedupe_key, owner_token, attempt_no, lease)


def finalize_measurement_attempt(
    conn: sqlite3.Connection,
    *,
    dedupe_key: str,
    owner_token: str,
    status: str,
    verdict: str | None,
    observation: Mapping[str, Any],
    cost_usd: float = 0.0,
    integration_sha: str | None = None,
) -> bool:
    if status not in {"measured", "retryable_failure", "exhausted"}:
        raise ValueError("invalid terminal measurement status")
    if verdict not in OUTCOME_VERDICTS:
        raise ValueError("invalid outcome verdict")
    if cost_usd < 0:
        raise ValueError("measurement cost cannot be negative")
    if integration_sha is not None and not _SHA_RE.fullmatch(integration_sha):
        raise ValueError("integration_sha must be a full git SHA")
    ensure_schema(conn)
    now = int(time.time())
    with _immediate_txn(conn):
        row = conn.execute(
            "SELECT task_id, proposal_id, contract_hash, phase, attempt_no FROM outcome_attempts "
            "WHERE dedupe_key = ? AND owner_token = ? AND status = 'measuring'",
            (dedupe_key, owner_token),
        ).fetchone()
        if row is None:
            return False
        cur = conn.execute(
            "UPDATE outcome_attempts SET status = ?, observation_json = ?, verdict = ?, "
            "cost_usd = ?, integration_sha = ?, completed_at = ? "
            "WHERE dedupe_key = ? AND owner_token = ? AND status = 'measuring'",
            (
                status,
                _canonical_json(observation),
                verdict,
                float(cost_usd),
                integration_sha,
                now,
                dedupe_key,
                owner_token,
            ),
        )
        if cur.rowcount != 1:
            return False
        _append_task_event(
            conn,
            row["task_id"],
            MEASUREMENT_COMPLETED_EVENT,
            {
                "schema_version": OUTCOME_SCHEMA_VERSION,
                "dedupe_key": dedupe_key,
                "proposal_id": row["proposal_id"],
                "contract_hash": row["contract_hash"],
                "phase": row["phase"],
                "attempt": row["attempt_no"],
                "status": status,
                "verdict": verdict,
                "cost_usd": float(cost_usd),
                "integration_sha": integration_sha,
                "observation_sha256": hashlib.sha256(
                    _canonical_json(observation).encode("utf-8")
                ).hexdigest(),
            },
            now=now,
        )
    return True


def recover_expired_attempts(conn: sqlite3.Connection, *, now: int | None = None) -> int:
    """Make expired owners terminal; retry requires the next attempt number."""
    ensure_schema(conn)
    now_ts = int(time.time() if now is None else now)
    with _immediate_txn(conn):
        rows = conn.execute(
            "SELECT dedupe_key, task_id, proposal_id, contract_hash, phase, attempt_no "
            "FROM outcome_attempts WHERE status = 'measuring' AND lease_expires_at < ?",
            (now_ts,),
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE outcome_attempts SET status = 'retryable_failure', completed_at = ?, "
                "observation_json = ? WHERE dedupe_key = ? AND status = 'measuring'",
                (now_ts, _canonical_json({"error": "lease_expired"}), row["dedupe_key"]),
            )
            _append_task_event(
                conn,
                row["task_id"],
                MEASUREMENT_COMPLETED_EVENT,
                {
                    "schema_version": OUTCOME_SCHEMA_VERSION,
                    "dedupe_key": row["dedupe_key"],
                    "proposal_id": row["proposal_id"],
                    "contract_hash": row["contract_hash"],
                    "phase": row["phase"],
                    "attempt": row["attempt_no"],
                    "status": "retryable_failure",
                    "verdict": None,
                    "cost_usd": 0.0,
                    "reason": "lease_expired",
                },
                now=now_ts,
            )
    return len(rows)


def _integration_sha_from_payload(payload: Any) -> str | None:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            return None
    if not isinstance(payload, Mapping):
        return None
    for key in _SHA_KEYS:
        value = str(payload.get(key) or "").strip()
        if _SHA_RE.fullmatch(value):
            return value.lower()
    nested = payload.get("outcome")
    if isinstance(nested, Mapping):
        return _integration_sha_from_payload(nested)
    return None


def measurement_readiness(
    conn: sqlite3.Connection, *, task_id: str, contract: Mapping[str, Any]
) -> dict[str, Any]:
    if not bool(contract.get("requires_delivery_sha")):
        return {"ready": True, "reason": None, "integration_sha": None}
    placeholders = ",".join("?" for _ in _INTEGRATION_EVENT_KINDS)
    rows = conn.execute(
        f"SELECT payload FROM task_events WHERE task_id = ? AND kind IN ({placeholders}) "
        "ORDER BY id DESC",
        (task_id, *_INTEGRATION_EVENT_KINDS),
    ).fetchall()
    for row in rows:
        sha = _integration_sha_from_payload(row["payload"])
        if sha:
            return {"ready": True, "reason": None, "integration_sha": sha}
    return {"ready": False, "reason": "integration_sha_missing", "integration_sha": None}


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def enrich_autoresearch_outcomes(
    items: Sequence[Mapping[str, Any]], *, conn: sqlite3.Connection | None = None
) -> list[dict[str, Any]]:
    """Project canonical outcomes onto proposal cards without mutating state."""
    own_conn = conn is None
    opened: sqlite3.Connection | None = None
    if conn is None:
        try:
            from hermes_cli import kanban_db

            opened = kanban_db.connect()
            conn = opened
        except Exception:
            conn = None
    contracts: dict[str, sqlite3.Row] = {}
    attempts: dict[str, sqlite3.Row] = {}
    try:
        if conn is not None and _table_exists(conn, "outcome_contracts"):
            proposal_ids = [str(item.get("id") or "") for item in items if item.get("id")]
            if proposal_ids:
                marks = ",".join("?" for _ in proposal_ids)
                for row in conn.execute(
                    f"SELECT * FROM outcome_contracts WHERE proposal_id IN ({marks})", proposal_ids
                ).fetchall():
                    contracts[str(row["proposal_id"])] = row
                if _table_exists(conn, "outcome_attempts"):
                    for row in conn.execute(
                        f"SELECT a.* FROM outcome_attempts a JOIN ("
                        f"SELECT proposal_id, MAX(created_at) AS newest FROM outcome_attempts "
                        f"WHERE proposal_id IN ({marks}) GROUP BY proposal_id"
                        f") latest ON latest.proposal_id = a.proposal_id AND latest.newest = a.created_at",
                        proposal_ids,
                    ).fetchall():
                        attempts[str(row["proposal_id"])] = row

        projected: list[dict[str, Any]] = []
        for raw in items:
            item = dict(raw)
            proposal_id = str(item.get("id") or "")
            contract = contracts.get(proposal_id)
            attempt = attempts.get(proposal_id)
            if contract is not None:
                item["contract_hash"] = contract["contract_hash"]
                item["probe_contract"] = {
                    "contract_id": contract["contract_id"],
                    "contract_hash": contract["contract_hash"],
                    "baseline_recorded_at": contract["baseline_recorded_at"],
                    "release_fingerprint": contract["release_fingerprint"],
                }
                try:
                    item["outcome_baseline"] = json.loads(contract["baseline_json"])
                except (ValueError, TypeError):
                    item["outcome_baseline"] = None
            canonical = normalize_outcome_fields(item, source="autoresearch")
            if contract is not None and canonical["outcome_applicability"] == "applicable":
                canonical.update(
                    {
                        "measurement_status": "pending",
                        "outcome_verdict": None,
                        "evidence_grade": "contract_verified",
                        "calibration_eligible": False,
                    }
                )
            if attempt is not None and canonical["outcome_applicability"] == "applicable":
                canonical["measurement_status"] = attempt["status"]
                canonical["outcome_verdict"] = attempt["verdict"]
                item["outcome_cost_usd"] = float(attempt["cost_usd"] or 0.0)
                item["outcome_measured_at"] = attempt["completed_at"]
                item["outcome_integration_sha"] = attempt["integration_sha"]
                try:
                    item["outcome_observation"] = json.loads(attempt["observation_json"] or "null")
                except (ValueError, TypeError):
                    item["outcome_observation"] = None
            item.update(canonical)
            item.setdefault("outcome_schema_version", OUTCOME_SCHEMA_VERSION)
            projected.append(item)
        return projected
    finally:
        if own_conn and opened is not None:
            opened.close()


def outcome_metrics(items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    applicable = [item for item in items if item.get("outcome_applicability") == "applicable"]
    measured = [item for item in applicable if item.get("measurement_status") == "measured"]
    counts = {
        verdict: sum(1 for item in measured if item.get("outcome_verdict") == verdict)
        for verdict in ("improved", "neutral", "worsened", "unmeasurable", "confounded")
    }
    cost = sum(float(item.get("outcome_cost_usd") or 0.0) for item in items)
    improved = counts["improved"]
    return {
        "applicable": len(applicable),
        "not_applicable": len(items) - len(applicable),
        "pending": sum(
            1
            for item in applicable
            if item.get("measurement_status") in {"pending", "measuring", "retryable_failure"}
        ),
        "measured": len(measured),
        "measurement_coverage": len(measured) / len(applicable) if applicable else 0.0,
        **counts,
        "measurement_cost_usd": round(cost, 6),
        "cost_per_measured_usd": cost / len(measured) if measured and cost else None,
        "cost_per_improved_usd": cost / improved if improved and cost else None,
    }


def shadow_marker_path() -> Path:
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()) / "state" / "autoresearch-outcome-shadow.enabled"


def shadow_enabled() -> bool:
    return shadow_marker_path().is_file()


def run_shadow_verifier(
    *,
    conn: sqlite3.Connection | None = None,
    repo_root: Path | None = None,
    phase: str = "shadow",
    max_measurements: int = 3,
    require_enabled: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Measure eligible registered contracts without changing delivery state."""
    if require_enabled and not shadow_enabled():
        return {
            "ok": True,
            "enabled": False,
            "dry_run": bool(dry_run),
            "eligible": 0,
            "measured": 0,
            "pending": 0,
            "retryable_failure": 0,
            "exhausted": 0,
            "skipped_existing": 0,
            "cost_usd": 0.0,
        }
    limit = max(0, min(20, int(max_measurements)))
    root = Path(repo_root or Path(__file__).resolve().parents[1]).resolve()
    own_conn = conn is None
    if conn is None:
        from hermes_cli import kanban_db

        conn = kanban_db.connect()
    summary = {
        "ok": True,
        "enabled": True,
        "dry_run": bool(dry_run),
        "eligible": 0,
        "measured": 0,
        "pending": 0,
        "retryable_failure": 0,
        "exhausted": 0,
        "skipped_existing": 0,
        "cost_usd": 0.0,
        "outcomes": [],
    }
    try:
        if not _table_exists(conn, "outcome_contracts"):
            if not dry_run:
                ensure_schema(conn)
            return summary
        if not dry_run:
            recover_expired_attempts(conn)
        rows = conn.execute(
            "SELECT c.*, t.status AS task_status FROM outcome_contracts c "
            "JOIN tasks t ON t.id = c.task_id ORDER BY c.created_at, c.proposal_id"
        ).fetchall()
        for row in rows:
            existing = conn.execute(
                "SELECT status, verdict FROM outcome_attempts WHERE task_id = ? AND contract_hash = ? "
                "AND status IN ('measured', 'exhausted') ORDER BY completed_at DESC LIMIT 1",
                (row["task_id"], row["contract_hash"]),
            ).fetchone()
            if existing is not None:
                summary["skipped_existing"] += 1
                continue
            contract = json.loads(row["contract_json"])
            baseline = json.loads(row["baseline_json"])
            readiness = measurement_readiness(conn, task_id=row["task_id"], contract=contract)
            if not readiness["ready"]:
                summary["pending"] += 1
                continue
            summary["eligible"] += 1
            if summary["measured"] + summary["retryable_failure"] + summary["exhausted"] >= limit:
                continue
            previous = conn.execute(
                "SELECT COALESCE(MAX(attempt_no), 0) FROM outcome_attempts "
                "WHERE task_id = ? AND contract_hash = ? AND phase = ?",
                (row["task_id"], row["contract_hash"], phase),
            ).fetchone()[0]
            attempt_no = int(previous or 0) + 1
            max_attempts = min(3, max(1, int((contract.get("budget") or {}).get("max_attempts") or 1)))
            if attempt_no > max_attempts:
                summary["exhausted"] += 1
                continue
            if dry_run:
                summary["outcomes"].append(
                    {"proposal_id": row["proposal_id"], "task_id": row["task_id"], "action": "would_measure"}
                )
                continue
            claim = claim_measurement_attempt(
                conn,
                task_id=row["task_id"],
                proposal_id=row["proposal_id"],
                contract_hash=row["contract_hash"],
                phase=phase,
                attempt_no=attempt_no,
                lease_seconds=int((contract.get("budget") or {}).get("timeout_seconds") or 30) + 30,
            )
            if claim is None:
                continue
            observation = capture_probe(contract, repo_root=root)
            verdict = compare_observations(contract, baseline, observation)
            if observation.get("ok"):
                terminal_status = "measured"
            elif attempt_no < max_attempts:
                terminal_status = "retryable_failure"
                verdict = None
            else:
                terminal_status = "exhausted"
                verdict = "unmeasurable"
            finalized = finalize_measurement_attempt(
                conn,
                dedupe_key=claim.dedupe_key,
                owner_token=claim.owner_token,
                status=terminal_status,
                verdict=verdict,
                observation=observation,
                cost_usd=0.0,
                integration_sha=readiness["integration_sha"],
            )
            if finalized:
                summary[terminal_status] += 1
                summary["outcomes"].append(
                    {
                        "proposal_id": row["proposal_id"],
                        "task_id": row["task_id"],
                        "contract_id": row["contract_id"],
                        "contract_hash": row["contract_hash"],
                        "attempt": attempt_no,
                        "status": terminal_status,
                        "verdict": verdict,
                        "integration_sha": readiness["integration_sha"],
                    }
                )
        return summary
    finally:
        if own_conn and conn is not None:
            conn.close()


def migrate_shared_state(
    *,
    proposals_dir: Path,
    strategist_outcomes_path: Path,
    kanban_db_path: Path | None = None,
    apply: bool = False,
    backup_root: Path | None = None,
) -> dict[str, Any]:
    """Dry-run/apply the additive common fields with full pre-write backup.

    When ``kanban_db_path`` is supplied, dry-run also inventories the additive
    SQLite objects. Apply takes a transactionally consistent SQLite backup via
    the backup API before creating any missing tables or indexes. A second
    apply is a true no-op and therefore creates no redundant backup.
    """
    pdir = Path(proposals_dir)
    strategist_path = Path(strategist_outcomes_path)
    db_path = Path(kanban_db_path) if kanban_db_path is not None else None
    proposal_changes: list[tuple[Path, dict[str, Any]]] = []
    proposal_paths = sorted(pdir.glob("*.json")) if pdir.exists() else []
    if pdir.exists():
        for path in proposal_paths:
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            if not isinstance(current, dict):
                continue
            updated = dict(current)
            updated.update(normalize_outcome_fields(updated, source="autoresearch"))
            updated.setdefault("outcome_schema_version", OUTCOME_SCHEMA_VERSION)
            updated.setdefault("outcome_source", "autoresearch")
            if _canonical_json(updated) != _canonical_json(current):
                proposal_changes.append((path, updated))

    try:
        strategist_current = json.loads(strategist_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        strategist_current = []
    if not isinstance(strategist_current, list):
        strategist_current = []
    strategist_updated = [
        normalize_strategist_record(item) if isinstance(item, Mapping) else item
        for item in strategist_current
    ]
    strategist_changed = _canonical_json(strategist_updated) != _canonical_json(strategist_current)

    missing_schema_objects: list[str] = []
    if db_path is not None:
        if not db_path.is_file():
            raise FileNotFoundError(f"Kanban database does not exist: {db_path}")
        uri = f"file:{db_path.resolve()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as conn:
            missing_schema_objects = _missing_outcome_schema_objects(conn)

    backup_dir: Path | None = None
    if apply and (proposal_changes or strategist_changed or missing_schema_objects):
        root = Path(backup_root) if backup_root is not None else strategist_path.parents[2] / "backups"
        backup_dir = root / f"autoresearch-outcomes-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
        backup_dir.mkdir(parents=True, exist_ok=False)
        if pdir.exists():
            shutil.copytree(pdir, backup_dir / "proposals")
        if strategist_path.exists():
            (backup_dir / "strategist").mkdir(parents=True, exist_ok=True)
            shutil.copy2(strategist_path, backup_dir / "strategist" / strategist_path.name)
        if db_path is not None:
            source_uri = f"file:{db_path.resolve()}?mode=ro"
            source = sqlite3.connect(source_uri, uri=True)
            destination = sqlite3.connect(backup_dir / db_path.name)
            try:
                source.backup(destination)
            finally:
                destination.close()
                source.close()
        def _normalize_proposal(current: Any) -> Any:
            if not isinstance(current, Mapping):
                return current
            normalized = dict(current)
            normalized.update(normalize_outcome_fields(normalized, source="autoresearch"))
            normalized.setdefault("outcome_schema_version", OUTCOME_SCHEMA_VERSION)
            normalized.setdefault("outcome_source", "autoresearch")
            return normalized

        for path, _ in proposal_changes:
            locked_json_update(path, default={}, transform=_normalize_proposal)
        if strategist_changed:
            def _normalize_strategist(current: Any) -> Any:
                if not isinstance(current, list):
                    return current
                return [
                    normalize_strategist_record(item) if isinstance(item, Mapping) else item
                    for item in current
                ]

            locked_json_update(strategist_path, default=[], transform=_normalize_strategist)
        if db_path is not None and missing_schema_objects:
            with sqlite3.connect(db_path) as conn:
                ensure_schema(conn)

    return {
        "ok": True,
        "apply": bool(apply),
        "proposal_total": len(proposal_paths),
        "proposal_changes": len(proposal_changes),
        "strategist_total": len(strategist_current),
        "strategist_changes": 1 if strategist_changed else 0,
        "schema_changes": len(missing_schema_objects),
        "missing_schema_objects": missing_schema_objects,
        "backup_dir": str(backup_dir) if backup_dir is not None else None,
    }


__all__ = [
    "CONTRACT_EVENT",
    "ContractError",
    "EVIDENCE_GRADES",
    "MEASUREMENT_COMPLETED_EVENT",
    "MEASUREMENT_STARTED_EVENT",
    "MeasurementClaim",
    "OUTCOME_SCHEMA_VERSION",
    "atomic_write_json",
    "build_probe_contract",
    "build_vision_metric_contract",
    "capture_probe",
    "claim_measurement_attempt",
    "compare_observations",
    "enrich_autoresearch_outcomes",
    "ensure_schema",
    "finalize_measurement_attempt",
    "locked_json_update",
    "measurement_readiness",
    "migrate_shared_state",
    "normalize_outcome_fields",
    "normalize_strategist_record",
    "outcome_metrics",
    "recover_expired_attempts",
    "register_contract",
    "release_fingerprint",
    "run_shadow_verifier",
    "shadow_enabled",
    "shadow_marker_path",
    "shared_state_lock",
]
