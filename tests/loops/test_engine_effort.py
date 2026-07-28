"""Reasoning-Effort im Loop-Runner: Transport, fail-closed, Präzedenz.

Vor 2026-07-28 endete der Engine-Contract bei ``run(model, prompt, cwd,
timeout_s)`` — der Loop-Runner konnte keinen Effort setzen. Für Codex war das
besonders teuer: ``~/.codex/config.toml`` setzt global
``model_reasoning_effort = "xhigh"``, d.h. JEDE Codex-Phase lief auf xhigh,
egal was Pack oder Dashboard anzeigten.

Diese Tests prüfen die Kette an der Stelle, wo sie zählt: am tatsächlichen
Prozess-Kommando. Ein Test, der nur ``PhaseCfg.effort`` liest, hätte den alten
Bug NICHT gefunden — der Wert stand da, er kam nur nie am CLI an.
"""

from __future__ import annotations

import subprocess
import tempfile
import textwrap
from pathlib import Path
from unittest import mock

import pytest

from loops import engines
from loops import runner as runner_module
from loops.runner import ManifestError, load_pack


class _FakeProc:
    returncode = 0
    stdout = "ok"
    stderr = ""


def _capture_cmd(engine: str, *args, **kwargs) -> list[str]:
    """Ruft eine echte Engine auf und gibt ihr Prozess-Kommando zurück."""
    captured: list[list[str]] = []

    def fake_run(cmd, **_kw):
        captured.append(list(cmd))
        return _FakeProc()

    with mock.patch.object(subprocess, "run", side_effect=fake_run):
        engines.get_engine(engine)(*args, **kwargs)
    assert len(captured) == 1, f"erwartet genau einen Prozessaufruf, war {len(captured)}"
    return captured[0]


# --- Transport: kommt der Effort am CLI an? ------------------------------


def test_claude_engine_passes_effort_as_effort_flag():
    cmd = _capture_cmd("claude", "claude-opus-5", "P", Path("/tmp"), 60, "high")
    assert "--effort" in cmd
    assert cmd[cmd.index("--effort") + 1] == "high"


def test_codex_engine_passes_effort_as_config_override():
    """Codex hat KEIN --effort; der Weg ist `-c model_reasoning_effort=<l>`.

    Das ist zugleich der Override gegen die globale xhigh-Host-Config.
    """
    cmd = _capture_cmd("codex", "gpt-5.6-sol", "P", Path("/tmp"), 60, "medium")
    assert "-c" in cmd
    assert cmd[cmd.index("-c") + 1] == "model_reasoning_effort=medium"


def test_xai_engine_passes_effort_as_reasoning_effort_flag():
    cmd = _capture_cmd("xai", "grok-4.5", "P", Path("/tmp"), 60, "low")
    assert "--reasoning-effort" in cmd
    assert cmd[cmd.index("--reasoning-effort") + 1] == "low"


@pytest.mark.parametrize(
    ("engine", "model"),
    [("claude", "claude-opus-5"), ("codex", "gpt-5.6-sol"), ("xai", "grok-4.5")],
)
def test_no_effort_means_no_flag(engine: str, model: str):
    """Ohne Effort bleibt das Flag weg — der CLI-Default gilt unverändert."""
    cmd = " ".join(_capture_cmd(engine, model, "P", Path("/tmp"), 60, None))
    assert "--effort" not in cmd
    assert "model_reasoning_effort" not in cmd
    assert "--reasoning-effort" not in cmd


def test_every_registered_engine_accepts_the_effort_parameter():
    """Contract-Wache: eine Engine ohne ``effort``-Parameter würde erst zur
    Laufzeit mit TypeError sterben — mitten in einem Nachtlauf."""
    import inspect

    for name, fn in engines.ENGINES.items():
        params = inspect.signature(fn).parameters
        assert "effort" in params, f"Engine {name!r} hat keinen effort-Parameter"


# --- fail-closed: ungültige Kombinationen ---------------------------------


def test_validate_effort_rejects_unknown_level():
    with pytest.raises(engines.EffortUnsupported, match="turbo"):
        engines.validate_effort("claude", "turbo")


def test_validate_effort_rejects_effort_on_engine_without_transport():
    """Ein stiller Drop wäre der Fehlermodus, den diese Kette beseitigt."""
    with pytest.raises(engines.EffortUnsupported, match="transportiert keinen"):
        engines.validate_effort("kimi", "high")


def test_validate_effort_normalises_case_and_blank():
    assert engines.validate_effort("codex", "  XHIGH ") == "xhigh"
    assert engines.validate_effort("claude", "") is None
    assert engines.validate_effort("claude", None) is None


def test_engine_effort_sets_match_the_documented_cli_truth():
    """Die Sets stammen aus den CLIs selbst (siehe Modul-Kommentare). Ein
    stiller Drift hier macht das Dashboard-Angebot unwahr."""
    assert engines.effort_levels_for("claude") == ("low", "medium", "high", "xhigh", "max")
    assert engines.effort_levels_for("codex") == (
        "none", "minimal", "low", "medium", "high", "xhigh",
    )
    assert engines.effort_levels_for("xai") == ("low", "medium", "high")
    for without in ("kimi", "hermes", "neuralwatt", "alibaba-token-plan"):
        assert engines.effort_levels_for(without) == ()


def test_claude_effort_levels_come_from_the_spawn_constant():
    """EINE Quelle für Kanban-Spawn, Lanes-Tab und Loop-Runner."""
    from hermes_cli.kanban_worker_runtime import CLAUDE_CLI_EFFORT_LEVELS

    assert engines.effort_levels_for("claude") == tuple(CLAUDE_CLI_EFFORT_LEVELS)


# --- Manifest + Override-Präzedenz ----------------------------------------


def _write_pack(root: Path, name: str = "efforts", **phase_extra: str) -> Path:
    d = root / name
    d.mkdir(parents=True)
    for prompt in ("PLANNER-PROMPT.md", "BUILDER-PROMPT.md", "VERIFIER-PROMPT.md"):
        (d / prompt).write_text("x", encoding="utf-8")
    plan_effort = phase_extra.get("plan_effort", "high")
    build_effort = phase_extra.get("build_effort", "medium")
    (d / "pack.yaml").write_text(
        textwrap.dedent(f"""
            name: {name}
            type: pipeline
            repo: /home/piet/.hermes/hermes-agent
            phases:
              plan:
                engine: claude
                model: claude-opus-5
                timeout: 60
                prompt: PLANNER-PROMPT.md
                effort: {plan_effort}
              build:
                engine: codex
                model: gpt-5.6-sol
                timeout: 60
                prompt: BUILDER-PROMPT.md
                effort: {build_effort}
              verify:
                engine: claude
                model: claude-sonnet-5
                timeout: 60
                prompt: VERIFIER-PROMPT.md
        """),
        encoding="utf-8",
    )
    return d


def test_pack_manifest_carries_per_phase_effort(tmp_path):
    _write_pack(tmp_path)
    pack = load_pack(tmp_path, "efforts")
    assert pack.phases["plan"].effort == "high"
    assert pack.phases["build"].effort == "medium"
    assert pack.phases["verify"].effort is None


def test_manifest_rejects_unknown_effort(tmp_path):
    _write_pack(tmp_path, plan_effort="turbo")
    with pytest.raises(ManifestError, match="turbo"):
        load_pack(tmp_path, "efforts")


def test_effort_override_precedence_oneshot_beats_night_beats_pack(tmp_path):
    _write_pack(tmp_path)
    pack = load_pack(tmp_path, "efforts")
    state_root = tmp_path / "state"
    state = state_root / pack.name
    state.mkdir(parents=True)

    # nur pack.yaml
    assert runner_module.LoopRunner(pack, state_root=state_root).phase_cfg("plan").effort == "high"

    # Night schlägt pack.yaml
    (state / runner_module.NIGHT_OVERRIDES_FILENAME).write_text(
        "PHASE_PLAN_EFFORT=low\n", encoding="utf-8"
    )
    assert runner_module.LoopRunner(pack, state_root=state_root).phase_cfg("plan").effort == "low"

    # One-Shot schlägt Night
    (state / "overrides.env").write_text("PHASE_PLAN_EFFORT=max\n", encoding="utf-8")
    assert runner_module.LoopRunner(pack, state_root=state_root).phase_cfg("plan").effort == "max"


def test_engine_swap_drops_the_pack_effort_instead_of_carrying_it_over(tmp_path):
    """`claude: max` darf nach einem Swap auf xai nicht mitwandern — xai kennt
    kein max und der Lauf wäre sonst fail-closed tot statt einfach ohne Effort."""
    _write_pack(tmp_path, plan_effort="max")
    pack = load_pack(tmp_path, "efforts")
    state_root = tmp_path / "state"
    state = state_root / pack.name
    state.mkdir(parents=True)
    (state / "overrides.env").write_text("PHASE_PLAN_ENGINE=xai\n", encoding="utf-8")
    cfg = runner_module.LoopRunner(pack, state_root=state_root).phase_cfg("plan")
    assert cfg.engine == "xai"
    assert cfg.effort is None


def test_effort_override_is_validated_against_the_effective_engine(tmp_path):
    _write_pack(tmp_path)
    pack = load_pack(tmp_path, "efforts")
    state_root = tmp_path / "state"
    state = state_root / pack.name
    state.mkdir(parents=True)
    # xhigh gibt es bei claude, aber nicht bei xai.
    (state / "overrides.env").write_text(
        "PHASE_PLAN_ENGINE=xai\nPHASE_PLAN_EFFORT=xhigh\n", encoding="utf-8"
    )
    with pytest.raises(engines.EffortUnsupported, match="xai"):
        runner_module.LoopRunner(pack, state_root=state_root).phase_cfg("plan")


def test_runner_hands_the_effective_effort_to_the_engine(tmp_path):
    """Der Beweis über die ganze Kette: pack.yaml → Override → Engine-Argument."""
    _write_pack(tmp_path)
    pack = load_pack(tmp_path, "efforts")
    state_root = tmp_path / "state"
    state = state_root / pack.name
    state.mkdir(parents=True)
    (state / "overrides.env").write_text("PHASE_BUILD_EFFORT=minimal\n", encoding="utf-8")
    runner = runner_module.LoopRunner(pack, state_root=state_root)
    assert runner.phase_cfg("build").effort == "minimal"

    cmd = _capture_cmd(
        "codex",
        runner.phase_cfg("build").model,
        "P",
        Path(tempfile.mkdtemp()),
        60,
        runner.phase_cfg("build").effort,
    )
    assert "model_reasoning_effort=minimal" in cmd


# --- Autoland-Autorität bleibt effort-blind -------------------------------


def test_effort_does_not_move_the_autoland_safety_hash():
    """Effort ist Route, nicht Landungsautorität. Stünde er in der
    Sicherheitsprojektion, würde jede Effort-Wahl das Autoland eines
    kuratierten Packs abschießen."""
    base = {
        "name": "p", "type": "pipeline", "repo": "/r", "stability": "stable",
        "phases": {
            "plan": {"prompt": "PLANNER-PROMPT.md", "engine": "claude", "model": "m"},
            "build": {"prompt": "BUILDER-PROMPT.md", "engine": "codex", "model": "m"},
            "verify": {"prompt": "VERIFIER-PROMPT.md", "engine": "claude", "model": "m"},
        },
        "stop": {"max_rounds": 3},
        "params": {}, "notify": {}, "autoland": True,
    }
    with_effort = {
        **base,
        "phases": {
            phase: {**cfg, "effort": "high"} for phase, cfg in base["phases"].items()
        },
    }
    assert runner_module._autoland_safety_hash(base) == runner_module._autoland_safety_hash(
        with_effort
    )
