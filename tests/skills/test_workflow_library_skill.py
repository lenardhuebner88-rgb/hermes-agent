from __future__ import annotations

from pathlib import Path

from agent.skill_utils import parse_frontmatter


REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO_ROOT / "skills" / "software-development" / "workflow-library"
DOC_PATH = REPO_ROOT / "website" / "docs" / "user-guide" / "features" / "workflow-library.md"


def test_workflow_library_skill_entrypoint_and_templates_exist() -> None:
    skill_md = SKILL_DIR / "SKILL.md"
    assert skill_md.exists()

    frontmatter, body = parse_frontmatter(skill_md.read_text(encoding="utf-8"))
    assert frontmatter["name"] == "workflow-library"
    assert "Promptgenerator" in body or "prompt generator" in body.lower()
    assert "Goal vs Loop vs Plan-Spec" in body
    assert "Verification" in body

    for relative in [
        "templates/goal-prompt-generator.md",
        "templates/loop-prompt-generator.md",
        "templates/plan-spec-generator.md",
        "templates/codebase-analysis-goal.md",
        "templates/codebase-modernization-goal.md",
        "templates/debugging-goal.md",
        "templates/research-goal.md",
        "templates/docs-goal.md",
        "templates/ops-readonly-goal.md",
    ]:
        content = (SKILL_DIR / relative).read_text(encoding="utf-8")
        assert "/goal" in content
        assert "Done" in content or "Stop" in content


def test_curated_prompts_and_eval_references_cover_required_mvp() -> None:
    curated = (SKILL_DIR / "references" / "curated-online-prompts.md").read_text(encoding="utf-8")
    for required in [
        "Anthropic Prompt Engineering Docs",
        "OpenAI Prompt Engineering Guide",
        "GitHub Copilot Prompt Engineering",
        "Cursor Rules Docs",
        "Aider Usage Tips",
        "SWE-bench",
        "Terminal-Bench",
        "HumanEval",
        "Codebase Analysis Goal",
        "Codebase Modernization Loop",
        "Plan-Spec Only",
    ]:
        assert required in curated
    assert curated.count("## Prompt Card:") >= 8
    assert curated.count("Stand-Datum: 2026-06-11") >= 8

    eval_definition = (SKILL_DIR / "references" / "eval-definition.md").read_text(encoding="utf-8")
    for required in ["Eval Card", "Score Rubric", "Eval-Level", "Task Success", "Judge Friendliness"]:
        assert required in eval_definition

    harness = (SKILL_DIR / "references" / "harness.md").read_text(encoding="utf-8")
    for required in ["Aufnahmekriterien", "Static Check", "Negative Tests", "Sandbox-Fixtures"]:
        assert required in harness


def test_workflow_library_docs_explain_goal_and_loop_scope() -> None:
    doc = DOC_PATH.read_text(encoding="utf-8")
    assert "Persistent Goals" in doc
    assert "/goal" in doc
    assert "/loop" in doc
    assert "kein eigener Slash Command" in doc
    assert "Goal-Prompt-Generator" in doc
    assert "Loop-Prompt-Generator" in doc
    assert "Plan-Spec-Generator" in doc
    assert "Eval-Level" in doc
