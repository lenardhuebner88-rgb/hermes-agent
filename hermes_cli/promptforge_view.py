"""Prompt-Schmiede endpoints.

- GET  /api/promptforge/catalog   — the static curated prompt catalog (read-only).
- POST /api/promptforge/generate  — turn a plain-language problem description into a
  polished, ready-to-paste agent prompt using a single cheap LLM call.

Both live under /api/ → gated by the blanket auth_middleware (session token), and
intentionally NOT in PUBLIC_API_PATHS. The catalog is a Python constant
(hermes_cli/promptforge_catalog.py).

Cost safety: the generator model is SERVER-FIXED to the free Gemini Flash tier
(provider="gemini", model="gemini-3-flash-preview"). The caller cannot select the
generator model — the `modelId` in the request is only the *target* model the
generated prompt is meant to run on, surfaced as a hint in the output. max_tokens is
capped. On any LLM failure the endpoint returns `fallback: true` with no prompt, and
the frontend falls back to its deterministic local composer.
"""
from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel

from hermes_cli.promptforge_catalog import PROMPTFORGE_CATALOG

# Server-fixed free generator model — do NOT let callers override (key-burn guard).
_GEN_PROVIDER = "gemini"
_GEN_MODEL = "gemini-3-flash-preview"
_GEN_MAX_TOKENS = 1200
_GEN_TIMEOUT = 40.0


class PromptForgeGenBody(BaseModel):
    problem: str
    targetId: str = "generic"
    taskTypeId: str = "feature"
    modeId: str = "stop-on-doubt"
    modelId: str = ""


def _by_id(items: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
    return next((it for it in items if it.get("id") == key), None)


def _block_body(block_id: str) -> str:
    block = _by_id(PROMPTFORGE_CATALOG["blocks"], block_id)
    return str(block.get("body", "")) if block else ""


def _build_system_prompt(target: dict[str, Any], task_type: dict[str, Any], mode: dict[str, Any], model_id: str) -> str:
    """Compose the instruction the generator model follows. The catalog blocks are
    the reference material; the model weaves them into a tailored prompt."""
    overrides = mode.get("overrides", {})
    persistence = overrides.get("persistence") or _block_body("persistence")
    rev_gate = overrides.get("reversibilityGate", "")
    escalation = overrides.get("escalation", "")
    wrap = target.get("wrapMode", "system-prompt")

    lines = [
        "You are an expert prompt engineer for autonomous coding agents (Claude Code, Codex).",
        "Turn the operator's plain-language problem into ONE polished, ready-to-paste prompt for",
        "the target tool. Output ONLY the final prompt text — no preamble, no explanation, no",
        "markdown code fences.",
        "",
        f"TARGET TOOL: {target.get('label', '')} — {target.get('mechanicNote', '')}",
        f"TASK TYPE: {task_type.get('label', '')}",
        f"MODE: {mode.get('label', '')} — {mode.get('description', '')}",
    ]
    if model_id:
        lines.append(f"TARGET MODEL (mention once as a comment for the operator): {model_id}")
    lines += [
        "",
        "Weave in these best-practice building blocks, adapted to the problem (do not paste verbatim):",
        f"- Persistence: {persistence}",
        f"- Verification: {_block_body('verification')}",
        f"- Scope discipline: {_block_body('scope-constraints')}",
    ]
    if rev_gate:
        lines.append(f"- Reversibility/safety gate: {rev_gate}")
    if escalation:
        lines.append(f"- Escalation: {escalation}")
    lines += [
        "",
        "IMPORTANT — the operator is NOT a developer and does NOT know the codebase layout.",
        "Never ask them for file paths or scope. Instead, instruct the agent to LOCATE the",
        "relevant files itself first, and to change only what the task needs.",
        "",
        "TASK-TYPE BLUEPRINT (follow this structure):",
        task_type.get("typeBody", ""),
        "",
        "CRITICAL — completion / acceptance criteria:",
        "Derive 2-4 MEASURABLE acceptance criteria directly from the operator's problem — the",
        "observable end-state that proves it is solved.",
    ]
    if wrap == "completion-condition":
        lines += [
            "This is a /goal prompt: an evaluator checks completion from the agent's TRANSCRIPT",
            "output only (never the filesystem). Each criterion MUST be provable from what the",
            "agent prints (e.g. 'the build exits 0', 'a screenshot shows the grouped list', 'git",
            "status is clean'). Begin the prompt with a '/goal Completion condition: …' line that",
            "states these criteria, then append 'or stop after N turns'.",
        ]
    elif wrap == "interval-loop":
        lines += [
            "This is a /loop prompt: begin with the '/loop' cadence line, and add a per-round",
            "protocol — each round state [DONE] or [CONTINUE: <reason>]; stop after K rounds or",
            "when [DONE]; never proceed if a round made no measurable progress.",
        ]
    elif wrap == "full-auto":
        lines += [
            "This is a codex /goal full-auto prompt: AGENTS.md is the operating manual. Include a",
            "hard deny-list (force-push, mass-delete, writing credentials to unrelated files,",
            "exfiltration).",
        ]
    else:
        lines.append("Wrap the whole thing as an XML-tagged <system_prompt>…</system_prompt>.")
    lines += [
        "",
        "Write the final prompt in the SAME LANGUAGE as the operator's problem description.",
        "Keep it tight and skimmable. No filler, no meta-commentary.",
    ]
    return "\n".join(line for line in lines if line is not None)


def register_promptforge_routes(app: Any) -> None:
    @app.get("/api/promptforge/catalog")
    async def get_promptforge_catalog() -> dict[str, Any]:
        return PROMPTFORGE_CATALOG

    @app.post("/api/promptforge/generate")
    async def generate_prompt(body: PromptForgeGenBody) -> dict[str, Any]:
        problem = (body.problem or "").strip()
        if not problem:
            return {"prompt": "", "fallback": True, "error": "empty problem"}

        target = _by_id(PROMPTFORGE_CATALOG["targets"], body.targetId) or _by_id(PROMPTFORGE_CATALOG["targets"], "generic")
        task_type = _by_id(PROMPTFORGE_CATALOG["taskTypes"], body.taskTypeId) or PROMPTFORGE_CATALOG["taskTypes"][0]
        mode = _by_id(PROMPTFORGE_CATALOG["modes"], body.modeId) or PROMPTFORGE_CATALOG["modes"][0]
        system_prompt = _build_system_prompt(target, task_type, mode, body.modelId)

        def _generate() -> str:
            # Imported lazily so the catalog GET route never pays the agent import cost.
            from agent.auxiliary_client import call_llm

            resp = call_llm(
                provider=_GEN_PROVIDER,
                model=_GEN_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": problem},
                ],
                temperature=0.4,
                max_tokens=_GEN_MAX_TOKENS,
                timeout=_GEN_TIMEOUT,
            )
            return str(resp.choices[0].message.content or "").strip()

        try:
            prompt = await asyncio.to_thread(_generate)
        except Exception as exc:  # noqa: BLE001 — any provider failure → graceful fallback
            return {"prompt": "", "fallback": True, "error": str(exc)[:200]}

        if not prompt:
            return {"prompt": "", "fallback": True, "error": "empty completion"}
        return {"prompt": prompt, "fallback": False, "model": _GEN_MODEL}
