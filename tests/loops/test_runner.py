"""Tests für loops.runner — Pack-Loader, Disposition, Git-Plumbing, Mini-Läufe.

Echte Formate statt Synthetik: das ausgelieferte builder-reviewer/pack.yaml,
Plan-Dateien im Planner-Schema, echte temp-Git-Repos. Engine-Aufrufe laufen über
eine Fake-Engine (keine CLI-Prozesse in Tests).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from loops import engines
from loops.runner import (
    PACKS_DIR,
    PHASES_BY_TYPE,
    LoopRunner,
    ManifestError,
    bump_retry,
    load_pack,
    main,
    parse_overrides,
    parse_retry,
    parse_worktree_paths,
    resolve_packs_dir,
)

# ── Helfer ───────────────────────────────────────────────────────────────────

PLAN_BODY = """---
id: fl-20260702-beispiel
title: Beispiel-Fix
priority: P1
retry: 0
created_by: loop-planner
done_when: |
  pytest tests/test_x.py ist gruen und war vorher rot
anti_scope: |
  nichts ausserhalb modul.py
tests: |
  tests/test_x.py
files_hint: modul.py
---
## Kontext & Schwachstelle
Evidenz: modul.py:42 wirft bei leerem Input.

## Ansatz
Guard einbauen + Regressionstest.
"""


def g(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, encoding="utf-8", check=False,
    )


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    g(path, "init", "-b", "main")
    g(path, "config", "user.email", "loop@test")
    g(path, "config", "user.name", "loop-test")
    (path / "README.md").write_text("hallo\n", encoding="utf-8")
    g(path, "add", "-A")
    g(path, "commit", "-m", "init")
    return path


def write_pack(packs_dir: Path, name: str, ptype: str, repo: Path, **overrides) -> Path:
    """Temp-Pack mit Fake-Engine-Prompts, die Phase+Pfade maschinenlesbar tragen."""
    pack_dir = packs_dir / name
    pack_dir.mkdir(parents=True)
    phases = {}
    for pname in PHASES_BY_TYPE[ptype]:
        prompt = pack_dir / f"{pname}.md"
        lines = [f"PHASE={pname}", "STATE={{STATE_DIR}}", "WT={{WT}}", "PARAMS={{PARAMS}}"]
        if pname == "plan":
            lines.append("HAS_WEB={{HAS_WEB}}")
        if pname in ("build", "verify"):
            lines.append("PLAN={{PLAN_PATH}}")
        if pname == "verify":
            lines.append("RANGE={{RANGE}}")
        prompt.write_text("\n".join(lines) + "\n", encoding="utf-8")
        phases[pname] = {"engine": "fake", "model": "fake-1", "timeout": 60, "prompt": f"{pname}.md"}
    manifest = {
        "name": name, "type": ptype, "repo": str(repo), "phases": phases,
        "stop": {"max_rounds": 6, "max_hours": 1, "fail_streak": 2, "dry_rounds": 2},
        **overrides,
    }
    (pack_dir / "pack.yaml").write_text(yaml.safe_dump(manifest, allow_unicode=True), encoding="utf-8")
    return pack_dir


def parse_kv(prompt: str) -> dict[str, str]:
    out = {}
    for line in prompt.splitlines():
        if "=" in line:
            key, val = line.split("=", 1)
            out[key] = val
    return out


@pytest.fixture
def fake_engine(monkeypatch):
    """Registriert Engine 'fake'; Verhalten pro Phase via behaviors-Dict setzbar."""
    calls: list[str] = []
    behaviors: dict = {}

    def run(model, prompt, cwd, timeout_s):
        kv = parse_kv(prompt)
        calls.append(kv["PHASE"])
        return behaviors[kv["PHASE"]](kv, Path(cwd))

    monkeypatch.setitem(engines.ENGINES, "fake", run)
    return behaviors, calls


def ok(status: str):
    """Behavior-Fabrik: schreibt last-status und meldet Erfolg."""
    def _run(kv, cwd):
        (Path(kv["STATE"]) / "last-status").write_text(status + "\n", encoding="utf-8")
        return engines.EngineResult(rc=0, output="", usage_limit=False)
    return _run


def commit_in(cwd: Path, name: str) -> None:
    f = cwd / "modul.py"
    old = f.read_text(encoding="utf-8") if f.exists() else ""
    f.write_text(old + f"# fix {name}\n", encoding="utf-8")
    g(cwd, "add", "-A")
    g(cwd, "commit", "-m", f"loop(test): {name}")


# ── (a)+(b) Manifest laden/validieren ────────────────────────────────────────

def test_shipped_builder_reviewer_pack_loads():
    pack = load_pack(PACKS_DIR, "builder-reviewer")
    assert pack.type == "pipeline"
    assert set(pack.phases) == {"plan", "build", "verify"}
    assert pack.phases["plan"].model == "claude-fable-5"
    assert pack.phases["build"].model == "claude-sonnet-5"
    assert pack.stop["fail_streak"] == 2
    assert pack.stop["dry_rounds"] == 2  # Default gemerged
    assert pack.branch == "loop/builder-reviewer"
    assert pack.autoland is False


def test_shipped_blank_template_loads():
    pack = load_pack(PACKS_DIR, "_blank")
    assert pack.type == "sweep"
    assert set(pack.phases) == {"round"}


def test_missing_pack_lists_available():
    with pytest.raises(ManifestError, match="gibt-es-nicht"):
        load_pack(PACKS_DIR, "gibt-es-nicht")


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        ({"type": "zirkus"}, "pipeline|sweep"),
        ({"repo": ""}, "repo"),
        ({"name": "anders"}, "Ordnernamen"),
    ],
)
def test_broken_manifest_fields(tmp_path, fake_engine, mutation, match):
    repo = init_repo(tmp_path / "repo")
    pack_dir = write_pack(tmp_path / "packs", "kaputt", "sweep", repo)
    manifest = yaml.safe_load((pack_dir / "pack.yaml").read_text(encoding="utf-8"))
    manifest.update(mutation)
    (pack_dir / "pack.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    with pytest.raises(ManifestError, match=match):
        load_pack(tmp_path / "packs", "kaputt")


def test_manifest_phase_errors(tmp_path, fake_engine):
    repo = init_repo(tmp_path / "repo")
    pack_dir = write_pack(tmp_path / "packs", "kaputt", "pipeline", repo)
    manifest = yaml.safe_load((pack_dir / "pack.yaml").read_text(encoding="utf-8"))
    # Falsche Phasenmenge für den Archetyp
    broken = dict(manifest)
    broken["phases"] = {"plan": manifest["phases"]["plan"]}
    (pack_dir / "pack.yaml").write_text(yaml.safe_dump(broken), encoding="utf-8")
    with pytest.raises(ManifestError, match="genau die Phasen"):
        load_pack(tmp_path / "packs", "kaputt")
    # Unbekannte Engine
    broken = dict(manifest)
    broken["phases"] = dict(manifest["phases"])
    broken["phases"]["build"] = dict(manifest["phases"]["build"], engine="warpantrieb")
    (pack_dir / "pack.yaml").write_text(yaml.safe_dump(broken), encoding="utf-8")
    with pytest.raises(ManifestError, match="warpantrieb"):
        load_pack(tmp_path / "packs", "kaputt")
    # Fehlende Prompt-Datei
    broken = dict(manifest)
    broken["phases"] = dict(manifest["phases"])
    broken["phases"]["build"] = dict(manifest["phases"]["build"], prompt="fehlt.md")
    (pack_dir / "pack.yaml").write_text(yaml.safe_dump(broken), encoding="utf-8")
    with pytest.raises(ManifestError, match="Prompt-Datei fehlt"):
        load_pack(tmp_path / "packs", "kaputt")


def test_autoland_is_forced_off_in_v1(tmp_path, fake_engine, capsys):
    repo = init_repo(tmp_path / "repo")
    write_pack(tmp_path / "packs", "lander", "sweep", repo, autoland=True)
    pack = load_pack(tmp_path / "packs", "lander")
    assert pack.autoland is False
    assert "autoland" in capsys.readouterr().err


# ── (c) Retry-Disposition auf echter Plan-Datei ──────────────────────────────

def test_parse_and_bump_retry(tmp_path):
    plan = tmp_path / "P1-beispiel.md"
    plan.write_text(PLAN_BODY, encoding="utf-8")
    assert parse_retry(plan.read_text(encoding="utf-8")) == 0
    assert bump_retry(plan) == 1
    text = plan.read_text(encoding="utf-8")
    assert text.count("retry:") == 1
    assert "retry: 1\n" in text
    # Frontmatter-Rest unversehrt
    assert "created_by: loop-planner" in text


def test_handle_fail_retry_then_bounce(tmp_path, fake_engine):
    repo = init_repo(tmp_path / "repo")
    write_pack(tmp_path / "packs", "disp", "pipeline", repo)
    pack = load_pack(tmp_path / "packs", "disp")
    runner = LoopRunner(pack, state_root=tmp_path / "state")
    runner.ensure_dirs()
    plan = runner.queue / "10-building" / "P1-beispiel.md"
    plan.write_text(PLAN_BODY, encoding="utf-8")

    assert runner.handle_fail(plan, "verify: FAIL tautologisch") == "retry"
    retried = runner.queue / "00-planned" / "P1-beispiel.md"
    assert retried.is_file()
    text = retried.read_text(encoding="utf-8")
    assert "retry: 1" in text and "## Loop-Fail" in text

    retried.rename(plan)
    assert runner.handle_fail(plan, "verify: FAIL erneut") == "bounced"
    bounced = runner.queue / "90-bounced" / "P1-beispiel.md"
    assert bounced.is_file()
    assert bounced.read_text(encoding="utf-8").count("## Loop-Fail") == 2


# ── (e) Git-Plumbing gegen echtes Repo ───────────────────────────────────────

def test_ensure_wt_guard_clean_and_revert(tmp_path, fake_engine):
    repo = init_repo(tmp_path / "repo")
    write_pack(tmp_path / "packs", "gitp", "sweep", repo)
    pack = load_pack(tmp_path / "packs", "gitp")
    runner = LoopRunner(pack, state_root=tmp_path / "state")
    runner.ensure_wt()
    assert runner.wt.is_dir()
    assert g(runner.wt, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "loop/gitp"
    runner.ensure_wt()  # idempotent
    assert g(repo, "worktree", "list", "--porcelain").stdout.count("worktree ") == 2

    # guard_clean räumt tracked UND untracked Reste (Driver-Ebene, hook-frei)
    (runner.wt / "README.md").write_text("dirty\n", encoding="utf-8")
    (runner.wt / "neu.tmp").write_text("rest\n", encoding="utf-8")
    assert runner.guard_clean() is True
    assert g(runner.wt, "status", "--porcelain").stdout.strip() == ""

    # revert_range: Branch bleibt 'verified oder reverted'
    prehead = runner.rev_parse()
    commit_in(runner.wt, "t1")
    assert runner.rev_parse() != prehead
    assert runner.revert_range(prehead) is True
    assert (runner.wt / "modul.py").exists() is False or "fix t1" not in (
        runner.wt / "modul.py"
    ).read_text(encoding="utf-8")


def test_parse_worktree_paths():
    porcelain = (
        "worktree /home/x/repo\nHEAD abc\nbranch refs/heads/main\n\n"
        "worktree /home/x/state/wt\nHEAD def\nbranch refs/heads/loop/p\n"
    )
    assert parse_worktree_paths(porcelain) == ["/home/x/repo", "/home/x/state/wt"]


# ── Overrides ────────────────────────────────────────────────────────────────

def test_overrides_env_switches_model_and_limits(tmp_path, fake_engine):
    repo = init_repo(tmp_path / "repo")
    write_pack(tmp_path / "packs", "over", "pipeline", repo)
    pack = load_pack(tmp_path / "packs", "over")
    state = tmp_path / "state" / "over"
    state.mkdir(parents=True)
    (state / "overrides.env").write_text(
        "# Kommentar\nPHASE_BUILD_MODEL=claude-haiku-4-5\nMAX_ROUNDS=3\n",
        encoding="utf-8",
    )
    runner = LoopRunner(pack, state_root=tmp_path / "state")
    assert runner.phase_cfg("build").model == "claude-haiku-4-5"
    assert runner.phase_cfg("plan").model == "fake-1"  # unverändert
    assert runner.stop_cfg("max_rounds") == 3
    assert parse_overrides(state / "fehlt.env") == {}


# ── (f) Pipeline-Mini-Läufe mit Fake-Engine ──────────────────────────────────

def test_pipeline_happy_path_plan_build_verify(tmp_path, fake_engine):
    behaviors, calls = fake_engine
    repo = init_repo(tmp_path / "repo")
    write_pack(tmp_path / "packs", "happy", "pipeline", repo)
    pack = load_pack(tmp_path / "packs", "happy")
    runner = LoopRunner(pack, state_root=tmp_path / "state")

    def plan_phase(kv, cwd):
        state = Path(kv["STATE"])
        (state / "queue" / "00-planned" / "P1-beispiel.md").write_text(PLAN_BODY, encoding="utf-8")
        (state / "last-status").write_text("PLANNED 1\n", encoding="utf-8")
        assert kv["HAS_WEB"] in ("0", "1")
        return engines.EngineResult(rc=0, output="", usage_limit=False)

    def build_phase(kv, cwd):
        assert kv["PLAN"].endswith("10-building/P1-beispiel.md")
        commit_in(cwd, "t1")
        (Path(kv["STATE"]) / "last-status").write_text("BUILT fl-20260702-beispiel\n", encoding="utf-8")
        return engines.EngineResult(rc=0, output="", usage_limit=False)

    behaviors["plan"] = plan_phase
    behaviors["build"] = build_phase
    behaviors["verify"] = ok("PASS fl-20260702-beispiel")

    runner.cmd_night()

    assert calls == ["plan", "build", "verify"]
    assert (runner.queue / "20-verified" / "P1-beispiel.md").is_file()
    log = g(runner.wt, "log", "--oneline", "main..loop/happy").stdout
    assert "loop(test): t1" in log
    ledger = runner.ledger_path.read_text(encoding="utf-8")
    assert "verified" in ledger and "PLAN: 1" in ledger


def test_pipeline_verify_fail_reverts_then_bounces(tmp_path, fake_engine):
    behaviors, calls = fake_engine
    repo = init_repo(tmp_path / "repo")
    write_pack(tmp_path / "packs", "bounce", "pipeline", repo)
    pack = load_pack(tmp_path / "packs", "bounce")
    runner = LoopRunner(pack, state_root=tmp_path / "state")
    runner.ensure_dirs()
    (runner.queue / "00-planned" / "P1-beispiel.md").write_text(PLAN_BODY, encoding="utf-8")
    builds = []

    def build_phase(kv, cwd):
        builds.append(1)
        commit_in(cwd, f"t{len(builds)}")
        (Path(kv["STATE"]) / "last-status").write_text("BUILT fl-20260702-beispiel\n", encoding="utf-8")
        return engines.EngineResult(rc=0, output="", usage_limit=False)

    behaviors["build"] = build_phase
    behaviors["verify"] = ok("FAIL tautologischer Test")

    runner.cmd_run()

    bounced = runner.queue / "90-bounced" / "P1-beispiel.md"
    assert bounced.is_file(), "nach 2. verify-FAIL muss der Plan bouncen"
    assert "retry: 1" in bounced.read_text(encoding="utf-8")
    assert len(builds) == 2  # 1 Erstbau + 1 Retry, dann Fail-Streak-Stop
    # Branch-Invariante: jeder Build-Commit hat seinen Revert
    log = g(runner.wt, "log", "--oneline", "main..loop/bounce").stdout
    assert log.count("Revert") == 2
    # Arbeitsbaum netto unverändert gegenüber main
    assert g(runner.wt, "diff", "main..HEAD", "--", "modul.py").stdout.strip() == ""


def test_pipeline_build_fail_without_commit(tmp_path, fake_engine):
    behaviors, calls = fake_engine
    repo = init_repo(tmp_path / "repo")
    write_pack(tmp_path / "packs", "bfail", "pipeline", repo)
    pack = load_pack(tmp_path / "packs", "bfail")
    runner = LoopRunner(pack, state_root=tmp_path / "state")
    runner.ensure_dirs()
    (runner.queue / "00-planned" / "P1-beispiel.md").write_text(PLAN_BODY, encoding="utf-8")

    def build_fail(kv, cwd):
        (cwd / "halbfertig.py").write_text("kaputt\n", encoding="utf-8")  # Phase-Rest
        (Path(kv["STATE"]) / "last-status").write_text("BUILD_FAIL gates rot\n", encoding="utf-8")
        return engines.EngineResult(rc=0, output="", usage_limit=False)

    behaviors["build"] = build_fail
    runner.cmd_run()

    assert calls.count("build") == 2  # Retry, dann Fail-Streak
    assert calls.count("verify") == 0
    assert (runner.queue / "90-bounced" / "P1-beispiel.md").is_file()
    # guard_clean hat die Phase-Reste geräumt
    assert g(runner.wt, "status", "--porcelain").stdout.strip() == ""


def test_stop_file_halts_between_rounds(tmp_path, fake_engine):
    behaviors, calls = fake_engine
    repo = init_repo(tmp_path / "repo")
    write_pack(tmp_path / "packs", "stopper", "pipeline", repo)
    pack = load_pack(tmp_path / "packs", "stopper")
    runner = LoopRunner(pack, state_root=tmp_path / "state")
    runner.ensure_dirs()
    for i in (1, 2):
        (runner.queue / "00-planned" / f"P1-plan{i}.md").write_text(PLAN_BODY, encoding="utf-8")

    def build_and_stop(kv, cwd):
        commit_in(cwd, "t1")
        (Path(kv["STATE"]) / "STOP").write_text("", encoding="utf-8")
        (Path(kv["STATE"]) / "last-status").write_text("BUILT x\n", encoding="utf-8")
        return engines.EngineResult(rc=0, output="", usage_limit=False)

    behaviors["build"] = build_and_stop
    behaviors["verify"] = ok("PASS x")
    runner.cmd_run()

    assert calls.count("build") == 1, "STOP muss vor Runde 2 greifen"
    assert runner.qcount("00-planned") == 1


# ── Sweep-Archetyp ───────────────────────────────────────────────────────────

def test_sweep_stops_after_dry_rounds(tmp_path, fake_engine):
    behaviors, calls = fake_engine
    repo = init_repo(tmp_path / "repo")
    write_pack(tmp_path / "packs", "sweepy", "sweep", repo)
    pack = load_pack(tmp_path / "packs", "sweepy")
    runner = LoopRunner(pack, state_root=tmp_path / "state")
    behaviors["round"] = ok("DRY")
    runner.cmd_run()
    assert calls == ["round", "round"]  # dry_rounds=2


def test_sweep_stops_on_blocked_streak(tmp_path, fake_engine):
    behaviors, calls = fake_engine
    repo = init_repo(tmp_path / "repo")
    write_pack(tmp_path / "packs", "blocky", "sweep", repo)
    pack = load_pack(tmp_path / "packs", "blocky")
    runner = LoopRunner(pack, state_root=tmp_path / "state")
    behaviors["round"] = ok("BLOCKED kein sicherer Fix")
    runner.cmd_run()
    assert calls == ["round", "round"]  # fail_streak=2


def test_sweep_writes_heartbeat_current_and_history(tmp_path, fake_engine):
    import json

    behaviors, calls = fake_engine
    repo = init_repo(tmp_path / "repo")
    write_pack(tmp_path / "packs", "pulsig", "sweep", repo)
    pack = load_pack(tmp_path / "packs", "pulsig")
    runner = LoopRunner(pack, state_root=tmp_path / "state")
    behaviors["round"] = ok("DRY")
    runner.cmd_run()
    hb = json.loads((runner.state / "heartbeat.json").read_text(encoding="utf-8"))
    assert hb["current"] is None, "nach Phasen-Ende darf keine Phase als aktiv stehen"
    assert len(hb["last"]) == 2 and hb["last"][0]["phase"] == "round"
    assert {"secs", "rc", "at", "engine", "model"} <= set(hb["last"][0])


def test_sweep_stops_on_usage_limit(tmp_path, fake_engine):
    behaviors, calls = fake_engine
    repo = init_repo(tmp_path / "repo")
    write_pack(tmp_path / "packs", "limity", "sweep", repo)
    pack = load_pack(tmp_path / "packs", "limity")
    runner = LoopRunner(pack, state_root=tmp_path / "state")

    def round_limit(kv, cwd):
        return engines.EngineResult(
            rc=1, output="You've hit your session limit · resets 9:50pm", usage_limit=True
        )

    behaviors["round"] = round_limit
    runner.cmd_run()
    assert calls == ["round"], "Usage-Limit muss sofort stoppen — kein Spinnen"


# ── Regressionen aus der adversarialen Review 2026-07-02 ────────────────────

def test_bump_retry_inserts_missing_retry_line(tmp_path):
    # Blocker 2: Planner-LLM garantiert das Schema nicht — ohne retry-Zeile war
    # der Bump ein stiller No-Op und der Plan bounct nie.
    plan = tmp_path / "P1-ohne-retry.md"
    plan.write_text("---\nid: fl-x\ntitle: t\n---\nBody\n", encoding="utf-8")
    assert bump_retry(plan) == 1
    text = plan.read_text(encoding="utf-8")
    assert parse_retry(text) == 1
    assert bump_retry(plan) == 2  # zweiter Bump zählt jetzt hoch, kein No-Op


def test_build_usage_limit_with_commit_is_marked_unverified(tmp_path, fake_engine):
    # Blocker 1: Commit + Usage-Limit im Build darf nicht spurlos bleiben —
    # Plan bleibt in 10-building, Ledger weist UNVERIFIED aus.
    behaviors, calls = fake_engine
    repo = init_repo(tmp_path / "repo")
    write_pack(tmp_path / "packs", "ulimit", "pipeline", repo)
    pack = load_pack(tmp_path / "packs", "ulimit")
    runner = LoopRunner(pack, state_root=tmp_path / "state")
    runner.ensure_dirs()
    (runner.queue / "00-planned" / "P1-beispiel.md").write_text(PLAN_BODY, encoding="utf-8")

    def build_commit_then_limit(kv, cwd):
        commit_in(cwd, "teilarbeit")
        return engines.EngineResult(
            rc=1, output="You've hit your session limit · resets 9:50pm", usage_limit=True
        )

    behaviors["build"] = build_commit_then_limit
    runner.cmd_run()

    assert calls == ["build"], "nach Usage-Limit keine weitere Phase"
    assert (runner.queue / "10-building" / "P1-beispiel.md").is_file()
    ledger = runner.ledger_path.read_text(encoding="utf-8")
    assert "UNVERIFIED" in ledger and "usage-limit" in ledger


def test_ensure_wt_heals_stale_registration(tmp_path, fake_engine):
    # Major 4: Worktree-Dir gelöscht, aber noch registriert → prune + neu anlegen.
    import shutil

    repo = init_repo(tmp_path / "repo")
    write_pack(tmp_path / "packs", "stale", "sweep", repo)
    pack = load_pack(tmp_path / "packs", "stale")
    runner = LoopRunner(pack, state_root=tmp_path / "state")
    runner.ensure_wt()
    shutil.rmtree(runner.wt)
    runner.ensure_wt()
    assert runner.wt.is_dir()
    assert g(runner.wt, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "loop/stale"


def test_sweep_cleans_phase_leftovers_between_rounds(tmp_path, fake_engine):
    # Major 3: Sweep muss wie die Pipeline vor jeder Runde guard_clean fahren.
    behaviors, calls = fake_engine
    repo = init_repo(tmp_path / "repo")
    write_pack(tmp_path / "packs", "muell", "sweep", repo)
    pack = load_pack(tmp_path / "packs", "muell")
    runner = LoopRunner(pack, state_root=tmp_path / "state")
    seen_leftover = []

    def round_leaves_mess(kv, cwd):
        seen_leftover.append((cwd / "muell.tmp").exists())
        (cwd / "muell.tmp").write_text("rest\n", encoding="utf-8")
        (Path(kv["STATE"]) / "last-status").write_text("DRY\n", encoding="utf-8")
        return engines.EngineResult(rc=0, output="", usage_limit=False)

    behaviors["round"] = round_leaves_mess
    runner.cmd_run()
    assert seen_leftover == [False, False], "Reste der Vorrunde müssen weggeräumt sein"


def test_bad_numeric_override_falls_back_instead_of_crashing(tmp_path, fake_engine):
    repo = init_repo(tmp_path / "repo")
    write_pack(tmp_path / "packs", "badov", "pipeline", repo)
    pack = load_pack(tmp_path / "packs", "badov")
    state = tmp_path / "state" / "badov"
    state.mkdir(parents=True)
    (state / "overrides.env").write_text("PHASE_BUILD_TIMEOUT=abc\nMAX_ROUNDS=zwei\n", encoding="utf-8")
    runner = LoopRunner(pack, state_root=tmp_path / "state")
    assert runner.phase_cfg("build").timeout == pack.phases["build"].timeout
    assert runner.stop_cfg("max_rounds") == pack.stop["max_rounds"]


# ── Custom-Packs-Suchpfad (Werkstatt-Substrat v2.1) ──────────────────────────

def test_resolve_packs_dir_prefers_repo_then_custom(tmp_path, fake_engine):
    repo = init_repo(tmp_path / "repo")
    primary = tmp_path / "primary"
    custom = tmp_path / "custom"
    write_pack(primary, "nur-repo", "sweep", repo)
    write_pack(custom, "nur-custom", "sweep", repo)
    assert resolve_packs_dir("nur-repo", primary, custom) == primary
    assert resolve_packs_dir("nur-custom", primary, custom) == custom
    # unbekannt → primary (load_pack liefert dann die klare Fehlermeldung)
    assert resolve_packs_dir("gibtsnicht", primary, custom) == primary
    with pytest.raises(ManifestError, match="ungültig"):
        resolve_packs_dir("../boese", primary, custom)


def test_resolve_packs_dir_rejects_collision(tmp_path, fake_engine):
    repo = init_repo(tmp_path / "repo")
    primary = tmp_path / "primary"
    custom = tmp_path / "custom"
    write_pack(primary, "doppelt", "sweep", repo)
    write_pack(custom, "doppelt", "sweep", repo)
    with pytest.raises(ManifestError, match="doppelt"):
        resolve_packs_dir("doppelt", primary, custom)


# ── Pack-Lint: JEDES ausgelieferte Pack muss laden und den Konventionen genügen ─

def test_all_shipped_packs_load_and_validate():
    names = sorted(p.name for p in PACKS_DIR.iterdir() if p.is_dir())
    assert "builder-reviewer" in names and "_blank" in names
    for name in names:
        pack = load_pack(PACKS_DIR, name)
        assert pack.autoland is False, f"{name}: autoland ist in v1 verboten"
        assert pack.stability in ("stable", "experimental"), f"{name}: stability ungültig"
        assert pack.description, f"{name}: description fehlt"
        for pname, phase in pack.phases.items():
            text = (pack.pack_dir / phase.prompt).read_text(encoding="utf-8")
            assert "{{STATE_DIR}}" in text, f"{name}/{pname}: STATE_DIR-Platzhalter fehlt"
            assert "last-status" in text, f"{name}/{pname}: last-status-Protokoll fehlt"
            assert "push" in text.lower(), f"{name}/{pname}: Verbote-Block fehlt (push)"


# ── Landung (v2.3 Stufe 1) — Schienen gegen echte temp-Repos ─────────────────

def make_landable(tmp_path, name="landeplatz"):
    repo = init_repo(tmp_path / "repo")
    write_pack(tmp_path / "packs", name, "sweep", repo)
    pack = load_pack(tmp_path / "packs", name)
    runner = LoopRunner(pack, state_root=tmp_path / "state")
    runner.ensure_dirs()
    runner.ensure_wt()
    commit_in(runner.wt, "l1")  # Branch 1 Commit vor main
    runner._land_gates = lambda repo, base: (True, "seamed grün")  # Seam
    pushes = []
    runner._push = lambda repo: (pushes.append(str(repo)) or (True, "ok"))
    return repo, runner, pushes


def test_land_happy_path_merges_tags_archives_and_freshens(tmp_path, fake_engine):
    repo, runner, pushes = make_landable(tmp_path)
    (runner.queue / "20-verified" / "P1-fertig.md").write_text(PLAN_BODY, encoding="utf-8")

    assert runner.cmd_land(push=True) is True
    assert "loop(test): l1" in g(repo, "log", "--oneline", "-3", "main").stdout
    assert g(repo, "tag", "-l", "loop-land/landeplatz/*").stdout.strip()
    assert (runner.queue / "30-landed" / "P1-fertig.md").is_file()
    assert runner.qcount("20-verified") == 0
    assert pushes, "piet-fork-Push muss versucht werden"
    # FRESH: Branch neu von neuem main gezogen → nichts mehr ahead
    assert g(repo, "rev-list", "--count", f"main..{runner.pack.branch}").stdout.strip() == "0"
    assert "LAND ✅" in runner.ledger_path.read_text(encoding="utf-8")


def test_land_aborts_on_dirty_live_checkout(tmp_path, fake_engine):
    repo, runner, pushes = make_landable(tmp_path)
    (repo / "README.md").write_text("fremde parallele arbeit\n", encoding="utf-8")
    before = g(repo, "rev-parse", "main").stdout
    assert runner.cmd_land() is False
    assert g(repo, "rev-parse", "main").stdout == before
    assert not pushes


def test_land_auto_rebases_clean_divergence(tmp_path, fake_engine):
    repo, runner, pushes = make_landable(tmp_path)
    # main läuft konfliktfrei weiter → früher Abbruch, jetzt Auto-Rebase + Land
    (repo / "anders.py").write_text("x = 1\n", encoding="utf-8")
    g(repo, "add", "-A")
    g(repo, "commit", "-m", "parallel auf main")
    assert runner.cmd_land(push=True) is True
    log = g(repo, "log", "--oneline", "-5", "main").stdout
    assert "loop(test): l1" in log and "parallel auf main" in log
    assert g(repo, "tag", "-l", "loop-rebase/*").stdout.strip(), "Rebase-Anker fehlt"
    assert "auto-rebase" in runner.ledger_path.read_text(encoding="utf-8")
    assert pushes, "piet-fork-Push muss versucht werden"


def test_land_aborts_on_rebase_conflict(tmp_path, fake_engine):
    repo, runner, pushes = make_landable(tmp_path)
    # Gleiche Datei auf main UND Loop-Branch → Rebase-Konflikt → Abbruch wie heute
    (repo / "konflikt.txt").write_text("main-seite\n", encoding="utf-8")
    g(repo, "add", "-A")
    g(repo, "commit", "-m", "parallel auf main")
    (runner.wt / "konflikt.txt").write_text("loop-seite\n", encoding="utf-8")
    g(runner.wt, "add", "-A")
    g(runner.wt, "commit", "-m", "loop(test): konflikt")
    main_before = g(repo, "rev-parse", "main").stdout
    branch_before = g(repo, "rev-parse", runner.pack.branch).stdout
    assert runner.cmd_land() is False
    assert g(repo, "rev-parse", "main").stdout == main_before
    assert (
        g(repo, "rev-parse", runner.pack.branch).stdout == branch_before
    ), "rebase --abort muss den Branch unverändert lassen"
    assert g(repo, "tag", "-l", "loop-land/*").stdout.strip() == "", "ff-Anker muss weg"
    assert g(repo, "tag", "-l", "loop-rebase/*").stdout.strip() == "", "Rebase-Anker muss weg"
    assert "Auto-Rebase-Konflikt" in runner.ledger_path.read_text(encoding="utf-8")
    assert not pushes


def test_land_aborts_rebase_when_pack_worktree_dirty(tmp_path, fake_engine):
    repo, runner, pushes = make_landable(tmp_path)
    (repo / "anders.py").write_text("x = 1\n", encoding="utf-8")
    g(repo, "add", "-A")
    g(repo, "commit", "-m", "parallel auf main")
    (runner.wt / "unfertig.txt").write_text("dirty\n", encoding="utf-8")
    main_before = g(repo, "rev-parse", "main").stdout
    assert runner.cmd_land() is False
    assert g(repo, "rev-parse", "main").stdout == main_before
    assert not pushes
    ledger = runner.ledger_path.read_text(encoding="utf-8")
    assert "dirty" in ledger, "Abbruch muss den Dirty-Grund ins LEDGER schreiben"


def test_land_rolls_back_when_gates_fail(tmp_path, fake_engine):
    repo, runner, pushes = make_landable(tmp_path)
    base = g(repo, "rev-parse", "main").stdout.strip()
    runner._land_gates = lambda repo, b: (False, "affected rot (rc=1):\nboom")
    assert runner.cmd_land() is False
    assert g(repo, "rev-parse", "main").stdout.strip() == base, "reset --keep auf Anker-Stand"
    assert not pushes
    assert "rollback" in runner.ledger_path.read_text(encoding="utf-8")


def test_land_aborts_on_unverified_work(tmp_path, fake_engine):
    repo, runner, pushes = make_landable(tmp_path)
    (runner.queue / "10-building" / "P1-offen.md").write_text(PLAN_BODY, encoding="utf-8")
    before = g(repo, "rev-parse", "main").stdout
    assert runner.cmd_land() is False
    assert g(repo, "rev-parse", "main").stdout == before


def test_land_noop_when_nothing_ahead(tmp_path, fake_engine):
    repo = init_repo(tmp_path / "repo")
    write_pack(tmp_path / "packs", "leer", "sweep", repo)
    pack = load_pack(tmp_path / "packs", "leer")
    runner = LoopRunner(pack, state_root=tmp_path / "state")
    assert runner.cmd_land() is True  # nichts zu tun ist kein Fehler


# ── CLI ──────────────────────────────────────────────────────────────────────

def test_cli_status_is_readonly_on_fresh_state(tmp_path, capsys):
    rc = main(["--pack", "builder-reviewer", "--cmd", "status",
               "--state-root", str(tmp_path / "leer")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "builder-reviewer" in out and "fehlt" in out
    # status legt weder State-Verzeichnis noch Worktree an
    assert not (tmp_path / "leer" / "builder-reviewer" / "wt").exists()


def test_cli_unknown_pack_exits_2(tmp_path, capsys):
    rc = main(["--pack", "nope", "--cmd", "status", "--state-root", str(tmp_path)])
    assert rc == 2
    assert "MANIFEST-FEHLER" in capsys.readouterr().err
