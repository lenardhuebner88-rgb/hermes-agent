from __future__ import annotations

from pathlib import Path

from agent.skill_preprocessing import (
    expand_inline_shell,
    load_skills_config,
    preprocess_skill_content,
    run_inline_shell,
    substitute_template_vars,
)


class TestLoadSkillsConfig:
    def test_returns_skills_section_when_configured(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda: {"skills": {"template_vars": False}},
        )

        assert load_skills_config() == {"template_vars": False}

    def test_returns_empty_dict_for_missing_or_invalid_section(self, monkeypatch):
        monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"skills": []})

        assert load_skills_config() == {}

    def test_returns_empty_dict_when_config_load_fails(self, monkeypatch):
        def fail():
            raise RuntimeError("no config")

        monkeypatch.setattr("hermes_cli.config.load_config", fail)

        assert load_skills_config() == {}


class TestSubstituteTemplateVars:
    def test_replaces_available_supported_tokens(self, tmp_path):
        content = "Use ${HERMES_SKILL_DIR}; session=${HERMES_SESSION_ID}"

        result = substitute_template_vars(content, tmp_path, "session-123")

        assert result == f"Use {tmp_path}; session=session-123"

    def test_leaves_unavailable_supported_tokens_in_place(self, tmp_path):
        content = "Use ${HERMES_SKILL_DIR}; session=${HERMES_SESSION_ID}"

        result = substitute_template_vars(content, None, None)

        assert result == content

    def test_leaves_unknown_hermes_tokens_in_place(self, tmp_path):
        content = "${HERMES_UNKNOWN} ${HERMES_SKILL_DIR}"

        result = substitute_template_vars(content, tmp_path, None)

        assert result == f"${{HERMES_UNKNOWN}} {tmp_path}"

    def test_returns_same_plain_content_object_without_template_scan(self, tmp_path):
        content = "plain skill body with no template tokens"

        result = substitute_template_vars(content, tmp_path, "session-123")

        assert result is content


class TestRunInlineShell:
    def test_returns_trimmed_stdout(self, tmp_path):
        result = run_inline_shell("printf 'hello\\n'", tmp_path, 5)

        assert result == "hello"

    def test_uses_stderr_when_stdout_is_empty(self, tmp_path):
        result = run_inline_shell("printf 'problem\\n' >&2", tmp_path, 5)

        assert result == "problem"

    def test_runs_with_skill_directory_as_cwd(self, tmp_path):
        (tmp_path / "name.txt").write_text("skill-dir", encoding="utf-8")

        result = run_inline_shell("cat name.txt", tmp_path, 5)

        assert result == "skill-dir"

    def test_timeout_returns_error_marker(self, tmp_path):
        result = run_inline_shell("sleep 2", tmp_path, 1)

        assert result == "[inline-shell timeout after 1s: sleep 2]"

    def test_invalid_timeout_returns_error_marker(self, tmp_path):
        result = run_inline_shell("printf ok", tmp_path, "nope")  # type: ignore[arg-type]

        assert result == "[inline-shell error: invalid timeout: nope]"

    def test_missing_cwd_returns_specific_error_marker(self, tmp_path):
        missing = tmp_path / "missing"

        result = run_inline_shell("printf ok", missing, 5)

        assert result == f"[inline-shell error: cwd not found: {missing}]"

    def test_truncates_large_output(self, tmp_path):
        result = run_inline_shell("python3 - <<'PY'\nprint('x' * 5000)\nPY", tmp_path, 5)

        assert len(result) < 5000
        assert result.endswith("...[truncated]")


class TestExpandInlineShell:
    def test_returns_same_content_when_no_inline_shell_marker(self, tmp_path):
        content = "static skill body"

        result = expand_inline_shell(content, tmp_path, 5)

        assert result is content

    def test_replaces_multiple_inline_shell_snippets(self, tmp_path):
        content = "A=!`printf one` B=!`printf two`"

        result = expand_inline_shell(content, tmp_path, 5)

        assert result == "A=one B=two"

    def test_empty_inline_shell_snippet_collapses_to_empty_string(self, tmp_path):
        content = "before !`   ` after"

        result = expand_inline_shell(content, tmp_path, 5)

        assert result == "before  after"

    def test_does_not_match_multiline_shell_snippets(self, tmp_path):
        content = "before !`printf one\nprintf two` after"

        result = expand_inline_shell(content, tmp_path, 5)

        assert result == content


class TestPreprocessSkillContent:
    def test_empty_content_returns_unchanged(self, tmp_path):
        assert preprocess_skill_content("", tmp_path, "sid", {}) == ""

    def test_applies_template_vars_and_inline_shell_when_enabled(self, tmp_path):
        content = "${HERMES_SKILL_DIR} !`printf ok` ${HERMES_SESSION_ID}"
        cfg = {"template_vars": True, "inline_shell": True, "inline_shell_timeout": 5}

        result = preprocess_skill_content(content, tmp_path, "sid", cfg)

        assert result == f"{tmp_path} ok sid"

    def test_respects_disabled_template_vars_and_inline_shell(self, tmp_path):
        content = "${HERMES_SKILL_DIR} !`printf SHOULD_NOT_RUN`"
        cfg = {"template_vars": False, "inline_shell": False}

        result = preprocess_skill_content(content, tmp_path, "sid", cfg)

        assert result == content

    def test_invalid_inline_shell_timeout_falls_back_to_default(self, tmp_path):
        content = "!`printf ok`"
        cfg = {"template_vars": True, "inline_shell": True, "inline_shell_timeout": "bad"}

        result = preprocess_skill_content(content, tmp_path, "sid", cfg)

        assert result == "ok"

    def test_loads_config_when_not_supplied(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "agent.skill_preprocessing.load_skills_config",
            lambda: {"template_vars": True, "inline_shell": False},
        )

        result = preprocess_skill_content("${HERMES_SESSION_ID}", tmp_path, "sid")

        assert result == "sid"
