"""Tests für hermes_cli.control_loops — Loops-API des /control-Dashboards.

Isoliertes Muster wie test_health_status.py: frische FastAPI-App + register,
Pfad-Seams (PACKS_DIR_OVERRIDE/STATE_ROOT_OVERRIDE/MODELS_PATH_OVERRIDE) auf
tmp, systemd hinter dem _systemctl-Seam gefaked. Kein echtes systemctl/git-Repo.
"""

from __future__ import annotations

import fcntl
import subprocess
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import control_loops


def write_pack(packs_dir: Path, name: str, ptype: str, repo: Path) -> None:
    d = packs_dir / name
    d.mkdir(parents=True)
    phase_names = ("plan", "build", "verify") if ptype == "pipeline" else ("round",)
    phases = {}
    for pname in phase_names:
        (d / f"{pname}.md").write_text(
            f"PHASE={pname} STATE={{{{STATE_DIR}}}}\n"
            "Schreibe das Ergebnis nach last-status.\nVerbote: NIE push/merge/deploy.\n",
            encoding="utf-8",
        )
        phases[pname] = {
            "engine": "claude", "model": "claude-sonnet-5",
            "timeout": 600, "prompt": f"{pname}.md",
        }
    (d / "pack.yaml").write_text(
        yaml.safe_dump({
            "name": name, "type": ptype, "repo": str(repo),
            "description": f"Testpack {name}", "stability": "experimental",
            "phases": phases,
            "params": {"fokus": "standard-fokus"},
        }),
        encoding="utf-8",
    )


@pytest.fixture
def api(tmp_path, monkeypatch):
    packs = tmp_path / "packs"
    repo = tmp_path / "kein-repo"  # existiert nicht → commits_ahead == 0
    write_pack(packs, "nacht", "sweep", repo)
    write_pack(packs, "fliessband", "pipeline", repo)
    (packs / "_vorlage").mkdir()  # Unterstrich-Packs sind unsichtbar

    models = tmp_path / "models.yaml"
    models.write_text(
        yaml.safe_dump({"engines": {"claude": {"label": "Claude (Abo)",
                                               "models": ["claude-sonnet-5"]}}}),
        encoding="utf-8",
    )

    monkeypatch.setattr(control_loops, "PACKS_DIR_OVERRIDE", packs)
    monkeypatch.setattr(control_loops, "STATE_ROOT_OVERRIDE", tmp_path / "state")
    monkeypatch.setattr(control_loops, "MODELS_PATH_OVERRIDE", models)

    calls: list[tuple[str, ...]] = []

    def fake_systemctl(*args: str) -> subprocess.CompletedProcess:
        calls.append(args)
        if args[0] == "is-enabled":
            return subprocess.CompletedProcess(args, 1, stdout="disabled\n", stderr="")
        if args[0] == "is-active":
            return subprocess.CompletedProcess(args, 0, stdout="active\n", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(control_loops, "_systemctl", fake_systemctl)
    # Start-Endpoint probet is-active nach kurzem Sleep — im Test nicht warten.
    monkeypatch.setattr(control_loops, "_unit_failed_fast", lambda unit, probe=0.6: False)

    app = FastAPI()
    control_loops.register_loops_routes(app)
    return TestClient(app), calls, tmp_path


def test_list_loops_shows_packs_hides_templates(api):
    client, _calls, _tmp = api
    data = client.get("/api/loops").json()
    names = [p["name"] for p in data["packs"]]
    assert names == ["fliessband", "nacht"]
    nacht = next(p for p in data["packs"] if p["name"] == "nacht")
    assert nacht["running"] is False
    assert nacht["queue"] is None  # sweep hat keine Queue
    assert nacht["commits_ahead"] == 0
    assert nacht["timer_enabled"] is False
    band = next(p for p in data["packs"] if p["name"] == "fliessband")
    assert band["queue"] == {s: 0 for s in ("00-planned", "10-building", "20-verified", "30-landed", "90-bounced")}
    assert band["phases"]["build"]["model"] == "claude-sonnet-5"


def test_models_endpoint_serves_catalog(api):
    client, _calls, _tmp = api
    data = client.get("/api/loops/models").json()
    assert data["engines"]["claude"]["models"] == ["claude-sonnet-5"]


def test_unknown_and_invalid_pack_names_404(api):
    client, _calls, _tmp = api
    assert client.get("/api/loops/gibtsnicht/detail").status_code == 404
    assert client.post("/api/loops/Evil_Name/stop").status_code == 404
    assert client.post("/api/loops/_vorlage/stop").status_code == 404


def test_start_writes_overrides_and_starts_unit(api):
    client, calls, tmp = api
    resp = client.post("/api/loops/nacht/start", json={
        "overrides": {"PHASE_ROUND_MODEL": "claude-haiku-4-5", "MAX_ROUNDS": 3,
                      "FOKUS": "auth.py Token-Refresh"},
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["started"] is True
    env = (tmp / "state" / "nacht" / "overrides.env").read_text(encoding="utf-8")
    assert "PHASE_ROUND_MODEL=claude-haiku-4-5" in env
    assert "MAX_ROUNDS=3" in env
    assert "FOKUS=auth.py Token-Refresh" in env  # Pack-Param dynamisch erlaubt
    # --no-block ist Pflicht: oneshot-Units halten den Client sonst stundenlang
    assert ("start", "--no-block", "hermes-loop@nacht.service") in calls
    # alten failed-Zustand vorher räumen (sonst blockt der Restart)
    assert ("reset-failed", "hermes-loop@nacht.service") in calls


def test_start_accepts_skip_plan_override(api):
    client, calls, tmp = api
    resp = client.post("/api/loops/nacht/start", json={
        "overrides": {"SKIP_PLAN": "1"},
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["started"] is True
    env = (tmp / "state" / "nacht" / "overrides.env").read_text(encoding="utf-8")
    assert "SKIP_PLAN=1" in env


def test_start_reports_502_when_unit_fails_fast(api, monkeypatch):
    # UI-Start-Bug 2026-07-03: Unit stirbt sofort (203/EXEC), aber --no-block hatte
    # rc 0 → früher "started: true". Jetzt muss der Sofort-Fail als 502 durchschlagen.
    client, _calls, _tmp = api
    from hermes_cli import control_loops as cl
    monkeypatch.setattr(cl, "_unit_failed_fast", lambda unit, probe=0.6: True)
    resp = client.post("/api/loops/nacht/start", json={"overrides": {}})
    assert resp.status_code == 502
    assert "sofort gescheitert" in resp.json()["detail"]


def test_start_rejects_override_for_foreign_param(api):
    client, calls, _tmp = api
    # FOCUS ist KEIN Param des Packs (es heißt fokus) → 400 statt stillem No-Op
    resp = client.post("/api/loops/nacht/start", json={"overrides": {"FOCUSX": "x"}})
    assert resp.status_code == 400
    assert "Pack-Params" in resp.json()["detail"]


def test_start_rejects_bad_override_keys_and_values(api):
    client, calls, _tmp = api
    resp = client.post("/api/loops/nacht/start", json={"overrides": {"RM_RF": "x"}})
    assert resp.status_code == 400
    resp = client.post("/api/loops/nacht/start", json={"overrides": {"FOCUS": "a\nBOOM=1"}})
    assert resp.status_code == 400
    assert not any(c[0] == "start" for c in calls), "bei 400 darf kein Unit-Start passieren"


def test_start_conflicts_while_running(api):
    client, _calls, tmp = api
    state = tmp / "state" / "nacht"
    state.mkdir(parents=True)
    lock = (state / ".lock").open("w", encoding="utf-8")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # simuliert laufenden Runner
    try:
        resp = client.post("/api/loops/nacht/start", json={"overrides": {}})
        assert resp.status_code == 409
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def test_stop_sets_stop_file(api):
    client, _calls, tmp = api
    resp = client.post("/api/loops/nacht/stop")
    assert resp.status_code == 200
    assert (tmp / "state" / "nacht" / "STOP").exists()
    data = client.get("/api/loops").json()
    nacht = next(p for p in data["packs"] if p["name"] == "nacht")
    assert nacht["stop_requested"] is True


def test_timer_toggle_calls_systemctl(api):
    client, calls, _tmp = api
    resp = client.post("/api/loops/nacht/timer", json={"enabled": True})
    assert resp.status_code == 200
    assert ("enable", "--now", "hermes-loop@nacht.timer") in calls
    resp = client.post("/api/loops/nacht/timer", json={"enabled": False})
    assert ("disable", "--now", "hermes-loop@nacht.timer") in calls


def test_files_repo_pack_readonly_and_put_403(api):
    client, _calls, _tmp = api
    data = client.get("/api/loops/nacht/files").json()
    names = [f["name"] for f in data["files"]]
    assert "pack.yaml" in names and "round.md" in names
    assert all(f["editable"] is False for f in data["files"])
    resp = client.put("/api/loops/nacht/files/round.md", json={"content": "x"})
    assert resp.status_code == 403


def test_duplicate_then_edit_custom_pack_with_lint(api, tmp_path):
    client, _calls, _tmp = api
    # Duplikat entsteht im (Test-)Packs-Dir und ist danach als eigenes Pack sichtbar
    resp = client.post("/api/loops/duplicate", json={"source": "nacht", "name": "nacht-kopie"})
    assert resp.status_code == 200, resp.text
    names = [p["name"] for p in client.get("/api/loops").json()["packs"]]
    assert "nacht-kopie" in names
    # Kollision → 409
    assert client.post("/api/loops/duplicate", json={"source": "nacht", "name": "nacht-kopie"}).status_code == 409

    from hermes_cli import control_loops as cl
    problem = cl._lint_pack_dir(cl._packs_dir(), "nacht-kopie")
    assert problem is None


VALID_ROUND = "STATE={{STATE_DIR}}\nSchreibe nach last-status.\nVerbote: NIE push/merge.\n"


def test_put_custom_pack_lints_before_persist(api, monkeypatch):
    client, _calls, _tmp = api
    from hermes_cli import control_loops as cl
    from loops import runner as lr
    # Im Test zeigt der Override auf das Packs-Dir; markieren wir es als custom,
    # greift der echte Editier-Pfad (source == custom).
    monkeypatch.setattr(lr, "CUSTOM_PACKS_DIR", cl._packs_dir())

    data = client.get("/api/loops/nacht/files").json()
    assert data["source"] == "custom"
    assert all(f["editable"] is True for f in data["files"])

    # gültiger Prompt-Edit → persistiert
    resp = client.put("/api/loops/nacht/files/round.md", json={"content": VALID_ROUND})
    assert resp.status_code == 200, resp.text
    assert (cl._packs_dir() / "nacht" / "round.md").read_text(encoding="utf-8") == VALID_ROUND

    # Prompt ohne Pflicht-Konventionen → 400, Datei unverändert
    resp = client.put("/api/loops/nacht/files/round.md", json={"content": "nur text"})
    assert resp.status_code == 400 and "Lint" in resp.json()["detail"]
    assert (cl._packs_dir() / "nacht" / "round.md").read_text(encoding="utf-8") == VALID_ROUND

    # kaputtes Manifest → 400 via Schattenkopie, Original bleibt ladbar
    resp = client.put("/api/loops/nacht/files/pack.yaml", json={"content": "type: zirkus"})
    assert resp.status_code == 400
    assert client.get("/api/loops/nacht/detail").status_code == 200

    # Dateinamens-Härte: Traversal/Neuanlage
    assert client.put("/api/loops/nacht/files/gibtsnicht.md", json={"content": VALID_ROUND}).status_code == 404
    assert client.put("/api/loops/nacht/files/boese.sh", json={"content": "x"}).status_code == 400


def test_land_endpoint_spawns_detached_and_409_when_running(api, monkeypatch):
    client, _calls, tmp = api
    from hermes_cli import control_loops as cl
    spawned = []
    monkeypatch.setattr(cl, "_spawn_land", lambda pack, log: spawned.append((pack.name, log.name)))
    resp = client.post("/api/loops/nacht/land")
    assert resp.status_code == 200, resp.text
    assert resp.json()["land_started"] is True
    assert spawned and spawned[0][0] == "nacht" and spawned[0][1].startswith("land-")

    state = tmp / "state" / "nacht"
    state.mkdir(parents=True, exist_ok=True)
    lock = (state / ".lock").open("w", encoding="utf-8")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert client.post("/api/loops/nacht/land").status_code == 409
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def test_summary_contains_heartbeat_when_present(api):
    client, _calls, tmp = api
    state = tmp / "state" / "nacht"
    state.mkdir(parents=True, exist_ok=True)
    (state / "heartbeat.json").write_text(
        '{"current": {"phase": "round", "engine": "claude", "model": "claude-sonnet-5", '
        '"started_at": "2026-07-02T23:30:00", "timeout": 2400}, '
        '"last": [{"phase": "round", "secs": 512, "rc": 0, "at": "2026-07-02T22:00:00"}]}',
        encoding="utf-8",
    )
    nacht = next(p for p in client.get("/api/loops").json()["packs"] if p["name"] == "nacht")
    assert nacht["heartbeat"]["current"]["phase"] == "round"
    assert nacht["heartbeat"]["last"][0]["secs"] == 512


def test_detail_returns_ledger_and_queue(api):
    client, _calls, tmp = api
    state = tmp / "state" / "fliessband"
    (state / "queue" / "00-planned").mkdir(parents=True)
    (state / "queue" / "00-planned" / "P1-x.md").write_text("---\nretry: 0\n---\n", encoding="utf-8")
    state.joinpath("LEDGER.md").write_text(
        "# LEDGER\n- 2026-07-02 21:00 PLAN: 1 Pläne\n", encoding="utf-8"
    )
    (state / "overrides.env").write_text("MAX_ROUNDS=5\n", encoding="utf-8")
    data = client.get("/api/loops/fliessband/detail").json()
    assert data["queue_entries"]["00-planned"] == ["P1-x.md"]
    assert any("PLAN: 1" in line for line in data["ledger_tail"])
    assert data["overrides"] == {"MAX_ROUNDS": "5"}
