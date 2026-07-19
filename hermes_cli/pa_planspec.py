"""PlanSpec drafting and approval-gated ingest for the Projekte PA.

Drafting is read-only apart from the profile-local markdown/metadata files.
Board mutation remains behind the existing ``pa_action`` claim-once executor:
the draft endpoint validates, the proposal endpoint creates an approval card,
and only a confirmed ``planspec.ingest`` action invokes ``hermes plan ingest``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import subprocess
import time
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from hermes_constants import get_hermes_home

VALIDATE_TIMEOUT_SECONDS = 45
INGEST_TIMEOUT_SECONDS = 180
ENGINE_OUTPUT_MAX_CHARS = 128_000
ENGINE_ERROR_EXCERPT_CHARS = 4_000
FINDINGS_MAX = 24
FINDING_MAX_CHARS = 600
PROCESS_TAIL_MAX_CHARS = 4_000

DRAFT_ID_RE = re.compile(r"^draft_[a-f0-9]{24}$")
_FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n(?P<yaml>.*?)\r?\n---[ \t]*(?:\r?\n|\Z)(?P<body>.*)\Z",
    re.DOTALL,
)
_FENCE_OPEN_RE = re.compile(r"```(?:markdown|md|yaml)?[ \t]*\r?\n(?=---)", re.IGNORECASE)
_VALIDATE_STATUS_RE = re.compile(
    r"plan validate:\s*(CLEAN|WARN|BLOCK|INVALID)\b", re.IGNORECASE
)
_TASK_ID_RE = re.compile(r"\bt_[A-Za-z0-9]+\b")

DRAFT_SYSTEM_PROMPT = """Du erzeugst genau EINE vollständige PlanSpec-Markdown-Datei.
Keine Einleitung, keine Nachbemerkung. Ein einzelner Markdown-Fence ist erlaubt.

Der folgende Schema-Kern ist bindend:
- YAML-Frontmatter beginnt und endet mit einer eigenen Zeile `---`.
- `freigabe: operator` ist zwingend. Verwende niemals `complete`: Der Ingest
  materialisiert nur eine gehaltene Kette; die Arbeitsfreigabe bleibt bei Piet.
- `live_test_depth` ist genau `smoke`, `contract` oder `ui-real`.
- `taskgraph_hints.binding: true`.
- `taskgraph_hints.subtasks` enthält pro Slice mindestens `id`, `title`, `lane`
  und `deps`. IDs sind eindeutig; deps verweisen nur auf IDs derselben Liste;
  keine Selbstabhängigkeiten und keine Zyklen.
- Verwende kleine vertikale, unabhängig reviewbare Slices. Ein kohärentes Feature
  bleibt standardmäßig bei einem Owner. Nutze nur echte Hermes-Lanes wie coder,
  premium, scout, verifier, reviewer oder critic.
- Jeder umsetzende Slice bekommt konkrete, testbare `acceptance_criteria`.
- Wenn Zielpfade aus dem Auftrag hervorgehen, trage exakte repo-relative Pfade
  unter `scope_files` ein. Erfinde keine Pfade; wenn sie unbekannt sind, formuliere
  zuerst einen read-only scout-Slice, der sie belegt.
- Hinterlasse keinerlei Platzhalterreste: keine spitzen Schema-Platzhalter,
  kein TODO/FIXME/TBD und insbesondere niemals die literale Zeichenfolge `...`.
- Design-/UI-Slices benennen Referenz, Desktop- und 390px-Mobile-Sichtprüfung,
  Overflow-/Konsolencheck und Screenshot-Evidenz.
- Nach dem Frontmatter folgt kurze Prosa mit Ziel, Grenzen und Done-when.

Formuliere Titel, Kriterien und Anweisungen konkret aus. YAML muss parsebar sein.
"""


class DraftExtractionError(ValueError):
    """The engine response did not contain one usable PlanSpec document."""


class DraftNotFoundError(ValueError):
    """A valid draft id whose markdown or metadata is unavailable."""


class DraftNotReadyError(ValueError):
    """A draft that must not be submitted for approval."""


class DraftIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idea: str = Field(min_length=1, max_length=4_000)
    project: str | None = Field(default=None, max_length=128)
    engine: str | None = Field(default=None, max_length=32)
    model: str | None = Field(default=None, max_length=128)

    @field_validator("idea")
    @classmethod
    def _idea_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("idea darf nicht leer sein")
        return value

    @field_validator("project")
    @classmethod
    def _project_not_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class ProposeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draft_id: str = Field(min_length=1, max_length=128)


def planspecs_dir() -> Path:
    return get_hermes_home() / "pa" / "planspecs"


def _draft_path_unchecked(draft_id: str) -> Path:
    if not DRAFT_ID_RE.fullmatch(draft_id or ""):
        raise ValueError("Ungültige draft_id")
    root = planspecs_dir().resolve()
    candidate = (root / f"{draft_id}.md").resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:  # Defensive parity with pa_chat.resolve_asset.
        raise ValueError("Ungültige draft_id") from exc
    return candidate


def resolve_draft(draft_id: str) -> Path:
    candidate = _draft_path_unchecked(draft_id)
    if not candidate.is_file():
        raise DraftNotFoundError("Unbekannte draft_id")
    return candidate


def _metadata_path(draft_id: str) -> Path:
    return _draft_path_unchecked(draft_id).with_suffix(".json")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _bounded_text(value: object, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _bounded_tail(value: object, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return "…" + text[-max(0, limit - 1) :]


def _bounded_findings(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    findings: list[str] = []
    for value in values[:FINDINGS_MAX]:
        rendered = _bounded_text(value, FINDING_MAX_CHARS)
        if rendered:
            findings.append(rendered)
    return findings


def _candidate_document(raw: str) -> str:
    stripped = (raw or "").strip()
    if len(stripped) > ENGINE_OUTPUT_MAX_CHARS:
        raise DraftExtractionError("Engine-Ausgabe ist zu groß")
    if stripped.startswith("---"):
        return stripped

    opening = _FENCE_OPEN_RE.search(stripped)
    if opening is not None:
        closing = stripped.rfind("```")
        if closing > opening.end():
            return stripped[opening.end() : closing].strip()
    raise DraftExtractionError("Engine-Ausgabe enthält keine PlanSpec-YAML-Frontmatter")


def _frontmatter_and_body(text: str) -> tuple[dict[str, Any], str]:
    match = _FRONTMATTER_RE.fullmatch(text.strip())
    if match is None:
        raise DraftExtractionError("PlanSpec braucht vollständige YAML-Frontmatter")
    try:
        frontmatter = yaml.safe_load(match.group("yaml"))
    except yaml.YAMLError as exc:
        raise DraftExtractionError(f"PlanSpec-YAML ist ungültig: {exc}") from exc
    if not isinstance(frontmatter, dict):
        raise DraftExtractionError("PlanSpec-Frontmatter muss ein YAML-Objekt sein")
    body = match.group("body").strip()
    if not body:
        raise DraftExtractionError("PlanSpec braucht Prosa nach dem Frontmatter")
    return frontmatter, body


def extract_planspec_text(raw: str) -> tuple[str, dict[str, Any]]:
    """Extract a fenced or whole-document PlanSpec and prove its YAML parses."""
    candidate = _candidate_document(raw)
    frontmatter, _body = _frontmatter_and_body(candidate)
    return candidate.strip() + "\n", frontmatter


def extract_slices(frontmatter: dict[str, Any]) -> list[dict[str, Any]]:
    hints = frontmatter.get("taskgraph_hints")
    raw_slices = hints.get("subtasks") if isinstance(hints, dict) else None
    if not isinstance(raw_slices, list):
        return []
    slices: list[dict[str, Any]] = []
    for raw in raw_slices:
        if not isinstance(raw, dict):
            continue
        deps = raw.get("deps")
        slices.append(
            {
                "id": str(raw.get("id") or "").strip(),
                "title": str(raw.get("title") or "").strip(),
                "lane": str(raw.get("lane") or "").strip(),
                "deps": [str(value).strip() for value in deps]
                if isinstance(deps, list)
                else [],
            }
        )
    return slices


def compose_draft_prompt(*, idea: str, project: str | None) -> str:
    project_line = project or "(kein Projekt-Scope angegeben)"
    return (
        f"{DRAFT_SYSTEM_PROMPT}\n\n"
        f"PROJEKTKONTEXT: {project_line}\n"
        f"AUFTRAG/IDEE:\n{idea.strip()}"
    )


def _resolve_engine_model(engine: str | None, model: str | None) -> tuple[str, str]:
    from hermes_cli import pa_chat

    selected_engine = (engine or pa_chat.DEFAULT_ENGINE).strip() or pa_chat.DEFAULT_ENGINE
    spec = pa_chat.ENGINE_REGISTRY.get(selected_engine)
    if spec is None:
        raise ValueError("Unbekannte PA-Engine")
    selected_model = (model or spec.default_model).strip() or spec.default_model
    selected_model = pa_chat._LEGACY_MODEL_ALIASES.get(selected_engine, {}).get(
        selected_model, selected_model
    )
    if selected_model not in spec.models:
        raise ValueError("PA-Modell passt nicht zur Engine")
    return selected_engine, selected_model


def _plan_env() -> dict[str, str]:
    env = os.environ.copy()
    env["HERMES_PA_PLANS_ROOT"] = str(planspecs_dir().resolve())
    return env


def parse_validation_output(
    *, returncode: int, stdout: str, stderr: str
) -> dict[str, Any]:
    """Normalize the CLI JSON/text contract to CLEAN/WARN/BLOCK."""
    payload: dict[str, Any] | None = None
    stdout_s = (stdout or "").strip()
    if stdout_s:
        try:
            decoded = json.loads(stdout_s)
            if isinstance(decoded, dict):
                payload = decoded
        except json.JSONDecodeError:
            payload = None

    if payload is not None:
        disposition = str(payload.get("disposition") or "").lower()
        status = {
            "clean": "CLEAN",
            "warn": "WARN",
            "block": "BLOCK",
            "invalid": "BLOCK",
        }.get(disposition, "BLOCK")
        findings = _bounded_findings(payload.get("findings"))
        if returncode != 0 and status in {"CLEAN", "WARN"}:
            status = "BLOCK"
            findings.append(f"Validator endete unerwartet mit Exit {returncode}")
        return {"status": status, "findings": findings, "exit": int(returncode)}

    combined = "\n".join(part for part in (stdout or "", stderr or "") if part)
    match = _VALIDATE_STATUS_RE.search(combined)
    token = match.group(1).upper() if match else ""
    status = "BLOCK" if token in {"BLOCK", "INVALID", ""} else token
    findings = _bounded_findings(
        [line.strip()[2:].strip() for line in combined.splitlines() if line.strip().startswith("- ")]
    )
    if not match:
        detail = _bounded_text(combined, FINDING_MAX_CHARS)
        findings = [
            f"Validator-Ausgabe nicht erkennbar (Exit {returncode})"
            + (f": {detail}" if detail else "")
        ]
    elif returncode != 0 and status in {"CLEAN", "WARN"}:
        status = "BLOCK"
        findings.append(f"Validator endete unerwartet mit Exit {returncode}")
    return {"status": status, "findings": findings, "exit": int(returncode)}


def run_plan_validate(path: Path) -> dict[str, Any]:
    from hermes_cli.pa_chat import _hermes_bin

    try:
        completed = subprocess.run(
            [_hermes_bin(), "plan", "validate", str(path), "--json"],
            capture_output=True,
            text=True,
            timeout=VALIDATE_TIMEOUT_SECONDS,
            check=False,
            env=_plan_env(),
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "BLOCK",
            "findings": ["PlanSpec-Validator hat das Zeitlimit überschritten"],
            "exit": 124,
        }
    except OSError as exc:
        return {
            "status": "BLOCK",
            "findings": [f"PlanSpec-Validator nicht verfügbar: {_bounded_text(exc, 400)}"],
            "exit": 127,
        }

    validation = parse_validation_output(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )
    try:
        frontmatter, _body = _frontmatter_and_body(path.read_text(encoding="utf-8"))
        if str(frontmatter.get("freigabe") or "").strip().lower() != "operator":
            validation["status"] = "BLOCK"
            validation["findings"] = _bounded_findings(
                [
                    *validation.get("findings", []),
                    "PA-PlanSpecs müssen freigabe: operator verwenden",
                ]
            )
    except (OSError, UnicodeError, DraftExtractionError) as exc:
        validation["status"] = "BLOCK"
        validation["findings"] = _bounded_findings(
            [*validation.get("findings", []), f"Draft nicht lesbar: {exc}"]
        )
    return validation


def _public_validation(validation: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": str(validation.get("status") or "BLOCK"),
        "findings": _bounded_findings(validation.get("findings")),
    }


def _write_metadata(draft_id: str, record: dict[str, Any]) -> None:
    path = _metadata_path(draft_id)
    temp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    temp.write_text(
        json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)


def _read_metadata(draft_id: str) -> dict[str, Any]:
    path = _metadata_path(draft_id)
    if not path.is_file():
        raise DraftNotFoundError("Kein Validate-Ergebnis für diese draft_id")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DraftNotReadyError("Validate-Metadaten sind nicht lesbar") from exc
    if not isinstance(record, dict) or record.get("draft_id") != draft_id:
        raise DraftNotReadyError("Validate-Metadaten passen nicht zur draft_id")
    return record


def draft_planspec(payload: DraftIn) -> dict[str, Any]:
    from hermes_cli import pa_chat

    engine, model = _resolve_engine_model(payload.engine, payload.model)
    prompt = compose_draft_prompt(idea=payload.idea, project=payload.project)
    raw = pa_chat.run_engine(engine, prompt, model=model, image_paths=[])
    try:
        planspec_text, frontmatter = extract_planspec_text(raw)
    except DraftExtractionError as exc:
        exc.engine_excerpt = _bounded_text(raw, ENGINE_ERROR_EXCERPT_CHARS)  # type: ignore[attr-defined]
        raise

    root = planspecs_dir()
    root.mkdir(parents=True, exist_ok=True)
    draft_id = f"draft_{secrets.token_hex(12)}"
    path = _draft_path_unchecked(draft_id)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(planspec_text)

    validation = run_plan_validate(path)
    slices = extract_slices(frontmatter)
    record = {
        "version": 1,
        "draft_id": draft_id,
        "created_at": int(time.time()),
        "engine": engine,
        "model": model,
        "project": payload.project,
        "planspec_sha256": _sha256_text(planspec_text),
        "validation": _public_validation(validation),
        "slices": slices,
        "gates": {
            "freigabe": str(frontmatter.get("freigabe") or "").strip(),
            "live_test_depth": str(frontmatter.get("live_test_depth") or "").strip(),
        },
    }
    _write_metadata(draft_id, record)
    return {
        "draft_id": draft_id,
        "planspec_text": planspec_text,
        "validation": _public_validation(validation),
        "slices": slices,
    }


def proposal_snapshot(draft_id: str) -> dict[str, Any]:
    path = resolve_draft(draft_id)
    record = _read_metadata(draft_id)
    text = path.read_text(encoding="utf-8")
    if record.get("planspec_sha256") != _sha256_text(text):
        raise DraftNotReadyError("Draft wurde seit dem letzten Validate verändert")
    validation = record.get("validation")
    if not isinstance(validation, dict):
        raise DraftNotReadyError("Kein gültiges Validate-Ergebnis vorhanden")
    status = str(validation.get("status") or "BLOCK").upper()
    if status == "BLOCK":
        findings = _bounded_findings(validation.get("findings"))
        detail = "; ".join(findings[:3]) or "Validator blockiert den Draft"
        raise DraftNotReadyError(f"BLOCK: {detail}")
    if status not in {"CLEAN", "WARN"}:
        raise DraftNotReadyError("Unbekannter Validate-Status")
    return record


def build_ingest_question(envelope: dict[str, Any]) -> str:
    payload = envelope.get("payload") if isinstance(envelope, dict) else None
    if not isinstance(payload, dict):
        raise ValueError("planspec.ingest braucht ein Payload-Objekt")
    draft_id = str(payload.get("draft_id") or "")
    record = proposal_snapshot(draft_id)
    validation = record["validation"]
    gates = record.get("gates") if isinstance(record.get("gates"), dict) else {}
    slices = record.get("slices") if isinstance(record.get("slices"), list) else []
    lines = [
        "PlanSpec als gehaltene Kette ingesten?",
        f"Draft: `{draft_id}`",
        f"Validate: {validation['status']} ({len(validation.get('findings') or [])} Findings)",
        "Gates: "
        f"freigabe={gates.get('freigabe') or '-'} · "
        f"live_test_depth={gates.get('live_test_depth') or '-'}",
        f"Slices ({len(slices)}):",
    ]
    for item in slices[:30]:
        if not isinstance(item, dict):
            continue
        deps = item.get("deps") if isinstance(item.get("deps"), list) else []
        dep_text = ", ".join(str(dep) for dep in deps) or "—"
        title = " ".join(str(item.get("title") or "").split())
        lines.append(
            f"- `{item.get('id') or '-'}` [{item.get('lane') or '-'}] {title} · deps: {dep_text}"
        )
    if len(slices) > 30:
        lines.append(f"- plus {len(slices) - 30} weitere Slices")
    findings = _bounded_findings(validation.get("findings"))
    for finding in findings:
        lines.append(f"- Validate-Finding: {finding}")
    reason = payload.get("reason") or envelope.get("reason")
    if reason:
        lines.append(f"Grund: {_bounded_text(reason, 500)}")
    return "\n".join(lines)


def _ingest_ids(stdout: str) -> tuple[str | None, list[str]]:
    chain_id: str | None = None
    task_ids: list[str] = []
    try:
        decoded = json.loads((stdout or "").strip())
    except json.JSONDecodeError:
        decoded = None
    if isinstance(decoded, dict):
        raw_chain = decoded.get("root_task_id") or decoded.get("chain_id")
        if isinstance(raw_chain, str) and _TASK_ID_RE.fullmatch(raw_chain):
            chain_id = raw_chain
        raw_tasks = decoded.get("child_ids") or decoded.get("task_ids")
        if isinstance(raw_tasks, list):
            task_ids = [
                value
                for value in raw_tasks
                if isinstance(value, str) and _TASK_ID_RE.fullmatch(value)
            ]
    if chain_id is None:
        match = re.search(r"\broot\s+(t_[A-Za-z0-9]+)\b", stdout or "", re.IGNORECASE)
        if match:
            chain_id = match.group(1)
    if not task_ids:
        task_ids = list(dict.fromkeys(_TASK_ID_RE.findall(stdout or "")))
        if chain_id in task_ids:
            task_ids.remove(chain_id)
    return chain_id, task_ids


def ingest_draft(payload: dict[str, str]) -> dict[str, Any]:
    """Revalidate at the executor boundary, then ingest at most once.

    WARN policy: ingest is allowed only when the persisted, content-matched
    validation shown on the approval card was WARN with the same findings. A
    new or changed WARN is blocked. Any content change is blocked as well.
    """
    draft_id = payload.get("draft_id", "")
    try:
        path = resolve_draft(draft_id)
        record = _read_metadata(draft_id)
        text = path.read_text(encoding="utf-8")
    except (ValueError, OSError, UnicodeError) as exc:
        return {"ok": False, "exit": 1, "error": _bounded_text(exc, 1_000)}

    shown_validation = record.get("validation")
    shown_status = (
        str(shown_validation.get("status") or "").upper()
        if isinstance(shown_validation, dict)
        else ""
    )
    approved_hash = record.get("planspec_sha256")
    content_matches = approved_hash == _sha256_text(text)
    recheck = run_plan_validate(path)
    status = str(recheck.get("status") or "BLOCK").upper()
    findings = _bounded_findings(recheck.get("findings"))
    base = {
        "exit": int(recheck.get("exit") or 0),
        "draft_id": draft_id,
        "validation": {"status": status, "findings": findings},
    }
    if status == "BLOCK":
        return {
            **base,
            "ok": False,
            "error": "PlanSpec-Re-Check blockiert den Ingest",
        }
    try:
        content_matches = content_matches and approved_hash == _sha256_text(
            path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError) as exc:
        return {
            **base,
            "ok": False,
            "error": f"Draft nach Re-Check nicht lesbar: {_bounded_text(exc, 800)}",
        }
    if not content_matches:
        return {
            **base,
            "ok": False,
            "error": "Draft wurde nach der Approval-Card verändert",
        }
    if status == "WARN" and shown_status != "WARN":
        return {
            **base,
            "ok": False,
            "error": "Neue WARN-Findings waren auf der Approval-Card nicht sichtbar",
        }
    shown_findings = (
        _bounded_findings(shown_validation.get("findings"))
        if isinstance(shown_validation, dict)
        else []
    )
    if status == "WARN" and shown_findings != findings:
        return {
            **base,
            "ok": False,
            "error": "Geänderte WARN-Findings waren auf der Approval-Card nicht sichtbar",
        }
    if status not in {"CLEAN", "WARN"}:
        return {**base, "ok": False, "error": "Unbekannter Validate-Status"}

    from hermes_cli.pa_chat import _hermes_bin

    argv = [
        _hermes_bin(),
        "plan",
        "ingest",
        str(path),
        "--author",
        "pa-executor",
        "--json",
    ]
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=INGEST_TIMEOUT_SECONDS,
            check=False,
            env=_plan_env(),
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return {
            **base,
            "ok": False,
            "exit": 124,
            "error": "PlanSpec-Ingest hat das Zeitlimit überschritten; kein Retry",
        }
    except OSError as exc:
        return {
            **base,
            "ok": False,
            "exit": 127,
            "error": f"PlanSpec-Ingest nicht verfügbar: {_bounded_text(exc, 800)}",
        }

    stdout_tail = _bounded_tail(completed.stdout, PROCESS_TAIL_MAX_CHARS)
    stderr_tail = _bounded_tail(completed.stderr, PROCESS_TAIL_MAX_CHARS)
    chain_id, task_ids = _ingest_ids(completed.stdout)
    result: dict[str, Any] = {
        **base,
        "ok": completed.returncode == 0,
        "exit": int(completed.returncode),
        "stdout_tail": stdout_tail,
        "chain_id": chain_id,
        "task_ids": task_ids,
    }
    if stderr_tail:
        result["stderr_tail"] = stderr_tail
    if completed.returncode != 0:
        result["error"] = _bounded_text(
            completed.stderr or completed.stdout or "PlanSpec-Ingest fehlgeschlagen",
            1_000,
        )
    return result


def register_pa_planspec_routes(app: FastAPI) -> None:
    from hermes_cli import pa_chat

    @app.post("/api/pa/planspec/draft")
    async def pa_planspec_draft(payload: DraftIn) -> dict[str, Any]:
        try:
            return await pa_chat._run_sync(draft_planspec, payload)
        except DraftExtractionError as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": str(exc),
                    "engine_output": getattr(exc, "engine_excerpt", ""),
                },
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except pa_chat.PAEngineError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/pa/planspec/propose")
    async def pa_planspec_propose(payload: ProposeIn) -> dict[str, int]:
        try:
            await pa_chat._run_sync(proposal_snapshot, payload.draft_id)
            from hermes_cli.pa_actions import enqueue_pa_action

            question_id = await pa_chat._run_sync(
                enqueue_pa_action,
                "planspec.ingest",
                {"draft_id": payload.draft_id},
                reason="Validierten PlanSpec-Entwurf als gehaltene Kette anlegen",
            )
        except DraftNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (DraftNotReadyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"question_id": int(question_id)}
