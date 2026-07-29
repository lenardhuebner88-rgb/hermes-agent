"""Tests für loops.engines — Registry, Usage-Limit-Erkennung, Claude-CLI-Adapter."""

from __future__ import annotations

import subprocess
import json
from pathlib import Path
from urllib.parse import quote

import pytest
import yaml

from loops import engines
from loops.engines import claude_cli, codex_cli, kimi_cli


def test_registry_contains_claude_and_rejects_unknown():
    assert "claude" in engines.ENGINES
    assert "hermes" in engines.ENGINES
    assert "neuralwatt" in engines.ENGINES
    assert "xai" in engines.ENGINES
    with pytest.raises(KeyError, match="warpantrieb"):
        engines.get_engine("warpantrieb")


def test_hermes_profile_builds_oneshot_command_with_sandbox(monkeypatch, tmp_path):
    from loops.engines import hermes_profile

    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["cwd"] = kwargs.get("cwd")
        seen["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(cmd, 0, stdout="Antwort", stderr="")

    monkeypatch.setattr(hermes_profile.subprocess, "run", fake_run)
    result = hermes_profile.run("reviewer", "sag OK", tmp_path, 60)
    assert result.rc == 0 and result.output == "Antwort"
    cmd = seen["cmd"]
    assert cmd[0].endswith("hermes")
    assert cmd[cmd.index("-p") + 1] == "reviewer"  # "model" = Hermes-PROFIL
    assert cmd[cmd.index("-z") + 1] == "sag OK"
    assert seen["cwd"] == str(tmp_path)
    # kanban.db ist bewusst profil-übergreifend → Sandbox-Mode ist Pflicht
    assert seen["env"]["HERMES_SANDBOX_MODE"] == "1"


def test_hermes_profile_codex_quota_wortlaut_is_usage_limit(monkeypatch, tmp_path):
    from loops.engines import hermes_profile

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 1, stdout="",
            stderr="hermes -z: agent failed: Codex provider quota exhausted (429); retry after 1200s.",
        )

    monkeypatch.setattr(hermes_profile.subprocess, "run", fake_run)
    result = hermes_profile.run("coder", "x", tmp_path, 60)
    assert result.usage_limit is True


def test_hermes_profile_timeout_maps_to_timed_out(monkeypatch, tmp_path):
    from loops.engines import hermes_profile

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 60, output=b"teil", stderr=None)

    monkeypatch.setattr(hermes_profile.subprocess, "run", fake_run)
    result = hermes_profile.run("reviewer", "x", tmp_path, 60)
    assert result.timed_out is True and result.rc == 124


def test_neuralwatt_cli_builds_oneshot_command_with_sandbox(monkeypatch, tmp_path):
    from loops.engines import neuralwatt_cli

    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["cwd"] = kwargs.get("cwd")
        seen["env"] = kwargs.get("env")
        seen["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(cmd, 0, stdout="Antwort", stderr="")

    monkeypatch.setattr(neuralwatt_cli.subprocess, "run", fake_run)
    result = neuralwatt_cli.run("glm-5.2", "sag OK", tmp_path, 60)
    assert result.rc == 0 and result.output == "Antwort"
    cmd = seen["cmd"]
    assert cmd[0].endswith("hermes")
    assert cmd == [cmd[0], "-m", "glm-5.2", "--provider", "neuralwatt", "-z", "sag OK"]
    assert seen["cwd"] == str(tmp_path)
    assert seen["timeout"] == 60
    # kanban.db ist bewusst profil-übergreifend → Sandbox-Mode ist Pflicht
    assert seen["env"]["HERMES_SANDBOX_MODE"] == "1"


def test_neuralwatt_cli_timeout_maps_to_timed_out(monkeypatch, tmp_path):
    from loops.engines import neuralwatt_cli

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 60, output=b"teil", stderr=None)

    monkeypatch.setattr(neuralwatt_cli.subprocess, "run", fake_run)
    result = neuralwatt_cli.run("glm-5.2", "x", tmp_path, 60)
    assert result.timed_out is True and result.rc == 124


def test_neuralwatt_cli_flags_usage_limit_output(monkeypatch, tmp_path):
    from loops.engines import neuralwatt_cli

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 1, stdout="",
            stderr="hermes -z: agent failed: HTTP 429 rate_limit_exceeded",
        )

    monkeypatch.setattr(neuralwatt_cli.subprocess, "run", fake_run)
    result = neuralwatt_cli.run("kimi-k2.7-code", "x", tmp_path, 60)
    assert result.usage_limit is True


def test_xai_cli_maps_grok_45_to_official_grok_build_subscription_slot(
    monkeypatch, tmp_path
):
    from loops.engines import xai_cli

    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["cwd"] = kwargs.get("cwd")
        seen["env"] = kwargs.get("env")
        seen["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(cmd, 0, stdout="Grok answer", stderr="")

    monkeypatch.setattr(xai_cli.subprocess, "run", fake_run)
    result = xai_cli.run("grok-4.5", "build it", tmp_path, 321)

    assert result.rc == 0 and result.output == "Grok answer"
    assert seen["cmd"] == [
        xai_cli.GROK_BIN,
        "--no-memory",
        "--no-subagents",
        "--disable-web-search",
        "--always-approve",
        "--model",
        "grok-4.5",
        "--single",
        "build it",
        "--output-format",
        "plain",
    ]
    assert seen["env"]["HERMES_SANDBOX_MODE"] == "1"
    assert seen["cwd"] == str(tmp_path)
    assert seen["timeout"] == 321


def test_xai_cli_timeout_maps_to_124_and_merges_output(monkeypatch, tmp_path):
    from loops.engines import xai_cli

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"], output=b"partial out", stderr=b"partial err")

    monkeypatch.setattr(xai_cli.subprocess, "run", fake_run)
    result = xai_cli.run("grok-4.5", "x", tmp_path, 60)

    assert result == engines.EngineResult(
        rc=124,
        output="partial outpartial err",
        usage_limit=False,
        timed_out=True,
    )


def test_xai_cli_uses_shared_usage_limit_detection(monkeypatch, tmp_path):
    from loops.engines import xai_cli

    monkeypatch.setattr(
        xai_cli.subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(
            cmd, 1, stdout="", stderr="HTTP 429 rate_limit_exceeded"
        ),
    )

    result = xai_cli.run("grok-4.5", "x", tmp_path, 60)
    assert result.usage_limit is True


def test_xai_cli_captures_real_session_token_usage_without_changing_command(monkeypatch, tmp_path):
    from loops.engines import xai_cli

    grok_home = tmp_path / "grok-home"
    monkeypatch.setattr(xai_cli, "GROK_HOME", grok_home, raising=False)
    sid = "019f589b-56b7-7362-a20d-0ea300e5bef9"
    session = grok_home / "sessions" / quote(str(tmp_path), safe="") / sid

    def fake_run(cmd, **kwargs):
        session.mkdir(parents=True)
        log = grok_home / "logs" / "unified.jsonl"
        log.parent.mkdir(parents=True)
        rows = [
            {"sid": sid, "msg": "shell.turn.inference_done", "ctx": {"prompt_tokens": 100, "cached_prompt_tokens": 80, "completion_tokens": 20, "reasoning_tokens": 15}},
            {"sid": sid, "msg": "shell.turn.inference_done", "ctx": {"prompt_tokens": 120, "cached_prompt_tokens": 100, "completion_tokens": 30, "reasoning_tokens": 25}},
        ]
        log.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="Grok answer", stderr="")

    monkeypatch.setattr(xai_cli.subprocess, "run", fake_run)
    result = xai_cli.run("grok-4.5", "build it", tmp_path, 321)

    assert result.input_tokens == 220
    assert result.cached_input_tokens == 180
    assert result.output_tokens == 50
    assert result.reasoning_tokens == 40
    assert result.total_tokens == 270
    assert result.provenance_path == str(session / "updates.jsonl")


@pytest.mark.parametrize(
    "text",
    [
        "You've hit your session limit · resets 9:50pm (Europe/Berlin)",
        "You have reached your usage limit",
        "API error: 429 Too Many Requests",
        "rate limit exceeded, retry later",
        "You've hit your usage limit",
    ],
)
def test_usage_limit_detected(text):
    assert engines.detect_usage_limit(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "Alles gut, 12 Tests grün",
        "limit_order.py angepasst",  # 'limit' allein reicht nicht
        "",
        # Codex-CLI-Footer: "140,429" matchte \b429\b (Komma = Wortgrenze) —
        # klassifizierte ein echtes Verifier-FAIL als usage-limit (2026-07-05).
        "hook: Stop\nhook: Stop Completed\ntokens used\n140,429",
        "FAIL holds ignore dispatcher config\ntokens used\n96,429",
    ],
)
def test_usage_limit_not_overtriggered(text):
    assert engines.detect_usage_limit(text) is False


def test_usage_limit_ignores_phantom_matches_outside_tail():
    # Real 2026-07-05 night run: a 69k-line codex build output contained the
    # agent's own test string and grep-style line refs in the MIDDLE, but a
    # clean tail — must not be flagged as a real usage-limit hit.
    middle = (
        '("quota 429 from provider", guarded)\n'
        "tests/hermes_cli/test_kanban_cli.py:429:    kc.build_parser(top)\n"
    )
    padding = "x" * 5000
    text = padding + middle + padding + "\nAlles gut, 12 Tests grün.\n"
    assert len(text) > 4000
    assert engines.detect_usage_limit(text) is False


def test_usage_limit_detects_real_message_in_tail():
    padding = "x" * 5000
    text = padding + "You've hit your session limit · resets 9:50pm"
    assert engines.detect_usage_limit(text) is True


def test_claude_cli_builds_headless_command(monkeypatch, tmp_path):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["cwd"] = kwargs.get("cwd")
        return subprocess.CompletedProcess(cmd, 0, stdout="OK", stderr="")

    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)
    result = claude_cli.run("claude-fable-5", "sag OK", tmp_path, 60)
    assert result.rc == 0 and result.output == "OK" and result.usage_limit is False
    assert seen["cwd"] == str(tmp_path)
    cmd = seen["cmd"]
    assert cmd[0].endswith("claude")
    assert "-p" in cmd
    assert cmd[cmd.index("--model") + 1] == "claude-fable-5"
    assert cmd[cmd.index("--permission-mode") + 1] == "bypassPermissions"
    assert cmd[-1] == "sag OK"


def test_claude_cli_timeout_maps_to_timed_out(monkeypatch, tmp_path):
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 60, output=b"teil", stderr=b"")

    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)
    result = claude_cli.run("claude-fable-5", "x", tmp_path, 60)
    assert result.timed_out is True and result.rc == 124
    assert "teil" in result.output


def test_claude_cli_flags_usage_limit_output(monkeypatch, tmp_path):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 1, stdout="You've hit your session limit · resets 9:50pm", stderr=""
        )

    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)
    result = claude_cli.run("claude-sonnet-5", "x", tmp_path, 60)
    assert result.usage_limit is True


def test_registry_contains_kimi_and_codex():
    assert "kimi" in engines.ENGINES
    assert "codex" in engines.ENGINES


def test_kimi_cli_builds_headless_command(monkeypatch, tmp_path):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["cwd"] = kwargs.get("cwd")
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, 0, stdout="OK", stderr="")

    monkeypatch.setattr(kimi_cli.subprocess, "run", fake_run)
    result = kimi_cli.run("kimi-code/kimi-for-coding", "sag OK", tmp_path, 60)
    assert result.rc == 0 and result.output == "OK" and result.usage_limit is False
    assert seen["cwd"] == str(tmp_path)
    assert "shell" not in seen["kwargs"]
    cmd = seen["cmd"]
    assert cmd[0].endswith("kimi")
    assert cmd[cmd.index("--model") + 1] == "kimi-code/kimi-for-coding"
    assert "-p" in cmd
    assert "--yolo" not in cmd
    assert "--auto" not in cmd
    assert cmd[-1] == "sag OK"


def test_kimi_cli_maps_catalog_k3_to_managed_oauth_alias(monkeypatch, tmp_path):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="OK", stderr="")

    monkeypatch.setattr(kimi_cli.subprocess, "run", fake_run)
    result = kimi_cli.run("k3", "sag OK", tmp_path, 60)

    assert result.rc == 0
    assert seen["cmd"][seen["cmd"].index("--model") + 1] == "kimi-code/k3"


def test_kimi_cli_timeout_maps_to_timed_out(monkeypatch, tmp_path):
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 60, output=b"teil", stderr=b"")

    monkeypatch.setattr(kimi_cli.subprocess, "run", fake_run)
    result = kimi_cli.run("kimi-code/kimi-for-coding", "x", tmp_path, 60)
    assert result.timed_out is True and result.rc == 124
    assert "teil" in result.output


def test_kimi_cli_flags_provider_rate_limit_output(monkeypatch, tmp_path):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 1, stdout='error code="provider.rate_limit" — retry later', stderr=""
        )

    monkeypatch.setattr(kimi_cli.subprocess, "run", fake_run)
    result = kimi_cli.run("kimi-code/kimi-for-coding", "x", tmp_path, 60)
    assert result.usage_limit is True


def test_kimi_cli_flags_generic_usage_limit_output(monkeypatch, tmp_path):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 1, stdout="rate limit exceeded, retry later", stderr=""
        )

    monkeypatch.setattr(kimi_cli.subprocess, "run", fake_run)
    result = kimi_cli.run("kimi-code/kimi-for-coding", "x", tmp_path, 60)
    assert result.usage_limit is True


def test_codex_cli_builds_headless_command(monkeypatch, tmp_path):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["cwd"] = kwargs.get("cwd")
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, 0, stdout="OK", stderr="")

    monkeypatch.setattr(codex_cli.subprocess, "run", fake_run)
    result = codex_cli.run("gpt-5.5", "sag OK", tmp_path, 60)
    assert result.rc == 0 and result.output == "OK" and result.usage_limit is False
    assert seen["cwd"] == str(tmp_path)
    assert "shell" not in seen["kwargs"]
    cmd = seen["cmd"]
    assert cmd[0].endswith("codex")
    assert cmd[1] == "exec"
    assert cmd[cmd.index("--model") + 1] == "gpt-5.5"
    assert cmd[cmd.index("--sandbox") + 1] == "danger-full-access"
    assert "--full-auto" not in cmd
    assert cmd[-1] == "sag OK"


def test_codex_cli_parses_total_token_footer(monkeypatch, tmp_path):
    monkeypatch.setattr(
        codex_cli.subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(
            cmd, 0, stdout="OK\ntokens used\n27,684\n", stderr="",
        ),
    )
    result = codex_cli.run("gpt-5.6-sol", "x", tmp_path, 60)
    assert result.total_tokens == 27_684


def test_codex_cli_timeout_maps_to_timed_out(monkeypatch, tmp_path):
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 60, output=b"teil", stderr=b"")

    monkeypatch.setattr(codex_cli.subprocess, "run", fake_run)
    result = codex_cli.run("gpt-5.5", "x", tmp_path, 60)
    assert result.timed_out is True and result.rc == 124
    assert "teil" in result.output


def test_codex_cli_flags_usage_limit_output(monkeypatch, tmp_path):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 1, stdout="API error: 429 Too Many Requests", stderr=""
        )

    monkeypatch.setattr(codex_cli.subprocess, "run", fake_run)
    result = codex_cli.run("gpt-5.5", "x", tmp_path, 60)
    assert result.usage_limit is True


def test_models_yaml_loads_and_registered_engines_have_adapter():
    models_path = Path(__file__).resolve().parents[2] / "loops" / "models.yaml"
    catalog = yaml.safe_load(models_path.read_text(encoding="utf-8"))
    assert "engines" in catalog

    # neuralwatt hat seit der neuralwatt_cli-Engine einen echten Adapter
    assert "neuralwatt" in catalog["engines"]
    assert catalog["engines"]["neuralwatt"]["models"]
    assert "neuralwatt" in engines.ENGINES

    for name, spec in catalog["engines"].items():
        assert "label" in spec
        assert "models" in spec
        if spec["models"]:
            assert name in engines.ENGINES, (
                f"Katalog-Engine {name!r} hat Modelle, aber keinen registrierten Adapter"
            )


# ── Hermes-Engine: Binärpfad-Auflösung ───────────────────────────────────────

def test_resolve_hermes_bin_prefers_env_var(monkeypatch, tmp_path):
    from loops.engines import hermes_profile

    explicit = str(tmp_path / "my-hermes")
    monkeypatch.setenv("HERMES_BIN", explicit)
    assert hermes_profile._resolve_hermes_bin() == explicit


def test_resolve_hermes_bin_falls_back_to_repo_venv(monkeypatch, tmp_path):
    from loops.engines import hermes_profile

    monkeypatch.delenv("HERMES_BIN", raising=False)
    # Wir simulieren, dass weder HERMES_BIN noch ein 'hermes' auf PATH existiert.
    monkeypatch.setattr(hermes_profile.shutil, "which", lambda _name: None)

    fake_venv_bin = tmp_path / "venv" / "bin" / "hermes"
    fake_venv_bin.parent.mkdir(parents=True)
    fake_venv_bin.write_text("#!/bin/sh\necho fake", encoding="utf-8")
    fake_venv_bin.chmod(0o755)

    real_repo_root = hermes_profile.REPO_ROOT
    try:
        hermes_profile.REPO_ROOT = tmp_path
        assert hermes_profile._resolve_hermes_bin() == str(fake_venv_bin)
    finally:
        hermes_profile.REPO_ROOT = real_repo_root


def test_resolve_hermes_bin_falls_back_to_bare_name(monkeypatch):
    from loops.engines import hermes_profile

    monkeypatch.delenv("HERMES_BIN", raising=False)
    monkeypatch.setattr(hermes_profile.shutil, "which", lambda _name: None)
    real_repo_root = hermes_profile.REPO_ROOT
    try:
        hermes_profile.REPO_ROOT = Path("/nonexistent/repo")
        assert hermes_profile._resolve_hermes_bin() == "hermes"
    finally:
        hermes_profile.REPO_ROOT = real_repo_root


def test_alibaba_token_plan_engine_registered_and_command_shape(tmp_path, monkeypatch):
    from loops.engines import alibaba_token_plan_cli

    assert "alibaba-token-plan" in engines.ENGINES
    assert engines.ENGINES["alibaba-token-plan"] is alibaba_token_plan_cli.run

    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(kwargs.get("env") or {})
        captured["cwd"] = kwargs.get("cwd")
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(alibaba_token_plan_cli.subprocess, "run", fake_run)
    monkeypatch.setattr(alibaba_token_plan_cli, "QWEN_BIN", "/opt/qwen-bin")
    wt = tmp_path / "wt"
    wt.mkdir()
    result = alibaba_token_plan_cli.run(
        "qwen3.8-max-preview", "do work", wt, 90
    )

    assert result.rc == 0
    assert captured["cmd"] == [
        "/opt/qwen-bin",
        "-m",
        "qwen3.8-max-preview",
        "--safe-mode",
        "--sandbox",
        "-y",
        "--output-format",
        "text",
        "-p",
        "do work",
    ]
    assert captured["env"].get("HERMES_SANDBOX_MODE") == "1"
    assert captured["cwd"] == str(wt)


def test_alibaba_token_plan_yolo_flag_enables_tools_non_interactively(
    tmp_path, monkeypatch
):
    """Ohne -y verweigert die Qwen-CLI JEDES Tool im nicht-interaktiven Modus.

    Belegt am Nachtlauf 2026-07-29 (dashboard-experience): der Builder lief 270s,
    schrieb 0 Zeilen und die CLI meldete je Tool woertlich "requires user approval
    but cannot execute in non-interactive mode ... use the -y flag (YOLO mode)".
    Der Loop wertete das als build-fail ohne Grund, weil last-status leer blieb.
    """
    from loops.engines import alibaba_token_plan_cli

    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(alibaba_token_plan_cli.subprocess, "run", fake_run)
    wt = tmp_path / "wt"
    wt.mkdir()
    alibaba_token_plan_cli.run("qwen3.8-max-preview", "build it", wt, 90)

    assert "-y" in captured["cmd"], (
        "ohne -y kann der Builder im Loop kein einziges Tool aufrufen"
    )


def test_alibaba_token_plan_mounts_loop_state_dir_into_sandbox(tmp_path, monkeypatch):
    """Der Builder-Vertrag verlangt Schreibzugriff AUSSERHALB des Worktrees.

    Der Runner haelt wt = state/"wt" und status_path = state/"last-status". Die
    Docker-Sandbox mountet per Default nur cwd, also den Worktree — Plan-Datei
    (queue/10-building/), before_evidence und last-status liegen daneben und sind
    unerreichbar. Ohne diesen Mount kann der Builder den Vertrag nicht erfuellen,
    selbst wenn er alle Tools haette.
    """
    from loops.engines import alibaba_token_plan_cli

    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["env"] = dict(kwargs.get("env") or {})
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(alibaba_token_plan_cli.subprocess, "run", fake_run)
    monkeypatch.delenv("SANDBOX_MOUNTS", raising=False)
    state = tmp_path / "dashboard-experience"
    wt = state / "wt"
    wt.mkdir(parents=True)

    alibaba_token_plan_cli.run("qwen3.8-max-preview", "build it", wt, 90)

    mounts = captured["env"].get("SANDBOX_MOUNTS", "")
    assert f"{state}:{state}:rw" in mounts, (
        f"Loop-State {state} muss rw in die Sandbox gemountet sein, ist: {mounts!r}"
    )


def test_alibaba_token_plan_keeps_existing_sandbox_mounts(tmp_path, monkeypatch):
    """Ein vom Operator gesetzter Mount darf nicht still ueberschrieben werden."""
    from loops.engines import alibaba_token_plan_cli

    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["env"] = dict(kwargs.get("env") or {})
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(alibaba_token_plan_cli.subprocess, "run", fake_run)
    monkeypatch.setenv("SANDBOX_MOUNTS", "/opt/data:/opt/data:ro")
    state = tmp_path / "pack"
    wt = state / "wt"
    wt.mkdir(parents=True)

    alibaba_token_plan_cli.run("qwen3.8-max-preview", "build it", wt, 90)

    mounts = captured["env"]["SANDBOX_MOUNTS"]
    assert "/opt/data:/opt/data:ro" in mounts
    assert f"{state}:{state}:rw" in mounts


def test_alibaba_token_plan_skips_mount_when_parent_missing(tmp_path, monkeypatch):
    """Fail-soft: ist cwd kein <state>/wt, wird nichts Fremdes gemountet."""
    from loops.engines import alibaba_token_plan_cli

    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["env"] = dict(kwargs.get("env") or {})
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(alibaba_token_plan_cli.subprocess, "run", fake_run)
    monkeypatch.delenv("SANDBOX_MOUNTS", raising=False)
    lone = tmp_path / "not-a-worktree"
    lone.mkdir()

    alibaba_token_plan_cli.run("qwen3.8-max-preview", "build it", lone, 90)

    assert captured["env"].get("SANDBOX_MOUNTS", "") == ""


def test_models_yaml_lists_kimi_k3_and_alibaba_qwen():
    import yaml
    from loops.runner import MODELS_FILE

    data = yaml.safe_load(MODELS_FILE.read_text(encoding="utf-8"))
    engines_cat = data["engines"]
    assert "k3" in engines_cat["kimi"]["models"]
    assert engines_cat["alibaba-token-plan"]["models"] == ["qwen3.8-max-preview"]
