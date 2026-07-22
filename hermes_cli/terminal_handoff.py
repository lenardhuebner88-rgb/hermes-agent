"""Terminal → PlanSpec / Direct-Task handoff helpers (W3-S2).

Owns the structured handoff envelope, raw-artifact materialisation under
``terminal_runs_root()``, and stable raw-free PlanSpec identity paths under
``DEFAULT_PLANS_ROOT/Hermes/plans/terminal-handoff/{correlation_id}.md``.

No second Context-Capsule, no second blob DB, no raw transcript in PlanSpec or
task bodies. Validate is write-free (temp 0600 dir outside the vault).
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import shutil
import stat
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------------------------------------------------------------------------
# Limits / constants
# ---------------------------------------------------------------------------

HANDOFF_SCHEMA_VERSION = 1
RAW_MAX_BYTES = 1 * 1024 * 1024  # 1 MiB
RAW_MAX_LINES = 5000
_IMMUTABLE_MODE = 0o600
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_SLUG_RE = re.compile(r"[^a-z0-9]+")
HANDOFF_RAW_ARTIFACT_KIND = "handoff_raw"
HANDOFF_PLANS_SUBDIR = Path("Hermes") / "plans" / "terminal-handoff"


class HandoffError(ValueError):
    """Structured handoff domain error (maps to HTTP 4xx)."""

    def __init__(self, message: str, *, status_code: int = 400, code: str = "handoff_error"):
        super().__init__(message)
        self.status_code = int(status_code)
        self.code = code


class HandoffConflict(HandoffError):
    def __init__(self, message: str, *, code: str = "handoff_conflict"):
        super().__init__(message, status_code=409, code=code)


class HandoffSchemaError(HandoffError):
    def __init__(self, message: str, *, code: str = "handoff_schema"):
        super().__init__(message, status_code=422, code=code)


# ---------------------------------------------------------------------------
# Pydantic request models (extra=forbid)
# ---------------------------------------------------------------------------


class CapsuleFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    correlation_id: Optional[str] = None
    context: Optional[dict[str, Any]] = None
    context_fingerprint: Optional[str] = None
    terminal_run_id: Optional[str] = None
    task_id: Optional[str] = None
    run_id: Optional[int] = None
    pane_id: Optional[str] = None
    workspace_path: Optional[str] = None
    base_sha: Optional[str] = None
    candidate_sha: Optional[str] = None


class DraftFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Optional[str] = None
    goal: Optional[str] = None
    body: Optional[str] = None
    notes: Optional[str] = None
    slug: Optional[str] = None
    mode: str = Field(default="planspec")  # planspec | direct | candidate
    freigabe: Optional[str] = None
    live_test_depth: Optional[str] = None
    dry_run: bool = False

    @field_validator("mode")
    @classmethod
    def _mode(cls, v: str) -> str:
        mode = (v or "planspec").strip().lower()
        if mode not in {"planspec", "direct", "candidate"}:
            raise ValueError("mode must be planspec|direct|candidate")
        return mode


class RawFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: Optional[str] = None
    encoding: str = "utf-8"
    # Clients may send pre-captured bytes as text only; binary blobs rejected.


class StructuredHandoffRequest(BaseModel):
    """Versioned structured handoff request. Legacy ``content`` is rejected."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = HANDOFF_SCHEMA_VERSION
    capsule: CapsuleFields = Field(default_factory=CapsuleFields)
    draft: DraftFields = Field(default_factory=DraftFields)
    raw: RawFields = Field(default_factory=RawFields)
    session: Optional[str] = None
    window: Optional[str] = None
    start: int = -120
    terminal_run_id: Optional[str] = None
    author: Optional[str] = None

    @field_validator("schema_version")
    @classmethod
    def _sv(cls, v: int) -> int:
        if int(v) != HANDOFF_SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version {v}")
        return int(v)


# ---------------------------------------------------------------------------
# Envelope (transport view only — NOT a second capsule)
# ---------------------------------------------------------------------------


@dataclass
class ArtifactDescriptor:
    artifact_kind: str
    sha256: str
    size: int
    source_path: str
    terminal_run_id: str
    mode: int = _IMMUTABLE_MODE
    schema_version: int = HANDOFF_SCHEMA_VERSION

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TerminalHandoffEnvelope:
    """Additive transport view over the existing execution capsule.

    Does not invent a second capsule. Bound windows keep the existing
    ``execution_capsule.correlation_id`` and normalized context/fingerprint.
    """

    schema_version: int
    source_kind: str
    source_id: str
    terminal_run_id: Optional[str]
    correlation_id: Optional[str]
    context: Optional[dict[str, Any]]
    context_fingerprint: Optional[str]
    task_id: Optional[str]
    run_id: Optional[int]
    pane_id: Optional[str]
    workspace_path: Optional[str]
    agent_session: Optional[str]
    native_session: Optional[str]
    base_sha: Optional[str]
    candidate_sha: Optional[str]
    gates: list[str] = field(default_factory=list)
    artifact: Optional[ArtifactDescriptor] = None
    disposition: str = "preview"

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if self.artifact is not None:
            d["artifact"] = self.artifact.as_dict()
        return d


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _plans_root() -> Path:
    from hermes_cli import planspecs

    return Path(planspecs.DEFAULT_PLANS_ROOT)


def handoff_plans_dir(*, plans_root: Optional[Path] = None) -> Path:
    root = Path(plans_root) if plans_root is not None else _plans_root()
    return root / HANDOFF_PLANS_SUBDIR


def stable_handoff_planspec_path(
    correlation_id: str, *, plans_root: Optional[Path] = None
) -> Path:
    cid = (correlation_id or "").strip()
    if not cid:
        raise HandoffError("correlation_id required for stable planspec path")
    if "/" in cid or ".." in cid or cid in {".", ".."}:
        raise HandoffError("correlation_id must be a single path segment")
    return handoff_plans_dir(plans_root=plans_root) / f"{cid}.md"


def slugify(text: str, *, fallback: str = "terminal-handoff") -> str:
    cleaned = _SLUG_RE.sub("-", (text or "").strip().lower()).strip("-")
    return cleaned or fallback


# ---------------------------------------------------------------------------
# Raw normalize / materialize
# ---------------------------------------------------------------------------


def normalize_raw_text(text: str, *, encoding: str = "utf-8") -> bytes:
    """Normalize ANSI/encoding only; reject oversize visibly (no silent trim)."""
    if text is None:
        raise HandoffError("raw.text is required")
    if not isinstance(text, str):
        raise HandoffSchemaError("raw.text must be a string")
    # Decode/encode round-trip for declared encoding.
    try:
        normalized = text.encode(encoding, errors="strict").decode(encoding, errors="strict")
    except (LookupError, UnicodeError) as exc:
        raise HandoffError(f"raw encoding error: {exc}") from exc
    cleaned = _ANSI_RE.sub("", normalized)
    # Normalize newlines to \n, strip trailing NULs only.
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    lines = cleaned.split("\n")
    if len(lines) > RAW_MAX_LINES:
        raise HandoffError(
            f"raw exceeds {RAW_MAX_LINES} lines (got {len(lines)})",
            code="raw_too_many_lines",
        )
    payload = cleaned.encode("utf-8")
    if len(payload) > RAW_MAX_BYTES:
        raise HandoffError(
            f"raw exceeds {RAW_MAX_BYTES} bytes (got {len(payload)})",
            code="raw_too_large",
        )
    return payload


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def materialize_raw_artifact(
    *,
    terminal_run_id: str,
    raw_bytes: bytes,
    filename: str = "handoff-raw.txt",
) -> ArtifactDescriptor:
    """Atomically write 0600 raw bytes under terminal_runs_root artifacts/."""
    from hermes_constants import terminal_runs_root

    run_id = (terminal_run_id or "").strip()
    if not run_id or "/" in run_id or run_id in {".", ".."}:
        raise HandoffError("terminal_run_id required for raw artifact")
    if len(raw_bytes) > RAW_MAX_BYTES:
        raise HandoffError(
            f"raw exceeds {RAW_MAX_BYTES} bytes (got {len(raw_bytes)})",
            code="raw_too_large",
        )
    digest = sha256_bytes(raw_bytes)
    art_dir = terminal_runs_root() / run_id / "artifacts"
    art_dir.mkdir(parents=True, exist_ok=True)
    dest = art_dir / filename
    tmp = art_dir / f".tmp-{digest[:16]}-{os.getpid()}"
    try:
        with tmp.open("wb") as fh:
            fh.write(raw_bytes)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, _IMMUTABLE_MODE)
        os.replace(tmp, dest)
        os.chmod(dest, _IMMUTABLE_MODE)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return ArtifactDescriptor(
        artifact_kind=HANDOFF_RAW_ARTIFACT_KIND,
        sha256=digest,
        size=len(raw_bytes),
        source_path=str(dest.resolve()),
        terminal_run_id=run_id,
    )


# ---------------------------------------------------------------------------
# PlanSpec text (raw-free) + validate/ingest helpers
# ---------------------------------------------------------------------------


def build_raw_free_planspec_text(
    *,
    title: str,
    goal: str,
    body: str = "",
    notes: str = "",
    correlation_id: str,
    artifact: Optional[ArtifactDescriptor] = None,
    freigabe: str = "operator",
    live_test_depth: str = "contract",
    slice_name: Optional[str] = None,
) -> str:
    """Server-built PlanSpec markdown without embedding raw capture."""
    topic = (title or goal or "Terminal handoff").strip()
    slice_id = slice_name or f"terminal-handoff-{correlation_id[:12]}"
    lines = [
        "---",
        f'topic: "{topic.replace(chr(34), chr(39))}"',
        f'slice: "{slice_id}"',
        f"freigabe: {freigabe}",
        f"live_test_depth: {live_test_depth}",
        "binding: true",
        "---",
        "",
        f"# {topic}",
        "",
        "## Goal",
        "",
        (goal or topic).strip() or topic,
        "",
    ]
    if body.strip():
        lines.extend(["## Context", "", body.strip(), ""])
    if notes.strip():
        lines.extend(["## Notes", "", notes.strip(), ""])
    lines.extend(
        [
            "## Handoff identity",
            "",
            f"- correlation_id: `{correlation_id}`",
        ]
    )
    if artifact is not None:
        lines.extend(
            [
                f"- artifact_kind: `{artifact.artifact_kind}`",
                f"- artifact_sha256: `{artifact.sha256}`",
                f"- artifact_size: `{artifact.size}`",
                f"- terminal_run_id: `{artifact.terminal_run_id}`",
                "",
                "Raw capture is stored as an immutable task attachment; it is not embedded here.",
                "",
            ]
        )
    else:
        lines.append("")
    lines.extend(
        [
            "## Subtasks",
            "",
            "### W3-H1 Implement handoff outcome",
            "",
            f"- lane: coder",
            f"- goal: Execute the terminal handoff goal for correlation `{correlation_id}`.",
            "",
        ]
    )
    text = "\n".join(lines)
    # Fail closed if raw-looking fences slipped in via body/notes with secret canaries
    # is not our job; embedding of separate raw block is rejected by callers.
    return text


def reject_if_raw_embedded(planspec_text: str, raw_bytes: Optional[bytes]) -> None:
    if not raw_bytes:
        return
    # If the exact normalized raw payload appears inside the planspec, reject.
    try:
        raw_text = raw_bytes.decode("utf-8")
    except UnicodeError:
        return
    sample = raw_text.strip()
    if sample and len(sample) >= 32 and sample in planspec_text:
        raise HandoffError(
            "planspec text must not re-embed normalized raw capture",
            code="raw_embedded",
        )


def write_stable_planspec(
    *,
    correlation_id: str,
    text: str,
    plans_root: Optional[Path] = None,
) -> Path:
    """Atomically write 0600 planspec at stable correlation path.

    Same digest → idempotent reuse. Different bytes for same correlation → 409.
    """
    path = stable_handoff_planspec_path(correlation_id, plans_root=plans_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8")
    digest = sha256_bytes(data)
    if path.exists():
        existing = path.read_bytes()
        if sha256_bytes(existing) == digest:
            return path
        raise HandoffConflict(
            f"stable planspec path exists with different digest for {correlation_id}"
        )
    tmp = path.parent / f".tmp-{correlation_id}-{os.getpid()}"
    try:
        with tmp.open("wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, _IMMUTABLE_MODE)
        os.replace(tmp, path)
        os.chmod(path, _IMMUTABLE_MODE)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return path


def validate_handoff_planspec_text(
    text: str, *, plans_root: Optional[Path] = None
) -> dict[str, Any]:
    """Write-free validate in a 0600 temp dir outside the vault; always cleaned."""
    from hermes_cli import planspecs

    root = Path(plans_root) if plans_root is not None else _plans_root()
    # Temp outside vault: system temp, not under plans_root.
    tmp_dir = Path(
        tempfile.mkdtemp(prefix="hermes-handoff-validate-", dir=tempfile.gettempdir())
    )
    try:
        # Directories need execute bit; files stay 0600.
        os.chmod(tmp_dir, 0o700)
        tmp_file = tmp_dir / "handoff-validate.md"
        tmp_file.write_text(text, encoding="utf-8")
        os.chmod(tmp_file, _IMMUTABLE_MODE)
        # Prefer full validate when path constraints allow; else structural parse.
        try:
            # Copy into a nested temp plans root so path containment passes without
            # touching the real vault.
            mirror_root = tmp_dir / "plans-root"
            mirror_dir = mirror_root / HANDOFF_PLANS_SUBDIR
            mirror_dir.mkdir(parents=True, exist_ok=True)
            mirror_path = mirror_dir / "validate.md"
            shutil.copy2(tmp_file, mirror_path)
            os.chmod(mirror_path, _IMMUTABLE_MODE)
            return planspecs.validate_planspec(mirror_path, plans_root=mirror_root)
        except Exception as exc:  # noqa: BLE001 — surface as findings
            return {
                "ok": False,
                "disposition": "invalid",
                "findings": [f"validate failed: {exc}"],
            }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Capsule / envelope assembly from server-observed terminal state
# ---------------------------------------------------------------------------


def build_envelope_from_window(
    *,
    session: str,
    window: str,
    window_info: Mapping[str, Any],
    execution_capsule: Optional[Mapping[str, Any]] = None,
    terminal_run_id: Optional[str] = None,
    artifact: Optional[ArtifactDescriptor] = None,
    gates: Optional[list[str]] = None,
    source_kind: str = "agent_terminal",
) -> TerminalHandoffEnvelope:
    """Build additive envelope. Bound capsule fields are authoritative."""
    capsule = dict(execution_capsule or {})
    corr = capsule.get("correlation_id")
    context = capsule.get("context") if isinstance(capsule.get("context"), dict) else None
    fingerprint = capsule.get("context_fingerprint") or (
        (context or {}).get("fingerprint") if isinstance(context, dict) else None
    )
    return TerminalHandoffEnvelope(
        schema_version=HANDOFF_SCHEMA_VERSION,
        source_kind=source_kind,
        source_id=f"{session}:{window}",
        terminal_run_id=terminal_run_id or capsule.get("terminal_run_id"),
        correlation_id=str(corr) if corr else None,
        context=context,
        context_fingerprint=str(fingerprint) if fingerprint else None,
        task_id=(str(capsule.get("task_id")) if capsule.get("task_id") else None),
        run_id=(int(capsule["run_id"]) if capsule.get("run_id") is not None else None),
        pane_id=str(window_info.get("pane_id") or capsule.get("pane_id") or "") or None,
        workspace_path=(
            str(window_info.get("cwd") or capsule.get("workspace_path") or "") or None
        ),
        agent_session=str(session) if session else None,
        native_session=str(window_info.get("native_session") or "") or None,
        base_sha=(str(capsule.get("base_sha")) if capsule.get("base_sha") else None),
        candidate_sha=(
            str(capsule.get("candidate_sha")) if capsule.get("candidate_sha") else None
        ),
        gates=list(gates or []),
        artifact=artifact,
        disposition="preview",
    )


def mint_handoff_correlation_id() -> str:
    return f"handoff_{secrets.token_hex(16)}"


def parse_structured_request(payload: Mapping[str, Any]) -> StructuredHandoffRequest:
    """Parse request; reject legacy content-shaped POSTs with 422 before writes."""
    if not isinstance(payload, Mapping):
        raise HandoffSchemaError("body must be an object")
    if "content" in payload and "schema_version" not in payload:
        raise HandoffSchemaError(
            "legacy content handoff is no longer accepted; send structured schema_version=1"
        )
    if "content" in payload:
        raise HandoffSchemaError(
            "content field is forbidden; use capsule/draft/raw separation",
            code="legacy_content_forbidden",
        )
    try:
        return StructuredHandoffRequest.model_validate(dict(payload))
    except Exception as exc:  # pydantic ValidationError
        raise HandoffSchemaError(f"invalid handoff schema: {exc}") from exc


# ---------------------------------------------------------------------------
# Backward-compatible draft writer (legacy callers) — DISABLED for vault writes
# ---------------------------------------------------------------------------


def write_handoff_draft(
    content: str,
    *,
    slug: str | None = None,
    plans_root: Path | None = None,
) -> Path:
    """Legacy helper — refuse durable vault writes.

    W3-S2: old SPA content POSTs must not materialise drafts under the plans
    root. Callers must migrate to structured validate/ingest.
    """
    raise HandoffSchemaError(
        "write_handoff_draft is retired; use structured handoff validate/ingest "
        f"(refused slug={slugify(slug or 'legacy')})",
        code="legacy_write_forbidden",
    )


# ---------------------------------------------------------------------------
# High-level validate / materialize entrypoints used by routes
# ---------------------------------------------------------------------------


def validate_structured_handoff(
    payload: Mapping[str, Any],
    *,
    window_info: Optional[Mapping[str, Any]] = None,
    execution_capsule: Optional[Mapping[str, Any]] = None,
    terminal_run_id: Optional[str] = None,
    plans_root: Optional[Path] = None,
) -> dict[str, Any]:
    """Write-free validate. Never mints durable correlation or artifacts."""
    req = parse_structured_request(payload)
    run_id = (terminal_run_id or req.capsule.terminal_run_id or "").strip() or None
    raw_bytes = None
    if req.raw.text is not None:
        raw_bytes = normalize_raw_text(req.raw.text, encoding=req.raw.encoding)
    envelope = build_envelope_from_window(
        session=req.session or "",
        window=req.window or "",
        window_info=window_info or {},
        execution_capsule=execution_capsule
        or {
            "correlation_id": req.capsule.correlation_id,
            "context": req.capsule.context,
            "context_fingerprint": req.capsule.context_fingerprint,
            "terminal_run_id": run_id,
            "task_id": req.capsule.task_id,
            "run_id": req.capsule.run_id,
            "pane_id": req.capsule.pane_id,
            "workspace_path": req.capsule.workspace_path,
            "base_sha": req.capsule.base_sha,
            "candidate_sha": req.capsule.candidate_sha,
        },
        terminal_run_id=run_id,
    )
    # Preview-only correlation for text build (not durable).
    preview_corr = envelope.correlation_id or "validate-preview"
    text = build_raw_free_planspec_text(
        title=req.draft.title or f"Terminal handoff {preview_corr[:12]}",
        goal=req.draft.goal or req.draft.body or req.draft.title or "Terminal handoff",
        body=req.draft.body or "",
        notes=req.draft.notes or "",
        correlation_id=preview_corr,
        artifact=None,
        freigabe="operator",
        live_test_depth="contract",
        slice_name=slugify(req.draft.slug or req.draft.title or "terminal-handoff"),
    )
    reject_if_raw_embedded(text, raw_bytes)
    result = validate_handoff_planspec_text(text, plans_root=plans_root)
    return {
        "ok": bool(result.get("ok")),
        "disposition": result.get("disposition") or ("valid" if result.get("ok") else "invalid"),
        "findings": result.get("findings") or [],
        "envelope": envelope.as_dict(),
        "raw_bytes": len(raw_bytes) if raw_bytes is not None else 0,
        "raw_sha256": sha256_bytes(raw_bytes) if raw_bytes is not None else None,
        "writes": False,
    }


def materialize_structured_handoff_planspec(
    payload: Mapping[str, Any],
    *,
    window_info: Optional[Mapping[str, Any]] = None,
    execution_capsule: Optional[Mapping[str, Any]] = None,
    terminal_run_id: str,
    plans_root: Optional[Path] = None,
    mint_correlation_if_unbound: bool = True,
) -> dict[str, Any]:
    """Materialize durable raw artifact + stable planspec path (no DB writes)."""
    req = parse_structured_request(payload)
    run_id = (terminal_run_id or req.capsule.terminal_run_id or "").strip()
    if not run_id:
        raise HandoffError(
            "terminal_run_id/manifest required; legacy windows must start a new session",
            code="legacy_window",
        )
    if req.raw.text is None:
        raise HandoffError("raw.text is required for materialize")
    raw_bytes = normalize_raw_text(req.raw.text, encoding=req.raw.encoding)
    capsule = dict(execution_capsule or {})
    corr = (capsule.get("correlation_id") or req.capsule.correlation_id or "").strip()
    if not corr:
        if not mint_correlation_if_unbound:
            raise HandoffError("bound correlation_id required")
        corr = mint_handoff_correlation_id()
    artifact = materialize_raw_artifact(terminal_run_id=run_id, raw_bytes=raw_bytes)
    text = build_raw_free_planspec_text(
        title=req.draft.title or f"Terminal handoff {corr[:12]}",
        goal=req.draft.goal or req.draft.body or req.draft.title or "Terminal handoff",
        body=req.draft.body or "",
        notes=req.draft.notes or "",
        correlation_id=corr,
        artifact=artifact,
        freigabe="operator",
        live_test_depth="contract",
        slice_name=slugify(req.draft.slug or req.draft.title or f"handoff-{corr[:12]}"),
    )
    reject_if_raw_embedded(text, raw_bytes)
    path = write_stable_planspec(
        correlation_id=corr, text=text, plans_root=plans_root
    )
    envelope = build_envelope_from_window(
        session=req.session or "",
        window=req.window or "",
        window_info=window_info or {},
        execution_capsule={
            **capsule,
            "correlation_id": corr,
            "terminal_run_id": run_id,
        },
        terminal_run_id=run_id,
        artifact=artifact,
        gates=["artifact_materialized"],
    )
    envelope.disposition = "materialized"
    return {
        "ok": True,
        "path": str(path),
        "correlation_id": corr,
        "envelope": envelope.as_dict(),
        "artifact": artifact.as_dict(),
    }
