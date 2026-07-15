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
import importlib.metadata
import json
import os
import platform
from pathlib import Path, PurePosixPath
import re
import resource
import select
import signal
import shutil
import sqlite3
import subprocess
import sys
import tempfile
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

_INTEGRATION_EVENT_KINDS = ("integration_merged", "INTEGRATOR_VERIFIED")
_DEPLOYMENT_EVENT_KINDS = ("deployment_verified", "deployed")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
_CONTENT_SHA_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
_CONTRACT_ID_RE = re.compile(r"^outcome:[a-z0-9_.-]+:[0-9a-f]{16}$")

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
_HISTORICAL_REPLAY_CASES: dict[str, dict[str, str]] = {
    "reconcile_flood_limit": {
        "claim": (
            "Two concurrent reconcile processes produce at most five tasks, "
            "with one owner, no loser mutation and an idempotent second wave."
        ),
        "metric": "contract_violations",
        "outcome_class": "autoresearch-reconcile-flood-limit/v1",
    },
    "explicit_lifecycle_truth": {
        "claim": (
            "Explicit delivery_state=none remains history while lifecycle-less "
            "legacy applied payloads retain compatibility."
        ),
        "metric": "lifecycle_misclassifications",
        "outcome_class": "autoresearch-explicit-lifecycle-truth/v1",
    },
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


def _source_epoch(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _pytest_version() -> str:
    try:
        return importlib.metadata.version("pytest")
    except importlib.metadata.PackageNotFoundError:
        return "unavailable"


def _seal_evidence(evidence: MutableMapping[str, Any]) -> dict[str, Any]:
    material = {key: value for key, value in evidence.items() if key != "evidence_ref"}
    evidence["evidence_ref"] = "outcome-evidence:sha256:" + hashlib.sha256(
        _canonical_json(material).encode("utf-8")
    ).hexdigest()
    return dict(evidence)


def seal_evidence(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Seal structured bounded evidence after adding reviewed metadata."""
    return _seal_evidence(dict(evidence))


def _evidence_ref_is_valid(evidence: Mapping[str, Any]) -> bool:
    material = {key: value for key, value in evidence.items() if key != "evidence_ref"}
    expected = "outcome-evidence:sha256:" + hashlib.sha256(
        _canonical_json(material).encode("utf-8")
    ).hexdigest()
    return evidence.get("evidence_ref") == expected


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

    if applicability == "not_applicable":
        # Terminal no-delivery has no measurement workflow. Explicit stale
        # fields from legacy projections cannot turn it into exhausted work.
        measurement_status = "not_started"
    elif explicit_status in MEASUREMENT_STATUSES:
        measurement_status = explicit_status
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

    if (
        applicability != "applicable"
        or measurement_status not in {"measured", "exhausted"}
        or not has_contract
    ):
        evidence_grade = "legacy_observational"
    elif explicit_grade in EVIDENCE_GRADES:
        evidence_grade = explicit_grade
    else:
        evidence_grade = "contract_verified"

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


def _validated_repo_path(
    raw: Any, *, tests_only: bool = False, repo_root: Path | None = None
) -> str:
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
    if repo_root is not None:
        root = Path(repo_root).resolve()
        cursor = root
        for part in path.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise ContractError("probe paths may not traverse symlinks")
        resolved = (root / path.as_posix()).resolve()
        if root not in resolved.parents:
            raise ContractError("probe path escaped repository root")
    return path.as_posix()


def _probe_blueprint(proposal: Mapping[str, Any], *, repo_root: Path) -> dict[str, Any]:
    measurement_kind = str(proposal.get("measurement_kind") or "").strip()
    if measurement_kind == "runtime_observation" or str(proposal.get("mode") or "") == "runtime":
        key = str(proposal.get("metric_key") or "").strip()
        basename = key.rsplit(".", 1)[-1]
        direction = _VISION_METRIC_DIRECTIONS.get(key, _VISION_METRIC_DIRECTIONS.get(basename))
        if direction is None:
            raise ContractError(f"runtime metric {key!r} is not allowlisted")
        rule = "higher_is_better" if direction == 1 else "lower_is_better"
        return {
            "probe_id": "vision_metric_snapshot.v1",
            "probe_args": {"metric_key": key},
            "claim": f"The deployed runtime moves {key} in the reviewed {rule} direction.",
            "measurement_kind": "runtime_observation",
            "success_template_id": "vision_metric_direction.v1",
            "success_parameters": {
                "metric_key": key,
                "direction": direction,
                "neutral_tolerance": 0.05,
            },
            "success_rule": {
                "metric": key,
                "operator": rule,
                "neutral_tolerance": 0.05,
            },
            "outcome_class": f"vision-metric:{key}:{rule}/v1",
            "counter_probes": [],
            "counter_rules": [],
            "trigger": "deployed_runtime",
            "timeout_seconds": 5,
            # Preserve the Strategist's reviewed three-day maturity contract.
            # The upper bound prevents an arbitrarily late snapshot from being
            # treated as attributable to this deployment.
            "observation_window": {
                "kind": "bounded",
                "min_age_seconds": 3 * 86_400,
                "max_age_seconds": 7 * 86_400,
            },
            "max_source_age_seconds": 86_400,
        }

    affected = proposal.get("affected_tests")
    if isinstance(affected, Sequence) and not isinstance(affected, (str, bytes)) and affected:
        targets = [
            _validated_repo_path(item, tests_only=True, repo_root=repo_root)
            for item in list(affected)[:4]
        ]
        counters_raw = proposal.get("counter_tests")
        counters = (
            [
                _validated_repo_path(item, tests_only=True, repo_root=repo_root)
                for item in list(counters_raw)[:4]
            ]
            if isinstance(counters_raw, Sequence)
            and not isinstance(counters_raw, (str, bytes))
            else []
        )
        pattern_counters_raw = proposal.get("counter_patterns")
        pattern_counters: list[dict[str, Any]] = []
        if isinstance(pattern_counters_raw, Sequence) and not isinstance(
            pattern_counters_raw, (str, bytes)
        ):
            for raw in list(pattern_counters_raw)[:4]:
                if not isinstance(raw, Mapping):
                    raise ContractError("counter pattern must be an object")
                pattern_rule = str(raw.get("pattern_rule") or "").strip()
                if pattern_rule not in _SOURCE_PATTERN_RULES:
                    raise ContractError(f"unknown counter pattern rule: {pattern_rule!r}")
                pattern_counters.append(
                    {
                        "probe_id": "source_pattern.v1",
                        "probe_args": {
                            "path": _validated_repo_path(
                                raw.get("path"), repo_root=repo_root
                            ),
                            "pattern_rule": pattern_rule,
                        },
                    }
                )
        counter_probes = (
            [{"probe_id": "pytest_target.v1", "probe_args": {"targets": counters}}]
            if counters
            else []
        ) + pattern_counters
        counter_rules = (
            [{"metric": "returncode", "operator": "must_remain_passing"}]
            if counters
            else []
        ) + [
            {"metric": "occurrences", "operator": "must_not_increase"}
            for _ in pattern_counters
        ]
        return {
            "probe_id": "pytest_target.v1",
            "probe_args": {"targets": targets},
            "claim": "The reviewed regression changes from failing to passing while counter tests remain passing.",
            "measurement_kind": "invariant",
            "success_template_id": "pytest_failing_to_passing.v1",
            "success_parameters": {"failing_values": [1], "passing_value": 0},
            "success_rule": {"metric": "returncode", "operator": "failing_to_passing"},
            "outcome_class": "pytest-regression:failing-to-passing/v1",
            "counter_probes": counter_probes,
            "counter_rules": counter_rules,
            "trigger": "integrated_commit",
            "timeout_seconds": 120,
        }

    target = _validated_repo_path(
        proposal.get("target_path") or proposal.get("target"), repo_root=repo_root
    )
    category_text = " ".join(
        str(proposal.get(key) or "").strip().lower().replace("-", "_")
        for key in ("category", "theme")
    )
    rule = next((name for name in _SOURCE_PATTERN_RULES if name in category_text), None)
    if rule:
        return {
            "probe_id": "source_pattern.v1",
            "probe_args": {"path": target, "pattern_rule": rule},
            "claim": f"The integrated change reduces occurrences of the reviewed {rule} pattern.",
            "measurement_kind": "metric_delta",
            "success_template_id": "source_occurrences_lower.v1",
            "success_parameters": {"minimum_delta": 1},
            "success_rule": {"metric": "occurrences", "operator": "lower_is_better", "minimum_delta": 1},
            "outcome_class": f"source-pattern:{rule}/v1",
            "counter_probes": [],
            "counter_rules": [],
            "trigger": "integrated_commit",
            "timeout_seconds": 30,
        }

    return {
        "probe_id": "delivery_evidence.v1",
        "probe_args": {"target": target},
        "claim": "Delivery is recorded without claiming a measurable benefit.",
        "measurement_kind": "invariant",
        "success_template_id": "delivery_no_benefit_claim.v1",
        "success_parameters": {"benefit_claim_allowed": False},
        "success_rule": {"metric": "delivery", "operator": "no_benefit_claim"},
        "outcome_class": "delivery-evidence:unmeasurable/v1",
        "counter_probes": [],
        "counter_rules": [],
        "trigger": "integrated_commit",
        "timeout_seconds": 30,
    }


def _materialize_contract(blueprint: Mapping[str, Any]) -> dict[str, Any]:
    timeout = int(blueprint["timeout_seconds"])
    payload = {
        "schema_version": OUTCOME_SCHEMA_VERSION,
        "outcome_contract_version": OUTCOME_SCHEMA_VERSION,
        "claim": blueprint["claim"],
        "measurement_kind": blueprint["measurement_kind"],
        "probe_id": blueprint["probe_id"],
        "probe_args": blueprint["probe_args"],
        "success_template_id": blueprint["success_template_id"],
        "success_parameters": blueprint["success_parameters"],
        "success_rule": blueprint["success_rule"],
        "outcome_class": blueprint["outcome_class"],
        "counter_probes": blueprint["counter_probes"],
        "counter_rules": blueprint["counter_rules"],
        "sampling_plan": {
            "sample_count": 1,
            "aggregation": "single_observation",
            "noise_rule": "no_retry_cherry_picking",
        },
        "observation_window": blueprint.get(
            "observation_window", {"kind": "immediate", "max_age_seconds": 900}
        ),
        "trigger": blueprint["trigger"],
        "environment_requirements": blueprint.get("environment_requirements")
        or {
            "fingerprint_schema": "hermes-outcome-env/v1",
            "python_major_minor": f"{sys.version_info.major}.{sys.version_info.minor}",
            "platform_system": platform.system().lower(),
            "platform_machine": platform.machine().lower(),
            "pytest_version": _pytest_version(),
            "max_source_age_seconds": int(
                blueprint.get("max_source_age_seconds", 900)
            ),
        },
        "measurement_budget": {
            "max_attempts": 3,
            "max_samples": 1,
            "timeout_seconds": timeout,
            "max_output_bytes": 262_144,
            "max_memory_mb": 1024,
            "max_cost_usd": 0.0,
        },
        "calibration_eligible": False,
    }
    digest = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    payload["contract_sha256"] = digest
    payload["contract_hash"] = digest
    payload["contract_id"] = f"outcome:{payload['probe_id']}:{digest[:16]}"
    # Compatibility aliases for the first common-adapter readers. They are
    # derived, hashed fields and cannot diverge from the full contract.
    payload["args"] = payload["probe_args"]
    payload["comparator"] = {
        "metric": payload["success_rule"]["metric"],
        "rule": payload["success_rule"]["operator"],
    }
    payload["requires_delivery_sha"] = payload["trigger"] in {
        "integrated_commit", "deployed_runtime"
    }
    payload["sampling"] = {"samples": 1, "aggregation": "single_observation"}
    payload["budget"] = {
        "max_attempts": payload["measurement_budget"]["max_attempts"],
        "max_samples": payload["measurement_budget"]["max_samples"],
        "timeout_seconds": payload["measurement_budget"]["timeout_seconds"],
    }
    # Aliases are also immutable. Rehash the complete payload without the
    # derived identity so legacy and current readers share one integrity bit.
    identity_free = {
        key: value for key, value in payload.items()
        if key not in {"contract_id", "contract_hash", "contract_sha256"}
    }
    digest = hashlib.sha256(_canonical_json(identity_free).encode("utf-8")).hexdigest()
    payload["contract_sha256"] = digest
    payload["contract_hash"] = digest
    payload["contract_id"] = f"outcome:{payload['probe_id']}:{digest[:16]}"
    return payload


def build_probe_contract(proposal: Mapping[str, Any], *, repo_root: Path) -> dict[str, Any]:
    """Materialize one canonical contract from the immutable allowlist."""
    root = Path(repo_root)
    if not root.is_absolute():
        raise ContractError("repo_root must be absolute")
    return _materialize_contract(_probe_blueprint(proposal, repo_root=root))


def build_vision_metric_contract(metric_key: str, *, direction: int) -> dict[str, Any]:
    """Build the Strategist's fixed snapshot intent contract."""
    key = str(metric_key or "").strip()
    basename = key.rsplit(".", 1)[-1]
    expected = _VISION_METRIC_DIRECTIONS.get(key, _VISION_METRIC_DIRECTIONS.get(basename))
    if expected is None or direction not in {-1, 1} or expected != direction:
        raise ContractError(f"vision metric {key!r} is not in the reviewed direction allowlist")
    return _materialize_contract(
        _probe_blueprint(
            {
                "mode": "runtime",
                "measurement_kind": "runtime_observation",
                "metric_key": key,
                "target": "hermes_cli/vision_metrics.py",
            },
            repo_root=Path(__file__).resolve().parents[1],
        )
    )


def build_historical_replay_contract(case_id: str) -> dict[str, Any]:
    """Materialize one fixed, operator-only historical replay contract."""
    case = _HISTORICAL_REPLAY_CASES.get(str(case_id or "").strip())
    if case is None:
        raise ContractError(f"unknown historical replay case: {case_id!r}")
    return _materialize_contract(
        {
            "probe_id": "historical_replay.v1",
            "probe_args": {"case_id": str(case_id)},
            "claim": case["claim"],
            "measurement_kind": "invariant",
            "success_template_id": "historical_contract_violations_lower.v1",
            "success_parameters": {"minimum_delta": 1, "target_value": 0},
            "success_rule": {
                "metric": case["metric"],
                "operator": "lower_is_better",
                "minimum_delta": 1,
                "target_value": 0,
            },
            "outcome_class": case["outcome_class"],
            "counter_probes": [],
            "counter_rules": [],
            "trigger": "integrated_commit",
            "timeout_seconds": 120,
        }
    )


def seal_historical_replay_observation(
    contract: Mapping[str, Any],
    *,
    target_sha: str,
    value: int,
    details: Mapping[str, Any],
) -> dict[str, Any]:
    """Seal one structured observation emitted by the reviewed replay tool."""
    validated = validate_probe_contract(contract)
    if validated.get("probe_id") != "historical_replay.v1":
        raise ContractError("historical replay evidence requires its fixed probe")
    sha = str(target_sha or "").strip().lower()
    if _SHA_RE.fullmatch(sha) is None:
        raise ContractError("historical replay target must be a full git SHA")
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ContractError("historical replay value must be a non-negative integer")
    metric = str(validated["success_rule"]["metric"])
    environment = _environment_descriptor(validated)
    return _seal_evidence(
        {
            "ok": True,
            "contract_sha256": validated["contract_sha256"],
            "target_sha": sha,
            "expected_target_sha": sha,
            "observed_value": {
                "ok": True,
                "metric": metric,
                "value": value,
                "sample_count": 1,
            },
            "counter_observations": [],
            "source_generated_at": _utc_now(),
            "source_schema_version": "historical-replay-observation/v1",
            "environment": environment,
            "environment_fingerprint": hashlib.sha256(
                _canonical_json(environment).encode("utf-8")
            ).hexdigest(),
            "captured_at": _utc_now(),
            "sample_count": 1,
            "cost_usd": 0.0,
            "cost_accounting": {
                "status": "complete",
                "known_task_runs": 0,
                "unknown_task_runs": 0,
                "unknown_task_run_refs": [],
            },
            "metric": metric,
            "value": value,
            "details": dict(details),
        }
    )


def capture_vision_snapshot_baseline(
    contract: Mapping[str, Any], snapshot: Mapping[str, Any], *, now: float | None = None
) -> dict[str, Any]:
    """Turn one real vision-metrics snapshot into a preregistered baseline."""
    validated = validate_probe_contract(contract)
    if validated.get("probe_id") != "vision_metric_snapshot.v1":
        raise ContractError("vision baseline requires the vision metric probe")
    generated_at = snapshot.get("generated_at")
    if generated_at is None:
        raise ContractError("vision baseline source timestamp is missing")
    if isinstance(generated_at, (int, float)) and not isinstance(generated_at, bool):
        generated_epoch = float(generated_at)
    elif isinstance(generated_at, str):
        try:
            generated_epoch = datetime.fromisoformat(generated_at.replace("Z", "+00:00")).timestamp()
        except ValueError as exc:
            raise ContractError("vision baseline source timestamp is invalid") from exc
    else:
        raise ContractError("vision baseline source timestamp is invalid")
    if (time.time() if now is None else float(now)) - generated_epoch > 86_400:
        raise ContractError("vision baseline source snapshot is stale")
    payload = snapshot.get("metrics") if isinstance(snapshot.get("metrics"), Mapping) else snapshot
    key = str(validated["probe_args"]["metric_key"])
    value = _metric_value(payload, key)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ContractError(f"vision baseline metric {key!r} is missing")
    source_schema = snapshot.get("schema_version") or snapshot.get("schema")
    if source_schema is None:
        raise ContractError("vision baseline source schema is missing")
    evidence: dict[str, Any] = {
        "ok": True,
        "contract_sha256": validated["contract_sha256"],
        "target_sha": hashlib.sha256(_canonical_json(snapshot).encode("utf-8")).hexdigest(),
        "expected_target_sha": None,
        "observed_value": {
            "ok": True,
            "metric": key,
            "value": float(value),
            "sample_count": 1,
        },
        "counter_observations": [],
        "source_generated_at": generated_at,
        "source_schema_version": str(source_schema),
        "environment": _environment_descriptor(validated),
        "environment_fingerprint": _environment_fingerprint(validated),
        "captured_at": _utc_now(),
        "sample_count": 1,
        "cost_usd": 0.0,
        "metric": key,
        "value": float(value),
    }
    return _seal_evidence(evidence)


def _blueprint_from_contract(contract: Mapping[str, Any]) -> dict[str, Any]:
    probe_id = str(contract.get("probe_id") or "")
    args = contract.get("probe_args")
    if not isinstance(args, Mapping):
        raise ContractError("probe_args must be an object")

    def _immutable_context(blueprint: dict[str, Any]) -> dict[str, Any]:
        requirements = contract.get("environment_requirements")
        if not isinstance(requirements, Mapping):
            raise ContractError("environment requirements must be an object")
        blueprint["environment_requirements"] = dict(requirements)
        window = contract.get("observation_window")
        if not isinstance(window, Mapping):
            raise ContractError("observation window must be an object")
        blueprint["observation_window"] = dict(window)
        return blueprint
    if probe_id == "pytest_target.v1":
        targets = args.get("targets")
        if not isinstance(targets, Sequence) or isinstance(targets, (str, bytes)) or not targets:
            raise ContractError("pytest targets must be a non-empty list")
        counter_probes = contract.get("counter_probes")
        counters: list[str] = []
        pattern_counters: list[dict[str, str]] = []
        if counter_probes:
            if not isinstance(counter_probes, list) or len(counter_probes) > 5:
                raise ContractError("pytest contract has an invalid counter probe set")
            for counter in counter_probes:
                if not isinstance(counter, Mapping):
                    raise ContractError("counter probe must be an object")
                counter_id = str(counter.get("probe_id") or "")
                counter_args = counter.get("probe_args") or {}
                if counter_id == "pytest_target.v1":
                    if counters:
                        raise ContractError("pytest counter probe may appear only once")
                    raw = counter_args.get("targets")
                    if not isinstance(raw, list):
                        raise ContractError("counter targets must be a list")
                    counters = [
                        _validated_repo_path(item, tests_only=True) for item in raw
                    ]
                elif counter_id == "source_pattern.v1":
                    rule = str(counter_args.get("pattern_rule") or "")
                    if rule not in _SOURCE_PATTERN_RULES:
                        raise ContractError("unknown counter source pattern rule")
                    pattern_counters.append(
                        {
                            "path": _validated_repo_path(counter_args.get("path")),
                            "pattern_rule": rule,
                        }
                    )
                else:
                    raise ContractError(f"unknown counter probe: {counter_id!r}")
        return _immutable_context(_probe_blueprint(
            {
                "affected_tests": list(targets),
                "counter_tests": counters,
                "counter_patterns": pattern_counters,
            },
            repo_root=Path.cwd().resolve(),
        ))
    if probe_id == "source_pattern.v1":
        rule = str(args.get("pattern_rule") or "")
        if rule not in _SOURCE_PATTERN_RULES:
            raise ContractError("unknown source pattern rule")
        return _immutable_context(_probe_blueprint(
            {"target": args.get("path"), "category": rule},
            repo_root=Path.cwd().resolve(),
        ))
    if probe_id == "delivery_evidence.v1":
        return _immutable_context(_probe_blueprint(
            {"target": args.get("target"), "category": "delivery"},
            repo_root=Path.cwd().resolve(),
        ))
    if probe_id == "vision_metric_snapshot.v1":
        return _immutable_context(_probe_blueprint(
            {
                "mode": "runtime",
                "measurement_kind": "runtime_observation",
                "metric_key": args.get("metric_key"),
                "target": "hermes_cli/vision_metrics.py",
            },
            repo_root=Path.cwd().resolve(),
        ))
    if probe_id == "historical_replay.v1":
        case_id = str(args.get("case_id") or "")
        case = _HISTORICAL_REPLAY_CASES.get(case_id)
        if case is None:
            raise ContractError("unknown historical replay case")
        return _immutable_context(
            {
                "probe_id": "historical_replay.v1",
                "probe_args": {"case_id": case_id},
                "claim": case["claim"],
                "measurement_kind": "invariant",
                "success_template_id": "historical_contract_violations_lower.v1",
                "success_parameters": {"minimum_delta": 1, "target_value": 0},
                "success_rule": {
                    "metric": case["metric"],
                    "operator": "lower_is_better",
                    "minimum_delta": 1,
                    "target_value": 0,
                },
                "outcome_class": case["outcome_class"],
                "counter_probes": [],
                "counter_rules": [],
                "trigger": "integrated_commit",
                "timeout_seconds": 120,
            }
        )
    raise ContractError(f"unknown probe_id: {probe_id!r}")


def validate_probe_contract(contract: Mapping[str, Any]) -> dict[str, Any]:
    """Reject forged hashes, altered rules/budgets and unknown template data."""
    if not isinstance(contract, Mapping):
        raise ContractError("outcome contract must be an object")
    identity_free = {
        key: value for key, value in contract.items()
        if key not in {"contract_id", "contract_hash", "contract_sha256"}
    }
    digest = hashlib.sha256(_canonical_json(identity_free).encode("utf-8")).hexdigest()
    if contract.get("contract_hash") != digest or contract.get("contract_sha256") != digest:
        raise ContractError("contract hash does not match canonical contract data")
    expected_id = f"outcome:{contract.get('probe_id')}:{digest[:16]}"
    if contract.get("contract_id") != expected_id or not _CONTRACT_ID_RE.fullmatch(expected_id):
        raise ContractError("contract id does not match canonical hash")
    expected = _materialize_contract(_blueprint_from_contract(contract))
    if _canonical_json(contract) != _canonical_json(expected):
        raise ContractError("contract differs from its versioned allowlisted template")
    return dict(contract)


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


def _resolve_probe_path(repo_root: Path, raw: Any, *, tests_only: bool = False) -> Path:
    relative = _validated_repo_path(raw, tests_only=tests_only, repo_root=repo_root)
    return (Path(repo_root).resolve() / relative).resolve()


def _probe_paths(contract: Mapping[str, Any]) -> list[str]:
    probe_id = contract.get("probe_id")
    args = contract.get("probe_args") or contract.get("args") or {}
    if probe_id == "pytest_target.v1":
        paths = list(args.get("targets") or [])
        for counter in contract.get("counter_probes") or []:
            counter_args = counter.get("probe_args") or {}
            if counter.get("probe_id") == "pytest_target.v1":
                paths.extend(counter_args.get("targets") or [])
            elif counter.get("probe_id") == "source_pattern.v1":
                paths.append(str(counter_args.get("path") or ""))
        return [str(path) for path in paths]
    if probe_id == "source_pattern.v1":
        return [str(args.get("path") or "")]
    if probe_id == "delivery_evidence.v1":
        return [str(args.get("target") or "")]
    return []


def _environment_descriptor(contract: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": "hermes-outcome-env/v1",
        "python_major_minor": f"{sys.version_info.major}.{sys.version_info.minor}",
        "platform_system": platform.system().lower(),
        "platform_machine": platform.machine().lower(),
        "pytest_version": _pytest_version(),
        "probe_id": contract.get("probe_id"),
    }


def _environment_satisfies_contract(
    contract: Mapping[str, Any], environment: Mapping[str, Any]
) -> bool:
    requirements = contract.get("environment_requirements") or {}
    pairs = {
        "schema": requirements.get("fingerprint_schema"),
        "python_major_minor": requirements.get("python_major_minor"),
        "platform_system": requirements.get("platform_system"),
        "platform_machine": requirements.get("platform_machine"),
        "pytest_version": requirements.get("pytest_version"),
    }
    return all(
        expected is not None and environment.get(key) == expected
        for key, expected in pairs.items()
    )


def _environment_fingerprint(contract: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        _canonical_json(_environment_descriptor(contract)).encode("utf-8")
    ).hexdigest()


def _git_head(repo_root: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(Path(repo_root).resolve()), "rev-parse", "HEAD"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
            check=False,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = completed.stdout.strip().lower()
    return value if completed.returncode == 0 and _SHA_RE.fullmatch(value) else None


def _content_sha(repo_root: Path, paths: Sequence[str]) -> str:
    material: list[dict[str, Any]] = []
    for relative in sorted(set(paths)):
        path = _resolve_probe_path(Path(repo_root), relative, tests_only=relative.startswith("tests/"))
        try:
            data = path.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
        except OSError:
            digest = "missing"
        material.append({"path": relative, "sha256": digest})
    return hashlib.sha256(_canonical_json(material).encode("utf-8")).hexdigest()


def _target_sha(repo_root: Path, paths: Sequence[str]) -> str:
    head = _git_head(repo_root)
    if head is not None:
        try:
            completed = subprocess.run(
                ["git", "-C", str(Path(repo_root).resolve()), "status", "--porcelain", "--", *paths],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=5,
                check=False,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"},
            )
            if completed.returncode == 0 and not completed.stdout.strip():
                return head
        except (OSError, subprocess.SubprocessError):
            pass
    return _content_sha(repo_root, paths)


def _kill_probe_process_group(proc: subprocess.Popen[Any]) -> None:
    """Hard-stop a bounded probe and every process it started."""
    killpg = getattr(os, "killpg", None)
    if killpg is not None:
        try:
            # The probe is started with ``start_new_session=True``, therefore
            # its PID is also its dedicated process-group id.  Never resolve a
            # potentially recycled PGID after the fact.
            killpg(proc.pid, signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    proc.kill()


def _run_bounded_pytest(
    targets: Sequence[str], *, repo_root: Path, budget: Mapping[str, Any]
) -> dict[str, Any]:
    timeout = min(120, max(1, int(budget.get("timeout_seconds") or 120)))
    max_output = min(1_048_576, max(1024, int(budget.get("max_output_bytes") or 262_144)))
    memory_bytes = min(2_048, max(256, int(budget.get("max_memory_mb") or 1024))) * 1024 * 1024
    with tempfile.TemporaryDirectory(prefix="hermes-outcome-probe-") as temp_home:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": temp_home,
            "HERMES_HOME": str(Path(temp_home) / ".hermes"),
            "PYTHONHASHSEED": "0",
            "PYTHONPATH": str(Path(repo_root).resolve()),
            "TZ": "UTC",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "HERMES_SANDBOX_MODE": "1",
        }

        def _limit_memory() -> None:
            resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))

        started = time.monotonic()
        proc = subprocess.Popen(
            [sys.executable, "-m", "pytest", "-q", *targets],
            cwd=str(Path(repo_root).resolve()),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
            preexec_fn=_limit_memory,
        )
        assert proc.stdout is not None
        fd = proc.stdout.fileno()
        output = bytearray()
        deadline = time.monotonic() + timeout
        error: str | None = None
        while True:
            if time.monotonic() >= deadline:
                error = "timeout"
                _kill_probe_process_group(proc)
                break
            ready, _, _ = select.select([fd], [], [], 0.05)
            if ready:
                chunk = os.read(fd, min(65_536, max_output + 1 - len(output)))
                if chunk:
                    output.extend(chunk)
                    if len(output) > max_output:
                        error = "output_limit"
                        _kill_probe_process_group(proc)
                        break
                elif proc.poll() is not None:
                    break
            elif proc.poll() is not None:
                break
        proc.wait(timeout=5)
        if error is not None:
            return {
                "ok": False,
                "metric": "returncode",
                "value": None,
                "error": error,
                "output_sha256": hashlib.sha256(bytes(output[:max_output])).hexdigest(),
                "duration_ms": round((time.monotonic() - started) * 1000, 3),
            }
        return {
            "ok": proc.returncode in {0, 1},
            "metric": "returncode",
            "value": int(proc.returncode),
            "sample_count": 1,
            "output_sha256": hashlib.sha256(bytes(output)).hexdigest(),
            "output_bytes": len(output),
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
        }


def _metric_value(payload: Mapping[str, Any], dotted: str) -> Any:
    current: Any = payload
    for part in dotted.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def _execute_probe(
    probe_id: str, args: Mapping[str, Any], *, repo_root: Path, budget: Mapping[str, Any]
) -> dict[str, Any]:
    started = time.monotonic()
    if probe_id == "source_pattern.v1":
        rule = str(args.get("pattern_rule") or "")
        pattern = _SOURCE_PATTERN_RULES.get(rule)
        if pattern is None:
            raise ContractError(f"unknown source pattern rule: {rule!r}")
        path = _resolve_probe_path(repo_root, args.get("path"))
        try:
            if path.stat().st_size > 2_000_000:
                return {
                    "ok": False,
                    "metric": "occurrences",
                    "value": None,
                    "error": "input_limit",
                }
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
        targets = [
            _validated_repo_path(item, tests_only=True, repo_root=repo_root)
            for item in list(targets_raw)[:4]
        ]
        return _run_bounded_pytest(targets, repo_root=repo_root, budget=budget)

    if probe_id == "delivery_evidence.v1":
        _resolve_probe_path(repo_root, args.get("target"))
        return {
            "ok": True,
            "metric": "delivery",
            "value": 0,
            "sample_count": 1,
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
        }

    if probe_id == "vision_metric_snapshot.v1":
        key = str(args.get("metric_key") or "")
        from hermes_constants import get_hermes_home

        path = Path(get_hermes_home()) / "state" / "vision-metrics.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            return {
                "ok": False,
                "metric": key,
                "value": None,
                "error": type(exc).__name__,
            }
        metric_payload = payload.get("metrics") if isinstance(payload.get("metrics"), Mapping) else payload
        value = _metric_value(metric_payload, key)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return {"ok": False, "metric": key, "value": None, "error": "metric_missing"}
        return {
            "ok": True,
            "metric": key,
            "value": float(value),
            "sample_count": 1,
            "source_generated_at": payload.get("generated_at"),
            "source_schema_version": payload.get("schema_version") or payload.get("schema"),
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
        }
    if probe_id == "historical_replay.v1":
        case = _HISTORICAL_REPLAY_CASES.get(str(args.get("case_id") or ""))
        if case is None:
            raise ContractError("unknown historical replay case")
        return {
            "ok": False,
            "metric": case["metric"],
            "value": None,
            "error": "operator_replay_required",
        }

    raise ContractError(f"unknown probe_id: {probe_id!r}")


def capture_probe(
    contract: Mapping[str, Any], *, repo_root: Path, expected_target_sha: str | None = None
) -> dict[str, Any]:
    """Execute the complete bounded probe plan and return safe structured evidence."""
    validated = validate_probe_contract(contract)
    root = Path(repo_root).resolve()
    paths = _probe_paths(validated)
    target_sha = _target_sha(root, paths)
    primary = _execute_probe(
        str(validated["probe_id"]),
        validated["probe_args"],
        repo_root=root,
        budget=validated["measurement_budget"],
    )
    counters = [
        _execute_probe(
            str(counter["probe_id"]),
            counter["probe_args"],
            repo_root=root,
            budget=validated["measurement_budget"],
        )
        for counter in validated["counter_probes"]
    ]
    if validated["probe_id"] == "vision_metric_snapshot.v1":
        source_generated_at = primary.get("source_generated_at")
        raw_source_schema = primary.get("source_schema_version")
        source_schema_version = str(raw_source_schema) if raw_source_schema is not None else None
    else:
        source_generated_at = primary.get("source_generated_at") or _utc_now()
        source_schema_version = str(
            primary.get("source_schema_version") or str(validated["probe_id"])
        )
    evidence = {
        "ok": bool(primary.get("ok")) and all(bool(counter.get("ok")) for counter in counters),
        "contract_sha256": validated["contract_sha256"],
        "target_sha": target_sha,
        "expected_target_sha": expected_target_sha,
        "observed_value": primary,
        "counter_observations": counters,
        "source_generated_at": source_generated_at,
        "source_schema_version": source_schema_version,
        "environment": _environment_descriptor(validated),
        "environment_fingerprint": _environment_fingerprint(validated),
        "captured_at": _utc_now(),
        "sample_count": 1,
        "cost_usd": 0.0,
    }
    confounded_reasons: list[str] = []
    if expected_target_sha is not None and target_sha != expected_target_sha:
        confounded_reasons.append("target_sha_mismatch")
    if validated["probe_id"] == "vision_metric_snapshot.v1":
        generated_epoch = _source_epoch(source_generated_at)
        max_source_age = int(
            validated["environment_requirements"].get("max_source_age_seconds") or 0
        )
        source_age = time.time() - generated_epoch if generated_epoch is not None else None
        if generated_epoch is None:
            confounded_reasons.append("source_timestamp_invalid")
        elif source_age is not None and (
            source_age > max_source_age or source_age < -300
        ):
            confounded_reasons.append("stale_source_snapshot")
    if confounded_reasons:
        evidence["confounded_reasons"] = sorted(set(confounded_reasons))
    # Compatibility flat fields remain projections of the structured value.
    for key in ("metric", "value", "duration_ms", "output_sha256", "error"):
        if key in primary:
            evidence[key] = primary[key]
    return _seal_evidence(evidence)


def validate_baseline(contract: Mapping[str, Any], baseline: Mapping[str, Any]) -> None:
    validated = validate_probe_contract(contract)
    required = {
        "contract_sha256",
        "target_sha",
        "observed_value",
        "counter_observations",
        "source_generated_at",
        "source_schema_version",
        "environment_fingerprint",
        "evidence_ref",
    }
    missing = sorted(key for key in required if key not in baseline)
    if missing:
        raise ContractError("baseline is missing required evidence: " + ", ".join(missing))
    if baseline.get("contract_sha256") != validated["contract_sha256"]:
        raise ContractError("baseline contract hash mismatch")
    target_sha = str(baseline.get("target_sha") or "")
    if not (_SHA_RE.fullmatch(target_sha) or _CONTENT_SHA_RE.fullmatch(target_sha)):
        raise ContractError("baseline target SHA is invalid")
    if not baseline.get("ok"):
        raise ContractError("baseline probe did not produce valid evidence")
    environment = baseline.get("environment")
    if not isinstance(environment, Mapping):
        raise ContractError("baseline environment descriptor is missing")
    expected_environment_fingerprint = hashlib.sha256(
        _canonical_json(environment).encode("utf-8")
    ).hexdigest()
    if baseline.get("environment_fingerprint") != expected_environment_fingerprint:
        raise ContractError("baseline environment fingerprint is invalid")
    if not _environment_satisfies_contract(validated, environment):
        raise ContractError("baseline environment does not satisfy its contract")
    if not str(baseline.get("evidence_ref") or "").startswith("outcome-evidence:sha256:"):
        raise ContractError("baseline evidence reference is invalid")
    if not _evidence_ref_is_valid(baseline):
        raise ContractError("baseline evidence seal does not match its contents")


def compare_observations(
    contract: Mapping[str, Any], baseline: Mapping[str, Any], current: Mapping[str, Any]
) -> str:
    if not baseline.get("ok") or not current.get("ok"):
        return "unmeasurable"
    if baseline.get("environment_fingerprint") != current.get("environment_fingerprint"):
        return "confounded"
    if baseline.get("source_schema_version") != current.get("source_schema_version"):
        return "confounded"
    if current.get("confounded_reasons"):
        return "confounded"
    baseline_counters = baseline.get("counter_observations") or []
    current_counters = current.get("counter_observations") or []
    rules = contract.get("counter_rules") or []
    if len(rules) != len(baseline_counters) or len(rules) != len(current_counters):
        return "unmeasurable"
    for rule_spec, before_counter, after_counter in zip(
        rules, baseline_counters, current_counters, strict=True
    ):
        if not before_counter.get("ok") or not after_counter.get("ok"):
            return "unmeasurable"
        if rule_spec.get("operator") == "must_remain_passing":
            if before_counter.get("value") != 0:
                return "unmeasurable"
            if after_counter.get("value") != 0:
                return "worsened"
        elif rule_spec.get("operator") == "must_not_increase":
            before_value = before_counter.get("value")
            after_value = after_counter.get("value")
            if not isinstance(before_value, (int, float)) or not isinstance(
                after_value, (int, float)
            ):
                return "unmeasurable"
            if after_value > before_value:
                return "worsened"
        else:
            return "unmeasurable"

    rule_spec = contract.get("success_rule") or contract.get("comparator") or {}
    rule = rule_spec.get("operator") or rule_spec.get("rule")
    baseline_value = baseline.get("observed_value") or baseline
    current_value = current.get("observed_value") or current
    before = baseline_value.get("value")
    after = current_value.get("value")
    tolerance = float(rule_spec.get("neutral_tolerance") or 0.0)
    if (
        rule in {"lower_is_better", "higher_is_better"}
        and isinstance(before, (int, float))
        and isinstance(after, (int, float))
        and tolerance > 0
    ):
        delta = abs(float(after) - float(before))
        if abs(float(before)) > 1e-9:
            if delta / abs(float(before)) < tolerance:
                return "neutral"
        elif delta < 1e-9:
            return "neutral"
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
    if rule == "higher_is_better" and isinstance(before, (int, float)) and isinstance(after, (int, float)):
        if after > before:
            return "improved"
        if after < before:
            return "worsened"
        return "neutral"
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
            cost_breakdown_json TEXT,
            source_refs_json TEXT,
            integration_sha TEXT,
            created_at INTEGER NOT NULL,
            completed_at INTEGER,
            UNIQUE(task_id, contract_hash, phase, attempt_no)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_outcome_contracts_task ON outcome_contracts(task_id)",
        "CREATE INDEX IF NOT EXISTS idx_outcome_attempts_task ON outcome_attempts(task_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_outcome_attempts_status ON outcome_attempts(status, lease_expires_at)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_outcome_attempts_one_active "
        "ON outcome_attempts(task_id, contract_hash) WHERE status = 'measuring'",
    )
    for statement in statements:
        conn.execute(statement)
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(outcome_attempts)").fetchall()
    }
    if "cost_breakdown_json" not in columns:
        conn.execute("ALTER TABLE outcome_attempts ADD COLUMN cost_breakdown_json TEXT")
    if "source_refs_json" not in columns:
        conn.execute("ALTER TABLE outcome_attempts ADD COLUMN source_refs_json TEXT")


def _missing_outcome_schema_objects(conn: sqlite3.Connection) -> list[str]:
    expected = (
        ("table", "outcome_contracts"),
        ("table", "outcome_attempts"),
        ("index", "idx_outcome_contracts_task"),
        ("index", "idx_outcome_attempts_task"),
        ("index", "idx_outcome_attempts_status"),
        ("index", "idx_outcome_attempts_one_active"),
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
    validated_contract = validate_probe_contract(contract)
    validate_baseline(validated_contract, baseline)
    ensure_schema(conn)
    now = int(time.time())
    contract_hash = str(validated_contract["contract_hash"])
    contract_id = str(validated_contract["contract_id"])
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
                _canonical_json(validated_contract),
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
                "claim": validated_contract["claim"],
                "outcome_class": validated_contract["outcome_class"],
                "trigger": validated_contract["trigger"],
                "baseline_target_sha": baseline["target_sha"],
                "baseline_evidence_ref": baseline["evidence_ref"],
                "baseline_source_generated_at": baseline["source_generated_at"],
                "baseline_source_schema_version": baseline["source_schema_version"],
                "baseline_environment_fingerprint": baseline["environment_fingerprint"],
                "baseline_cost_usd": float(baseline.get("cost_usd") or 0.0),
                "baseline_recorded_at": now,
                "release_fingerprint": release_fingerprint,
                # Append-only replay material. Probe output is already bounded
                # and represented only by structured values/hashes; no raw
                # stdout or secret-bearing environment is persisted here.
                "contract": validated_contract,
                "baseline": dict(baseline),
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
    if not _CONTENT_SHA_RE.fullmatch(str(contract_hash or "")):
        raise ValueError("contract_hash must be a full sha256")
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
    cost_breakdown: Mapping[str, float] | None = None,
    source_refs: Sequence[str] | None = None,
    integration_sha: str | None = None,
) -> bool:
    if status not in {"measured", "retryable_failure", "exhausted"}:
        raise ValueError("invalid terminal measurement status")
    if verdict not in OUTCOME_VERDICTS:
        raise ValueError("invalid outcome verdict")
    breakdown = {
        str(key): float(value)
        for key, value in (cost_breakdown or {}).items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }
    if any(value < 0 for value in breakdown.values()):
        raise ValueError("measurement cost components cannot be negative")
    if breakdown:
        cost_usd = round(sum(breakdown.values()), 8)
    if cost_usd < 0:
        raise ValueError("measurement cost cannot be negative")
    refs = sorted({str(ref) for ref in (source_refs or []) if str(ref).strip()})
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
            "cost_usd = ?, cost_breakdown_json = ?, source_refs_json = ?, "
            "integration_sha = ?, completed_at = ? "
            "WHERE dedupe_key = ? AND owner_token = ? AND status = 'measuring'",
            (
                status,
                _canonical_json(observation),
                verdict,
                float(cost_usd),
                _canonical_json(breakdown),
                _canonical_json(refs),
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
                "cost_breakdown": breakdown,
                "source_refs": refs,
                "integration_sha": integration_sha,
                "evidence_ref": observation.get("evidence_ref"),
                "observation": dict(observation),
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
            "SELECT a.dedupe_key, a.task_id, a.proposal_id, a.contract_hash, a.phase, "
            "a.attempt_no, c.contract_json FROM outcome_attempts a "
            "LEFT JOIN outcome_contracts c ON c.task_id = a.task_id "
            "AND c.contract_hash = a.contract_hash "
            "WHERE a.status = 'measuring' AND a.lease_expires_at < ?",
            (now_ts,),
        ).fetchall()
        for row in rows:
            try:
                contract = validate_probe_contract(json.loads(row["contract_json"] or "{}"))
                max_attempts = min(
                    3,
                    max(
                        1,
                        int((contract.get("measurement_budget") or {}).get("max_attempts") or 1),
                    ),
                )
            except (ContractError, ValueError, TypeError):
                max_attempts = 1
            exhausted = int(row["attempt_no"]) >= max_attempts
            status = "exhausted" if exhausted else "retryable_failure"
            verdict = "unmeasurable" if exhausted else None
            observation = {
                "ok": False,
                "error": "lease_expired",
                "captured_at": _utc_now(),
                "contract_sha256": row["contract_hash"],
            }
            observation["evidence_ref"] = "outcome-evidence:sha256:" + hashlib.sha256(
                _canonical_json(observation).encode("utf-8")
            ).hexdigest()
            conn.execute(
                "UPDATE outcome_attempts SET status = ?, verdict = ?, completed_at = ?, "
                "observation_json = ?, cost_breakdown_json = '{}', source_refs_json = '[]' "
                "WHERE dedupe_key = ? AND status = 'measuring'",
                (status, verdict, now_ts, _canonical_json(observation), row["dedupe_key"]),
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
                    "status": status,
                    "verdict": verdict,
                    "cost_usd": 0.0,
                    "cost_breakdown": {},
                    "source_refs": [],
                    "reason": "lease_expired",
                    "evidence_ref": observation["evidence_ref"],
                    "observation": observation,
                    "observation_sha256": hashlib.sha256(
                        _canonical_json(observation).encode("utf-8")
                    ).hexdigest(),
                },
                now=now_ts,
            )
    return len(rows)


_INTERVENTION_EVENT_KINDS = frozenset(
    {
        "freigabe_released",
        "freigabe_vetoed",
        "operator_override",
        "operator_decision",
        "manual_override",
    }
)


def _measurement_accounting(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    baseline: Mapping[str, Any],
    observation: Mapping[str, Any],
) -> tuple[dict[str, float], list[str], int, dict[str, Any]]:
    """Collect additive, source-addressable costs and operator interventions."""
    booked_refs: set[str] = set()
    if _table_exists(conn, "outcome_attempts"):
        for row in conn.execute(
            "SELECT source_refs_json FROM outcome_attempts "
            "WHERE task_id = ? AND status != 'measuring' ORDER BY created_at, attempt_no",
            (task_id,),
        ).fetchall():
            try:
                booked_refs.update(str(ref) for ref in json.loads(row["source_refs_json"] or "[]"))
            except (TypeError, ValueError):
                continue
    baseline_ref = str(baseline.get("evidence_ref") or "").strip()
    observation_ref = str(observation.get("evidence_ref") or "").strip()
    baseline_unbooked = bool(baseline_ref and baseline_ref not in booked_refs)
    breakdown = {
        "research_usd": (
            max(0.0, float(baseline.get("research_cost_usd") or 0.0))
            if baseline_unbooked else 0.0
        ),
        "delivery_usd": 0.0,
        "review_usd": 0.0,
        "baseline_probe_usd": (
            max(0.0, float(baseline.get("cost_usd") or 0.0))
            if baseline_unbooked else 0.0
        ),
        "outcome_probe_usd": (
            max(0.0, float(observation.get("cost_usd") or 0.0))
            if observation_ref and observation_ref not in booked_refs else 0.0
        ),
    }
    refs = [
        str(ref)
        for ref in (baseline_ref, observation_ref)
        if ref and ref not in booked_refs
    ]
    known_task_runs = 0
    unknown_task_run_refs: list[str] = []
    if _table_exists(conn, "task_runs"):
        columns = {row[1] for row in conn.execute("PRAGMA table_info(task_runs)").fetchall()}
        if {"id", "task_id", "profile", "cost_usd"}.issubset(columns):
            rows = conn.execute(
                "SELECT id, profile, cost_usd FROM task_runs "
                "WHERE task_id = ? ORDER BY id",
                (task_id,),
            ).fetchall()
            for row in rows:
                run_ref = f"task-run:{row['id']}"
                if run_ref in booked_refs:
                    continue
                refs.append(run_ref)
                if row["cost_usd"] is None:
                    unknown_task_run_refs.append(run_ref)
                    continue
                known_task_runs += 1
                value = max(0.0, float(row["cost_usd"]))
                component = (
                    "review_usd"
                    if "review" in str(row["profile"] or "").lower()
                    else "delivery_usd"
                )
                breakdown[component] += value
    interventions = 0
    if _table_exists(conn, "task_events"):
        placeholders = ",".join("?" for _ in _INTERVENTION_EVENT_KINDS)
        rows = conn.execute(
            f"SELECT id FROM task_events WHERE task_id = ? AND kind IN ({placeholders}) "
            "ORDER BY id",
            (task_id, *_INTERVENTION_EVENT_KINDS),
        ).fetchall()
        intervention_refs = [
            f"task-event:{row['id']}"
            for row in rows
            if f"task-event:{row['id']}" not in booked_refs
        ]
        interventions = len(intervention_refs)
        refs.extend(intervention_refs)
    cost_accounting = {
        "status": "partial" if unknown_task_run_refs else "complete",
        "known_task_runs": known_task_runs,
        "unknown_task_runs": len(unknown_task_run_refs),
        "unknown_task_run_refs": sorted(unknown_task_run_refs),
    }
    return (
        {key: round(value, 8) for key, value in breakdown.items()},
        sorted(set(refs)),
        interventions,
        cost_accounting,
    )


def _real_integrator_sha(payload: Any) -> str | None:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            return None
    if not isinstance(payload, Mapping):
        return None
    value = str(payload.get("merge_commit") or "").strip()
    return value.lower() if _SHA_RE.fullmatch(value) else None


def _deployment_witnesses(
    conn: sqlite3.Connection, task_id: str
) -> list[tuple[int, str]]:
    placeholders = ",".join("?" for _ in _DEPLOYMENT_EVENT_KINDS)
    rows = conn.execute(
        f"SELECT created_at, payload FROM task_events WHERE task_id = ? "
        f"AND kind IN ({placeholders}) ORDER BY id",
        (task_id, *_DEPLOYMENT_EVENT_KINDS),
    ).fetchall()
    witnesses: list[tuple[int, str]] = []
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except (ValueError, TypeError):
            continue
        deployed = str(payload.get("deployed_sha") or "").lower()
        running = str(payload.get("running_sha") or "").lower()
        if _SHA_RE.fullmatch(deployed) and deployed == running:
            witnesses.append((int(row["created_at"]), deployed))
    return witnesses


def _overlapping_runtime_windows(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    contract: Mapping[str, Any],
    deployed_at: int,
) -> list[str]:
    """Return other same-class tasks whose reviewed effect window overlaps."""
    if not _table_exists(conn, "outcome_contracts"):
        return []
    window = contract.get("observation_window") or {}
    duration = max(
        1,
        int(window.get("min_age_seconds") or window.get("max_age_seconds") or 1),
    )
    start = int(deployed_at)
    end = start + duration
    overlaps: list[str] = []
    rows = conn.execute(
        "SELECT task_id, contract_json FROM outcome_contracts WHERE task_id != ?",
        (task_id,),
    ).fetchall()
    for row in rows:
        try:
            other = validate_probe_contract(json.loads(row["contract_json"] or "{}"))
        except (ContractError, ValueError, TypeError):
            continue
        if (
            other.get("trigger") != "deployed_runtime"
            or other.get("outcome_class") != contract.get("outcome_class")
        ):
            continue
        other_window = other.get("observation_window") or {}
        other_duration = max(
            1,
            int(
                other_window.get("min_age_seconds")
                or other_window.get("max_age_seconds")
                or 1
            ),
        )
        for other_at, _sha in _deployment_witnesses(conn, str(row["task_id"])):
            if start <= other_at + other_duration and other_at <= end:
                overlaps.append(str(row["task_id"]))
                break
    return sorted(set(overlaps))


def measurement_readiness(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    contract: Mapping[str, Any],
    now: int | None = None,
) -> dict[str, Any]:
    validated = validate_probe_contract(contract)
    trigger = validated["trigger"]
    if trigger == "integrated_commit":
        by_kind: dict[str, dict[str, int]] = {
            kind: {} for kind in _INTEGRATION_EVENT_KINDS
        }
        placeholders = ",".join("?" for _ in _INTEGRATION_EVENT_KINDS)
        rows = conn.execute(
            f"SELECT id, kind, payload FROM task_events WHERE task_id = ? AND kind IN ({placeholders}) "
            "ORDER BY id DESC",
            (task_id, *_INTEGRATION_EVENT_KINDS),
        ).fetchall()
        for row in rows:
            sha = _real_integrator_sha(row["payload"])
            if sha:
                by_kind[str(row["kind"])].setdefault(sha, int(row["id"]))
        common = set(by_kind["integration_merged"]) & set(
            by_kind["INTEGRATOR_VERIFIED"]
        )
        if common:
            newest = max(
                common,
                key=lambda sha: max(
                    by_kind["integration_merged"][sha],
                    by_kind["INTEGRATOR_VERIFIED"][sha],
                ),
            )
            return {"ready": True, "reason": None, "integration_sha": newest}
        return {"ready": False, "reason": "integration_sha_missing", "integration_sha": None}

    if trigger == "deployed_runtime":
        witnesses = _deployment_witnesses(conn, task_id)
        if not witnesses:
            return {
                "ready": False,
                "reason": "deployment_sha_missing",
                "integration_sha": None,
            }
        deployed_at, deployed = witnesses[-1]
        window = validated.get("observation_window") or {}
        now_ts = int(time.time() if now is None else now)
        age = now_ts - deployed_at
        min_age = max(0, int(window.get("min_age_seconds") or 0))
        if age < min_age:
            return {
                "ready": False,
                "reason": "observation_window_not_mature",
                "integration_sha": deployed,
            }
        reasons: list[str] = []
        max_age = int(window.get("max_age_seconds") or 0)
        if max_age and age > max_age:
            reasons.append("stale_observation_window")
        overlaps = _overlapping_runtime_windows(
            conn,
            task_id=task_id,
            contract=validated,
            deployed_at=deployed_at,
        )
        if overlaps:
            reasons.append("overlapping_effect_window")
        result: dict[str, Any] = {
            "ready": True,
            "reason": None,
            "integration_sha": deployed,
        }
        if reasons:
            result["confounded_reasons"] = reasons
            result["confounded_task_ids"] = overlaps
        return result

    return {"ready": False, "reason": "unknown_trigger", "integration_sha": None}


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
    attempt_accounting: dict[str, dict[str, Any]] = {}
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
                        f"SELECT *, rowid AS outcome_rowid FROM outcome_attempts "
                        f"WHERE proposal_id IN ({marks}) "
                        "ORDER BY proposal_id, outcome_rowid",
                        proposal_ids,
                    ).fetchall():
                        attempt_proposal_id = str(row["proposal_id"])
                        attempts[attempt_proposal_id] = row
                        aggregate = attempt_accounting.setdefault(
                            attempt_proposal_id,
                            {
                                "known_cost_usd": 0.0,
                                "breakdown": {},
                                "complete": True,
                                "unknown_task_run_refs": set(),
                                "operator_interventions": 0,
                            },
                        )
                        aggregate["known_cost_usd"] += float(row["cost_usd"] or 0.0)
                        try:
                            row_breakdown = json.loads(row["cost_breakdown_json"] or "{}")
                        except (ValueError, TypeError):
                            row_breakdown = {}
                        if isinstance(row_breakdown, Mapping):
                            for key, value in row_breakdown.items():
                                if isinstance(value, (int, float)) and not isinstance(value, bool):
                                    aggregate["breakdown"][str(key)] = round(
                                        float(aggregate["breakdown"].get(str(key), 0.0))
                                        + float(value),
                                        8,
                                    )
                        try:
                            row_observation = json.loads(row["observation_json"] or "{}")
                        except (ValueError, TypeError):
                            row_observation = {}
                        cost_accounting = (
                            row_observation.get("cost_accounting")
                            if isinstance(row_observation, Mapping) else None
                        )
                        if not isinstance(cost_accounting, Mapping) or (
                            cost_accounting.get("status") != "complete"
                        ):
                            aggregate["complete"] = False
                        if isinstance(cost_accounting, Mapping):
                            aggregate["unknown_task_run_refs"].update(
                                str(ref)
                                for ref in cost_accounting.get("unknown_task_run_refs", [])
                                if str(ref).strip()
                            )
                        if isinstance(row_observation, Mapping):
                            aggregate["operator_interventions"] += int(
                                row_observation.get("operator_interventions") or 0
                            )

        projected: list[dict[str, Any]] = []
        for raw in items:
            item = dict(raw)
            proposal_id = str(item.get("id") or "")
            contract = contracts.get(proposal_id)
            attempt = attempts.get(proposal_id)
            if contract is not None:
                item["contract_hash"] = contract["contract_hash"]
                try:
                    contract_payload = json.loads(contract["contract_json"])
                except (ValueError, TypeError):
                    contract_payload = {}
                item["probe_contract"] = {
                    **contract_payload,
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
                        # A contract and baseline make the attempt auditable,
                        # but do not verify an outcome.  Only a terminal common
                        # verifier attempt earns contract_verified evidence.
                        "evidence_grade": "legacy_observational",
                        "calibration_eligible": False,
                    }
                )
            if attempt is not None and canonical["outcome_applicability"] == "applicable":
                accounting = attempt_accounting.get(proposal_id) or {}
                canonical["measurement_status"] = attempt["status"]
                canonical["outcome_verdict"] = attempt["verdict"]
                if attempt["status"] in {"measured", "exhausted"}:
                    canonical["evidence_grade"] = "contract_verified"
                item["outcome_cost_usd"] = round(
                    float(accounting.get("known_cost_usd") or 0.0), 8
                )
                item["outcome_cost_status"] = (
                    "complete" if accounting.get("complete") else "partial"
                )
                item["outcome_measured_at"] = attempt["completed_at"]
                item["outcome_integration_sha"] = attempt["integration_sha"]
                item["outcome_cost_breakdown"] = dict(accounting.get("breakdown") or {})
                item["outcome_unknown_cost_refs"] = sorted(
                    accounting.get("unknown_task_run_refs") or []
                )
                try:
                    item["outcome_observation"] = json.loads(attempt["observation_json"] or "null")
                except (ValueError, TypeError):
                    item["outcome_observation"] = None
                if isinstance(item["outcome_observation"], Mapping):
                    item["outcome_operator_interventions"] = int(
                        accounting.get("operator_interventions") or 0
                    )
            item.update(canonical)
            item.setdefault("outcome_schema_version", OUTCOME_SCHEMA_VERSION)
            projected.append(item)
        return projected
    finally:
        if own_conn and opened is not None:
            opened.close()


def outcome_metrics(items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    applicable = [item for item in items if item.get("outcome_applicability") == "applicable"]
    measured = [
        item
        for item in applicable
        if item.get("measurement_status") in {"measured", "exhausted"}
        and item.get("outcome_verdict") is not None
    ]
    verified = [item for item in measured if item.get("evidence_grade") == "contract_verified"]
    legacy = [item for item in measured if item.get("evidence_grade") != "contract_verified"]
    verified_counts = {
        verdict: sum(1 for item in verified if item.get("outcome_verdict") == verdict)
        for verdict in ("improved", "neutral", "worsened", "unmeasurable", "confounded")
    }
    legacy_counts = {
        verdict: sum(1 for item in legacy if item.get("outcome_verdict") == verdict)
        for verdict in ("improved", "neutral", "worsened", "unmeasurable", "confounded")
    }
    known_cost = sum(float(item.get("outcome_cost_usd") or 0.0) for item in verified)
    cost_complete = [item for item in verified if item.get("outcome_cost_status") == "complete"]
    unknown_cost_outcomes = len(verified) - len(cost_complete)
    complete_cost = unknown_cost_outcomes == 0
    interventions = sum(int(item.get("outcome_operator_interventions") or 0) for item in verified)
    integrated = [
        item for item in applicable if str(item.get("delivery_state") or "") == "integrated"
    ]
    directional = (
        verified_counts["improved"] + verified_counts["neutral"] + verified_counts["worsened"]
    )
    verified_improved = verified_counts["improved"]
    return {
        "applicable": len(applicable),
        "not_applicable": len(items) - len(applicable),
        "pending": sum(
            1
            for item in applicable
            if item.get("measurement_status") in {"pending", "measuring", "retryable_failure"}
        ),
        "measured": len(measured),
        "verified_measured": len(verified),
        "measurement_coverage": len(verified) / len(integrated) if integrated else 0.0,
        "outcome_coverage": len(verified) / len(integrated) if integrated else 0.0,
        "directional_coverage": directional / len(integrated) if integrated else 0.0,
        "verified_directional_denominator": directional,
        "verified_benefit_rate": verified_improved / directional if directional else None,
        "regression_rate": verified_counts["worsened"] / directional if directional else None,
        "unmeasurable_rate": (
            (verified_counts["unmeasurable"] + verified_counts["confounded"]) / len(verified)
            if verified else None
        ),
        "verified_improved": verified_improved,
        "legacy_improved": legacy_counts["improved"],
        # Existing API keys now deliberately mean contract-verified outcomes.
        "improved": verified_improved,
        "neutral": verified_counts["neutral"],
        "worsened": verified_counts["worsened"],
        "unmeasurable": verified_counts["unmeasurable"],
        "confounded": verified_counts["confounded"],
        "measurement_cost_usd": round(known_cost, 6) if complete_cost else None,
        "known_measurement_cost_usd": round(known_cost, 6),
        "cost_complete_outcomes": len(cost_complete),
        "unknown_cost_outcomes": unknown_cost_outcomes,
        "cost_coverage": len(cost_complete) / len(verified) if verified else 0.0,
        "cost_per_measured_usd": (
            known_cost / len(verified) if complete_cost and verified else None
        ),
        "cost_per_improved_usd": (
            known_cost / verified_improved if complete_cost and verified_improved else None
        ),
        "cost_per_verified_benefit_usd": (
            known_cost / verified_improved if complete_cost and verified_improved else None
        ),
        "operator_interventions": interventions,
        "operator_interventions_per_verified_benefit": (
            interventions / verified_improved if verified_improved else None
        ),
    }


def shadow_marker_path() -> Path:
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()) / "state" / "autoresearch-outcome-shadow.enabled"


def shadow_enabled() -> bool:
    return shadow_marker_path().is_file()


def outcome_enforcement_enabled() -> bool:
    """Hard policy boundary for this release: observations never steer work."""
    return False


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
            try:
                contract = validate_probe_contract(json.loads(row["contract_json"]))
                baseline = json.loads(row["baseline_json"])
                validate_baseline(contract, baseline)
            except (ContractError, ValueError, TypeError):
                summary["pending"] += 1
                continue
            active = conn.execute(
                "SELECT 1 FROM outcome_attempts WHERE task_id = ? AND contract_hash = ? "
                "AND status = 'measuring' LIMIT 1",
                (row["task_id"], row["contract_hash"]),
            ).fetchone()
            if active is not None:
                summary["pending"] += 1
                continue
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
            budget = contract["measurement_budget"]
            max_attempts = min(3, max(1, int(budget.get("max_attempts") or 1)))
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
                lease_seconds=int(budget.get("timeout_seconds") or 30) + 30,
            )
            if claim is None:
                summary["pending"] += 1
                continue
            try:
                observation = capture_probe(
                    contract,
                    repo_root=root,
                    expected_target_sha=readiness["integration_sha"],
                )
                readiness_reasons = list(readiness.get("confounded_reasons") or [])
                if readiness_reasons:
                    observation = _seal_evidence(
                        {
                            **observation,
                            "confounded_reasons": sorted(
                                set(
                                    list(observation.get("confounded_reasons") or [])
                                    + readiness_reasons
                                )
                            ),
                            "confounded_task_ids": list(
                                readiness.get("confounded_task_ids") or []
                            ),
                        }
                    )
            except (ContractError, OSError, subprocess.SubprocessError) as exc:
                observation = _seal_evidence(
                    {
                        "ok": False,
                        "contract_sha256": contract["contract_sha256"],
                        "expected_target_sha": readiness["integration_sha"],
                        "error": type(exc).__name__,
                        "captured_at": _utc_now(),
                        "cost_usd": 0.0,
                    }
                )
            verdict = compare_observations(contract, baseline, observation)
            if observation.get("ok"):
                terminal_status = "measured"
            elif attempt_no < max_attempts:
                terminal_status = "retryable_failure"
                verdict = None
            else:
                terminal_status = "exhausted"
                verdict = "unmeasurable"
            cost_breakdown, source_refs, interventions, cost_accounting = _measurement_accounting(
                conn,
                task_id=row["task_id"],
                baseline=baseline,
                observation=observation,
            )
            observation = _seal_evidence(
                {
                    **observation,
                    "operator_interventions": interventions,
                    "cost_breakdown": cost_breakdown,
                    "cost_accounting": cost_accounting,
                    "source_refs": source_refs,
                }
            )
            finalized = finalize_measurement_attempt(
                conn,
                dedupe_key=claim.dedupe_key,
                owner_token=claim.owner_token,
                status=terminal_status,
                verdict=verdict,
                observation=observation,
                cost_breakdown=cost_breakdown,
                source_refs=source_refs,
                integration_sha=readiness["integration_sha"],
            )
            if finalized:
                summary[terminal_status] += 1
                summary["cost_usd"] = round(
                    float(summary["cost_usd"]) + sum(cost_breakdown.values()), 8
                )
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


def strategist_outcome_class(record: Mapping[str, Any]) -> str | None:
    contract = record.get("probe_contract")
    if isinstance(contract, Mapping) and contract.get("outcome_class"):
        return str(contract["outcome_class"])
    key = str(record.get("metric_key") or "").strip()
    if not key:
        return None
    basename = key.rsplit(".", 1)[-1]
    direction = _VISION_METRIC_DIRECTIONS.get(key, _VISION_METRIC_DIRECTIONS.get(basename))
    if direction is None:
        return None
    operator = "higher_is_better" if direction == 1 else "lower_is_better"
    return f"vision-metric:{key}:{operator}/v1"


def project_strategist_outcomes(
    records: Sequence[Any], *, conn: sqlite3.Connection | None, terminalize_missing: bool = False
) -> list[Any]:
    """Materialize the legacy Strategist readmodel from contract/task truth."""
    task_columns: set[str] = set()
    has_events = False
    if conn is not None and _table_exists(conn, "tasks"):
        task_columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
        has_events = _table_exists(conn, "task_events")
    projected: list[Any] = []
    for raw in records:
        if not isinstance(raw, Mapping):
            projected.append(raw)
            continue
        rec = normalize_strategist_record(raw)
        root_id = rec.get("root_task_id")
        rec.setdefault("source", "strategist")
        rec.setdefault("subject_type", "strategist_lever")
        rec.setdefault("subject_id", root_id or rec.get("lever_key"))
        rec.setdefault("legacy_provenance", "lever-outcomes.json/v1")
        outcome_class = strategist_outcome_class(rec)
        if outcome_class is not None:
            rec["outcome_class"] = outcome_class

        contract_row = None
        attempt_row = None
        if (
            conn is not None
            and root_id is not None
            and _table_exists(conn, "outcome_contracts")
        ):
            contract_row = conn.execute(
                "SELECT * FROM outcome_contracts WHERE task_id = ? ORDER BY created_at DESC LIMIT 1",
                (root_id,),
            ).fetchone()
            if contract_row is not None and _table_exists(conn, "outcome_attempts"):
                attempt_row = conn.execute(
                    "SELECT * FROM outcome_attempts WHERE task_id = ? AND contract_hash = ? "
                    "ORDER BY created_at DESC, attempt_no DESC LIMIT 1",
                    (root_id, contract_row["contract_hash"]),
                ).fetchone()
        if contract_row is not None:
            try:
                rec["probe_contract"] = json.loads(contract_row["contract_json"])
                rec["outcome_baseline"] = json.loads(contract_row["baseline_json"])
            except (TypeError, ValueError):
                pass
            rec["contract_hash"] = contract_row["contract_hash"]
            rec["contract_registered"] = True
        if attempt_row is not None and attempt_row["status"] in {"measured", "exhausted"}:
            try:
                observation = json.loads(attempt_row["observation_json"] or "null")
            except (TypeError, ValueError):
                observation = None
            rec.update(
                {
                    "status": "measured",
                    "measured_at": attempt_row["completed_at"],
                    "outcome_applicability": "applicable",
                    "measurement_status": attempt_row["status"],
                    "outcome_verdict": attempt_row["verdict"],
                    "verdict": attempt_row["verdict"],
                    "evidence_grade": "contract_verified",
                    "outcome_authority": "task_events",
                    "outcome_integration_sha": attempt_row["integration_sha"],
                    "outcome_cost_usd": float(attempt_row["cost_usd"] or 0.0),
                    "outcome_observation": observation,
                }
            )
            projected.append(rec)
            continue

        # Existing measured rows are immutable historical observations. Their
        # verdict and timestamp survive even when later task retention changed.
        if rec.get("status") == "measured" or rec.get("measured_at") is not None:
            rec.update(
                {
                    "outcome_applicability": "applicable",
                    "measurement_status": "measured",
                    "outcome_verdict": rec.get("outcome_verdict", rec.get("verdict")),
                    "evidence_grade": "legacy_observational",
                    "outcome_authority": "legacy_ledger",
                }
            )
            projected.append(rec)
            continue

        task = None
        events: list[sqlite3.Row] = []
        if conn is not None and root_id is not None and "status" in task_columns:
            task = conn.execute(
                "SELECT status FROM tasks WHERE id = ?", (root_id,)
            ).fetchone()
            if has_events:
                events = conn.execute(
                    "SELECT id, kind, payload, created_at FROM task_events "
                    "WHERE task_id = ? ORDER BY id",
                    (root_id,),
                ).fetchall()
        by_kind: dict[str, set[str]] = {kind: set() for kind in _INTEGRATION_EVENT_KINDS}
        deployment_sha: str | None = None
        terminal_event: str | None = None
        event_refs: list[str] = []
        shipped_at: int | None = None
        for event in events:
            event_refs.append(f"task-event:{event['id']}")
            kind = str(event["kind"])
            if kind in by_kind:
                sha = _real_integrator_sha(event["payload"])
                if sha:
                    by_kind[kind].add(sha)
                    shipped_at = int(event["created_at"])
            if kind in _DEPLOYMENT_EVENT_KINDS:
                try:
                    payload = json.loads(event["payload"] or "{}")
                except (TypeError, ValueError):
                    payload = {}
                deployed = str(payload.get("deployed_sha") or "").lower()
                running = str(payload.get("running_sha") or "").lower()
                if _SHA_RE.fullmatch(deployed) and deployed == running:
                    deployment_sha = deployed
                    shipped_at = int(event["created_at"])
            if kind in {"freigabe_completed", "freigabe_vetoed"}:
                terminal_event = kind
        integrated = by_kind["integration_merged"] & by_kind["INTEGRATOR_VERIFIED"]
        delivery_sha = sorted(integrated)[-1] if integrated else deployment_sha
        if delivery_sha:
            rec.update(
                {
                    "status": "shipped",
                    "shipped_at": rec.get("shipped_at") or shipped_at,
                    "delivery_state": "integrated",
                    "outcome_applicability": "applicable",
                    "measurement_status": "pending",
                    "outcome_verdict": None,
                    "evidence_grade": "legacy_observational",
                    "outcome_authority": "task_events",
                    "outcome_delivery_sha": delivery_sha,
                    "outcome_source_refs": event_refs,
                }
            )
            projected.append(rec)
            continue

        task_status = str(task["status"] or "") if task is not None else "missing"
        terminal = terminal_event is not None or task_status in {
            "done", "archived", "failed", "cancelled", "canceled"
        } or (task is None and terminalize_missing)
        if terminal:
            disposition = {
                "freigabe_completed": "done_elsewhere",
                "freigabe_vetoed": "vetoed",
            }.get(terminal_event or "", terminal_event or task_status or "missing_task")
            rec.update(
                {
                    "status": "archived",
                    "delivery_state": "none",
                    "delivery_disposition": disposition,
                    "outcome_applicability": "not_applicable",
                    "measurement_status": "not_started",
                    "outcome_verdict": None,
                    "evidence_grade": "legacy_observational",
                    "calibration_eligible": False,
                    "outcome_authority": "task_events" if task is not None else "legacy_ledger",
                    "outcome_source_refs": event_refs,
                }
            )
        elif task is not None:
            rec.update(
                {
                    "delivery_state": rec.get("delivery_state") or "queued",
                    "outcome_applicability": "applicable",
                    "measurement_status": "pending",
                    "outcome_verdict": None,
                    "evidence_grade": "legacy_observational",
                    "outcome_authority": "task_events",
                    "outcome_source_refs": event_refs,
                }
            )
        else:
            # Runtime compatibility for not-yet-migrated historical fixtures.
            # The live migration calls with terminalize_missing=True.
            rec["outcome_authority"] = "legacy_ledger"
        projected.append(rec)
    return projected


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
    missing_schema_objects: list[str] = []
    truth_conn: sqlite3.Connection | None = None
    if db_path is not None:
        if not db_path.is_file():
            raise FileNotFoundError(f"Kanban database does not exist: {db_path}")
        uri = f"file:{db_path.resolve()}?mode=ro"
        truth_conn = sqlite3.connect(uri, uri=True)
        truth_conn.row_factory = sqlite3.Row
        missing_schema_objects = _missing_outcome_schema_objects(truth_conn)
    try:
        strategist_updated = project_strategist_outcomes(
            strategist_current,
            conn=truth_conn,
            terminalize_missing=True,
        )
    finally:
        if truth_conn is not None:
            truth_conn.close()
    strategist_changed = _canonical_json(strategist_updated) != _canonical_json(strategist_current)

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
                truth: sqlite3.Connection | None = None
                if db_path is not None:
                    truth = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
                    truth.row_factory = sqlite3.Row
                try:
                    return project_strategist_outcomes(
                        current, conn=truth, terminalize_missing=True
                    )
                finally:
                    if truth is not None:
                        truth.close()

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
    "capture_vision_snapshot_baseline",
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
    "outcome_enforcement_enabled",
    "outcome_metrics",
    "project_strategist_outcomes",
    "recover_expired_attempts",
    "register_contract",
    "release_fingerprint",
    "run_shadow_verifier",
    "seal_evidence",
    "shadow_enabled",
    "shadow_marker_path",
    "strategist_outcome_class",
    "shared_state_lock",
    "validate_baseline",
    "validate_probe_contract",
]
