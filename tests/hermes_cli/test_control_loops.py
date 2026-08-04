"""Tests für hermes_cli.control_loops — Loops-API des /control-Dashboards.

Isoliertes Muster wie test_health_status.py: frische FastAPI-App + register,
Pfad-Seams (PACKS_DIR_OVERRIDE/STATE_ROOT_OVERRIDE/MODELS_PATH_OVERRIDE) auf
tmp, systemd hinter dem _systemctl-Seam gefaked. Kein echtes systemctl/git-Repo.
"""

from __future__ import annotations

import fcntl
import json
import subprocess
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import control_loops

_REAL_SYSTEMCTL = control_loops._systemctl


def write_pack(packs_dir: Path, name: str, ptype: str, repo: Path, **overrides) -> None:
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
    manifest = {
        "name": name, "type": ptype, "repo": str(repo),
        "description": f"Testpack {name}", "stability": "experimental",
        "phases": phases,
        "params": {"fokus": "standard-fokus"},
        **overrides,
    }
    (d / "pack.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")


@pytest.fixture(autouse=True)
def hermetic_model_catalog(monkeypatch):
    """Kein Live-Provider-Probe in den Dashboard-Tests.

    Der Loops-Katalog ist seit 2026-07-28 dynamisch (voller Provider-Katalog wie
    im Lanes-Tab). Ungepatcht hinge jede Assertion über Modelllisten davon ab,
    welche Abos auf dem Testhost konfiguriert sind.
    """
    from loops import model_catalog

    monkeypatch.setattr(model_catalog, "_build_inventory_payload", lambda: {})
    monkeypatch.setattr(model_catalog, "_claude_cli_models", list)
    monkeypatch.setattr(model_catalog, "_hermes_profiles", list)
    model_catalog.reset_inventory_cache()
    yield
    model_catalog.reset_inventory_cache()


@pytest.fixture
def api(tmp_path, monkeypatch):
    packs = tmp_path / "packs"
    # Das Repo-VERZEICHNIS existiert (Normalfall: der Start-Endpoint lehnt seit
    # 2026-07-28 ein Pack mit totem repo-Pfad vorab mit 409 ab), es ist aber
    # kein Git-Repo → commits_ahead bleibt 0. Der Fall "Pfad fehlt ganz" hat
    # einen eigenen Test.
    repo = tmp_path / "kein-git-repo"
    repo.mkdir()
    write_pack(packs, "nacht", "sweep", repo)  # keine land_*-Felder → Loader-Defaults
    write_pack(
        packs, "fliessband", "pipeline", repo,
        land_remote="origin", land_push=False, land_gates=["npm run gate", "pytest -q"],
    )
    (packs / "_vorlage").mkdir()  # Unterstrich-Packs sind unsichtbar

    models = tmp_path / "models.yaml"
    models.write_text(
        yaml.safe_dump(
            {
                "engines": {
                    "claude": {
                        "label": "Claude (Abo)",
                        "models": ["claude-sonnet-5", "claude-opus-4-6"],
                    },
                    "kimi": {
                        "label": "Kimi Code",
                        "models": ["kimi-for-coding", "k3"],
                    },
                    "alibaba-token-plan": {
                        "label": "Alibaba Token Plan",
                        "models": ["qwen3.8-max-preview"],
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(control_loops, "PACKS_DIR_OVERRIDE", packs)
    monkeypatch.setattr(control_loops, "STATE_ROOT_OVERRIDE", tmp_path / "state")
    monkeypatch.setattr(control_loops, "MODELS_PATH_OVERRIDE", models)
    monkeypatch.setattr(control_loops, "SYSTEMD_USER_DIR_OVERRIDE", tmp_path / "systemd-user")

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
    client, _calls, tmp = api
    data = client.get("/api/loops").json()
    names = [p["name"] for p in data["packs"]]
    assert names == ["fliessband", "nacht"]
    nacht = next(p for p in data["packs"] if p["name"] == "nacht")
    assert nacht["running"] is False
    assert nacht["queue"] is None  # sweep hat keine Queue
    assert nacht["commits_ahead"] == 0
    assert nacht["timer_enabled"] is False
    assert nacht["timer_schedule"] == "23:37"
    assert nacht["timer_next_run"] is None
    assert nacht["autoland"] is False
    assert nacht["repo"] == str((tmp / "kein-git-repo").resolve())
    assert nacht["repo_exists"] is True
    assert nacht["base_branch"] == "main"
    assert nacht["land_remote"] == "piet-fork"
    assert nacht["land_push"] is True
    assert nacht["land_gates"] is None
    band = next(p for p in data["packs"] if p["name"] == "fliessband")
    assert band["queue"] == {s: 0 for s in ("00-planned", "10-building", "20-verified", "30-landed", "90-bounced")}
    assert band["phases"]["build"]["model"] == "claude-sonnet-5"
    assert band["land_remote"] == "origin"
    assert band["land_push"] is False
    assert band["land_gates"] == ["npm run gate", "pytest -q"]


def test_real_phase_usage_ledger_flows_to_summary_and_detail(api):
    client, _calls, tmp = api
    state = tmp / "state" / "fliessband"
    state.mkdir(parents=True)
    rows = [
        {"ts": "2026-07-13T01:00:00", "pack": "fliessband", "event": "phase_usage", "round": 1,
         "phase": "build", "engine": "xai", "model": "grok-4.5", "total_tokens": 270,
         "input_tokens": 220, "cached_input_tokens": 180, "output_tokens": 50,
         "reasoning_tokens": 40, "billing": "subscription", "metered_cost_eur": 0.0},
        {"ts": "2026-07-13T01:01:00", "pack": "fliessband", "event": "phase_usage", "round": 1,
         "phase": "verify", "engine": "codex", "model": "gpt-5.6-sol", "total_tokens": 100,
         "billing": "subscription", "metered_cost_eur": 0.0},
    ]
    (state / "ledger.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8",
    )

    pack = next(p for p in client.get("/api/loops").json()["packs"] if p["name"] == "fliessband")
    assert pack["token_usage"] == {
        "total_tokens": 370,
        "metered_cost_eur": 0.0,
        "billing": "subscription",
    }
    detail = client.get("/api/loops/fliessband/detail").json()
    assert detail["phase_usage"] == rows


def test_list_loops_batches_timer_systemctl_for_all_packs(api, monkeypatch):
    client, _calls, _tmp = api
    calls: list[tuple[str, ...]] = []

    def batched_systemctl(*args: str) -> subprocess.CompletedProcess:
        calls.append(args)
        if args[0] == "show":
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=(
                    "TimersCalendar={ OnCalendar=*-*-* 23:37:00 ; next_elapse=(null) }\n"
                    "Id=hermes-loop@fliessband.timer\nUnitFileState=disabled\n\n"
                    "TimersCalendar={ OnCalendar=*-*-* 23:37:00 ; next_elapse=(null) }\n"
                    "Id=hermes-loop@nacht.timer\nUnitFileState=disabled\n"
                ),
                stderr="",
            )
        if args[0] == "list-timers":
            return subprocess.CompletedProcess(args, 0, stdout="[]\n", stderr="")
        raise AssertionError(f"unexpected per-pack systemctl: {args}")

    monkeypatch.setattr(control_loops, "_systemctl", batched_systemctl)
    assert client.get("/api/loops").status_code == 200
    assert [call[0] for call in calls] == ["show", "list-timers"]



def test_commits_ahead_ignores_patch_equivalent_commits(monkeypatch):
    from hermes_cli import control_loops as cl
    from loops.runner import Pack

    pack = Pack(
        name="dashboard-polish",
        type="pipeline",
        repo=Path("/repo"),
        pack_dir=Path("/packs/dashboard-polish"),
        description="",
        stability="experimental",
        phases={},
        params={},
        stop={},
    )
    calls: list[tuple[str, ...]] = []

    def fake_git(repo: Path, *args: str) -> subprocess.CompletedProcess:
        calls.append(args)
        assert args == ("cherry", "-v", "main", "loop/dashboard-polish")
        return subprocess.CompletedProcess(
            ["git", *args],
            0,
            stdout=(
                "- 44a2814ea00000000000000000000000000000000 feat(control): port dashboard polish onto fleet view\n"
                "+ 1234567890000000000000000000000000000000 fix(control): remaining loop polish\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(cl, "_git", fake_git)

    assert cl._commits_ahead(pack) == ["1234567 fix(control): remaining loop polish"]
    assert calls == [("cherry", "-v", "main", "loop/dashboard-polish")]


@pytest.mark.parametrize(
    ("failure", "message_fragment"),
    [
        (
            lambda command: subprocess.TimeoutExpired(command, 30),
            "timed out",
        ),
        (
            lambda _command: FileNotFoundError(
                2, "No such file or directory: 'git'",
            ),
            "No such file or directory",
        ),
    ],
    ids=("timeout", "missing-git"),
)
def test_git_probe_failures_degrade_list_response_per_pack(
    api, monkeypatch, failure, message_fragment,
):
    client, _calls, _tmp = api
    real_run = control_loops.subprocess.run

    def failing_git(command, *args, **kwargs):
        if command[0] == "git":
            raise failure(command)
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(control_loops.subprocess, "run", failing_git)
    with TestClient(client.app, raise_server_exceptions=False) as degraded_client:
        response = degraded_client.get("/api/loops")

    assert response.status_code == 200, response.text
    for pack in response.json()["packs"]:
        assert pack["commits_ahead"] == 0
        assert "git-Probe fehlgeschlagen" in pack["error"]
        assert message_fragment in pack["error"]


@pytest.mark.parametrize(
    ("failure", "message_fragment"),
    [
        (
            lambda command: subprocess.TimeoutExpired(command, 30),
            "timed out",
        ),
        (
            lambda _command: FileNotFoundError(
                2, "No such file or directory: 'git'",
            ),
            "No such file or directory",
        ),
    ],
    ids=("timeout", "missing-git"),
)
def test_git_probe_failures_degrade_detail_response(
    api, monkeypatch, failure, message_fragment,
):
    client, _calls, _tmp = api
    real_run = control_loops.subprocess.run

    def failing_git(command, *args, **kwargs):
        if command[0] == "git":
            raise failure(command)
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(control_loops.subprocess, "run", failing_git)
    with TestClient(client.app, raise_server_exceptions=False) as degraded_client:
        response = degraded_client.get("/api/loops/nacht/detail")

    assert response.status_code == 200, response.text
    assert response.json()["commits"] == []
    assert "git-Probe fehlgeschlagen" in response.json()["error"]
    assert message_fragment in response.json()["error"]


def test_git_probe_failure_only_degrades_affected_pack(api, monkeypatch):
    client, _calls, tmp = api
    packs = control_loops._packs_dir()
    healthy_repo = tmp / "healthy-repo"
    broken_repo = tmp / "broken-repo"
    write_pack(packs, "healthy", "sweep", healthy_repo)
    write_pack(packs, "broken", "sweep", broken_repo)
    real_run = control_loops.subprocess.run

    def selective_git(command, *args, **kwargs):
        if command[:3] == ["git", "-C", str(broken_repo.resolve())]:
            raise subprocess.TimeoutExpired(command, 30)
        if command[0] == "git":
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    "+ 1234567890000000000000000000000000000000 "
                    "fix(control): healthy pack commit\n"
                ),
                stderr="",
            )
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(control_loops.subprocess, "run", selective_git)
    response = client.get("/api/loops")

    assert response.status_code == 200, response.text
    by_name = {pack["name"]: pack for pack in response.json()["packs"]}
    assert by_name["healthy"]["commits_ahead"] == 1
    assert "error" not in by_name["healthy"]
    assert by_name["broken"]["commits_ahead"] == 0
    assert "git-Probe fehlgeschlagen" in by_name["broken"]["error"]


def test_models_endpoint_serves_catalog(api):
    client, _calls, _tmp = api
    data = client.get("/api/loops/models").json()
    models = data["engines"]["claude"]["models"]
    assert "claude-sonnet-5" in models
    assert "claude-opus-4-6" in models  # fixture catalog for partial-pair tests
    assert data["engines"]["claude"]["label"] == "Claude (Abo)"


def test_list_loops_preserves_round_number_from_heartbeat(api):
    client, _calls, tmp = api
    state = tmp / "state" / "fliessband"
    state.mkdir(parents=True)
    (state / "heartbeat.json").write_text(
        '{"current":{"phase":"build","engine":"codex","model":"gpt-5.6-sol",'
        '"started_at":"2026-07-12T12:00:00","timeout":3600,"round":3},"last":[]}',
        encoding="utf-8",
    )

    pack = next(p for p in client.get("/api/loops").json()["packs"] if p["name"] == "fliessband")
    assert pack["heartbeat"]["current"]["round"] == 3


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


def test_start_rejects_unknown_engine_like_night_overrides_do(api):
    """Start muss dieselbe Katalogpruefung fahren wie die Night-Overrides.

    Bis 2026-07-29 pruefte `_validate_start_overrides` nur Autoland-Keys und
    Effort — eine unbekannte Engine passierte die HTTP-Schicht und starb erst in
    der systemd-Unit. Der Operator sah "Loop-Unit sofort gescheitert" statt zu
    erfahren, dass die Engine gar nicht existiert. `_validate_night_overrides`
    macht es fuer denselben Fehler seit jeher richtig (400 "unbekannt").
    """
    client, calls, _tmp = api
    resp = client.post(
        "/api/loops/nacht/start",
        json={"overrides": {"PHASE_ROUND_ENGINE": "warpantrieb"}},
    )
    assert resp.status_code == 400, resp.text
    assert "unbekannt" in resp.json()["detail"]
    assert not any(c[0] == "start" for c in calls), "bei 400 darf kein Unit-Start passieren"


def test_start_rejects_unknown_engine_on_a_partial_override(api):
    """Auch ein Teil-Override muss die Engine pruefen.

    Genau diese Form war die Nacht-Konfiguration vom 2026-07-29: nur
    PHASE_BUILD_ENGINE gesetzt, das Modell kam aus der pack.yaml. Ein Tippfehler
    im Engine-Namen darf nicht durchrutschen, nur weil kein MODEL-Key dabei war.
    """
    client, calls, _tmp = api
    resp = client.post(
        "/api/loops/nacht/start",
        json={"overrides": {"PHASE_ROUND_ENGINE": "codex-typo"}},
    )
    assert resp.status_code == 400, resp.text
    assert "unbekannt" in resp.json()["detail"]
    assert not any(c[0] == "start" for c in calls)


def test_start_still_allows_engine_swap_without_matching_model(api):
    """Bewusste Asymmetrie zu den Night-Overrides — nicht versehentlich verschaerfen.

    Night schreibt eine PERSISTENTE Konfiguration und pinnt deshalb zusaetzlich
    das Modell gegen den Katalog. Der Start ist einmalig und erlaubt weiterhin,
    nur die Engine zu tauschen (das Modell kommt dann aus der pack.yaml) — sonst
    braeche jeder bestehende Teil-Override-Aufruf.
    """
    client, _calls, tmp = api
    resp = client.post(
        "/api/loops/nacht/start",
        json={"overrides": {"PHASE_ROUND_ENGINE": "kimi"}},
    )
    assert resp.status_code == 200, resp.text
    env = (tmp / "state" / "nacht" / "overrides.env").read_text(encoding="utf-8")
    assert "PHASE_ROUND_ENGINE=kimi" in env


def test_summary_flags_a_pack_whose_repo_is_gone(api, tmp_path):
    """Ein Pack mit totem repo-Pfad sah bis 28.07. im Dashboard normal aus.

    Belegt an xai-hard-gate/loops-date-audit, die beide auf einen geloeschten
    Worktree zeigten: die Karte war unauffaellig, der Start starb erst in der
    Unit. Gleiche Semantik wie der Runner-Check (`pack.repo.is_dir()`).
    """
    client, _calls, tmp = api
    write_pack(tmp / "packs", "verwaist", "sweep", tmp_path / "weg-damit")
    packs = {p["name"]: p for p in client.get("/api/loops").json()["packs"]}
    assert packs["verwaist"]["repo_exists"] is False
    # Gegenprobe: das intakte Pack aus derselben Antwort bleibt unmarkiert,
    # sonst wuerde der Test jedes Pack als kaputt melden.
    assert packs["nacht"]["repo_exists"] is True


def test_list_loops_no_repo_lock_flag_by_default(api):
    """Grundzustand: kein anderer Prozess haelt den Repo-Lock — beide Packs
    ("nacht"/"fliessband") teilen sich `kein-git-repo`, keins traegt das Flag.
    Kontrollprobe, sonst koennte ein zu breiter Default alles markieren."""
    client, _calls, _tmp = api
    packs = client.get("/api/loops").json()["packs"]
    for pack in packs:
        assert "repo_locked" not in pack
        assert "repo_locked_by" not in pack


def test_land_blocked_by_sibling_pack_names_the_holder(api):
    """Baut darf parallel laufen; Landen bleibt repo-weit exklusiv. "nacht"
    haelt den Repo-Lock (wie `LoopRunner.locked()` es fuer eine Landung
    taete) UND laeuft selbst — "fliessband" (dasselbe Repo, nicht laufend)
    muss VOR dem Klick sehen, dass "nacht" blockiert; "nacht" selbst bekommt
    kein Fremd-Flag fuer den eigenen Lauf."""
    client, _calls, tmp = api
    from hermes_cli import control_loops as cl

    state_root = tmp / "state"
    state_root.mkdir(parents=True, exist_ok=True)
    repo = (tmp / "kein-git-repo").resolve()

    nacht_state = state_root / "nacht"
    nacht_state.mkdir(parents=True, exist_ok=True)
    nacht_lock = (nacht_state / ".lock").open("w", encoding="utf-8")
    fcntl.flock(nacht_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # simuliert laufenden Runner

    repo_lock_path = cl._repo_lock_path(repo)
    repo_lock = repo_lock_path.open("w", encoding="utf-8")
    fcntl.flock(repo_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # derselbe Lock wie LoopRunner.repo_locked()
    # Halter-Notiz wie LoopRunner.repo_locked() sie beim Nehmen schreibt. Der
    # Name wird daraus GELESEN, nicht mehr aus "welches Pack laeuft gerade"
    # geraten: seit Bauen parallel erlaubt ist, laufen Packs, ohne den
    # Land-Lock zu halten — die Anzeige benannte sonst ein bauendes Pack als
    # Blockierer der Landung.
    repo_lock.write("nacht\n4711\n")
    repo_lock.flush()
    try:
        packs = {p["name"]: p for p in client.get("/api/loops").json()["packs"]}
    finally:
        fcntl.flock(repo_lock, fcntl.LOCK_UN)
        repo_lock.close()
        fcntl.flock(nacht_lock, fcntl.LOCK_UN)
        nacht_lock.close()

    assert packs["nacht"]["running"] is True
    assert "repo_locked" not in packs["nacht"]  # der Halter selbst ist nicht blockiert

    assert packs["fliessband"]["running"] is False
    assert packs["fliessband"]["repo_locked"] is True
    assert packs["fliessband"]["repo_locked_by"] == "nacht"


def test_land_blocked_without_a_visible_holder_stays_generic(api):
    """Repo-Lock haengt, aber KEIN Pack der Gruppe laeuft sichtbar (z.B. ein
    Prozess ausserhalb der Pack-Liste) — der Name darf NICHT geraten werden,
    nur die generische Meldung."""
    client, _calls, tmp = api
    from hermes_cli import control_loops as cl

    state_root = tmp / "state"
    state_root.mkdir(parents=True, exist_ok=True)
    repo = (tmp / "kein-git-repo").resolve()

    repo_lock = cl._repo_lock_path(repo).open("w", encoding="utf-8")
    fcntl.flock(repo_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        packs = {p["name"]: p for p in client.get("/api/loops").json()["packs"]}
    finally:
        fcntl.flock(repo_lock, fcntl.LOCK_UN)
        repo_lock.close()

    assert packs["nacht"]["repo_locked"] is True
    assert packs["nacht"]["repo_locked_by"] is None
    assert packs["fliessband"]["repo_locked"] is True
    assert packs["fliessband"]["repo_locked_by"] is None


def test_start_refuses_a_pack_whose_repo_is_gone(api, tmp_path):
    """Statt einen Loop zu starten, der in der Unit stirbt und als kryptischer
    502 zurueckkommt, sagt der Endpoint vorher WAS fehlt."""
    client, calls, tmp = api
    write_pack(tmp / "packs", "verwaist", "sweep", tmp_path / "weg-damit")
    resp = client.post("/api/loops/verwaist/start", json={"overrides": {}})
    assert resp.status_code == 409
    assert "weg-damit" in resp.json()["detail"]
    # Kein systemctl start fuer dieses Pack — der Loop darf gar nicht erst laufen.
    assert not any(a[0] == "start" and "verwaist" in " ".join(a) for a in calls)


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


def test_missing_systemctl_binary_degrades_list_and_returns_defined_mutation_error(api, monkeypatch):
    client, _calls, _tmp = api
    real_run = control_loops.subprocess.run

    def missing_systemctl(*args, **kwargs):
        if args[0][0] == "systemctl":
            raise FileNotFoundError(2, "No such file or directory", "systemctl")
        return real_run(*args, **kwargs)

    monkeypatch.setattr(control_loops, "_systemctl", _REAL_SYSTEMCTL)
    monkeypatch.setattr(control_loops.subprocess, "run", missing_systemctl)

    resp = client.get("/api/loops")
    assert resp.status_code == 200, resp.text
    nacht = next(pack for pack in resp.json()["packs"] if pack["name"] == "nacht")
    assert nacht["timer_enabled"] is False
    assert nacht["timer_schedule"] == "23:37"
    assert nacht["timer_next_run"] is None

    resp = client.post("/api/loops/nacht/timer", json={"enabled": True})
    assert resp.status_code == 502
    assert "systemctl" in resp.json()["detail"]


def test_timer_schedule_persists_for_disabled_timer_without_starting_it(api):
    client, calls, tmp = api
    resp = client.put("/api/loops/nacht/timer/schedule", json={"time": "04:25"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "pack": "nacht",
        "timer_enabled": False,
        "timer_schedule": "04:25",
        "timer_next_run": None,
    }
    dropin = tmp / "systemd-user" / "hermes-loop@nacht.timer.d" / "schedule.conf"
    assert dropin.read_text(encoding="utf-8") == (
        "# Verwaltet vom Hermes /control Loop-Tab.\n"
        "[Timer]\n"
        "OnCalendar=\n"
        "OnCalendar=*-*-* 04:25:00\n"
    )
    assert ("daemon-reload",) in calls
    assert ("restart", "hermes-loop@nacht.timer") not in calls
    nacht = next(p for p in client.get("/api/loops").json()["packs"] if p["name"] == "nacht")
    assert nacht["timer_schedule"] == "04:25"


def test_timer_schedule_rearms_enabled_timer_and_reports_next_run(api, monkeypatch):
    client, calls, _tmp = api

    def enabled_systemctl(*args: str) -> subprocess.CompletedProcess:
        calls.append(args)
        if args[0] == "is-enabled":
            return subprocess.CompletedProcess(args, 0, stdout="enabled\n", stderr="")
        if args[0] == "list-timers":
            return subprocess.CompletedProcess(
                args, 0, stdout='[{"next":1783642500000000,"unit":"hermes-loop@nacht.timer"}]\n', stderr="",
            )
        if args[0] == "show":
            return subprocess.CompletedProcess(
                args, 0, stdout="Fri 2026-07-10 02:15:00 CEST\n", stderr="",
            )
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(control_loops, "_systemctl", enabled_systemctl)
    resp = client.put("/api/loops/nacht/timer/schedule", json={"time": "02:15"})
    assert resp.status_code == 200, resp.text
    assert ("restart", "hermes-loop@nacht.timer") in calls
    assert resp.json()["timer_next_run"] == "2026-07-10T00:15:00Z"


def test_timer_summary_uses_effective_systemd_schedule_and_reports_running_disabled_timer(api, monkeypatch):
    client, calls, _tmp = api

    def effective_systemctl(*args: str) -> subprocess.CompletedProcess:
        calls.append(args)
        if args[0] == "is-enabled":
            return subprocess.CompletedProcess(args, 1, stdout="disabled\n", stderr="")
        if "--property=TimersCalendar" in args:
            return subprocess.CompletedProcess(
                args, 0,
                stdout="{ OnCalendar=*-*-* 04:50:00 ; next_elapse=Fri 2026-07-10 04:50:00 CEST }\n",
                stderr="",
            )
        if args[0] == "list-timers":
            return subprocess.CompletedProcess(
                args, 0, stdout='[{"next":1783651800000000,"unit":"hermes-loop@nacht.timer"}]\n', stderr="",
            )
        if "--property=NextElapseUSecRealtime" in args:
            return subprocess.CompletedProcess(
                args, 0, stdout="Fri 2026-07-10 04:50:00 CEST\n", stderr="",
            )
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(control_loops, "_systemctl", effective_systemctl)
    nacht = next(p for p in client.get("/api/loops").json()["packs"] if p["name"] == "nacht")
    assert nacht["timer_enabled"] is False
    assert nacht["timer_schedule"] == "04:50"
    assert nacht["timer_next_run"] == "2026-07-10T02:50:00Z"


@pytest.mark.parametrize(
    "bad_time", ["7:30", "24:00", "23:60", "23:37\n", "0٣:45", "٢٣:٤٥", "morgen"],
)
def test_timer_schedule_rejects_invalid_time_without_writing(api, bad_time):
    client, calls, tmp = api
    resp = client.put("/api/loops/nacht/timer/schedule", json={"time": bad_time})
    assert resp.status_code == 400
    assert not (tmp / "systemd-user" / "hermes-loop@nacht.timer.d" / "schedule.conf").exists()
    assert ("daemon-reload",) not in calls


def test_timer_schedule_rolls_back_dropin_when_daemon_reload_fails(api, monkeypatch):
    client, calls, tmp = api
    first = client.put("/api/loops/nacht/timer/schedule", json={"time": "05:10"})
    assert first.status_code == 200
    dropin = tmp / "systemd-user" / "hermes-loop@nacht.timer.d" / "schedule.conf"
    before = dropin.read_bytes()
    reloads = 0

    def failing_reload(*args: str) -> subprocess.CompletedProcess:
        nonlocal reloads
        calls.append(args)
        if args[0] == "is-enabled":
            return subprocess.CompletedProcess(args, 1, stdout="disabled\n", stderr="")
        if args == ("daemon-reload",):
            reloads += 1
            if reloads == 1:
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="invalid unit")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(control_loops, "_systemctl", failing_reload)
    resp = client.put("/api/loops/nacht/timer/schedule", json={"time": "06:20"})
    assert resp.status_code == 502
    assert "daemon-reload" in resp.json()["detail"]
    assert dropin.read_bytes() == before
    assert reloads == 2, "Rollback muss systemd erneut auf den alten Drop-in laden"


def test_timer_schedule_removes_new_dropin_when_first_daemon_reload_fails(api, monkeypatch):
    client, calls, tmp = api
    reloads = 0

    def failing_reload(*args: str) -> subprocess.CompletedProcess:
        nonlocal reloads
        calls.append(args)
        if args[0] == "is-enabled":
            return subprocess.CompletedProcess(args, 1, stdout="disabled\n", stderr="")
        if args == ("daemon-reload",):
            reloads += 1
            return subprocess.CompletedProcess(
                args, 1 if reloads == 1 else 0, stdout="", stderr="invalid unit",
            )
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(control_loops, "_systemctl", failing_reload)
    resp = client.put("/api/loops/nacht/timer/schedule", json={"time": "06:20"})
    dropin = tmp / "systemd-user" / "hermes-loop@nacht.timer.d" / "schedule.conf"
    assert resp.status_code == 502
    assert not dropin.exists(), "Rollback muss einen erstmals angelegten Drop-in entfernen"
    assert reloads == 2


def test_timer_schedule_restores_and_rearms_previous_schedule_when_restart_fails(api, monkeypatch):
    client, calls, tmp = api
    seeded = client.put("/api/loops/nacht/timer/schedule", json={"time": "05:10"})
    assert seeded.status_code == 200
    dropin = tmp / "systemd-user" / "hermes-loop@nacht.timer.d" / "schedule.conf"
    before = dropin.read_bytes()
    restarts = 0
    reloads = 0

    def failing_restart(*args: str) -> subprocess.CompletedProcess:
        nonlocal reloads, restarts
        calls.append(args)
        if args[0] == "is-enabled":
            return subprocess.CompletedProcess(args, 0, stdout="enabled\n", stderr="")
        if args == ("daemon-reload",):
            reloads += 1
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args == ("restart", "hermes-loop@nacht.timer"):
            restarts += 1
            return subprocess.CompletedProcess(
                args, 1 if restarts == 1 else 0, stdout="", stderr="restart failed",
            )
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(control_loops, "_systemctl", failing_restart)
    resp = client.put("/api/loops/nacht/timer/schedule", json={"time": "06:20"})
    assert resp.status_code == 502
    assert "neu eingeplant" in resp.json()["detail"]
    assert dropin.read_bytes() == before
    assert reloads == 2
    assert restarts == 2, "Rollback muss den Timer mit dem alten Schedule erneut starten"


def test_files_repo_pack_readonly_and_put_403(api):
    client, _calls, _tmp = api
    data = client.get("/api/loops/nacht/files").json()
    names = [f["name"] for f in data["files"]]
    assert "pack.yaml" in names and "round.md" in names
    assert all(f["editable"] is False for f in data["files"])
    resp = client.put("/api/loops/nacht/files/round.md", json={"content": "x"})
    assert resp.status_code == 403


def test_queue_file_returns_bounced_plan_verbatim(api):
    client, _calls, tmp = api
    content = (
        "---\n"
        "id: P1-bounced-plan\n"
        "retry: 1\n"
        "---\n"
        "## Verifier-Evidence\n"
        "FAIL: Payload enthält noch nicht den vollständigen Text.\n\n"
        "## Loop-Fail\n"
        "Verifier hat den Byte-für-Byte-Nachweis abgelehnt.\n"
    )
    content_bytes = content.encode("utf-8")
    target = tmp / "state" / "fliessband" / "queue" / "90-bounced" / "P1-bounced-plan.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(content_bytes)

    response = client.get(
        "/api/loops/fliessband/queue/90-bounced/P1-bounced-plan.md",
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload == {
        "pack": "fliessband",
        "stage": "90-bounced",
        "filename": "P1-bounced-plan.md",
        "content": content,
    }
    assert payload["content"].encode("utf-8") == content_bytes


@pytest.mark.parametrize(
    ("stage", "filename"),
    [
        ("40-unknown", "P1-plan.md"),
        ("90-bounced", "P1-plan.txt"),
        ("90-bounced", "%2E%2E%2FLEDGER.md"),
        ("90-bounced", "..%2F"),
    ],
)
def test_queue_file_rejects_invalid_stage_and_filename(api, stage, filename):
    client, _calls, _tmp = api

    response = client.get(f"/api/loops/fliessband/queue/{stage}/{filename}")

    assert response.status_code == 400, response.text


def test_queue_file_missing_file_returns_404(api):
    client, _calls, _tmp = api

    response = client.get(
        "/api/loops/fliessband/queue/90-bounced/P1-missing.md",
    )

    assert response.status_code == 404


def test_queue_file_unknown_pack_returns_404(api):
    client, _calls, _tmp = api

    response = client.get(
        "/api/loops/unknown/queue/90-bounced/P1-plan.md",
    )

    assert response.status_code == 404


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


def test_night_overrides_get_empty_then_put_and_reset(api):
    client, _calls, tmp = api
    empty = client.get("/api/loops/fliessband/night-overrides").json()
    assert empty == {"pack": "fliessband", "overrides": {}}

    body = {
        "overrides": {
            "PHASE_PLAN_ENGINE": "kimi",
            "PHASE_PLAN_MODEL": "k3",
            "PHASE_BUILD_ENGINE": "alibaba-token-plan",
            "PHASE_BUILD_MODEL": "qwen3.8-max-preview",
        }
    }
    saved = client.put("/api/loops/fliessband/night-overrides", json=body).json()
    assert saved["ok"] is True
    assert saved["pack"] == "fliessband"
    assert saved["overrides"] == body["overrides"]

    path = tmp / "state" / "fliessband" / "night-overrides.env"
    assert path.is_file()
    assert path.stat().st_mode & 0o777 == 0o600
    text = path.read_text(encoding="utf-8")
    assert "PHASE_PLAN_ENGINE=kimi" in text
    assert "PHASE_BUILD_MODEL=qwen3.8-max-preview" in text

    # Full replace: only PLAN remains.
    replaced = client.put(
        "/api/loops/fliessband/night-overrides",
        json={"overrides": {"PHASE_PLAN_ENGINE": "kimi", "PHASE_PLAN_MODEL": "k3"}},
    ).json()
    assert replaced["overrides"] == {
        "PHASE_PLAN_ENGINE": "kimi",
        "PHASE_PLAN_MODEL": "k3",
    }
    assert "PHASE_BUILD_ENGINE" not in path.read_text(encoding="utf-8")

    reset = client.put("/api/loops/fliessband/night-overrides", json={"overrides": {}}).json()
    assert reset["ok"] is True
    assert reset["overrides"] == {}
    assert not path.exists()
    assert client.get("/api/loops/fliessband/night-overrides").json()["overrides"] == {}


def test_night_overrides_reject_bad_keys_values_and_catalog(api):
    client, _calls, _tmp = api

    bad_key = client.put(
        "/api/loops/fliessband/night-overrides",
        json={"overrides": {"MAX_ROUNDS": "3"}},
    )
    assert bad_key.status_code == 400
    assert "nicht erlaubt" in bad_key.json()["detail"]

    bad_phase = client.put(
        "/api/loops/fliessband/night-overrides",
        json={"overrides": {"PHASE_NOPE_ENGINE": "claude"}},
    )
    assert bad_phase.status_code == 400
    assert "Unbekannte Phase" in bad_phase.json()["detail"]

    bad_value = client.put(
        "/api/loops/fliessband/night-overrides",
        json={"overrides": {"PHASE_PLAN_ENGINE": "claude;rm"}},
    )
    assert bad_value.status_code == 400
    assert "ungültig" in bad_value.json()["detail"]

    bad_model = client.put(
        "/api/loops/fliessband/night-overrides",
        json={
            "overrides": {
                "PHASE_PLAN_ENGINE": "kimi",
                "PHASE_PLAN_MODEL": "not-a-kimi-model",
            }
        },
    )
    assert bad_model.status_code == 400
    assert "nicht freigegeben" in bad_model.json()["detail"]

    # Engine-only with incompatible pack.yaml model must fail closed.
    bad_partial = client.put(
        "/api/loops/fliessband/night-overrides",
        json={"overrides": {"PHASE_PLAN_ENGINE": "kimi"}},
    )
    assert bad_partial.status_code == 400
    assert "nicht freigegeben" in bad_partial.json()["detail"]


def test_night_overrides_partial_pair_resolves_from_pack(api):
    client, _calls, tmp = api
    # pack.yaml PLAN is claude/claude-sonnet-5 — model-only override keeps engine.
    ok = client.put(
        "/api/loops/fliessband/night-overrides",
        json={"overrides": {"PHASE_PLAN_MODEL": "claude-opus-4-6"}},
    )
    assert ok.status_code == 200
    assert ok.json()["overrides"] == {"PHASE_PLAN_MODEL": "claude-opus-4-6"}
    path = tmp / "state" / "fliessband" / "night-overrides.env"
    assert path.read_text(encoding="utf-8").strip().endswith("PHASE_PLAN_MODEL=claude-opus-4-6")


def test_models_catalog_exposes_kimi_k3_and_alibaba(api):
    client, _calls, _tmp = api
    data = client.get("/api/loops/models").json()
    assert "k3" in data["engines"]["kimi"]["models"]
    assert "alibaba-token-plan" in data["engines"]
    assert data["engines"]["alibaba-token-plan"]["models"] == ["qwen3.8-max-preview"]


# --- Reasoning-Effort + Operator-Autoland (2026-07-28) -------------------


def test_models_route_exposes_effort_levels_per_engine(api):
    """Das UI baut sein Effort-Control aus dieser Liste. Die Wahrheit kommt aus
    der Engine-Registrierung, NICHT aus models.yaml — sonst könnte das Angebot
    vom tatsächlichen CLI-Transport abdriften."""
    client, _calls, _tmp = api
    engines = client.get("/api/loops/models").json()["engines"]
    assert engines["claude"]["effort_levels"] == ["low", "medium", "high", "xhigh", "max"]
    # Kimi transportiert keinen Effort → leere Liste → kein Control im UI.
    assert engines["kimi"]["effort_levels"] == []
    assert engines["alibaba-token-plan"]["effort_levels"] == []


def test_pack_payload_carries_effort_and_autoland_capability(api):
    client, _calls, _tmp = api
    packs = {p["name"]: p for p in client.get("/api/loops").json()["packs"]}
    pipeline, sweep = packs["fliessband"], packs["nacht"]
    plan = pipeline["phases"]["plan"]
    assert plan["effort"] is None  # Testpack setzt keinen → Engine-Default
    assert plan["effort_support"] == ["low", "medium", "high", "xhigh", "max"]
    assert pipeline["autoland_capable"] is True
    assert sweep["autoland_capable"] is False


def test_start_rejects_an_effort_the_engine_cannot_transport(api):
    client, _calls, _tmp = api
    res = client.post(
        "/api/loops/fliessband/start", json={"overrides": {"PHASE_PLAN_EFFORT": "turbo"}}
    )
    assert res.status_code == 400
    assert "turbo" in res.json()["detail"]


def test_start_validates_effort_against_a_simultaneously_swapped_engine(api):
    """PHASE_*_ENGINE und PHASE_*_EFFORT im selben Start: es gilt das Set der
    NEUEN Engine."""
    client, _calls, _tmp = api
    res = client.post(
        "/api/loops/fliessband/start",
        json={"overrides": {"PHASE_PLAN_ENGINE": "kimi", "PHASE_PLAN_EFFORT": "high"}},
    )
    assert res.status_code == 400
    assert "transportiert keinen" in res.json()["detail"]


def test_start_accepts_a_valid_effort_and_writes_it_to_overrides(api):
    client, _calls, tmp = api
    res = client.post(
        "/api/loops/fliessband/start", json={"overrides": {"PHASE_PLAN_EFFORT": "xhigh"}}
    )
    assert res.status_code == 200, res.json()
    written = (tmp / "state" / "fliessband" / "overrides.env").read_text(encoding="utf-8")
    assert "PHASE_PLAN_EFFORT=xhigh" in written


def test_start_refuses_autoland_without_the_second_key(api):
    """Fehler im Dashboard statt generischem 'Unit sofort gescheitert'."""
    client, _calls, _tmp = api
    res = client.post("/api/loops/fliessband/start", json={"overrides": {"AUTOLAND": "1"}})
    assert res.status_code == 400
    assert "AUTOLAND_ACK" in res.json()["detail"]


def test_start_refuses_a_second_key_for_a_different_pack(api):
    client, _calls, _tmp = api
    res = client.post(
        "/api/loops/fliessband/start",
        json={"overrides": {"AUTOLAND": "1", "AUTOLAND_ACK": "nacht"}},
    )
    assert res.status_code == 400


def test_start_accepts_autoland_with_the_matching_second_key(api):
    client, _calls, tmp = api
    res = client.post(
        "/api/loops/fliessband/start",
        json={"overrides": {"AUTOLAND": "1", "AUTOLAND_ACK": "fliessband"}},
    )
    assert res.status_code == 200, res.json()
    written = (tmp / "state" / "fliessband" / "overrides.env").read_text(encoding="utf-8")
    assert "AUTOLAND=1" in written
    assert "AUTOLAND_ACK=fliessband" in written


def test_start_refuses_autoland_for_a_sweep_pack(api):
    client, _calls, _tmp = api
    res = client.post(
        "/api/loops/nacht/start",
        json={"overrides": {"AUTOLAND": "1", "AUTOLAND_ACK": "nacht"}},
    )
    assert res.status_code == 400
    assert "pipeline" in res.json()["detail"]


def test_night_overrides_accept_effort_and_reject_an_invalid_one(api):
    client, _calls, _tmp = api
    ok = client.put(
        "/api/loops/fliessband/night-overrides",
        json={"overrides": {"PHASE_BUILD_EFFORT": "high"}},
    )
    assert ok.status_code == 200, ok.json()
    bad = client.put(
        "/api/loops/fliessband/night-overrides",
        json={"overrides": {"PHASE_BUILD_EFFORT": "turbo"}},
    )
    assert bad.status_code == 400
    assert "turbo" in bad.json()["detail"]


# ── Eskalationen + Landungs-Liveness (Audit 2026-08-04) ─────────────────────

def test_detail_surfaces_escalations(api):
    """Jeder Pack-Prompt beauftragt ESCALATIONS.md — gelesen hat sie niemand.

    hermes-hardening trug so 137 Zeilen echter Out-of-Scope-Funde, die nur per
    Hand-grep im State-Verzeichnis auffindbar waren.
    """
    client, _calls, tmp = api
    state = tmp / "state" / "fliessband"
    state.mkdir(parents=True, exist_ok=True)
    state.joinpath("ESCALATIONS.md").write_text(
        "# ESCALATIONS\n\n## E4 — zod strippt das error-Feld\n- Schwere: mittel\n",
        encoding="utf-8",
    )
    data = client.get("/api/loops/fliessband/detail").json()
    assert any("E4 — zod strippt das error-Feld" in line for line in data["escalations_tail"])


def test_detail_escalations_empty_without_file(api):
    """Die meisten Packs haben keine ESCALATIONS.md — das darf nicht knallen."""
    client, _calls, _tmp = api
    assert client.get("/api/loops/fliessband/detail").json()["escalations_tail"] == []


def test_land_reports_502_when_the_landing_dies_immediately(api, monkeypatch):
    """Ein sofort sterbender Landungsprozess wurde als gruener Erfolg gemeldet.

    Popen wirft nur, wenn der Interpreter fehlt; ein Prozess, der anlaeuft und
    an ImportError/kaputtem PYTHONPATH stirbt, kam nie im Ledger an — die UI
    zeigte trotzdem die Erfolgs-Notiz.
    """
    client, _calls, _tmp = api
    from hermes_cli import control_loops as cl

    class _Dead:
        def poll(self):
            return 1

    def _spawn(pack, log):
        log.write_text("ModuleNotFoundError: No module named 'loops'\n", encoding="utf-8")
        return _Dead()

    monkeypatch.setattr(cl, "_spawn_land", _spawn)
    monkeypatch.setattr(cl, "_land_failed_fast", lambda proc, probe=0.0: proc.poll())
    resp = client.post("/api/loops/nacht/land")
    assert resp.status_code == 502, resp.text
    assert "rc=1" in resp.json()["detail"]
    assert "ModuleNotFoundError" in resp.json()["detail"]


def test_land_still_succeeds_when_the_process_stays_alive(api, monkeypatch):
    client, _calls, _tmp = api
    from hermes_cli import control_loops as cl

    class _Alive:
        def poll(self):
            return None

    monkeypatch.setattr(cl, "_spawn_land", lambda pack, log: _Alive())
    monkeypatch.setattr(cl, "_land_failed_fast", lambda proc, probe=0.0: proc.poll())
    resp = client.post("/api/loops/nacht/land")
    assert resp.status_code == 200, resp.text
    assert resp.json()["land_started"] is True
