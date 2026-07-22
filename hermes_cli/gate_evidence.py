"""Raw-free, content-addressed receipts for closed verification gates.

Gate evidence is record-only unless a caller explicitly opts into reuse.  This
module deliberately does not execute commands or mutate Kanban state.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

GATE_EVIDENCE_VERSION = "1"
MAX_REUSE_AGE = timedelta(hours=24)
REUSABLE_PHASES = frozenset({"pre_submit", "review"})
_SECRET_KEY = re.compile(r"(?:secret|token|password|passwd|api[_-]?key|credential|cookie|authorization)", re.I)
_RAW_KEYS = frozenset({"stdout", "stderr", "output", "raw", "log", "logs"})


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=False)
    if result.returncode:
        raise ValueError(f"not a readable git workspace: git {' '.join(args)} failed")
    return result.stdout.strip()


def _normalize_relative_paths(values: Iterable[str]) -> list[str]:
    normalized: set[str] = set()
    for raw in values:
        value = str(raw).replace("\\", "/").strip()
        path = PurePosixPath(value)
        if not value or path.is_absolute() or ".." in path.parts:
            raise ValueError("fingerprint paths must be non-empty workspace-relative paths")
        normalized.add(path.as_posix())
    return sorted(normalized)


def _file_digests(repo: Path, paths: Sequence[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    root = repo.resolve()
    for relative in _normalize_relative_paths(paths):
        candidate = (root / relative).resolve()
        if root not in candidate.parents and candidate != root:
            raise ValueError("fingerprint path escapes workspace")
        result[relative] = _sha256(candidate.read_bytes()) if candidate.is_file() else "missing"
    return result


def default_runtime_fingerprint(repo: str | Path) -> dict[str, str]:
    """Return stable runtime/toolchain identity without paths or environment data."""
    root = Path(repo)
    runtime = {
        "os": platform.system().lower(),
        "arch": platform.machine().lower(),
        "python_implementation": platform.python_implementation(),
        "python": platform.python_version(),
        "git": _version(["git", "--version"], root),
    }
    if (root / "package.json").exists():
        runtime["node"] = _version(["node", "--version"], root)
        runtime["npm"] = _version(["npm", "--version"], root)
    return runtime


def _version(argv: Sequence[str], cwd: Path) -> str:
    try:
        result = subprocess.run(list(argv), cwd=cwd, text=True, capture_output=True,
                                check=False, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"
    value = (result.stdout or result.stderr).strip().splitlines()
    return value[0] if result.returncode == 0 and value else "unavailable"


@dataclass(frozen=True)
class GateFingerprint:
    digest: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class GateEvidence:
    fingerprint: str
    gate_id: str
    gate_version: str
    phase: str
    status: str
    started_at: str
    finished_at: str
    duration_seconds: float
    results: list[dict[str, Any]]
    head_sha: str
    artifacts: list[str] = field(default_factory=list)
    version: str = GATE_EVIDENCE_VERSION

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "GateEvidence":
        fields = {key: value[key] for key in cls.__dataclass_fields__ if key in value}
        return cls(**fields)


@dataclass(frozen=True)
class GateEvidenceReceipt:
    path: Path
    digest: str
    evidence: GateEvidence


def build_gate_fingerprint(
    repo: str | Path,
    *,
    gate_id: str,
    gate_version: str,
    test_selection: Sequence[str] = (),
    config_paths: Sequence[str] = (),
    lockfile_paths: Sequence[str] = (),
    env: Mapping[str, str] | None = None,
    allowed_env: Sequence[str] = (),
    runtime: Mapping[str, str] | None = None,
) -> GateFingerprint:
    """Build a canonical fingerprint; only explicitly allowlisted, non-secret env is used."""
    root = Path(repo).resolve()
    if not gate_id or not gate_version:
        raise ValueError("gate_id and gate_version are required")
    safe_env: dict[str, str] = {}
    source_env = os.environ if env is None else env
    for key in sorted(set(allowed_env)):
        if _SECRET_KEY.search(key):
            continue
        if key in source_env:
            value = str(source_env[key])
            if str(root) not in value:
                safe_env[key] = value
    payload: dict[str, Any] = {
        "version": GATE_EVIDENCE_VERSION,
        "gate_id": gate_id,
        "gate_version": str(gate_version),
        "tree_sha": _git(root, "rev-parse", "HEAD^{tree}"),
        # History identity makes an otherwise tree-identical rebase/merge a miss.
        "head_sha": _git(root, "rev-parse", "HEAD"),
        "tests": _normalize_relative_paths(test_selection),
        "config": _file_digests(root, config_paths),
        "lockfiles": _file_digests(root, lockfile_paths),
        "runtime": dict(sorted((runtime or default_runtime_fingerprint(root)).items())),
        "env": safe_env,
    }
    return GateFingerprint(digest=_sha256(_canonical(payload)), payload=payload)


class GateEvidenceStore:
    """Atomic 0600 evidence files scoped to one terminal-run artifact directory."""

    def __init__(self, artifact_dir: str | Path):
        self.artifact_dir = Path(artifact_dir)

    def write(self, evidence: GateEvidence) -> GateEvidenceReceipt:
        payload = asdict(evidence)
        _assert_raw_free(payload)
        data = _canonical(payload) + b"\n"
        self.artifact_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.artifact_dir, 0o700)
        prefix = f"gate-evidence-{_safe_name(evidence.gate_id)}-{evidence.fingerprint[:12]}-"
        fd, temporary = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=self.artifact_dir)
        final = Path(temporary).with_suffix(".json")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, final)
            os.chmod(final, 0o600)
        except Exception:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise
        return GateEvidenceReceipt(path=final, digest=_sha256(data), evidence=evidence)

    def find_reusable(
        self,
        fingerprint: str,
        *,
        phase: str,
        reuse_enabled: bool = False,
        now: datetime | None = None,
        max_age: timedelta = MAX_REUSE_AGE,
    ) -> GateEvidenceReceipt | None:
        normalized_phase = phase.lower().replace("-", "_")
        if reuse_enabled is not True or normalized_phase not in REUSABLE_PHASES:
            return None
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        if not self.artifact_dir.is_dir():
            return None
        candidates = sorted(self.artifact_dir.glob("gate-evidence-*.json"), reverse=True)
        for path in candidates:
            try:
                data = path.read_bytes()
                evidence = GateEvidence.from_dict(json.loads(data))
                finished = datetime.fromisoformat(evidence.finished_at.replace("Z", "+00:00"))
                if finished.tzinfo is None:
                    finished = finished.replace(tzinfo=timezone.utc)
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                continue
            if (evidence.fingerprint == fingerprint and evidence.status == "passed"
                    and evidence.phase.lower().replace("-", "_") in REUSABLE_PHASES
                    and timedelta(0) <= current - finished <= max_age):
                return GateEvidenceReceipt(path=path, digest=_sha256(data), evidence=evidence)
        return None


def _safe_name(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-.") or "gate"


def _assert_raw_free(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).lower() in _RAW_KEYS:
                raise ValueError(f"raw output field is forbidden in GateEvidence: {key}")
            _assert_raw_free(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _assert_raw_free(child)
