"""Server-side scope-contract templates (PlanSpec B).

Reusable ``scope_contract`` defaults for recurring worker task types. A task
body may reference a template by name via a ``contract_profile: <name>`` line;
the worker-context renderer expands it to the full contract instead of the task
carrying inline ``scope_contract`` YAML boilerplate, and the dispatcher records
the expansion in ``run.metadata.expanded_contract`` as a permanent trace.

SECURITY INVARIANT (PlanSpec B — Step 4 dropped after Opus-4.8 review):
Only ``scope_contract_version`` is a *static* value safe to auto-fill from a
template. The gate's other required fields — ``scope_attestation`` and
``forbidden_actions_taken`` — are *runtime evidence* asserted by the
worker/reviewer at completion. Auto-filling them from a static template would
fabricate the attestation and bypass the security gate. :func:`validate_template`
fails closed if ``auto_fill`` carries anything other than
``scope_contract_version``.

Templates are versioned by name (``read_only_audit_v2`` → ``..._v3``) so that an
``expanded_contract`` trace recorded against an older run stays meaningful even
after a template definition changes (Pitfall 1 — template drift).
"""
from __future__ import annotations

from typing import Optional

# The ONLY field a static template may auto-fill. Everything else the gate
# requires is runtime evidence and must stay the worker's own assertion.
AUTO_FILLABLE_FIELDS = frozenset({"scope_contract_version"})

# Keys every template must define (evidence_requirements is optional).
_REQUIRED_LIST_FIELDS = ("forbidden_actions", "in_scope", "out_of_scope")


SCOPE_CONTRACT_TEMPLATES: dict[str, dict] = {
    "read_only_audit_v2": {
        "scope_contract_version": 2,
        "forbidden_actions": ["file_write", "git_push", "git_commit", "db_mutation"],
        "in_scope": ["read files", "run read-only commands", "query APIs"],
        "out_of_scope": ["any file writes", "any mutations", "any deploys"],
        "evidence_requirements": ["findings posted as kanban comment"],
        "auto_fill": {
            "scope_contract_version": 2,
        },
    },
    "code_implementation_v2": {
        "scope_contract_version": 2,
        "forbidden_actions": ["git_push", "deploy", "service_stop"],
        "in_scope": ["edit files in allowed_paths", "run tests", "run linting"],
        "out_of_scope": ["git push", "deploy", "infra mutation"],
        "evidence_requirements": ["targeted gate output", "changed paths listed at completion"],
        "auto_fill": {
            "scope_contract_version": 2,
        },
    },
    "research_synthesis_v2": {
        "scope_contract_version": 2,
        "forbidden_actions": ["file_write", "git_push", "git_commit", "deploy", "db_mutation"],
        "in_scope": ["read files", "query research APIs", "fetch web sources", "post findings as kanban comment"],
        "out_of_scope": ["any file writes", "code changes", "any mutations", "any deploys"],
        "evidence_requirements": ["sources cited", "synthesis posted as kanban comment"],
        "auto_fill": {
            "scope_contract_version": 2,
        },
    },
    "review_verdict_v2": {
        "scope_contract_version": 2,
        "forbidden_actions": ["file_write", "git_push", "git_commit", "deploy", "service_stop", "db_mutation"],
        "in_scope": ["read the diff", "read changed files", "run read-only gates", "post verdict metadata"],
        "out_of_scope": ["editing code under review", "merging", "deploying", "force-push"],
        "evidence_requirements": ["verdict in APPROVED|NEEDS_REVISION|BLOCKED", "evidence_audited list", "residual_risk"],
        "auto_fill": {
            "scope_contract_version": 2,
        },
    },
}


def template_names() -> list[str]:
    """Sorted list of registered template names."""
    return sorted(SCOPE_CONTRACT_TEMPLATES)


def get_template(name: Optional[str]) -> Optional[dict]:
    """Return the template dict for ``name`` or ``None`` when unknown."""
    if not name:
        return None
    return SCOPE_CONTRACT_TEMPLATES.get(name)


def validate_template(template: object) -> list[str]:
    """Return a list of structural problems with ``template`` (empty == valid).

    Enforces the security invariant: ``auto_fill`` may only carry
    ``scope_contract_version`` (see module docstring).
    """
    problems: list[str] = []
    if not isinstance(template, dict):
        return ["template must be a dict"]

    version = template.get("scope_contract_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 2:
        problems.append("scope_contract_version must be an int >= 2")

    for field in _REQUIRED_LIST_FIELDS:
        value = template.get(field)
        if not isinstance(value, list) or not value:
            problems.append(f"{field} must be a non-empty list")
        elif not all(isinstance(v, str) for v in value):
            problems.append(f"{field} entries must be strings")

    evidence = template.get("evidence_requirements")
    if evidence is not None and (
        not isinstance(evidence, list) or not all(isinstance(v, str) for v in evidence)
    ):
        problems.append("evidence_requirements, when present, must be a list of strings")

    auto_fill = template.get("auto_fill")
    if not isinstance(auto_fill, dict):
        problems.append("auto_fill must be a dict")
    else:
        extra = set(auto_fill) - AUTO_FILLABLE_FIELDS
        if extra:
            # Security gate: refuse to auto-fill runtime-evidence fields.
            problems.append(
                "auto_fill may only contain scope_contract_version; "
                f"forbidden auto-fill fields present: {sorted(extra)}"
            )
        if "scope_contract_version" in auto_fill:
            af_version = auto_fill["scope_contract_version"]
            if af_version != version:
                problems.append(
                    "auto_fill.scope_contract_version must match scope_contract_version"
                )

    return problems
