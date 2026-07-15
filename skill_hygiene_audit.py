#!/usr/bin/env python3
"""Deterministic, read-only hygiene audit for active Hermes SKILL.md files.

Only the explicitly supplied output files are written. All inspected skill roots
are read-only; finding values that might contain secrets or PII are never emitted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import yaml

DEFAULT_ROOT = Path("/home/piet/.hermes/skills")
PROFILES_ROOT = Path("/home/piet/.hermes/profiles")
REQUIRED_FRONTMATTER_FIELDS = ("name", "description")
INLINE_LINK_RE = re.compile(r"!?(?:\[[^\]]*\])\(([^)]+)\)")
REFERENCE_LINK_RE = re.compile(r"^\s*\[[^\]]+\]:\s*(\S+)", re.MULTILINE)
LEGACY_PATTERNS = {
    "openclaw": re.compile(r"\bopenclaw\b", re.IGNORECASE),
    "atlas": re.compile(r"\batlas\b", re.IGNORECASE),
    "mission_control": re.compile(r"\bmission[ _-]?control\b", re.IGNORECASE),
    "coordinator": re.compile(r"\bcoordinator\b", re.IGNORECASE),
}
SECRET_PATTERNS = {
    "private_key_block": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "api_key_assignment": re.compile(
        r"\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|secret|password)\b\s*[:=]"
        r"\s*[^\s#]{8,}",
        re.IGNORECASE,
    ),
    "bearer_token": re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE),
    "provider_token_prefix": re.compile(r"\b(?:sk|ghp|github_pat|xoxb|xoxp)-[A-Za-z0-9_-]{12,}"),
}
PII_PATTERNS = {
    "email_address": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    "phone_number": re.compile(r"(?<!\w)\+?\d[\d .()/-]{7,}\d(?!\w)"),
}
PRIVATE_KEY_BLOCK_RE = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?"
    r"-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    re.DOTALL,
)
PLACEHOLDER_RE = re.compile(
    r"(?:<[^>]*(?:TOKEN|KEY|SECRET|PASSWORD)[^>]*>|\$\{[^}]*"
    r"(?:TOKEN|KEY|SECRET|PASSWORD)[^}]*\}|\b(?:YOUR|REPLACE_ME|EXAMPLE)_"
    r"?[A-Z0-9_]*(?:TOKEN|KEY|SECRET|PASSWORD)\b)",
    re.IGNORECASE,
)


def roots() -> list[tuple[str, Path]]:
    result: list[tuple[str, Path]] = []
    if DEFAULT_ROOT.is_dir():
        result.append(("default", DEFAULT_ROOT))
    if PROFILES_ROOT.is_dir():
        for profile in sorted(PROFILES_ROOT.iterdir(), key=lambda item: item.name):
            skills = profile / "skills"
            if skills.is_dir():
                result.append((f"profile:{profile.name}", skills))
    return result


def active_skill_files(skill_roots: list[tuple[str, Path]]) -> list[tuple[str, Path, Path]]:
    files: list[tuple[str, Path, Path]] = []
    for label, root in skill_roots:
        for path in sorted(root.rglob("SKILL.md"), key=lambda item: item.as_posix()):
            if ".archive" in path.relative_to(root).parts:
                continue
            files.append((label, root, path))
    return files


def relative_id(label: str, root: Path, path: Path) -> str:
    return f"{label}/{path.relative_to(root).as_posix()}"


def parse_frontmatter(text: str) -> tuple[dict[str, Any] | None, str | None]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return None, "fehlender_oeffnender_trenner"
    closing_index = next((i for i, line in enumerate(lines[1:], 1) if line.strip() in {"---", "..."}), None)
    if closing_index is None:
        return None, "fehlender_schliessender_trenner"
    try:
        parsed = yaml.safe_load("".join(lines[1:closing_index]))
    except yaml.YAMLError:
        return None, "yaml_parsefehler"
    if not isinstance(parsed, dict):
        return None, "frontmatter_ist_keine_mapping"
    return parsed, None


def local_link_targets(text: str) -> list[str]:
    raw = INLINE_LINK_RE.findall(text) + REFERENCE_LINK_RE.findall(text)
    targets: set[str] = set()
    for target in raw:
        target = target.strip().strip("<>").split(maxsplit=1)[0]
        target = unquote(target.split("#", 1)[0])
        if not target or target.startswith(("#", "/", "~", "mailto:", "data:")):
            continue
        if re.match(r"^[a-z][a-z0-9+.-]*:", target, flags=re.IGNORECASE):
            continue
        targets.add(target)
    return sorted(targets)


def classify_patterns(text: str) -> tuple[Counter[str], Counter[str], int]:
    secrets = Counter()
    pii = Counter()
    for kind, pattern in SECRET_PATTERNS.items():
        secrets[kind] = len(pattern.findall(text))
    for kind, pattern in PII_PATTERNS.items():
        pii[kind] = len(pattern.findall(text))
    return secrets, pii, len(PLACEHOLDER_RE.findall(text))


def redact_text(value: str) -> str:
    """Replace detected secret/PII values before an audit artifact is emitted."""
    value = PRIVATE_KEY_BLOCK_RE.sub("[REDACTED_SECRET]", value)
    for pattern in SECRET_PATTERNS.values():
        value = pattern.sub("[REDACTED_SECRET]", value)
    for pattern in PII_PATTERNS.values():
        value = pattern.sub("[REDACTED_PII]", value)
    return value


def redact_result(value: Any) -> Any:
    """Recursively redact strings (including mapping keys) in audit output."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_result(item) for item in value]
    if isinstance(value, dict):
        return {redact_text(str(key)): redact_result(item) for key, item in value.items()}
    return value


def audit(skill_roots: list[tuple[str, Path]] | None = None) -> dict[str, Any]:
    if skill_roots is None:
        skill_roots = roots()
    files = active_skill_files(skill_roots)
    inventory = Counter()
    frontmatter_errors: list[dict[str, Any]] = []
    name_mismatches: list[dict[str, str]] = []
    missing_links: list[dict[str, str]] = []
    legacy_hits = Counter()
    legacy_files: dict[str, list[str]] = defaultdict(list)
    names: dict[str, list[dict[str, str]]] = defaultdict(list)
    file_stats: list[dict[str, Any]] = []
    secret_types = Counter()
    pii_types = Counter()
    placeholder_count = 0

    for label, root, path in files:
        identifier = relative_id(label, root, path)
        inventory[label] += 1
        raw = path.read_bytes()
        text = raw.decode("utf-8", errors="replace")
        line_count = len(text.splitlines())
        digest = hashlib.sha256(raw).hexdigest()
        file_stats.append({"file": identifier, "bytes": len(raw), "lines": line_count})

        frontmatter, error = parse_frontmatter(text)
        if error:
            frontmatter_errors.append({"file": identifier, "error": error})
        else:
            assert frontmatter is not None
            missing = [field for field in REQUIRED_FRONTMATTER_FIELDS if not isinstance(frontmatter.get(field), str) or not frontmatter[field].strip()]
            if missing:
                frontmatter_errors.append({"file": identifier, "error": "fehlende_pflichtfelder", "fields": missing})
            else:
                name_value = frontmatter["name"]
                assert isinstance(name_value, str)
                skill_name = name_value.strip()
                if skill_name != path.parent.name:
                    name_mismatches.append({"file": identifier, "frontmatter_name": skill_name, "directory": path.parent.name})
                names[skill_name].append({"file": identifier, "sha256": digest})

        for target in local_link_targets(text):
            # Link targets are intentionally not read; this checks only existence.
            if not (path.parent / target).exists():
                missing_links.append({"file": identifier, "target": target})

        for kind, pattern in LEGACY_PATTERNS.items():
            if pattern.search(text):
                legacy_hits[kind] += 1
                legacy_files[kind].append(identifier)

        secrets, pii, placeholders = classify_patterns(text)
        secret_types.update({kind: count for kind, count in secrets.items() if count})
        pii_types.update({kind: count for kind, count in pii.items() if count})
        placeholder_count += placeholders

    duplicate_names: list[dict[str, Any]] = []
    for name in sorted(names):
        entries = names[name]
        if len(entries) > 1:
            hashes = {entry["sha256"] for entry in entries}
            duplicate_names.append(
                {
                    "name": name,
                    "copies": len(entries),
                    "classification": "identisch" if len(hashes) == 1 else "divergent",
                    "files": [entry["file"] for entry in entries],
                }
            )

    return {
        "audit_schema_version": 1,
        "scope": {
            "roots": [{"label": label, "path": str(root)} for label, root in skill_roots],
            "active_definition": "SKILL.md unter den Wurzeln, mit Ausnahme jedes Pfads unter einer .archive-Komponente",
            "read_only": True,
        },
        "inventory": {
            "active_skill_files": len(files),
            "by_root": dict(sorted(inventory.items())),
            "roots_found": len(skill_roots),
        },
        "frontmatter": {
            "required_fields": list(REQUIRED_FRONTMATTER_FIELDS),
            "issues": sorted(frontmatter_errors, key=lambda item: item["file"]),
            "name_directory_mismatches": sorted(name_mismatches, key=lambda item: item["file"]),
        },
        "local_links": {
            "missing": sorted(missing_links, key=lambda item: (item["file"], item["target"])),
            "false_positive_boundaries": [
                "Geprueft werden nur Inline- und Referenz-Markdown-Links mit relativem Dateiziel.",
                "HTTP(S), andere URI-Schemata, Anker, absolute Pfade, Home-Pfade und data:-Links werden bewusst uebersprungen.",
                "Code-Fences, HTML-Attribute, dynamisch erzeugte Pfade und Link-Title-Syntax sind nicht vollstaendig geparst.",
                "Es wird nur die Existenz geprueft; Link-Ziele werden nicht gelesen und Symlink-Semantik nicht bewertet.",
            ],
        },
        "legacy_references": {
            "definition": "Textuelle Treffer in aktiven SKILL.md; kein Nachweis einer laufenden Integration oder Ausfuehrung.",
            "by_type": dict(sorted(legacy_hits.items())),
            "files_by_type": {kind: sorted(paths) for kind, paths in sorted(legacy_files.items())},
        },
        "duplicates_and_profile_forks": duplicate_names,
        "largest_files": {
            "by_bytes": sorted(file_stats, key=lambda item: (-item["bytes"], item["file"]))[:10],
            "by_lines": sorted(file_stats, key=lambda item: (-item["lines"], item["file"]))[:10],
        },
        "redacted_pattern_summary": {
            "secret_types": dict(sorted(secret_types.items())),
            "pii_types": dict(sorted(pii_types.items())),
            "placeholder_markers": placeholder_count,
            "redaction_guarantee": "Nur Musterarten und Anzahlen; keine Trefferwerte oder Fundstellen werden ausgegeben.",
        },
    }


def markdown_report(result: dict[str, Any]) -> str:
    inventory = result["inventory"]
    frontmatter = result["frontmatter"]
    links = result["local_links"]
    legacy = result["legacy_references"]
    redaction = result["redacted_pattern_summary"]
    duplicates = result["duplicates_and_profile_forks"]
    lines = [
        "# Hermes Skill-Hygiene-Audit",
        "",
        "## Ergebnis",
        f"- Aktive `SKILL.md`: **{inventory['active_skill_files']}** in **{inventory['roots_found']}** Wurzeln.",
        "- Inventar je Wurzel:",
    ]
    lines.extend(f"  - `{root}`: {count}" for root, count in inventory["by_root"].items())
    lines.extend(
        [
            f"- Frontmatter-Probleme: **{len(frontmatter['issues'])}**; Name-vs-Verzeichnis-Abweichungen: **{len(frontmatter['name_directory_mismatches'])}**.",
            f"- Fehlende lokale Markdown-Link-Ziele: **{len(links['missing'])}**.",
            f"- Skill-Namensduplikate/Profil-Forks: **{len(duplicates)}** ({sum(item['classification'] == 'identisch' for item in duplicates)} identisch, {sum(item['classification'] == 'divergent' for item in duplicates)} divergent).",
            "- Legacy-Textreferenzen je Typ: " + (", ".join(f"`{kind}`={count}" for kind, count in legacy["by_type"].items()) or "keine"),
            "- Redacted Muster: " + ", ".join(
                [
                    "Secrets=" + (", ".join(f"{kind}:{count}" for kind, count in redaction["secret_types"].items()) or "0"),
                    "PII=" + (", ".join(f"{kind}:{count}" for kind, count in redaction["pii_types"].items()) or "0"),
                    f"Platzhalter={redaction['placeholder_markers']}",
                ]
            ),
            "",
            "## Pruefregeln und Grenzen",
            f"- Pflichtfelder: {', '.join(f'`{field}`' for field in frontmatter['required_fields'])}; YAML wird mit `yaml.safe_load` geparst.",
            f"- Aktiv bedeutet: {result['scope']['active_definition']}.",
            f"- Legacy-Hits sind {legacy['definition'].lower()}",
            "- Link-Heuristik:",
        ]
    )
    lines.extend(f"  - {boundary}" for boundary in links["false_positive_boundaries"])
    lines.extend(
        [
            "- Redaction: " + redaction["redaction_guarantee"],
            "",
            "## Groesste Dateien nach Bytes",
            "",
            "| Datei | Bytes | Zeilen |",
            "| --- | ---: | ---: |",
        ]
    )
    lines.extend(f"| `{item['file']}` | {item['bytes']} | {item['lines']} |" for item in result["largest_files"]["by_bytes"])
    lines.extend(["", "## Groesste Dateien nach Zeilen", "", "| Datei | Zeilen | Bytes |", "| --- | ---: | ---: |"])
    lines.extend(f"| `{item['file']}` | {item['lines']} | {item['bytes']} |" for item in result["largest_files"]["by_lines"])
    lines.append("")
    return "\n".join(lines)


def validate_output_paths(
    results_path: Path, report_path: Path, skill_roots: list[tuple[str, Path]]
) -> None:
    """Reject artifact destinations that could modify an inspected skill root."""
    resolved_results = results_path.expanduser().resolve(strict=False)
    resolved_report = report_path.expanduser().resolve(strict=False)
    if resolved_results == resolved_report:
        raise ValueError("--results und --report muessen verschiedene Ziele haben")
    for _label, root in skill_roots:
        resolved_root = root.expanduser().resolve(strict=False)
        for output_path in (resolved_results, resolved_report):
            if output_path.is_relative_to(resolved_root):
                raise ValueError(
                    f"Ausgabeziel {output_path} liegt innerhalb einer Audit-Wurzel: {resolved_root}"
                )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, default=Path("results.json"))
    parser.add_argument("--report", type=Path, default=Path("report.md"))
    args = parser.parse_args()
    skill_roots = roots()
    validate_output_paths(args.results, args.report, skill_roots)
    result = redact_result(audit(skill_roots))
    validate_output_paths(args.results, args.report, skill_roots)
    args.results.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    validate_output_paths(args.results, args.report, skill_roots)
    args.report.write_text(markdown_report(result), encoding="utf-8")


if __name__ == "__main__":
    main()
