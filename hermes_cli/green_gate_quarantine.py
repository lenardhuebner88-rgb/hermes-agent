"""Exact-name quarantine policy for red nightly green-gate test files.

Only a canonical repository-relative file name written by a human can be
quarantined. Unknown gates, malformed records, wildcard entries, and unlisted
files all fail closed.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable

DEFAULT_ALLOWLIST_RELATIVE_PATH = Path("scripts/green_gate_quarantine_allowlist.json")
FAILURE_CLASS_ENVIRONMENT = "environment_quarantine"
FAILURE_CLASS_REGRESSION = "regression"
_PATTERN_CHARS = frozenset("*?[]{}!")
_SAFE_PATH_RE = re.compile(r"[A-Za-z0-9._@/+-]+")
_PY_FAILURE_SECTION_RE = re.compile(
    r"^=== (\d+) files? (?:with test failures|where all tests passed but pytest "
    r"exited non-zero|where no tests ran)\b.*===$",
    re.MULTILINE,
)
_PY_ANY_RED_SECTION_RE = re.compile(
    r"^=== \d+ files? .*?(?:fail|error|no tests ran|exited non-zero|timed out).*===$",
    re.IGNORECASE | re.MULTILINE,
)
_VITEST_FAILED_FILES_RE = re.compile(r"\bTest Files\s+(\d+) failed\b")
_VITEST_UNATTRIBUTED_RE = re.compile(
    r"\bUnhandled (?:Errors?|Rejections?)\b|\bErrors?\s+\d+\s+errors?\b",
    re.IGNORECASE,
)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
MAX_FAILURE_EVIDENCE_FILES = 100


class QuarantinePolicyError(ValueError):
    """The checked-in quarantine policy is malformed or non-exact."""


@dataclass(frozen=True)
class QuarantinePolicy:
    entries: tuple[str, ...]
    sha256: str

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self.entries)


def _canonical_name(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise QuarantinePolicyError("quarantine entries must be non-empty strings")
    if "\\" in value or any(char in value for char in _PATTERN_CHARS):
        raise QuarantinePolicyError(
            f"quarantine entry must be an exact POSIX path, got {value!r}"
        )
    if _SAFE_PATH_RE.fullmatch(value) is None:
        raise QuarantinePolicyError(
            f"quarantine entry contains unsafe path characters: {value!r}"
        )
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value:
        raise QuarantinePolicyError(
            f"quarantine entry must be canonical and repo-relative, got {value!r}"
        )
    if any(part in {"", ".", ".."} for part in path.parts):
        raise QuarantinePolicyError(
            f"quarantine entry may not traverse directories, got {value!r}"
        )
    return value


def _digest(entries: Iterable[str]) -> str:
    payload = json.dumps(
        list(entries), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def parse_quarantine_policy(raw: object) -> QuarantinePolicy:
    """Validate an already-decoded policy payload."""
    if not isinstance(raw, list):
        raise QuarantinePolicyError("quarantine policy must be a JSON list")
    entries = tuple(_canonical_name(item) for item in raw)
    if len(set(entries)) != len(entries):
        raise QuarantinePolicyError("quarantine policy contains duplicate entries")
    return QuarantinePolicy(entries=entries, sha256=_digest(entries))


def parse_quarantine_policy_text(
    text: str, *, source: str = "quarantine policy"
) -> QuarantinePolicy:
    """Decode and validate policy text obtained from a trusted git object."""
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise QuarantinePolicyError(f"cannot parse {source}: {exc}") from exc
    return parse_quarantine_policy(raw)


def load_quarantine_policy(path: Path) -> QuarantinePolicy:
    """Load an exact-name JSON list; a missing file is the safe empty policy."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return parse_quarantine_policy([])
    except OSError as exc:
        raise QuarantinePolicyError(
            f"cannot read quarantine policy {path}: {exc}"
        ) from exc
    return parse_quarantine_policy_text(text, source=str(path))


def _dedup_strings(values: Iterable[object]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in values:
        value = str(item).strip()
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def classify_failures(
    failed_test_files: Iterable[object],
    *,
    unattributed_fail_gates: Iterable[object],
    policy: QuarantinePolicy,
) -> dict[str, object]:
    """Classify a red run by exact membership; unknown evidence always blocks."""
    failed = _dedup_strings(failed_test_files)
    unattributed = _dedup_strings(unattributed_fail_gates)
    quarantined = [name for name in failed if name in policy.names]
    blocking = [name for name in failed if name not in policy.names]
    environment_only = bool(failed) and not blocking and not unattributed
    return {
        "failure_classification": (
            FAILURE_CLASS_ENVIRONMENT if environment_only else FAILURE_CLASS_REGRESSION
        ),
        "failed_test_files": failed,
        "quarantined_test_files": quarantined,
        "blocking_test_files": blocking,
        "unattributed_fail_gates": unattributed,
        "quarantine_policy_sha256": policy.sha256,
    }


def gate_file_evidence_complete(
    gate: str, log_text: str, failed_files: Iterable[str]
) -> bool:
    """Prove that a red gate's complete failure set is file-attributed.

    A parsed file is insufficient: the gate's own summary count must agree.
    Whole-project gates have no per-test-file completeness proof and therefore
    always remain unattributed/blocking.
    """
    files = tuple(dict.fromkeys(failed_files))
    cleaned = _ANSI_RE.sub("", log_text or "")
    normalized_gate = str(gate).strip().lower()
    if normalized_gate == "python":
        counts = [
            int(match.group(1)) for match in _PY_FAILURE_SECTION_RE.finditer(cleaned)
        ]
        red_sections = _PY_ANY_RED_SECTION_RE.findall(cleaned)
        return (
            bool(files)
            and bool(counts)
            and len(red_sections) == len(counts)
            and sum(counts) == len(files)
        )
    if normalized_gate == "vitest":
        counts = [int(value) for value in _VITEST_FAILED_FILES_RE.findall(cleaned)]
        return (
            bool(files)
            and bool(counts)
            and counts[-1] == len(files)
            and _VITEST_UNATTRIBUTED_RE.search(cleaned) is None
        )
    return False


def accepted_quarantined_files(
    record: dict[str, object], policy: QuarantinePolicy
) -> tuple[str, ...] | None:
    """Return accepted files for a stamped environment-only record, else None."""
    if record.get("failure_classification") != FAILURE_CLASS_ENVIRONMENT:
        return None
    if record.get("quarantine_policy_sha256") != policy.sha256:
        return None
    failed = record.get("failed_test_files")
    quarantined = record.get("quarantined_test_files")
    blocking = record.get("blocking_test_files")
    unattributed = record.get("unattributed_fail_gates")
    if not all(
        isinstance(value, list)
        for value in (failed, quarantined, blocking, unattributed)
    ):
        return None
    if blocking or unattributed or not failed or failed != quarantined:
        return None
    if not all(isinstance(item, str) for item in failed):
        return None
    try:
        canonical = tuple(_canonical_name(item) for item in failed)
    except QuarantinePolicyError:
        return None
    if len(set(canonical)) != len(canonical):
        return None
    if not set(canonical).issubset(policy.names):
        return None
    return canonical
