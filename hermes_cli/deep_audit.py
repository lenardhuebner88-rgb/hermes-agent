"""Read-only, tool-using Deep-Audit lane for Autoresearch.

The lane lets the configured ``auxiliary.code_audit`` model inspect a bounded
subsystem file set with read-only tools. It never exposes files outside that
set, never writes target code, and persists only structured findings/proposals.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from hermes_cli.autoresearch_lane_models import response_usage_metadata

_REPO = Path(__file__).resolve().parents[1]
_DEFAULT_AUDIT = _REPO / ".hermes" / "skill-audit"
_MAX_FILE_CHARS = 12_000
_MAX_ITERATIONS = 8

_SUMMABLE_USAGE_FIELDS = {
    ("cost", "request_cost_usd"),
    ("energy", "energy_kwh"),
    ("energy", "carbon_g_co2eq"),
}


def _merge_response_usage_metadata(acc: dict[str, Any], meta: dict[str, Any]) -> None:
    """Merge NeuralWatt response usage into run-level metadata."""
    if not meta:
        return
    acc.setdefault("response_usage_metadata", []).append(meta)
    for section in ("cost", "energy"):
        values = meta.get(section)
        if not isinstance(values, dict):
            continue
        target = acc.setdefault(section, {})
        for key, value in values.items():
            if (section, key) in _SUMMABLE_USAGE_FIELDS and isinstance(value, int | float):
                target[key] = float(target.get(key) or 0.0) + float(value)
            elif key not in target:
                target[key] = value
_MAX_GREP_RESULTS = 100

SUBSYSTEM_GLOBS: dict[str, tuple[str, ...]] = {
    "kanban": (
        "hermes_cli/kanban*.py",
        "plugins/kanban/**/*.py",
    ),
    "gateway-auth": (
        "gateway/run.py",
        "gateway/session.py",
        "gateway/config.py",
        "hermes_cli/dashboard_auth/**/*.py",
    ),
    "dashboard-api": (
        "hermes_cli/web_server.py",
        "hermes_cli/autoresearch_view.py",
        "plugins/kanban/dashboard/plugin_api.py",
    ),
    "autoresearch": (
        "hermes_cli/autoresearch_*.py",
        "scripts/autoresearch_*.py",
        "scripts/run_autoresearch_request.py",
    ),
    "cron-scheduler": (
        "cron/**/*.py",
        "hermes_cli/cron*.py",
    ),
    "credentials": (
        "agent/credential_pool.py",
        "hermes_cli/auth.py",
        "hermes_cli/auth_commands.py",
        "hermes_cli/secrets_cli.py",
        "tools/credential*.py",
    ),
    "mcp-catalog": (
        "hermes_cli/mcp*.py",
        "tools/mcp_*.py",
    ),
}

_FORBIDDEN_EXACT_NAMES = {"config.yaml", "auth.json", ".env"}
_FORBIDDEN_PARTS = {".git", "tests", "web_dist", "migrations", "migration"}
_FORBIDDEN_SECRET_DIRS = {"secrets", ".secrets"}
_SEVERITY_ORDINAL = {"critical": 4, "high": 3, "medium": 2, "low": 1}


class SandboxError(ValueError):
    """Raised internally when a read-only tool request violates the sandbox."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _audit_dir() -> Path:
    override = os.environ.get("HERMES_AUTORESEARCH_AUDIT_DIR")
    return Path(override) if override else _DEFAULT_AUDIT


def _state_dir() -> Path:
    override = os.environ.get("HERMES_AUTORESEARCH_STATE_DIR")
    return Path(override) if override else (_audit_dir() / "runner-state")


def _deep_state_dir() -> Path:
    return _state_dir() / "deep-audit"


def _lock_path() -> Path:
    return _deep_state_dir() / "current.lock"


def _status_path() -> Path:
    return _deep_state_dir() / "current.status"


def _findings_path() -> Path:
    return _deep_state_dir() / "last-findings.json"


def _requests_dir() -> Path:
    return _audit_dir() / "deep-audit-requests"


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except (OSError, ValueError):
        return False


def _repo_rel(path: Path) -> str:
    try:
        return path.resolve(strict=False).relative_to(_REPO.resolve(strict=False)).as_posix()
    except (OSError, ValueError):
        return path.as_posix()


def _is_forbidden_path(path: Path) -> bool:
    try:
        rel = path.resolve(strict=False).relative_to(_REPO.resolve(strict=False))
    except (OSError, ValueError):
        return True
    parts = rel.parts
    lower_parts = {p.lower() for p in parts}
    if any(p in _FORBIDDEN_PARTS for p in parts):
        return True
    if lower_parts & _FORBIDDEN_SECRET_DIRS:
        return True
    return path.name in _FORBIDDEN_EXACT_NAMES


def _coerce_file_cap(max_files: int | None) -> int:
    try:
        return max(1, min(12, int(max_files or 12)))
    except (TypeError, ValueError):
        return 12


def resolve_subsystem_files(subsystem: str, *, max_files: int = 12) -> list[Path]:
    """Resolve a subsystem key to real files, excluding forbidden paths."""
    if subsystem not in SUBSYSTEM_GLOBS:
        raise ValueError(f"unknown subsystem: {subsystem}")
    cap = _coerce_file_cap(max_files)
    seen: set[Path] = set()
    files: list[Path] = []
    for pattern in SUBSYSTEM_GLOBS[subsystem]:
        matches = sorted(_REPO.glob(pattern))
        for path in matches:
            try:
                rp = path.resolve(strict=False)
            except OSError:
                continue
            if rp in seen or _is_forbidden_path(rp):
                continue
            if rp.exists() and rp.is_file() and _under(rp, _REPO):
                seen.add(rp)
                files.append(rp)
                if len(files) >= cap:
                    return files
    return files


def _normalise_allowed_files(files: Iterable[str | Path]) -> set[Path]:
    allowed: set[Path] = set()
    for raw in files:
        path = Path(raw)
        if not path.is_absolute():
            path = _REPO / path
        try:
            rp = path.resolve(strict=False)
        except OSError:
            continue
        if rp.exists() and rp.is_file() and _under(rp, _REPO) and not _is_forbidden_path(rp):
            allowed.add(rp)
    return allowed


class DeepAuditSandbox:
    def __init__(self, allowed_files: Iterable[str | Path]) -> None:
        self.allowed_files = _normalise_allowed_files(allowed_files)

    def _resolve_request_path(self, raw: str | None) -> Path:
        value = (raw or ".").strip() or "."
        path = Path(value)
        if ".." in path.parts:
            raise SandboxError("refused: traversal segments are forbidden")
        if not path.is_absolute():
            path = _REPO / path
        try:
            rp = path.resolve(strict=False)
        except OSError as exc:
            raise SandboxError(f"refused: path could not be resolved: {type(exc).__name__}") from exc
        if not _under(rp, _REPO):
            raise SandboxError("refused: path is outside the repo")
        if _is_forbidden_path(rp):
            raise SandboxError("refused: forbidden path")
        return rp

    def _targets_for_path(self, raw: str | None) -> list[Path]:
        if raw is None or str(raw).strip() == "":
            return sorted(self.allowed_files)
        rp = self._resolve_request_path(str(raw))
        if rp in self.allowed_files:
            return [rp]
        matches = [p for p in sorted(self.allowed_files) if _under(p, rp)]
        if not matches:
            raise SandboxError("refused: path is not in the subsystem file list")
        return matches

    def read_file(self, path: str) -> dict[str, Any]:
        try:
            rp = self._resolve_request_path(path)
            if rp not in self.allowed_files:
                raise SandboxError("refused: file is not in the subsystem file list")
            text = rp.read_text(encoding="utf-8", errors="replace")
            truncated = len(text) > _MAX_FILE_CHARS
            text = text[:_MAX_FILE_CHARS]
            numbered = "\n".join(f"{i:04d}: {line}" for i, line in enumerate(text.splitlines(), 1))
            return {"ok": True, "path": _repo_rel(rp), "truncated": truncated, "content": numbered}
        except (OSError, SandboxError) as exc:
            return {"ok": False, "path": path, "error": str(exc)}

    def grep(self, pattern: str, path: str | None = None) -> dict[str, Any]:
        try:
            rx = re.compile(pattern)
            targets = self._targets_for_path(path)
            results: list[dict[str, Any]] = []
            for target in targets:
                try:
                    text = target.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for lineno, line in enumerate(text.splitlines(), 1):
                    if rx.search(line):
                        results.append({"file": _repo_rel(target), "line": lineno, "text": line[:500]})
                        if len(results) >= _MAX_GREP_RESULTS:
                            return {"ok": True, "pattern": pattern, "truncated": True, "results": results}
            return {"ok": True, "pattern": pattern, "truncated": False, "results": results}
        except (re.error, SandboxError) as exc:
            return {"ok": False, "pattern": pattern, "error": str(exc), "results": []}

    def list_dir(self, path: str = ".") -> dict[str, Any]:
        try:
            rp = self._resolve_request_path(path)
            entries: dict[str, str] = {}
            for allowed in self.allowed_files:
                try:
                    rel = allowed.relative_to(rp)
                except ValueError:
                    continue
                if not rel.parts:
                    continue
                name = rel.parts[0]
                entries[name] = "file" if len(rel.parts) == 1 else "dir"
            if not entries and rp not in {_REPO, *[p.parent for p in self.allowed_files]}:
                raise SandboxError("refused: directory is outside the subsystem file list")
            return {
                "ok": True,
                "path": _repo_rel(rp),
                "entries": [{"name": name, "type": entries[name]} for name in sorted(entries)],
            }
        except SandboxError as exc:
            return {"ok": False, "path": path, "error": str(exc), "entries": []}

    def dispatch(self, name: str, arguments: dict[str, Any]) -> str:
        if name == "read_file":
            result = self.read_file(str(arguments.get("path") or ""))
        elif name == "grep":
            result = self.grep(str(arguments.get("pattern") or ""), arguments.get("path"))
        elif name == "list_dir":
            result = self.list_dir(str(arguments.get("path") or "."))
        else:
            result = {"ok": False, "error": f"unknown tool: {name}"}
        return json.dumps(result, ensure_ascii=False)


def read_file(path: str, *, allowed_files: Iterable[str | Path]) -> dict[str, Any]:
    return DeepAuditSandbox(allowed_files).read_file(path)


def grep(pattern: str, path: str | None = None, *, allowed_files: Iterable[str | Path]) -> dict[str, Any]:
    return DeepAuditSandbox(allowed_files).grep(pattern, path)


def list_dir(path: str = ".", *, allowed_files: Iterable[str | Path]) -> dict[str, Any]:
    return DeepAuditSandbox(allowed_files).list_dir(path)


_READONLY_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read one allowed subsystem file with line numbers. Read-only.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Run a regex over allowed subsystem files only. Read-only.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string", "description": "Optional allowed file or directory."},
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List allowed subsystem files/directories visible at a repo path. Read-only.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "default": "."}},
                "additionalProperties": False,
            },
        },
    },
]

_REPORTING_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "report_finding",
            "description": (
                "Record ONE real, grounded audit finding. Call this once per distinct "
                "weakness you can prove from the file content. evidence MUST be a "
                "verbatim code excerpt copied from the file (no line-number prefixes, "
                "no paraphrase) so it can be verified against the source."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "fileline": {
                        "type": "string",
                        "description": "repo/relative/path.py:LINE (e.g. hermes_cli/foo.py:225).",
                    },
                    "severity": {
                        "type": "string",
                        "enum": ["critical", "high", "medium", "low"],
                    },
                    "category": {
                        "type": "string",
                        "description": "Short tag, e.g. bug_risk, race, missing_validation, info_leak, dead_code, error_handling.",
                    },
                    "title": {"type": "string", "description": "One-line summary of the weakness."},
                    "problem": {"type": "string", "description": "Why this is a real problem and its impact."},
                    "evidence": {
                        "type": "string",
                        "description": "Verbatim code excerpt from the file (the offending lines).",
                    },
                    "fix_hint": {"type": "string", "description": "Concrete suggestion for how to fix it."},
                },
                "required": ["fileline", "severity", "category", "title", "problem", "evidence", "fix_hint"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish_audit",
            "description": (
                "Call this exactly once when you are done — after you have reported "
                "every real finding via report_finding (or if there are genuinely none). "
                "Ends the audit."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "Optional one-line wrap-up."},
                },
                "additionalProperties": False,
            },
        },
    },
]


def tool_schemas() -> list[dict[str, Any]]:
    """All tools exposed to the audit model: read-only inspection + reporting."""
    return [*_READONLY_TOOLS, *_REPORTING_TOOLS]


def _message_from_response(resp: Any) -> Any:
    choices = getattr(resp, "choices", None)
    if choices is None and isinstance(resp, dict):
        choices = resp.get("choices")
    if not choices:
        return None
    choice = choices[0]
    return getattr(choice, "message", None) if not isinstance(choice, dict) else choice.get("message")


def _usage_tokens(resp: Any) -> int:
    usage = getattr(resp, "usage", None)
    if usage is None and isinstance(resp, dict):
        usage = resp.get("usage")
    total = getattr(usage, "total_tokens", None)
    if total is None and isinstance(usage, dict):
        total = usage.get("total_tokens")
    try:
        return int(total or 0)
    except (TypeError, ValueError):
        return 0


def _model_label(resp: Any) -> str | None:
    model = getattr(resp, "model", None)
    if model is None and isinstance(resp, dict):
        model = resp.get("model")
    return str(model).strip() if model else None


def _message_content(message: Any) -> str:
    if message is None:
        return ""
    content = getattr(message, "content", None)
    if content is None and isinstance(message, dict):
        content = message.get("content")
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content or "")


def _tool_calls(message: Any) -> list[Any]:
    if message is None:
        return []
    calls = getattr(message, "tool_calls", None)
    if calls is None and isinstance(message, dict):
        calls = message.get("tool_calls")
    return calls if isinstance(calls, list) else []


def _normalise_tool_call(tc: Any, index: int) -> tuple[str, str, str, dict[str, Any]]:
    if isinstance(tc, dict):
        fn = tc.get("function") or {}
        name = str(fn.get("name") or tc.get("name") or "")
        raw_args = fn.get("arguments") or tc.get("arguments") or "{}"
        call_id = str(tc.get("id") or f"call_{index}")
    else:
        fn = getattr(tc, "function", None)
        name = str(getattr(fn, "name", "") or getattr(tc, "name", "") or "")
        raw_args = getattr(fn, "arguments", None) or getattr(tc, "arguments", None) or "{}"
        call_id = str(getattr(tc, "id", None) or f"call_{index}")
    try:
        args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args if isinstance(raw_args, dict) else {})
    except ValueError:
        args = {}
    return call_id, name, json.dumps(args, ensure_ascii=False), args


def _assistant_tool_message(message: Any, calls: list[Any]) -> dict[str, Any]:
    normalised = []
    for i, tc in enumerate(calls):
        call_id, name, args_json, _args = _normalise_tool_call(tc, i)
        normalised.append({"id": call_id, "type": "function", "function": {"name": name, "arguments": args_json}})
    return {"role": "assistant", "content": _message_content(message) or "", "tool_calls": normalised}


def _loads_llm_json(text: str) -> dict[str, Any]:
    stripped = (text or "").strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        data = json.loads(stripped)
    except ValueError:
        match = re.search(r"\{.*\}", stripped, re.DOTALL)
        try:
            data = json.loads(match.group(0)) if match else {}
        except ValueError:
            data = {}
    return data if isinstance(data, dict) else {}


def _coerce_severity(value: Any) -> str:
    raw = str(value or "").strip().lower()
    return raw if raw in _SEVERITY_ORDINAL else "medium"


def _file_from_fileline(fileline: str) -> str | None:
    match = re.match(r"^(.+?):\d+(?::\d+)?$", fileline.strip())
    return match.group(1) if match else None


def _normalise_ws(text: str) -> str:
    """Collapse all runs of whitespace to single spaces for tolerant matching.

    MiniMax frequently re-indents or line-wraps the evidence snippet it copies
    out of a file, which makes a strict ``in`` substring test reject genuine
    findings. Comparing whitespace-normalised forms keeps grounding honest (the
    code must really contain the snippet) while tolerating reflow.
    """
    return re.sub(r"\s+", " ", str(text or "")).strip()


# Lines this short carry no proof — a per-line grounding fallback ignores them so
# that a single trivial token (e.g. "except:") can't ground a hallucinated block.
_MIN_GROUND_LINE_CHARS = 12


def _strip_evidence_noise(evidence: str) -> str:
    """Remove line-number prefixes and trailing model commentary from evidence.

    read_file prepends ``NNNN: `` to every line; models sometimes copy that, and
    sometimes append parentheticals like ``(truncated — ...)``. Both break a
    verbatim match even though the underlying code is real.
    """
    lines = []
    for line in str(evidence or "").splitlines():
        lines.append(re.sub(r"^\s*\d{1,5}:\s?", "", line))
    cleaned = "\n".join(lines)
    # Drop a trailing parenthetical the model tacked on after the real snippet.
    cleaned = re.sub(r"\s*\((?:truncated|full|omitted|etc|\.\.\.)[^)]*\)\s*$", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def _is_grounded(evidence: str, file_text: str) -> bool:
    """Return True if ``evidence`` is provably present in ``file_text``.

    Layered, increasingly tolerant — but every layer still requires the code to
    really be in the file, so hallucinated evidence is rejected:
      1. exact substring,
      2. whitespace-normalised substring (tolerates reflow/re-indent),
      3. after stripping line-number prefixes + trailing model commentary,
      4. per-line: at least one *substantial* evidence line is present verbatim
         (whitespace-normalised) in the file. Catches findings where the model
         slightly garbled the surrounding lines but quoted a real line exactly.
    """
    evidence = (evidence or "").strip()
    if not evidence:
        return False
    norm_file = _normalise_ws(file_text)
    for candidate in (evidence, _strip_evidence_noise(evidence)):
        if not candidate:
            continue
        if candidate in file_text:
            return True
        norm_candidate = _normalise_ws(candidate)
        if norm_candidate and norm_candidate in norm_file:
            return True
    # Per-line fallback: a single substantial verbatim line is enough proof.
    for raw_line in _strip_evidence_noise(evidence).splitlines():
        norm_line = _normalise_ws(raw_line)
        if len(norm_line.replace(" ", "")) >= _MIN_GROUND_LINE_CHARS and norm_line in norm_file:
            return True
    return False


def _make_finding(
    item: dict[str, Any],
    allowed_by_rel: dict[str, Path],
    model_label: str | None,
    file_text_cache: dict[str, str | None],
) -> dict[str, Any] | None:
    """Validate + normalise a single finding dict. Returns None if not grounded."""
    fileline = str(item.get("fileline") or "").strip()
    rel = _file_from_fileline(fileline)
    if not rel or rel not in allowed_by_rel:
        return None
    evidence = str(item.get("evidence") or "").strip()
    if not evidence:
        return None
    if rel not in file_text_cache:
        try:
            file_text_cache[rel] = allowed_by_rel[rel].read_text(encoding="utf-8", errors="replace")
        except OSError:
            file_text_cache[rel] = None
    text = file_text_cache[rel]
    if text is None or not _is_grounded(evidence, text):
        return None
    return {
        "fileline": fileline[:240],
        "severity": _coerce_severity(item.get("severity")),
        "category": str(item.get("category") or "bug_risk").strip()[:80] or "bug_risk",
        "title": str(item.get("title") or "Deep-Audit finding").strip()[:160],
        "problem": str(item.get("problem") or "").strip()[:1200],
        "evidence": evidence[:1000],
        "fix_hint": str(item.get("fix_hint") or "Manuell prüfen und gezielt beheben.").strip()[:1000],
        "_model_label": model_label or "",
    }


def _normalise_findings(raw: Any, allowed_files: Iterable[Path], model_label: str | None) -> list[dict[str, Any]]:
    """Validate a list of finding dicts (from report_finding tool-calls or JSON)."""
    allowed_by_rel = {_repo_rel(p): p for p in allowed_files}
    if isinstance(raw, dict):
        findings = raw.get("findings")
    elif isinstance(raw, list):
        findings = raw
    else:
        findings = None
    if not isinstance(findings, list):
        return []
    file_text_cache: dict[str, str | None] = {}
    out: list[dict[str, Any]] = []
    for item in findings:
        if not isinstance(item, dict):
            continue
        finding = _make_finding(item, allowed_by_rel, model_label, file_text_cache)
        if finding is not None:
            out.append(finding)
    return out


def run_deep_audit(
    *,
    subsystem: str,
    focus: str | None = None,
    max_files: int = 12,
    llm_call: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Run the read-only tool loop and return structured findings.

    Provider/model selection is intentionally delegated to
    ``agent.auxiliary_client.call_llm(task="code_audit")``.
    """
    request_id = f"deep-audit-{uuid.uuid4().hex[:12]}"
    started = time.time()
    try:
        files = resolve_subsystem_files(subsystem, max_files=max_files)
        sandbox = DeepAuditSandbox(files)
        rel_files = [_repo_rel(p) for p in files]
        if not files:
            return {
                "ok": False, "findings": [], "subsystem": subsystem, "model": None,
                "tokens": 0, "iterations": 0, "reason": "no files resolved", "request_id": request_id,
                "files": [],
            }
        if llm_call is None:
            from agent.auxiliary_client import call_llm as llm_call
        system = (
            "Du bist ein strenger, read-only Code-Auditor fuer Hermes. "
            "Zum Untersuchen nutzt du ausschliesslich die read-only Tools: read_file, grep, list_dir. "
            "Kein Code schreiben, keine Kommandos ausfuehren, keine Fixes anwenden. "
            "Finde echte Schwaechen: Bugs, Races, fehlende Auth/Validierung, Info-Leaks, "
            "toten Code oder gefaehrliche Fehlerbehandlung.\n"
            "WICHTIG — so meldest du Funde: Gib KEINE Prosa-Analyse und KEIN finales JSON zurueck. "
            "Rufe stattdessen fuer JEDEN belegten Fund das Tool report_finding auf "
            "(fileline=repo/pfad.py:zeile, severity, category, title, problem, evidence, fix_hint). "
            "Melde einen Fund SOFORT, sobald du ihn im Code gesehen hast — sammle sie NICHT bis zum Schluss. "
            "Du darfst report_finding mehrfach pro Schritt aufrufen.\n"
            "evidence MUSS ein WORTWOERTLICHER Code-Ausschnitt aus der gelesenen Datei sein: kopiere 1-4 Zeilen "
            "EXAKT so, wie read_file sie zeigt, aber OHNE den 'NNNN: '-Zeilennummern-Prefix. "
            "Schreibe NICHTS dazu (keine '...'-Auslassungen, keine '(truncated)'-Hinweise, keine Umformulierung) — "
            "sonst wird der Fund als unbelegt verworfen.\n"
            "Ablauf: erst mit read_file/grep den Code lesen, Funde via report_finding melden sobald du sie siehst, "
            "und wenn du das Subsystem durch hast, rufe GENAU EINMAL finish_audit auf (auch wenn es keine Funde gab). "
            "Erfinde nichts — nur was du im Code belegen kannst. Vergiss finish_audit am Ende nicht."
        )
        user = (
            f"Subsystem: {subsystem}\n"
            f"Focus: {(focus or 'general subsystem audit').strip()}\n"
            "Allowed files only:\n" + "\n".join(f"- {p}" for p in rel_files)
        )
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        tokens = 0
        model_label = None
        usage_metadata: dict[str, Any] = {}
        iterations = 0
        raw_findings: list[dict[str, Any]] = []
        finished = False
        files_read: set[str] = set()
        # Once the model has read enough of the subsystem (or used up most of its
        # budget), push it into a reporting phase: MiniMax tends to browse far
        # longer than needed and only dumps findings when told to stop. We keep
        # re-pushing every turn it browses instead of reporting/finishing so a
        # whole run can't end with findings still unreported.
        read_enough_at = max(2, len(rel_files))
        wrap_at = max(read_enough_at, _MAX_ITERATIONS - 6)
        for iterations in range(1, _MAX_ITERATIONS + 1):
            in_report_phase = (iterations >= wrap_at) or (len(files_read) >= read_enough_at and iterations >= 3)
            if in_report_phase and not finished:
                last_turn = iterations >= _MAX_ITERATIONS - 1
                messages.append({
                    "role": "user",
                    "content": (
                        "Genug untersucht. Melde JETZT jeden echten, belegten Fund mit report_finding "
                        "(du darfst mehrere report_finding-Aufrufe in einem Schritt machen; evidence "
                        "WORTWOERTLICH aus der Datei, ohne '...' und ohne Zusatztext). "
                        + ("Rufe danach finish_audit auf." if not last_turn else
                           "Dies ist dein LETZTER Schritt: melde Funde und rufe finish_audit. Keine read_file/grep mehr.")
                    ),
                })
            resp = llm_call(task="code_audit", tools=tool_schemas(), messages=messages, temperature=0, max_tokens=4000)
            _merge_response_usage_metadata(usage_metadata, response_usage_metadata(resp))
            tokens += _usage_tokens(resp)
            model_label = _model_label(resp) or model_label
            message = _message_from_response(resp)
            calls = _tool_calls(message)
            if not calls:
                # No tool call: the model went off-script (e.g. emitted prose).
                # Nudge it back onto the tool protocol and continue the loop.
                messages.append({"role": "assistant", "content": _message_content(message) or ""})
                messages.append({
                    "role": "user",
                    "content": (
                        "Nutze die Tools, nicht Freitext. Melde belegte Funde via report_finding "
                        "und beende dann mit finish_audit."
                    ),
                })
                continue
            messages.append(_assistant_tool_message(message, calls))
            for i, tc in enumerate(calls):
                call_id, name, _args_json, args = _normalise_tool_call(tc, i)
                if name == "report_finding":
                    raw_findings.append(args if isinstance(args, dict) else {})
                    messages.append({
                        "role": "tool", "tool_call_id": call_id, "name": name,
                        "content": json.dumps({"ok": True, "recorded": True}, ensure_ascii=False),
                    })
                elif name == "finish_audit":
                    finished = True
                    messages.append({
                        "role": "tool", "tool_call_id": call_id, "name": name,
                        "content": json.dumps({"ok": True, "done": True}, ensure_ascii=False),
                    })
                else:
                    if name == "read_file":
                        path_arg = str((args or {}).get("path") or "").strip()
                        if path_arg:
                            files_read.add(path_arg)
                    messages.append({
                        "role": "tool", "tool_call_id": call_id, "name": name,
                        "content": sandbox.dispatch(name, args),
                    })
            if finished:
                break
        findings = _normalise_findings(raw_findings, files, model_label)
        reason = ""
        if not finished:
            reason = "max iterations reached before finish_audit"
        result = {
            "ok": True,
            "findings": findings,
            "subsystem": subsystem,
            "model": model_label,
            "tokens": tokens,
            "iterations": iterations,
            "reason": reason,
            "request_id": request_id,
            "files": rel_files,
            "duration_s": round(time.time() - started, 3),
        }
        if usage_metadata:
            result.update(usage_metadata)
        return result
    except Exception as exc:
        return {
            "ok": False,
            "findings": [],
            "subsystem": subsystem,
            "model": None,
            "tokens": 0,
            "iterations": 0,
            "reason": f"{type(exc).__name__}: {exc}",
            "request_id": request_id,
            "files": [],
        }


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    try:
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def write_request(*, subsystem: str, focus: str | None, max_files: int) -> dict[str, Any]:
    files = resolve_subsystem_files(subsystem, max_files=max_files)
    request_id = f"deep-audit-{uuid.uuid4().hex[:12]}"
    payload = {
        "schema": "deep-audit-request-v1",
        "request_id": request_id,
        "created_at": _utc_now(),
        "subsystem": subsystem,
        "focus": focus or "",
        "max_files": _coerce_file_cap(max_files),
        "files": [_repo_rel(p) for p in files],
    }
    request_path = _requests_dir() / f"{request_id}.json"
    _atomic_write_json(request_path, payload)
    payload["request_path"] = str(request_path)
    return payload


def write_lock(*, request_id: str, subsystem: str, pid: int) -> None:
    _atomic_write_json(_lock_path(), {
        "pid": int(pid),
        "request_id": request_id,
        "subsystem": subsystem,
        "started_at": _utc_now(),
    })


def read_status() -> dict[str, Any]:
    try:
        lock = json.loads(_lock_path().read_text(encoding="utf-8")) if _lock_path().exists() else None
    except (OSError, ValueError):
        lock = None
    try:
        status = json.loads(_status_path().read_text(encoding="utf-8")) if _status_path().exists() else {}
    except (OSError, ValueError):
        status = {}
    if isinstance(lock, dict):
        return {
            "state": "running",
            "pid": lock.get("pid"),
            "request_id": lock.get("request_id"),
            "subsystem": lock.get("subsystem"),
            "started_at": lock.get("started_at"),
            "last_run": status.get("last_run"),
        }
    return {
        "state": "idle",
        "pid": None,
        "request_id": None,
        "subsystem": status.get("subsystem"),
        "started_at": None,
        "last_run": status.get("last_run"),
    }


def read_findings() -> dict[str, Any]:
    try:
        data = json.loads(_findings_path().read_text(encoding="utf-8")) if _findings_path().exists() else {}
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {
        "schema": "deep-audit-findings-v1",
        "ok": bool(data.get("ok", False)),
        "subsystem": data.get("subsystem"),
        "model": data.get("model"),
        "tokens": int(data.get("tokens") or 0),
        "iterations": int(data.get("iterations") or 0),
        "reason": str(data.get("reason") or ""),
        "findings": data.get("findings") if isinstance(data.get("findings"), list) else [],
        "proposals": data.get("proposals") if isinstance(data.get("proposals"), list) else [],
        "created_at": data.get("created_at"),
        "request_id": data.get("request_id"),
        "files": data.get("files") if isinstance(data.get("files"), list) else [],
    }


def _save_result(result: dict[str, Any], *, proposed: list[str], errors: int) -> None:
    payload = {
        "schema": "deep-audit-findings-v1",
        **result,
        "proposals": proposed,
        "created_at": _utc_now(),
    }
    _atomic_write_json(_findings_path(), payload)
    _atomic_write_json(_status_path(), {
        "state": "idle",
        "subsystem": result.get("subsystem"),
        "updated_at": _utc_now(),
        "last_run": {
            "ok": result.get("ok"),
            "finished_at": _utc_now(),
            "request_id": result.get("request_id"),
            "subsystem": result.get("subsystem"),
            "proposed": len(proposed),
            "findings": len(result.get("findings") or []),
            "tokens": int(result.get("tokens") or 0),
            "errors": errors,
            "reason": result.get("reason") or "",
        },
    })


def run_request_file(path: Path) -> dict[str, Any]:
    try:
        req = json.loads(path.read_text(encoding="utf-8"))
        subsystem = str(req.get("subsystem") or "")
        focus = str(req.get("focus") or "")
        max_files = _coerce_file_cap(req.get("max_files"))
        request_id = str(req.get("request_id") or path.stem)
    except Exception as exc:
        result = {"ok": False, "findings": [], "subsystem": None, "model": None, "tokens": 0,
                  "iterations": 0, "reason": f"invalid request: {type(exc).__name__}", "request_id": path.stem, "files": []}
        _save_result(result, proposed=[], errors=1)
        return result

    _deep_state_dir().mkdir(parents=True, exist_ok=True)
    _atomic_write_json(_lock_path(), {
        "pid": os.getpid(),
        "request_id": request_id,
        "subsystem": subsystem,
        "started_at": _utc_now(),
    })
    try:
        result = run_deep_audit(subsystem=subsystem, focus=focus, max_files=max_files)
        result["request_id"] = request_id
        proposed: list[str] = []
        errors = 0
        try:
            from hermes_cli import autoresearch_proposals, autoresearch_runs
            detection_only = 0  # high+-Intake-Gate: medium/low geloggt, nicht gequeued (H3)
            for finding in result.get("findings") or []:
                proposal = autoresearch_proposals._build_deep_audit_proposal(finding)
                existing = autoresearch_proposals.load_proposal(proposal["id"])
                if existing and existing.get("status") in {"proposed", "testing", "applied", "skipped"}:
                    continue
                if not autoresearch_proposals.meets_intake_threshold(proposal):
                    # medium/low → detection-only: bleibt im Finding-Result, nicht in der Queue.
                    detection_only += 1
                    continue
                autoresearch_proposals.save_proposal(proposal)
                proposed.append(proposal["id"])
            result["detection_only"] = detection_only
            autoresearch_runs.append_run(
                lane="deep-audit",
                request_id=request_id,
                tokens=int(result.get("tokens") or 0),
                proposed=len(proposed),
                errors=0 if result.get("ok") else 1,
                scanned=len(result.get("files") or []),
                model=result.get("model") or None,
                cost=result.get("cost") if isinstance(result.get("cost"), dict) else None,
                energy=result.get("energy") if isinstance(result.get("energy"), dict) else None,
                response_usage_metadata=(
                    result.get("response_usage_metadata")
                    if isinstance(result.get("response_usage_metadata"), list)
                    else None
                ),
            )
        except Exception:
            errors = 1
        _save_result(result, proposed=proposed, errors=errors)
        return result
    finally:
        try:
            _lock_path().unlink()
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a read-only Autoresearch Deep-Audit request.")
    parser.add_argument("--request", required=True)
    args = parser.parse_args(argv)
    run_request_file(Path(args.request))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
