from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml

from hermes_cli import agent_questions as aq
from hermes_cli import pa_actions, pa_loops


@pytest.fixture
def isolated_loops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Path]:
    home = tmp_path / "home"
    hermes_home = home / ".hermes"
    repo_packs = tmp_path / "repo-packs"
    models = tmp_path / "models.yaml"
    repo_packs.mkdir(parents=True)
    hermes_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(pa_loops, "PACKS_DIR_OVERRIDE", repo_packs)
    monkeypatch.setattr(pa_loops, "MODELS_FILE_OVERRIDE", models)
    monkeypatch.setattr(pa_loops, "STATE_ROOT_OVERRIDE", hermes_home / "loops")
    models.write_text(
        yaml.safe_dump(
            {
                "engines": {
                    "claude": {"models": ["claude-opus-4-8"]},
                    "codex": {"models": ["gpt-5.6-sol"]},
                    "kimi": {"models": ["kimi-code/kimi-for-coding"]},
                }
            }
        ),
        encoding="utf-8",
    )
    return {
        "home": home,
        "hermes_home": hermes_home,
        "repo_packs": repo_packs,
        "custom_packs": hermes_home / "loops" / "packs-custom",
        "state": hermes_home / "loops",
        "models": models,
    }


def _write_pack(
    base: Path,
    name: str,
    *,
    phases: dict[str, tuple[str, str]] | None = None,
) -> None:
    phase_defs = phases or {"round": ("kimi", "kimi-code/kimi-for-coding")}
    pack_type = "pipeline" if set(phase_defs) == {"plan", "build", "verify"} else "sweep"
    pack_dir = base / name
    pack_dir.mkdir(parents=True)
    manifest_phases: dict[str, dict[str, Any]] = {}
    for phase, (engine, model) in phase_defs.items():
        prompt = f"{phase.upper()}-PROMPT.md"
        (pack_dir / prompt).write_text("test prompt\n", encoding="utf-8")
        manifest_phases[phase] = {
            "engine": engine,
            "model": model,
            "timeout": 60,
            "prompt": prompt,
        }
    (pack_dir / "pack.yaml").write_text(
        yaml.safe_dump(
            {
                "name": name,
                "type": pack_type,
                "repo": "/tmp/not-used-by-pa-loop-tests",
                "phases": manifest_phases,
                "stop": {"max_rounds": 4},
                "params": {},
                "notify": {},
                "autoland": False,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _pipeline_phases() -> dict[str, tuple[str, str]]:
    return {
        "plan": ("claude", "claude-opus-4-8"),
        "build": ("codex", "gpt-5.6-sol"),
        "verify": ("claude", "claude-opus-4-8"),
    }


def test_loops_payload_schemas_are_closed_and_typed() -> None:
    assert aq.normalize_pa_action_payload(
        "loops.start_pack",
        {
            "pack": "builder-reviewer",
            "model": "gpt-5.6-sol",
            "max_rounds": 3,
            "reason": "heute prüfen",
        },
    ) == {
        "pack": "builder-reviewer",
        "model": "gpt-5.6-sol",
        "max_rounds": 3,
        "reason": "heute prüfen",
    }
    assert aq.normalize_pa_action_payload("loops.status", {}) == {}
    assert aq.normalize_pa_action_payload("loops.status", {"pack": "nacht"}) == {
        "pack": "nacht"
    }

    for payload in (
        {"pack": "nacht", "unknown": "x"},
        {"pack": "nacht", "max_rounds": True},
        {"pack": "nacht", "max_rounds": "3"},
        {"pack": "nacht", "max_rounds": 0},
        {"pack": "nacht", "max_rounds": 51},
    ):
        with pytest.raises(ValueError):
            aq.normalize_pa_action_payload("loops.start_pack", payload)
    with pytest.raises(ValueError):
        aq.normalize_pa_action_payload("loops.status", {"reason": "not in schema"})


def test_pack_allowlist_accepts_repo_and_custom_but_not_state_only(
    isolated_loops: dict[str, Path],
) -> None:
    _write_pack(isolated_loops["repo_packs"], "repo-pack")
    _write_pack(isolated_loops["custom_packs"], "custom-pack")
    (isolated_loops["state"] / "state-only").mkdir(parents=True)

    assert pa_loops.resolve_pack("repo-pack").name == "repo-pack"
    assert pa_loops.resolve_pack("custom-pack").name == "custom-pack"
    assert pa_loops.known_pack_names() == ["custom-pack", "repo-pack"]
    with pytest.raises(pa_loops.LoopActionError, match="Unbekanntes Pack.*gültige Packs"):
        pa_loops.resolve_pack("state-only")


@pytest.mark.parametrize(
    "name",
    ["../repo-pack", "repo-pack/..", "/repo-pack", "Repo-Pack", "repo_pack", "-bad"],
)
def test_pack_allowlist_rejects_escape_and_noncanonical_names(
    isolated_loops: dict[str, Path], name: str
) -> None:
    _write_pack(isolated_loops["repo_packs"], "repo-pack")
    with pytest.raises(pa_loops.LoopActionError, match="Pack-Name ungültig.*repo-pack"):
        pa_loops.resolve_pack(name)


def test_model_catalog_maps_only_matching_pack_phases(
    isolated_loops: dict[str, Path],
) -> None:
    _write_pack(
        isolated_loops["repo_packs"],
        "pipeline",
        phases=_pipeline_phases(),
    )
    pack = pa_loops.resolve_pack("pipeline")
    assert pa_loops._start_overrides(pack, {"model": "gpt-5.6-sol"}) == [
        "PHASE_BUILD_MODEL=gpt-5.6-sol"
    ]
    assert pa_loops._start_overrides(pack, {"model": "claude-opus-4-8"}) == [
        "PHASE_PLAN_MODEL=claude-opus-4-8",
        "PHASE_VERIFY_MODEL=claude-opus-4-8",
    ]
    with pytest.raises(pa_loops.LoopActionError, match="Unbekanntes Loop-Modell"):
        pa_loops._start_overrides(pack, {"model": "kimi-for-coding"})
    with pytest.raises(pa_loops.LoopActionError, match="keiner Engine-Phase"):
        pa_loops._start_overrides(
            pack, {"model": "kimi-code/kimi-for-coding"}
        )


def test_start_builds_no_block_argv_and_exact_one_run_overrides(
    isolated_loops: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_pack(
        isolated_loops["repo_packs"],
        "pipeline",
        phases=_pipeline_phases(),
    )
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        stdout = "active\n" if "is-active" in argv else "queued\n"
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(pa_loops.subprocess, "run", fake_run)
    monkeypatch.setattr(pa_loops.time, "sleep", lambda _seconds: None)
    result = pa_loops.start_pack(
        {"pack": "pipeline", "model": "gpt-5.6-sol", "max_rounds": 3}
    )

    expected_argv = [
        "systemctl",
        "--user",
        "start",
        "--no-block",
        "hermes-loop@pipeline",
    ]
    expected_content = (
        "# geschrieben vom Jarvis PA Executor; gilt für genau einen Lauf\n"
        "PHASE_BUILD_MODEL=gpt-5.6-sol\n"
        "MAX_ROUNDS=3\n"
    )
    expected_kwargs = {
        "capture_output": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": 30,
        "check": False,
    }
    assert calls == [
        (["systemctl", "--user", "reset-failed", "hermes-loop@pipeline.service"], expected_kwargs),
        (expected_argv, expected_kwargs),
        (["systemctl", "--user", "is-active", "hermes-loop@pipeline.service"], expected_kwargs),
    ]
    assert all("env" not in kwargs for _argv, kwargs in calls)
    assert result == {
        "ok": True,
        "exit": 0,
        "pack": "pipeline",
        "argv": expected_argv,
        "systemctl_output": {
            "reset_failed": {
                "argv": calls[0][0],
                "exit": 0,
                "stdout": "queued\n",
                "stderr": "",
            },
            "start": {
                "argv": expected_argv,
                "exit": 0,
                "stdout": "queued\n",
                "stderr": "",
            },
            "is_active": {
                "argv": calls[2][0],
                "exit": 0,
                "stdout": "active\n",
                "stderr": "",
            },
        },
        "overrides": {
            "path": str(isolated_loops["state"] / "pipeline" / "overrides.env"),
            "content": expected_content,
        },
        "one_run": True,
    }
    assert (
        isolated_loops["state"] / "pipeline" / "overrides.env"
    ).read_text(encoding="utf-8") == expected_content


def test_invalid_model_and_systemctl_failure_do_not_retry(
    isolated_loops: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_pack(isolated_loops["repo_packs"], "nightly")
    calls = 0

    def fake_run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        if "reset-failed" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, 5, stdout="", stderr="unit failed")

    monkeypatch.setattr(pa_loops.subprocess, "run", fake_run)
    invalid = pa_loops.start_pack({"pack": "nightly", "model": "kimi-k2.7-code"})
    assert invalid["ok"] is False
    assert "Unbekanntes Loop-Modell" in invalid["error"]
    assert calls == 0
    assert not (isolated_loops["state"] / "nightly" / "overrides.env").exists()

    failed = pa_loops.start_pack({"pack": "nightly"})
    assert failed["ok"] is False
    assert failed["exit"] == 5
    assert "unit failed" in failed["error"]
    assert calls == 2


def test_running_pack_fails_before_overrides_or_systemctl(
    isolated_loops: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_pack(isolated_loops["repo_packs"], "nightly")
    state = isolated_loops["state"] / "nightly"
    state.mkdir(parents=True)
    lock_path = state / ".lock"
    with lock_path.open("w", encoding="utf-8") as handle:
        pa_loops.fcntl.flock(handle, pa_loops.fcntl.LOCK_EX | pa_loops.fcntl.LOCK_NB)
        monkeypatch.setattr(
            pa_loops.subprocess,
            "run",
            lambda *_args, **_kwargs: pytest.fail("systemctl must not run"),
        )
        result = pa_loops.start_pack({"pack": "nightly", "max_rounds": 2})

    assert result["ok"] is False
    assert "läuft bereits" in result["error"]
    assert not (state / "overrides.env").exists()


def test_fast_failed_unit_is_reported_as_failure_without_retry(
    isolated_loops: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_pack(isolated_loops["repo_packs"], "nightly")
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        stdout = "failed\n" if "is-active" in argv else ""
        returncode = 3 if "is-active" in argv else 0
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(pa_loops.subprocess, "run", fake_run)
    monkeypatch.setattr(pa_loops.time, "sleep", lambda _seconds: None)

    result = pa_loops.start_pack({"pack": "nightly"})

    assert result["ok"] is False
    assert "nicht angelaufen" in result["error"]
    assert len(calls) == 3


def test_status_returns_bounded_ledger_heartbeat_age_and_stop(
    isolated_loops: dict[str, Path],
) -> None:
    _write_pack(isolated_loops["repo_packs"], "nightly")
    state = isolated_loops["state"] / "nightly"
    state.mkdir(parents=True)
    (state / "LEDGER.md").write_text(
        "".join(f"line-{index}\n" for index in range(25)), encoding="utf-8"
    )
    started = datetime.now(timezone.utc) - timedelta(seconds=65)
    (state / "heartbeat.json").write_text(
        json.dumps(
            {
                "current": {
                    "phase": "round",
                    "engine": "kimi",
                    "model": "kimi-code/kimi-for-coding",
                    "started_at": started.isoformat().replace("+00:00", "Z"),
                },
                "last": [],
            }
        ),
        encoding="utf-8",
    )
    (state / "STOP").write_text("", encoding="utf-8")

    result = pa_loops.status({"pack": "nightly"})

    assert result["ok"] is True and result["exit"] == 0
    assert result["ledger_tail_limit"] == 20
    evidence = result["packs"][0]
    assert evidence["ledger"]["lines"] == [f"line-{index}" for index in range(5, 25)]
    assert evidence["heartbeat"]["phase"] == "round"
    assert evidence["heartbeat"]["active"] is True
    assert 60 <= evidence["heartbeat"]["age_seconds"] <= 70
    assert evidence["stop"]["exists"] is True


def test_status_without_pack_covers_all_known_packs_and_missing_state(
    isolated_loops: dict[str, Path],
) -> None:
    _write_pack(isolated_loops["repo_packs"], "alpha")
    _write_pack(isolated_loops["custom_packs"], "beta")

    result = pa_loops.status({})

    assert result["ok"] is True
    assert [item["pack"] for item in result["packs"]] == ["alpha", "beta"]
    assert all(item["ledger"]["lines"] == [] for item in result["packs"])
    assert all(item["heartbeat"]["present"] is False for item in result["packs"])
    assert all(item["stop"]["exists"] is False for item in result["packs"])


def test_loop_card_text_is_category_specific_and_enqueue_uses_it(
    isolated_loops: dict[str, Path],
) -> None:
    _write_pack(
        isolated_loops["repo_packs"],
        "pipeline",
        phases=_pipeline_phases(),
    )
    question_db = isolated_loops["hermes_home"] / "question_events.db"

    event_id = pa_actions.enqueue_pa_action(
        "loops.start_pack",
        {"pack": "pipeline", "model": "gpt-5.6-sol", "max_rounds": 2},
        reason="Nachtlauf vorziehen",
        db_path=question_db,
    )
    event = aq.list_question_events(status="open", db_path=question_db)[0]

    assert event["id"] == event_id
    assert "Nachtlauf-Pack jetzt einmalig starten?" in event["question_text"]
    assert "Pack: `pipeline`" in event["question_text"]
    assert "Modell: `gpt-5.6-sol`" in event["question_text"]
    assert "One-Run" in event["question_text"]
    assert "PHASE_BUILD_MODEL=gpt-5.6-sol" in event["question_text"]
    assert "Grund: Nachtlauf vorziehen" in event["question_text"]

    status_text = pa_actions.build_action_question(
        aq.build_pa_action_envelope("loops.status", {}, reason=None)
    )
    assert "alle bekannten Packs" in status_text
    assert "LEDGER" in status_text and "STOP-Datei" in status_text
    fallback = pa_actions.build_action_question(
        aq.build_pa_action_envelope(
            "tmux.interrupt", {"session": "s", "window": "w"}, reason="halt"
        )
    )
    assert fallback == "PA-Aktion ausführen: tmux.interrupt? — halt"
