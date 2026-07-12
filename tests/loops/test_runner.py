"""Tests für loops.runner — Pack-Loader, Disposition, Git-Plumbing, Mini-Läufe.

Echte Formate statt Synthetik: das ausgelieferte builder-reviewer/pack.yaml,
Plan-Dateien im Planner-Schema, echte temp-Git-Repos. Engine-Aufrufe laufen über
eine Fake-Engine (keine CLI-Prozesse in Tests).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from loops import engines
from loops import runner as runner_module
from loops.runner import (
    PACKS_DIR,
    PHASES_BY_TYPE,
    LoopRunner,
    ManifestError,
    bump_retry,
    load_pack,
    main,
    parse_plan_frontmatter,
    parse_plan_id,
    parse_overrides,
    pass_status_matches_plan,
    parse_retry,
    parse_worktree_paths,
    read_all_ledger_stats,
    read_ledger_stats,
    resolve_packs_dir,
)

# ── Helfer ───────────────────────────────────────────────────────────────────

PLAN_BODY = """---
id: fl-20260702-beispiel
title: Beispiel-Fix
priority: P1
retry: 0
created_by: loop-planner
route: /control/loops
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

# Echte, live gebounct(e) Frontmatter-Fallgrube (2026-07-12): `title:` beginnt mit
# einem nackten `"` und läuft dann unquotiert weiter — invalides YAML. Wortgleich
# aus dem geernteten Original-Plan
# .hermes/loops/dashboard-experience/queue/00-planned/P1-loops-land-action-status-green.md
BROKEN_TITLE_PLAN = """---
id: dx-20260712-loops-land-status-green
title: "Landen"-Aktion in Loops nutzt Bronze/neutral statt Status-Grün (DESIGN-Doktrin #3)
priority: P1
retry: 1
created_by: opus-ux-planner
route: /control/loops
before_evidence: /home/piet/.hermes/loops/dashboard-experience/evidence/20260712T073115Z-before
done_when: |
  Für ein idle Pack mit unverdauten Commits (commits_ahead > 0) rendert der
  "Landen"-Trigger sowie sein Bestätigungs-Knopf ("Ja") NICHT mehr in der
  Status-Grün-Farbe (`--ln-ok` / `--color-status-ok`).
anti_scope: |
  Keine Layout-, Reihenfolge- oder Formänderung außer der Farb-/Vokabular-Korrektur.
tests: |
  web/src/control/views/LoopsView.test.tsx
files_hint: web/src/control/views/LoopsView.tsx
---
## Evidenz
Details siehe before-Evidenz.

## Ansatz
Kleinster konsistenter Fix, ausschließlich Farb-/Token-Tausch.
"""

# Gleiche Frontmatter, `title:` diesmal korrekt als EIN gequoteter Skalar mit
# escaptem internen `"` — valides YAML, entspricht der PLANNER-PROMPT-Regel.
QUOTED_TITLE_PLAN = BROKEN_TITLE_PLAN.replace(
    'title: "Landen"-Aktion in Loops nutzt Bronze/neutral statt Status-Grün (DESIGN-Doktrin #3)',
    'title: "\\"Landen\\"-Aktion in Loops nutzt Bronze/neutral statt Status-Grün (DESIGN-Doktrin #3)"',
)


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


def write_autoland_pack(packs_dir: Path, repo: Path, **overrides) -> Path:
    """Schreibt den exakten Fable→Sol→Fable-Vertrag für Loader-Tests."""
    pack_dir = write_pack(
        packs_dir, "dashboard-experience", "pipeline", repo,
        autoland=True, **overrides,
    )
    manifest_path = pack_dir / "pack.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    for phase, (engine, model, prompt_name) in runner_module.AUTOLAND_PHASE_CONTRACT.items():
        source = pack_dir / f"{phase}.md"
        (pack_dir / prompt_name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        manifest["phases"][phase].update(
            engine=engine, model=model, prompt=prompt_name,
        )
    manifest_path.write_text(
        yaml.safe_dump(manifest, allow_unicode=True), encoding="utf-8"
    )
    return pack_dir


def authorize_autoland_fixture(
    monkeypatch, packs_dir: Path, repo: Path, pack_dir: Path
) -> None:
    """Bindet die Produktionsschienen für einen expliziten Temp-Test neu."""
    manifest = pack_dir / "pack.yaml"
    monkeypatch.setattr(runner_module, "PACKS_DIR", packs_dir)
    monkeypatch.setattr(runner_module, "AUTOLAND_EXPECTED_REPO", repo.resolve())
    monkeypatch.setattr(
        runner_module,
        "AUTOLAND_MANIFEST_SHA256",
        {"dashboard-experience": runner_module.hashlib.sha256(manifest.read_bytes()).hexdigest()},
    )
    monkeypatch.setattr(
        runner_module,
        "AUTOLAND_PROMPT_SHA256",
        {
            "dashboard-experience": {
                prompt: runner_module.hashlib.sha256((pack_dir / prompt).read_bytes()).hexdigest()
                for _, _, prompt in runner_module.AUTOLAND_PHASE_CONTRACT.values()
            }
        },
    )


def load_autoland_fixture(tmp_path: Path, monkeypatch, **overrides):
    repo = init_repo(tmp_path / "repo")
    packs_dir = tmp_path / "packs"
    pack_dir = write_autoland_pack(packs_dir, repo, **overrides)
    authorize_autoland_fixture(monkeypatch, packs_dir, repo, pack_dir)
    pack = load_pack(packs_dir, "dashboard-experience")
    # Nach dem erfolgreichen Vertrags-Test nur den Prozessadapter durch den
    # registrierten Fake ersetzen; der geladene Produktionsvertrag bleibt belegt.
    for phase in pack.phases.values():
        phase.engine = "fake"
        phase.model = "fake-1"
    return repo, pack


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


def write_visual_evidence(
    state: Path, git_head: str, route: str = "/control/loops"
) -> Path:
    evidence_root = state / "evidence"
    evidence_dir = evidence_root / f"test-{len(list(evidence_root.glob('*-verifier')))}-verifier"
    evidence_dir.mkdir(parents=True)
    results = []
    for name, width, height in (
        ("mobile-390", 390, 844),
        ("tablet-820", 820, 1180),
        ("desktop-1366", 1366, 900),
    ):
        png = evidence_dir / f"control-loops-{name}.png"
        aria = evidence_dir / f"control-loops-{name}.aria.yml"
        png.write_bytes(f"png-{width}".encode())
        aria.write_text(f"- document: {route} @ {width}\n", encoding="utf-8")
        results.append(
            {
                "route": route,
                "viewport": {"name": name, "width": width, "height": height},
                "ok": True,
                "screenshotPath": str(png),
                "ariaSnapshotPath": str(aria),
                "ariaSnapshotError": None,
                "consoleErrors": [],
                "pageErrors": [],
                "overflow": {"ok": True, "scrollWidth": width, "innerWidth": width},
            }
        )
    (evidence_dir / "summary.json").write_text(
        json.dumps(
            {"ok": True, "gitHead": git_head, "routes": [route], "results": results}
        ),
        encoding="utf-8",
    )
    return evidence_dir


def ok_with_visual_evidence(status: str):
    def _run(kv, cwd):
        state = Path(kv["STATE"])
        write_visual_evidence(state, g(cwd, "rev-parse", "HEAD").stdout.strip())
        (state / "last-status").write_text(status + "\n", encoding="utf-8")
        return engines.EngineResult(rc=0, output="", usage_limit=False)
    return _run


def attest_visual_evidence(runner: LoopRunner, plan: Path) -> Path:
    evidence_dir = write_visual_evidence(runner.state, runner.rev_parse())
    ok_result, report = runner._record_visual_attestation(
        plan.read_text(encoding="utf-8"), evidence_dir
    )
    assert ok_result, report
    return evidence_dir


def commit_in(cwd: Path, name: str) -> None:
    f = cwd / "modul.py"
    old = f.read_text(encoding="utf-8") if f.exists() else ""
    f.write_text(old + f"# fix {name}\n", encoding="utf-8")
    g(cwd, "add", "-A")
    g(cwd, "commit", "-m", f"loop(test): {name}")


def commit_control_in(cwd: Path, name: str) -> None:
    """Autoland-erlaubter Testcommit innerhalb des Dashboard-Scopes."""
    target = cwd / "web" / "src" / "control" / "loop-test.ts"
    target.parent.mkdir(parents=True, exist_ok=True)
    old = target.read_text(encoding="utf-8") if target.exists() else ""
    target.write_text(old + f"// fix {name}\n", encoding="utf-8")
    g(cwd, "add", "-A")
    g(cwd, "commit", "-m", f"loop(test): {name}")


# ── (a)+(b) Manifest laden/validieren ────────────────────────────────────────

def test_shipped_builder_reviewer_pack_loads():
    pack = load_pack(PACKS_DIR, "builder-reviewer")
    assert pack.type == "pipeline"
    assert set(pack.phases) == {"plan", "build", "verify"}
    assert pack.phases["plan"].model == "claude-opus-4-8"
    assert pack.phases["build"].engine == "codex"
    assert pack.phases["build"].model == "gpt-5.6-sol"
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


def test_autoland_rejects_non_allowlisted_pack(tmp_path, fake_engine):
    repo = init_repo(tmp_path / "repo")
    write_pack(tmp_path / "packs", "lander", "sweep", repo, autoland=True)
    with pytest.raises(ManifestError, match="autoland nicht autorisiert"):
        load_pack(tmp_path / "packs", "lander")


def test_autoland_allowlist_requires_pipeline(tmp_path, fake_engine):
    repo = init_repo(tmp_path / "repo")
    write_pack(
        tmp_path / "packs", "dashboard-experience", "sweep", repo, autoland=True
    )
    with pytest.raises(ManifestError, match="type=pipeline"):
        load_pack(tmp_path / "packs", "dashboard-experience")


def test_autoland_allowlisted_pipeline_loads(tmp_path, fake_engine, monkeypatch):
    _, pack = load_autoland_fixture(tmp_path, monkeypatch)
    assert pack.autoland is True


def test_autoland_rejects_custom_copy_with_authorized_name(
    tmp_path, fake_engine, monkeypatch
):
    repo = init_repo(tmp_path / "repo")
    primary = tmp_path / "primary"
    custom = tmp_path / "custom"
    pack_dir = write_autoland_pack(custom, repo)
    authorize_autoland_fixture(monkeypatch, primary, repo, pack_dir)

    with pytest.raises(ManifestError, match="kuratierten Repo-Pack"):
        load_pack(custom, "dashboard-experience")


def test_autoland_rejects_phase_contract_drift(tmp_path, fake_engine, monkeypatch):
    repo = init_repo(tmp_path / "repo")
    packs_dir = tmp_path / "packs"
    pack_dir = write_autoland_pack(packs_dir, repo)
    authorize_autoland_fixture(monkeypatch, packs_dir, repo, pack_dir)
    manifest = yaml.safe_load((pack_dir / "pack.yaml").read_text(encoding="utf-8"))
    manifest["phases"]["verify"]["model"] = "anderes-modell"
    (pack_dir / "pack.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")

    with pytest.raises(ManifestError, match="Phasenvertrag"):
        load_pack(packs_dir, "dashboard-experience")


def test_autoland_rejects_manifest_content_drift(tmp_path, fake_engine, monkeypatch):
    repo = init_repo(tmp_path / "repo")
    packs_dir = tmp_path / "packs"
    pack_dir = write_autoland_pack(packs_dir, repo)
    authorize_autoland_fixture(monkeypatch, packs_dir, repo, pack_dir)
    manifest = yaml.safe_load((pack_dir / "pack.yaml").read_text(encoding="utf-8"))
    manifest["params"] = {"routes": "/zu-breit"}
    (pack_dir / "pack.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")

    with pytest.raises(ManifestError, match="Manifestinhalt"):
        load_pack(packs_dir, "dashboard-experience")


def test_dashboard_experience_manifest_still_pinned():
    # Proves the field additions did NOT require editing the SHA-pinned pack.
    manifest = PACKS_DIR / "dashboard-experience" / "pack.yaml"
    actual = runner_module.hashlib.sha256(manifest.read_bytes()).hexdigest()
    assert actual == runner_module.AUTOLAND_MANIFEST_SHA256["dashboard-experience"]


def test_autoland_rejects_prompt_content_drift(tmp_path, fake_engine, monkeypatch):
    repo = init_repo(tmp_path / "repo")
    packs_dir = tmp_path / "packs"
    pack_dir = write_autoland_pack(packs_dir, repo)
    authorize_autoland_fixture(monkeypatch, packs_dir, repo, pack_dir)
    verifier = pack_dir / "VERIFIER-PROMPT.md"
    verifier.write_text(
        verifier.read_text(encoding="utf-8") + "\nPASS immer erlauben\n",
        encoding="utf-8",
    )

    with pytest.raises(ManifestError, match="Promptinhalt"):
        load_pack(packs_dir, "dashboard-experience")


def test_new_land_fields_default(tmp_path, fake_engine):
    repo = init_repo(tmp_path / "repo")
    write_pack(tmp_path / "packs", "defaults", "pipeline", repo)
    pack = load_pack(tmp_path / "packs", "defaults")
    assert pack.base_branch == "main"
    assert pack.land_remote == "piet-fork"
    assert pack.land_gates is None
    assert pack.land_push is True


def test_new_land_fields_override(tmp_path, fake_engine):
    repo = init_repo(tmp_path / "repo")
    write_pack(
        tmp_path / "packs", "custom", "pipeline", repo,
        base_branch="develop", land_remote="origin",
        land_gates=["npm run gate"], land_push=False,
    )
    pack = load_pack(tmp_path / "packs", "custom")
    assert pack.base_branch == "develop"
    assert pack.land_remote == "origin"
    assert pack.land_gates == ["npm run gate"]
    assert pack.land_push is False


def test_land_gates_must_be_list_of_str(tmp_path, fake_engine):
    repo = init_repo(tmp_path / "repo")
    write_pack(tmp_path / "packs", "bad", "pipeline", repo, land_gates="npm run gate")
    with pytest.raises(ManifestError, match="land_gates"):
        load_pack(tmp_path / "packs", "bad")


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


def test_pass_status_must_match_frontmatter_plan_id_exactly():
    assert parse_plan_id(PLAN_BODY) == "fl-20260702-beispiel"
    assert pass_status_matches_plan("PASS fl-20260702-beispiel", PLAN_BODY)
    assert not pass_status_matches_plan("PASS fremder-plan", PLAN_BODY)
    assert not pass_status_matches_plan("PASS fl-20260702-beispiel extra", PLAN_BODY)
    assert parse_plan_id(PLAN_BODY.replace("id: fl-20260702-beispiel", "id: [kaputt]")) == ""


def test_broken_title_frontmatter_from_real_bounced_plan_is_unparseable():
    """Belegte Regression (2026-07-12): `title: "Landen"-Aktion …` beginnt mit
    einem nackten `"` und läuft dann unquotiert weiter — invalides YAML. Der
    fail-closed Parser liefert leer statt zu raten, damit kein impliziter PASS
    entsteht — genau das ließ vorher einen echten Verifier-PASS als
    PASS_ID_MISMATCH revertieren."""
    assert parse_plan_frontmatter(BROKEN_TITLE_PLAN) == {}
    assert parse_plan_id(BROKEN_TITLE_PLAN) == ""
    assert not pass_status_matches_plan(
        "PASS dx-20260712-loops-land-status-green", BROKEN_TITLE_PLAN
    )


def test_properly_quoted_title_frontmatter_parses_and_matches_pass():
    frontmatter = parse_plan_frontmatter(QUOTED_TITLE_PLAN)
    assert frontmatter["title"] == (
        '"Landen"-Aktion in Loops nutzt Bronze/neutral statt Status-Grün '
        "(DESIGN-Doktrin #3)"
    )
    assert parse_plan_id(QUOTED_TITLE_PLAN) == "dx-20260712-loops-land-status-green"
    assert pass_status_matches_plan(
        "PASS dx-20260712-loops-land-status-green", QUOTED_TITLE_PLAN
    )


def test_cmd_plan_bounces_plans_with_invalid_frontmatter_before_build(
    tmp_path, fake_engine
):
    """Bug #2 Teil A: ein Plan, dessen id sich nicht sicher parsen lässt, kann
    nie autolanden (pass_status_matches_plan bindet an genau diese ID) — vor
    Build+Verify verwerfen statt einen ganzen Zyklus zu verschwenden."""
    behaviors, _ = fake_engine
    repo = init_repo(tmp_path / "repo")
    write_pack(tmp_path / "packs", "planval", "pipeline", repo)
    pack = load_pack(tmp_path / "packs", "planval")
    runner = LoopRunner(pack, state_root=tmp_path / "state")

    def plan_phase(kv, cwd):
        state_dir = Path(kv["STATE"])
        (state_dir / "queue" / "00-planned" / "P1-gut.md").write_text(
            PLAN_BODY, encoding="utf-8"
        )
        (state_dir / "queue" / "00-planned" / "P1-kaputt.md").write_text(
            BROKEN_TITLE_PLAN, encoding="utf-8"
        )
        (state_dir / "last-status").write_text("PLANNED 2\n", encoding="utf-8")
        return engines.EngineResult(rc=0, output="", usage_limit=False)

    behaviors["plan"] = plan_phase

    assert runner.cmd_plan() is True
    assert runner.qcount("00-planned") == 1
    assert (runner.queue / "00-planned" / "P1-gut.md").is_file()
    assert not (runner.queue / "00-planned" / "P1-kaputt.md").exists()
    assert (runner.queue / "90-bounced" / "P1-kaputt.md").is_file()
    ledger = runner.ledger_path.read_text(encoding="utf-8")
    assert "plan-invalid: P1-kaputt.md" in ledger
    assert "unparsierbar" in ledger


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


# ── (d) pick_plan: frische Pläne vor Retries ─────────────────────────────────

def test_pick_plan_prefers_fresh_over_retry_despite_name_order(tmp_path, fake_engine):
    repo = init_repo(tmp_path / "repo")
    write_pack(tmp_path / "packs", "pick", "pipeline", repo)
    pack = load_pack(tmp_path / "packs", "pick")
    runner = LoopRunner(pack, state_root=tmp_path / "state")
    runner.ensure_dirs()
    (runner.queue / "00-planned" / "P1-x.md").write_text(
        PLAN_BODY.replace("retry: 0", "retry: 1"), encoding="utf-8"
    )
    (runner.queue / "00-planned" / "P2-y.md").write_text(PLAN_BODY, encoding="utf-8")

    picked = runner.pick_plan()
    assert picked.name == "P2-y.md"


def test_pick_plan_same_retry_falls_back_to_name_order(tmp_path, fake_engine):
    repo = init_repo(tmp_path / "repo")
    write_pack(tmp_path / "packs", "pick", "pipeline", repo)
    pack = load_pack(tmp_path / "packs", "pick")
    runner = LoopRunner(pack, state_root=tmp_path / "state")
    runner.ensure_dirs()
    (runner.queue / "00-planned" / "P2-y.md").write_text(PLAN_BODY, encoding="utf-8")
    (runner.queue / "00-planned" / "P1-x.md").write_text(PLAN_BODY, encoding="utf-8")

    picked = runner.pick_plan()
    assert picked.name == "P1-x.md"


def test_pick_plan_missing_retry_line_counts_as_zero(tmp_path, fake_engine):
    repo = init_repo(tmp_path / "repo")
    write_pack(tmp_path / "packs", "pick", "pipeline", repo)
    pack = load_pack(tmp_path / "packs", "pick")
    runner = LoopRunner(pack, state_root=tmp_path / "state")
    runner.ensure_dirs()
    (runner.queue / "00-planned" / "P1-x.md").write_text(
        PLAN_BODY.replace("retry: 0", "retry: 1"), encoding="utf-8"
    )
    no_retry_line = PLAN_BODY.replace("retry: 0\n", "")
    assert "retry:" not in no_retry_line
    (runner.queue / "00-planned" / "P2-y.md").write_text(no_retry_line, encoding="utf-8")

    picked = runner.pick_plan()
    assert picked.name == "P2-y.md"


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

    # GESTAGTE Reste (`git add -A` durchs Gate-Protokoll): checkout -- .
    # stellt aus dem Index her — ohne vorheriges reset blieben sie kleben
    # (live 2026-07-05: ABBRUCH nach usage-limit-Runde).
    (runner.wt / "README.md").write_text("staged dirty\n", encoding="utf-8")
    (runner.wt / "neu_staged.py").write_text("x = 1\n", encoding="utf-8")
    g(runner.wt, "add", "-A")
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


def test_ensure_wt_uses_base_branch(tmp_path, fake_engine):
    repo = init_repo(tmp_path / "repo")
    g(repo, "branch", "-m", "main", "trunk")
    write_pack(
        tmp_path / "packs", "wtbase", "pipeline", repo, base_branch="trunk"
    )
    pack = load_pack(tmp_path / "packs", "wtbase")
    runner = LoopRunner(pack, state_root=tmp_path / "state")

    runner.ensure_wt()

    head = g(runner.wt, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    assert head == pack.branch


def test_land_gates_custom_commands(tmp_path, fake_engine, monkeypatch):
    repo = init_repo(tmp_path / "repo")
    packs_dir = tmp_path / "packs"
    write_pack(
        packs_dir, "cgates", "pipeline", repo, land_gates=["true", "false"]
    )
    pack = load_pack(packs_dir, "cgates")
    runner = LoopRunner(pack, state_root=tmp_path / "state")

    ok, report = runner._land_gates(repo, pack.base_branch)

    assert ok is False
    assert "false" in report


def test_land_gates_custom_all_green(tmp_path, fake_engine):
    repo = init_repo(tmp_path / "repo")
    packs_dir = tmp_path / "packs"
    write_pack(packs_dir, "ggates", "pipeline", repo, land_gates=["true"])
    pack = load_pack(packs_dir, "ggates")
    runner = LoopRunner(pack, state_root=tmp_path / "state")

    ok, report = runner._land_gates(repo, pack.base_branch)

    assert ok is True


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


def test_skip_plan_override_skips_planning_phase(tmp_path, fake_engine):
    behaviors, calls = fake_engine
    repo = init_repo(tmp_path / "repo")
    write_pack(tmp_path / "packs", "skip", "pipeline", repo)
    pack = load_pack(tmp_path / "packs", "skip")
    state = tmp_path / "state" / "skip"
    state.mkdir(parents=True)
    (state / "overrides.env").write_text("SKIP_PLAN=1\n", encoding="utf-8")
    runner = LoopRunner(pack, state_root=tmp_path / "state")

    def build_phase(kv, cwd):
        assert kv["PLAN"].endswith("10-building/P1-beispiel.md")
        commit_in(cwd, "t1")
        (Path(kv["STATE"]) / "last-status").write_text("BUILT fl-20260702-beispiel\n", encoding="utf-8")
        return engines.EngineResult(rc=0, output="", usage_limit=False)

    (runner.queue / "00-planned").mkdir(parents=True, exist_ok=True)
    (runner.queue / "00-planned" / "P1-beispiel.md").write_text(PLAN_BODY, encoding="utf-8")
    behaviors["build"] = build_phase
    behaviors["verify"] = ok("PASS fl-20260702-beispiel")

    runner.cmd_night()

    assert calls == ["build", "verify"]
    assert "plan" not in calls


def test_skip_plan_override_falsy_still_plans(tmp_path, fake_engine):
    behaviors, calls = fake_engine
    repo = init_repo(tmp_path / "repo")
    write_pack(tmp_path / "packs", "noskip", "pipeline", repo)
    pack = load_pack(tmp_path / "packs", "noskip")
    state = tmp_path / "state" / "noskip"
    state.mkdir(parents=True)
    (state / "overrides.env").write_text("SKIP_PLAN=0\n", encoding="utf-8")
    runner = LoopRunner(pack, state_root=tmp_path / "state")

    def plan_phase(kv, cwd):
        state_dir = Path(kv["STATE"])
        (state_dir / "queue" / "00-planned" / "P1-beispiel.md").write_text(PLAN_BODY, encoding="utf-8")
        (state_dir / "last-status").write_text("PLANNED 1\n", encoding="utf-8")
        return engines.EngineResult(rc=0, output="", usage_limit=False)

    def build_phase(kv, cwd):
        commit_in(cwd, "t1")
        (Path(kv["STATE"]) / "last-status").write_text("BUILT fl-20260702-beispiel\n", encoding="utf-8")
        return engines.EngineResult(rc=0, output="", usage_limit=False)

    behaviors["plan"] = plan_phase
    behaviors["build"] = build_phase
    behaviors["verify"] = ok("PASS fl-20260702-beispiel")

    runner.cmd_night()

    assert calls == ["plan", "build", "verify"]


def test_cmd_night_consumes_overrides_env_after_start(tmp_path, fake_engine):
    behaviors, calls = fake_engine
    repo = init_repo(tmp_path / "repo")
    write_pack(tmp_path / "packs", "consume", "pipeline", repo)
    pack = load_pack(tmp_path / "packs", "consume")
    state = tmp_path / "state" / "consume"
    state.mkdir(parents=True)
    (state / "overrides.env").write_text("SKIP_PLAN=1\n", encoding="utf-8")
    runner = LoopRunner(pack, state_root=tmp_path / "state")

    def build_phase(kv, cwd):
        commit_in(cwd, "t1")
        (Path(kv["STATE"]) / "last-status").write_text("BUILT fl-20260702-beispiel\n", encoding="utf-8")
        return engines.EngineResult(rc=0, output="", usage_limit=False)

    (runner.queue / "00-planned").mkdir(parents=True, exist_ok=True)
    (runner.queue / "00-planned" / "P1-beispiel.md").write_text(PLAN_BODY, encoding="utf-8")
    behaviors["build"] = build_phase
    behaviors["verify"] = ok("PASS fl-20260702-beispiel")

    runner.cmd_night()

    # one-run semantics: overrides.env darf nach dem Start nicht mehr wirken.
    assert not (state / "overrides.env").is_file()
    assert (state / "overrides.consumed.env").read_text(encoding="utf-8") == "SKIP_PLAN=1\n"
    # self.overrides bleibt für den laufenden Prozess in Kraft (bereits geparst).
    assert runner.overrides.get("SKIP_PLAN") == "1"


def test_cmd_land_does_not_touch_overrides_env(tmp_path, fake_engine):
    repo = init_repo(tmp_path / "repo")
    write_pack(tmp_path / "packs", "landconsume", "pipeline", repo)
    pack = load_pack(tmp_path / "packs", "landconsume")
    state = tmp_path / "state" / "landconsume"
    state.mkdir(parents=True)
    (state / "overrides.env").write_text("PHASE_BUILD_ENGINE=codex\n", encoding="utf-8")
    runner = LoopRunner(pack, state_root=tmp_path / "state")

    runner.cmd_land()

    assert (state / "overrides.env").is_file()
    assert not (state / "overrides.consumed.env").is_file()


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


def test_verifier_nonzero_rc_cannot_land_even_if_it_writes_pass(tmp_path, fake_engine):
    behaviors, _ = fake_engine
    repo = init_repo(tmp_path / "repo")
    write_pack(
        tmp_path / "packs", "verify-rc", "pipeline", repo,
        stop={"max_rounds": 1, "max_hours": 1, "fail_streak": 1, "dry_rounds": 1},
    )
    pack = load_pack(tmp_path / "packs", "verify-rc")
    runner = LoopRunner(pack, state_root=tmp_path / "state")
    runner.ensure_dirs()
    (runner.queue / "00-planned" / "P1-beispiel.md").write_text(
        PLAN_BODY, encoding="utf-8"
    )

    def build_phase(kv, cwd):
        commit_in(cwd, "rc-guard")
        (Path(kv["STATE"]) / "last-status").write_text(
            "BUILT fl-20260702-beispiel\n", encoding="utf-8"
        )
        return engines.EngineResult(rc=0, output="", usage_limit=False)

    def broken_verifier(kv, cwd):
        (Path(kv["STATE"]) / "last-status").write_text(
            "PASS fl-20260702-beispiel\n", encoding="utf-8"
        )
        return engines.EngineResult(rc=1, output="crash", usage_limit=False)

    behaviors["build"] = build_phase
    behaviors["verify"] = broken_verifier
    runner.cmd_run()

    assert runner.qcount("20-verified") == 0
    assert "ENGINE_RC_1" in runner.ledger_path.read_text(encoding="utf-8")
    assert g(runner.wt, "diff", "main..HEAD").stdout.strip() == ""


def test_builder_pass_is_cleared_before_crashed_verifier(tmp_path, fake_engine):
    behaviors, _ = fake_engine
    repo = init_repo(tmp_path / "repo")
    write_pack(tmp_path / "packs", "stale-pass", "pipeline", repo)
    pack = load_pack(tmp_path / "packs", "stale-pass")
    runner = LoopRunner(pack, state_root=tmp_path / "state")
    runner.ensure_dirs()
    runner.ensure_wt()
    behaviors["build"] = ok("PASS fl-20260702-beispiel")
    behaviors["verify"] = lambda kv, cwd: engines.EngineResult(
        rc=1, output="crash before status", usage_limit=False
    )

    runner.run_phase("build", PLAN_PATH="unused")
    assert runner.last_status() == "PASS fl-20260702-beispiel"
    runner.run_phase("verify", PLAN_PATH="unused", RANGE="unused")
    assert runner.last_status() == ""


def test_phase_marks_worker_context_and_restores_environment(
    tmp_path, fake_engine, monkeypatch
):
    behaviors, _ = fake_engine
    repo = init_repo(tmp_path / "repo")
    write_pack(
        tmp_path / "packs", "worker-env", "sweep", repo,
        stop={"max_rounds": 1, "max_hours": 1, "fail_streak": 1, "dry_rounds": 1},
    )
    pack = load_pack(tmp_path / "packs", "worker-env")
    runner = LoopRunner(pack, state_root=tmp_path / "state")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_LOOP_WORKER", raising=False)
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    monkeypatch.delenv("GIT_CONFIG_NOSYSTEM", raising=False)

    def inspect_env(kv, cwd):
        assert os.environ["HERMES_KANBAN_TASK"] == "loop-worker-env-round"
        assert os.environ["HERMES_LOOP_WORKER"] == "1"
        status = subprocess.run(
            ["git", "-C", str(cwd), "status", "--short"],
            capture_output=True, text=True, check=False,
        )
        assert status.returncode == 0, "read-only/local git muss weiter funktionieren"
        push = subprocess.run(
            ["git", "-C", str(cwd), "push", "piet-fork", "HEAD:main"],
            capture_output=True, text=True, check=False,
        )
        assert push.returncode == 126
        assert "BLOCKED: loop worker" in push.stderr
        harmless_push_arg = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "push"],
            capture_output=True, text=True, check=False,
        )
        assert harmless_push_arg.returncode != 126
        assert "BLOCKED: loop worker" not in harmless_push_arg.stderr
        alias_push = subprocess.run(
            ["git", "-c", "alias.x=push", "x", "piet-fork", "HEAD:main"],
            cwd=str(cwd), capture_output=True, text=True, check=False,
        )
        assert alias_push.returncode == 126
        assert "keine git aliases" in alias_push.stderr
        local_alias = subprocess.run(
            ["git", "config", "alias.x", "push"],
            cwd=str(cwd), capture_output=True, text=True, check=False,
        )
        assert local_alias.returncode == 126
        assert "keine git aliases" in local_alias.stderr
        assert os.environ["GIT_CONFIG_GLOBAL"] == "/dev/null"
        assert os.environ["GIT_CONFIG_NOSYSTEM"] == "1"
        configured_pushurl = subprocess.run(
            ["git", "-C", str(cwd), "config", "--get", "remote.piet-fork.pushurl"],
            capture_output=True, text=True, check=False,
        )
        assert configured_pushurl.stdout.strip() == "disabled://loop-worker"
        return ok("DRY")(kv, cwd)

    behaviors["round"] = inspect_env
    runner.cmd_run()

    assert "HERMES_KANBAN_TASK" not in os.environ
    assert "HERMES_LOOP_WORKER" not in os.environ
    assert "GIT_CONFIG_COUNT" not in os.environ
    assert "GIT_CONFIG_GLOBAL" not in os.environ
    assert "GIT_CONFIG_NOSYSTEM" not in os.environ


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
        (Path(kv["STATE"]) / "last-status").write_text(
            "BUILT fl-20260702-beispiel\n", encoding="utf-8"
        )
        return engines.EngineResult(rc=0, output="", usage_limit=False)

    behaviors["build"] = build_and_stop
    behaviors["verify"] = ok("PASS fl-20260702-beispiel")
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
        assert pack.autoland is (name == "dashboard-experience")
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


def test_land_push_false_skips_push(tmp_path, fake_engine, monkeypatch):
    repo = init_repo(tmp_path / "repo")
    packs_dir = tmp_path / "packs"
    write_pack(
        packs_dir,
        "nopush",
        "pipeline",
        repo,
        land_gates=["true"],
        land_push=False,
    )
    pack = load_pack(packs_dir, "nopush")
    runner = LoopRunner(pack, state_root=tmp_path / "state")
    calls = []
    monkeypatch.setattr(
        runner, "_push", lambda repo: (calls.append(repo) or (True, "ok"))
    )
    runner.ensure_dirs()
    runner.ensure_wt()
    commit_in(runner.wt, "slice")
    (runner.queue / "20-verified" / "P1-fertig.md").write_text(
        PLAN_BODY, encoding="utf-8"
    )

    assert runner.cmd_land(push=True) is True
    assert calls == []


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


def test_allowlisted_night_autolands_exactly_one_verified_commit(
    tmp_path, fake_engine, monkeypatch
):
    behaviors, calls = fake_engine
    repo, pack = load_autoland_fixture(
        tmp_path, monkeypatch,
        stop={"max_rounds": 1, "max_hours": 1, "fail_streak": 1, "dry_rounds": 1},
    )
    state = tmp_path / "state" / "dashboard-experience"
    state.mkdir(parents=True)
    (state / "overrides.env").write_text(
        "MAX_ROUNDS=15\nMAX_HOURS=4\n", encoding="utf-8"
    )
    runner = LoopRunner(pack, state_root=tmp_path / "state")

    def plan_phase(kv, cwd):
        state = Path(kv["STATE"])
        (state / "queue" / "00-planned" / "P1-beispiel.md").write_text(
            PLAN_BODY, encoding="utf-8"
        )
        (state / "last-status").write_text("PLANNED 1\n", encoding="utf-8")
        return engines.EngineResult(rc=0, output="", usage_limit=False)

    def build_phase(kv, cwd):
        commit_control_in(cwd, "ux-1")
        (Path(kv["STATE"]) / "last-status").write_text(
            "BUILT fl-20260702-beispiel\n", encoding="utf-8"
        )
        return engines.EngineResult(rc=0, output="", usage_limit=False)

    behaviors["plan"] = plan_phase
    behaviors["build"] = build_phase
    behaviors["verify"] = ok_with_visual_evidence("PASS fl-20260702-beispiel")
    runner._land_gates = lambda repo, base: (True, "seamed grün")
    pushes = []
    runner._push = lambda repo: (pushes.append(str(repo)) or (True, "ok"))

    assert runner.cmd_night() is True
    assert calls == ["plan", "build", "verify"]
    assert pushes == [str(repo)]
    assert "loop(test): ux-1" in g(repo, "log", "--oneline", "-3", "main").stdout
    assert runner.qcount("00-planned") == 0
    assert runner.qcount("20-verified") == 0
    assert runner.qcount("30-landed") == 1
    assert "AUTOLAND bereit" not in runner.ledger_path.read_text(encoding="utf-8")
    assert "LAND ✅" in runner.ledger_path.read_text(encoding="utf-8")


def test_autoland_pass_without_fresh_visual_evidence_is_reverted(
    tmp_path, fake_engine, monkeypatch
):
    behaviors, _ = fake_engine
    repo, pack = load_autoland_fixture(
        tmp_path, monkeypatch,
        stop={"max_rounds": 1, "max_hours": 1, "fail_streak": 1, "dry_rounds": 1},
    )
    runner = LoopRunner(pack, state_root=tmp_path / "state")

    def plan_phase(kv, cwd):
        state = Path(kv["STATE"])
        (state / "queue" / "00-planned" / "P1-beispiel.md").write_text(
            PLAN_BODY, encoding="utf-8"
        )
        (state / "last-status").write_text("PLANNED 1\n", encoding="utf-8")
        return engines.EngineResult(rc=0, output="", usage_limit=False)

    def build_phase(kv, cwd):
        commit_control_in(cwd, "missing-visual")
        (Path(kv["STATE"]) / "last-status").write_text(
            "BUILT fl-20260702-beispiel\n", encoding="utf-8"
        )
        return engines.EngineResult(rc=0, output="", usage_limit=False)

    behaviors["plan"] = plan_phase
    behaviors["build"] = build_phase
    behaviors["verify"] = ok("PASS fl-20260702-beispiel")
    pushes = []
    runner._push = lambda repo: (pushes.append(str(repo)) or (True, "ok"))
    base = g(repo, "rev-parse", "main").stdout

    # Bug #1 (2026-07-12): der revertierte Round hinterlaesst einen netto-leeren
    # Branch (Build-Commit + Revert-Commit) UND den Retry-Plan zurueck in
    # 00-planned — _autoland_pending() ist jetzt False (nichts zu landen), also
    # wird gar nicht erst versucht zu autolanden. Vor dem Fix meldete
    # cmd_night hier faelschlich exit-4 ("unvollstaendige Landung"), obwohl der
    # fail_streak-Stop ein legitimer, erwarteter Rundenabschluss ist.
    assert runner.cmd_night() is True
    assert pushes == []
    assert g(repo, "rev-parse", "main").stdout == base
    assert runner.qcount("20-verified") == 0
    assert "VISUAL_EVIDENCE_FAIL" in runner.ledger_path.read_text(encoding="utf-8")


def test_autoland_blocks_tampered_visual_evidence(
    tmp_path, fake_engine, monkeypatch
):
    repo, pack = load_autoland_fixture(tmp_path, monkeypatch)
    runner = LoopRunner(pack, state_root=tmp_path / "state")
    runner.ensure_dirs()
    runner.ensure_wt()
    commit_control_in(runner.wt, "tampered-visual")
    plan = runner.queue / "20-verified" / "P1-fertig.md"
    plan.write_text(PLAN_BODY, encoding="utf-8")
    runner.status_path.write_text(
        "PASS fl-20260702-beispiel\n", encoding="utf-8"
    )
    evidence_dir = attest_visual_evidence(runner, plan)
    aria = sorted(evidence_dir.glob("*.aria.yml"))[0]
    aria.write_text("- document: nachtraeglich manipuliert\n", encoding="utf-8")
    base = g(repo, "rev-parse", "main").stdout

    assert runner._try_autoland("test") is False
    assert g(repo, "rev-parse", "main").stdout == base
    assert "nach Attestation verändert" in runner.ledger_path.read_text(
        encoding="utf-8"
    )


def test_visual_attestation_requires_exact_three_viewports(
    tmp_path, fake_engine, monkeypatch
):
    _, pack = load_autoland_fixture(tmp_path, monkeypatch)
    runner = LoopRunner(pack, state_root=tmp_path / "state")
    runner.ensure_dirs()
    runner.ensure_wt()
    evidence_dir = write_visual_evidence(runner.state, runner.rev_parse())
    summary_path = evidence_dir / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["results"][0]["viewport"]["width"] = 391
    summary_path.write_text(json.dumps(summary), encoding="utf-8")

    ok_result, report = runner._record_visual_attestation(PLAN_BODY, evidence_dir)

    assert ok_result is False
    assert "390" in report and "391" in report


def test_visual_attestation_binds_git_head_and_summary_paths(
    tmp_path, fake_engine, monkeypatch
):
    _, pack = load_autoland_fixture(tmp_path, monkeypatch)
    runner = LoopRunner(pack, state_root=tmp_path / "state")
    runner.ensure_dirs()
    runner.ensure_wt()
    evidence_dir = write_visual_evidence(runner.state, "0" * 40)

    ok_result, report = runner._record_visual_attestation(PLAN_BODY, evidence_dir)
    assert ok_result is False
    assert "gitHead" in report

    summary_path = evidence_dir / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["gitHead"] = runner.rev_parse()
    summary["results"][0]["screenshotPath"] = str(evidence_dir / "anderes.png")
    summary_path.write_text(json.dumps(summary), encoding="utf-8")

    ok_result, report = runner._record_visual_attestation(PLAN_BODY, evidence_dir)
    assert ok_result is False
    assert "Summary-Pfade" in report


def test_autoland_blocks_extra_commit_even_with_one_verified_plan(
    tmp_path, fake_engine, monkeypatch
):
    repo, pack = load_autoland_fixture(tmp_path, monkeypatch)
    runner = LoopRunner(pack, state_root=tmp_path / "state")
    runner.ensure_dirs()
    runner.ensure_wt()
    (runner.queue / "20-verified" / "P1-fertig.md").write_text(
        PLAN_BODY, encoding="utf-8"
    )
    commit_control_in(runner.wt, "erlaubt")
    commit_control_in(runner.wt, "extra")
    base = g(repo, "rev-parse", "main").stdout

    assert runner._try_autoland("test") is False
    assert g(repo, "rev-parse", "main").stdout == base
    assert "ahead=2" in runner.ledger_path.read_text(encoding="utf-8")


def test_autoland_blocks_commit_outside_dashboard_scope(
    tmp_path, fake_engine, monkeypatch
):
    repo, pack = load_autoland_fixture(tmp_path, monkeypatch)
    runner = LoopRunner(pack, state_root=tmp_path / "state")
    runner.ensure_dirs()
    runner.ensure_wt()
    (runner.queue / "20-verified" / "P1-fertig.md").write_text(
        PLAN_BODY, encoding="utf-8"
    )
    commit_in(runner.wt, "backend-drive-by")
    runner.status_path.write_text(
        "PASS fl-20260702-beispiel\n", encoding="utf-8"
    )
    base = g(repo, "rev-parse", "main").stdout

    assert runner._try_autoland("test") is False
    assert g(repo, "rev-parse", "main").stdout == base
    ledger = runner.ledger_path.read_text(encoding="utf-8")
    assert "außerhalb web/src/control" in ledger
    assert "modul.py" in ledger


def test_autoland_scope_detects_backend_to_dashboard_rename(
    tmp_path, fake_engine, monkeypatch
):
    repo, pack = load_autoland_fixture(tmp_path, monkeypatch)
    runner = LoopRunner(pack, state_root=tmp_path / "state")
    runner.ensure_dirs()
    runner.ensure_wt()
    (runner.queue / "20-verified" / "P1-fertig.md").write_text(
        PLAN_BODY, encoding="utf-8"
    )
    target = runner.wt / "web" / "src" / "control" / "stolen-readme.ts"
    target.parent.mkdir(parents=True, exist_ok=True)
    g(runner.wt, "mv", "README.md", str(target.relative_to(runner.wt)))
    g(runner.wt, "commit", "-m", "loop(test): disguised backend deletion")
    runner.status_path.write_text(
        "PASS fl-20260702-beispiel\n", encoding="utf-8"
    )

    assert runner._try_autoland("test") is False
    ledger = runner.ledger_path.read_text(encoding="utf-8")
    assert "README.md" in ledger, "--no-renames muss die out-of-scope Löschung zeigen"


def test_required_push_failure_rolls_back_and_preserves_verified_queue(
    tmp_path, fake_engine, monkeypatch
):
    repo, pack = load_autoland_fixture(tmp_path, monkeypatch)
    runner = LoopRunner(pack, state_root=tmp_path / "state")
    runner.ensure_dirs()
    runner.ensure_wt()
    commit_control_in(runner.wt, "push-fail")
    plan = runner.queue / "20-verified" / "P1-fertig.md"
    plan.write_text(PLAN_BODY, encoding="utf-8")
    runner.status_path.write_text("PASS fl-20260702-beispiel\n", encoding="utf-8")
    attest_visual_evidence(runner, plan)
    runner._land_gates = lambda repo, base: (True, "seamed grün")
    runner._push = lambda repo: (False, "remote unavailable")
    base = g(repo, "rev-parse", "main").stdout

    assert runner._try_autoland("test") is False
    assert g(repo, "rev-parse", "main").stdout == base
    assert plan.is_file(), "verifizierter Plan bleibt für einen späteren Resume erhalten"
    assert "Pflicht-Push fehlgeschlagen" in runner.ledger_path.read_text(encoding="utf-8")


def test_push_failure_never_rolls_back_parallel_main_commit(
    tmp_path, fake_engine, monkeypatch
):
    repo, pack = load_autoland_fixture(tmp_path, monkeypatch)
    runner = LoopRunner(pack, state_root=tmp_path / "state")
    runner.ensure_dirs()
    runner.ensure_wt()
    commit_control_in(runner.wt, "loop-commit")
    plan = runner.queue / "20-verified" / "P1-fertig.md"
    plan.write_text(PLAN_BODY, encoding="utf-8")
    runner.status_path.write_text(
        "PASS fl-20260702-beispiel\n", encoding="utf-8"
    )
    attest_visual_evidence(runner, plan)
    runner._land_gates = lambda repo, base: (True, "seamed grün")

    def concurrent_push_failure(repo_path):
        commit_in(repo_path, "foreign-main")
        return False, "remote unavailable"

    runner._push = concurrent_push_failure
    base = g(repo, "rev-parse", "main").stdout.strip()

    assert runner._try_autoland("test") is False
    current = g(repo, "rev-parse", "main").stdout.strip()
    assert current != base, "fremder main-Commit darf nicht durch Reset verschwinden"
    log = g(repo, "log", "--oneline", f"{base}..main").stdout
    assert "loop(test): loop-commit" in log
    assert "loop(test): foreign-main" in log
    assert plan.is_file()
    assert "MANUELL KLÄREN" in runner.ledger_path.read_text(encoding="utf-8")


def test_stop_file_blocks_autoland_resume(tmp_path, fake_engine, monkeypatch):
    repo, pack = load_autoland_fixture(tmp_path, monkeypatch)
    runner = LoopRunner(pack, state_root=tmp_path / "state")
    runner.ensure_dirs()
    runner.ensure_wt()
    commit_control_in(runner.wt, "angehalten")
    (runner.queue / "20-verified" / "P1-fertig.md").write_text(
        PLAN_BODY, encoding="utf-8"
    )
    runner.status_path.write_text(
        "PASS fl-20260702-beispiel\n", encoding="utf-8"
    )
    runner.stop_path.write_text("operator stop\n", encoding="utf-8")
    pushes = []
    runner._land_gates = lambda repo, base: (True, "seamed grün")
    runner._push = lambda repo: (pushes.append(str(repo)) or (True, "ok"))
    base = g(repo, "rev-parse", "main").stdout

    assert runner.cmd_night() is True
    assert g(repo, "rev-parse", "main").stdout == base
    assert pushes == []
    assert runner.stop_path.is_file()
    assert "STOP-Datei" in runner.ledger_path.read_text(encoding="utf-8")


def test_stop_set_during_verify_blocks_same_night_push(
    tmp_path, fake_engine, monkeypatch
):
    behaviors, calls = fake_engine
    repo, pack = load_autoland_fixture(
        tmp_path, monkeypatch,
        stop={"max_rounds": 1, "max_hours": 1, "fail_streak": 1, "dry_rounds": 1},
    )
    runner = LoopRunner(pack, state_root=tmp_path / "state")

    def plan_phase(kv, cwd):
        state = Path(kv["STATE"])
        (state / "queue" / "00-planned" / "P1-beispiel.md").write_text(
            PLAN_BODY, encoding="utf-8"
        )
        (state / "last-status").write_text("PLANNED 1\n", encoding="utf-8")
        return engines.EngineResult(rc=0, output="", usage_limit=False)

    def build_phase(kv, cwd):
        commit_control_in(cwd, "stop-before-land")
        (Path(kv["STATE"]) / "last-status").write_text(
            "BUILT fl-20260702-beispiel\n", encoding="utf-8"
        )
        return engines.EngineResult(rc=0, output="", usage_limit=False)

    def verify_and_stop(kv, cwd):
        state = Path(kv["STATE"])
        write_visual_evidence(state, g(cwd, "rev-parse", "HEAD").stdout.strip())
        (state / "last-status").write_text(
            "PASS fl-20260702-beispiel\n", encoding="utf-8"
        )
        (state / "STOP").write_text("operator stop\n", encoding="utf-8")
        return engines.EngineResult(rc=0, output="", usage_limit=False)

    behaviors["plan"] = plan_phase
    behaviors["build"] = build_phase
    behaviors["verify"] = verify_and_stop
    pushes = []
    runner._land_gates = lambda repo, base: (True, "seamed grün")
    runner._push = lambda repo: (pushes.append(str(repo)) or (True, "ok"))
    base = g(repo, "rev-parse", "main").stdout

    assert runner.cmd_night() is True
    assert calls == ["plan", "build", "verify"]
    assert pushes == []
    assert g(repo, "rev-parse", "main").stdout == base
    assert runner.qcount("20-verified") == 1
    assert "AUTOLAND angehalten (night)" in runner.ledger_path.read_text(
        encoding="utf-8"
    )


def test_autoland_accepts_ui_run_contract_overrides(tmp_path, fake_engine, monkeypatch):
    _, pack = load_autoland_fixture(tmp_path, monkeypatch)
    state = tmp_path / "state" / "dashboard-experience"
    state.mkdir(parents=True)
    (state / "overrides.env").write_text(
        "PHASE_PLAN_ENGINE=claude\n"
        "PHASE_PLAN_MODEL=claude-opus-4-8\n"
        "PHASE_BUILD_ENGINE=codex\n"
        "PHASE_BUILD_MODEL=gpt-5.6-sol\n"
        "PHASE_VERIFY_ENGINE=claude\n"
        "PHASE_VERIFY_MODEL=claude-opus-4-8\n"
        "MAX_ROUNDS=15\n"
        "MAX_HOURS=4\n",
        encoding="utf-8",
    )
    runner = LoopRunner(pack, state_root=tmp_path / "state")

    runner._validate_autoland_runtime()
    assert runner.phase_cfg("verify").model == "claude-opus-4-8"
    assert runner.stop_cfg("max_rounds") == 15
    assert runner.stop_cfg("max_hours") == 4
    assert runner._runtime_autoland_authorized() is True


def test_autoland_rejects_model_outside_ui_catalog(tmp_path, fake_engine, monkeypatch):
    _, pack = load_autoland_fixture(tmp_path, monkeypatch)
    state = tmp_path / "state" / "dashboard-experience"
    state.mkdir(parents=True)
    (state / "overrides.env").write_text(
        "PHASE_VERIFY_ENGINE=claude\n"
        "PHASE_VERIFY_MODEL=not-in-dashboard-catalog\n",
        encoding="utf-8",
    )
    runner = LoopRunner(pack, state_root=tmp_path / "state")

    with pytest.raises(RuntimeError, match="nicht im UI-Katalog"):
        runner._validate_autoland_runtime()


def test_autoland_custom_phase_contract_disables_automatic_landing(
    tmp_path, fake_engine, monkeypatch
):
    _, pack = load_autoland_fixture(tmp_path, monkeypatch)
    state = tmp_path / "state" / "dashboard-experience"
    state.mkdir(parents=True)
    (state / "overrides.env").write_text(
        "PHASE_BUILD_ENGINE=codex\nPHASE_BUILD_MODEL=gpt-5.5\n",
        encoding="utf-8",
    )
    runner = LoopRunner(pack, state_root=tmp_path / "state")

    runner._validate_autoland_runtime()
    assert runner._runtime_autoland_authorized() is False
    runner._prepare_runtime_land_mode()
    runner.consume_overrides()

    resumed = LoopRunner(pack, state_root=tmp_path / "state")
    assert resumed.overrides == {}
    assert resumed._manual_land_required("resume") is True


def test_autoland_rejects_fractional_budget_override(tmp_path, fake_engine, monkeypatch):
    _, pack = load_autoland_fixture(tmp_path, monkeypatch)
    state = tmp_path / "state" / "dashboard-experience"
    state.mkdir(parents=True)
    (state / "overrides.env").write_text("MAX_HOURS=1.5\n", encoding="utf-8")
    runner = LoopRunner(pack, state_root=tmp_path / "state")

    with pytest.raises(RuntimeError, match="ganze positive Zahl"):
        runner._validate_autoland_runtime()


@pytest.mark.parametrize("override", ["SKIP_PLAN=1", "PHASE_VERIFY_TIMEOUT=1", "UNKNOWN=1"])
def test_autoland_rejects_non_ui_runtime_overrides(
    tmp_path, fake_engine, monkeypatch, override
):
    _, pack = load_autoland_fixture(tmp_path, monkeypatch)
    state = tmp_path / "state" / "dashboard-experience"
    state.mkdir(parents=True)
    (state / "overrides.env").write_text(f"{override}\n", encoding="utf-8")
    runner = LoopRunner(pack, state_root=tmp_path / "state")

    with pytest.raises(RuntimeError, match="nicht erlaubte Runtime-Overrides"):
        runner._validate_autoland_runtime()


def test_autoland_resume_lands_first_and_preserves_next_run_overrides(
    tmp_path, fake_engine, monkeypatch
):
    behaviors, calls = fake_engine
    repo, pack = load_autoland_fixture(tmp_path, monkeypatch)
    state = tmp_path / "state" / "dashboard-experience"
    state.mkdir(parents=True)
    (state / "overrides.env").write_text("MAX_ROUNDS=1\n", encoding="utf-8")
    runner = LoopRunner(pack, state_root=tmp_path / "state")
    runner.ensure_dirs()
    runner.ensure_wt()
    commit_control_in(runner.wt, "resume")
    plan = runner.queue / "20-verified" / "P1-fertig.md"
    plan.write_text(
        PLAN_BODY, encoding="utf-8"
    )
    runner.status_path.write_text("PASS fl-20260702-beispiel\n", encoding="utf-8")
    attest_visual_evidence(runner, plan)
    runner._land_gates = lambda repo, base: (True, "seamed grün")
    runner._push = lambda repo: (True, "ok")

    assert runner.cmd_night() is True
    assert calls == [], "Resume landet nur; es plant nicht im selben Timer-Lauf weiter"
    assert (state / "overrides.env").is_file()
    assert not (state / "overrides.consumed.env").exists()


def test_autoland_pending_ignores_net_zero_verify_fail_revert(
    tmp_path, fake_engine, monkeypatch
):
    """Bug #1 (2026-07-12): ein verify-fail wird revertiert (Build-Commit +
    Revert-Commit). Der Branch steht dann mit ahead>0 vor main, traegt aber
    NETTO nichts zu landen — reine Commitzahl haette hier faelschlich
    'pending' gemeldet und den Resume-Zweig fuer immer festgehalten."""
    repo, pack = load_autoland_fixture(tmp_path, monkeypatch)
    runner = LoopRunner(pack, state_root=tmp_path / "state")
    runner.ensure_dirs()
    runner.ensure_wt()
    prehead = runner.rev_parse()
    commit_control_in(runner.wt, "verify-fail")
    assert runner.revert_range(prehead)

    assert runner._autoland_pending() is False

    commit_control_in(runner.wt, "echte-aenderung")
    assert runner._autoland_pending() is True


def test_autoland_resume_drains_pending_queue_instead_of_deadlocking(
    tmp_path, fake_engine, monkeypatch
):
    """Bug #1 (2026-07-12): vor dem Fix haette ein netto-leerer Branch (Build +
    Revert eines verify-fail) den Resume-Kurzschluss ausgeloest, obwohl der
    zugehoerige Retry-Plan schon wieder in 00-planned wartete — dessen Runde
    wurde NIE erreicht (_autoland_queue_ready blockt wegen planned>0). Der
    Fix laesst cmd_night in diesem Fall in den normalen Nachtlauf durchfallen
    und die wartende Queue abarbeiten."""
    behaviors, calls = fake_engine
    repo, pack = load_autoland_fixture(
        tmp_path, monkeypatch,
        stop={"max_rounds": 1, "max_hours": 1, "fail_streak": 1, "dry_rounds": 1},
    )
    runner = LoopRunner(pack, state_root=tmp_path / "state")
    runner.ensure_dirs()
    runner.ensure_wt()
    # Simuliert einen bereits abgeschlossenen verify-fail-Round.
    prehead = runner.rev_parse()
    commit_control_in(runner.wt, "verify-fail")
    assert runner.revert_range(prehead)
    # ... und der zugehörige Retry-Plan wartet bereits wieder in 00-planned
    # (genau das, was handle_fail nach einem verify-fail dort ablegt).
    (runner.queue / "00-planned" / "P1-beispiel.md").write_text(
        PLAN_BODY.replace("retry: 0", "retry: 1"), encoding="utf-8"
    )

    def plan_dry(kv, cwd):
        (Path(kv["STATE"]) / "last-status").write_text(
            "DRY /control/route\n", encoding="utf-8"
        )
        return engines.EngineResult(rc=0, output="", usage_limit=False)

    def build_phase(kv, cwd):
        commit_control_in(cwd, "ux-drain")
        (Path(kv["STATE"]) / "last-status").write_text(
            "BUILT fl-20260702-beispiel\n", encoding="utf-8"
        )
        return engines.EngineResult(rc=0, output="", usage_limit=False)

    behaviors["plan"] = plan_dry
    behaviors["build"] = build_phase
    behaviors["verify"] = ok_with_visual_evidence("PASS fl-20260702-beispiel")

    runner.cmd_night()

    assert calls == ["plan", "build", "verify"], (
        "Resume-Kurzschluss haette die wartende Retry-Queue nie erreicht (Deadlock)"
    )
    assert runner.qcount("00-planned") == 0


def test_autoland_requires_explicit_fable_pass_status(
    tmp_path, fake_engine, monkeypatch
):
    repo, pack = load_autoland_fixture(tmp_path, monkeypatch)
    runner = LoopRunner(pack, state_root=tmp_path / "state")
    runner.ensure_dirs()
    runner.ensure_wt()
    commit_control_in(runner.wt, "ohne-pass")
    (runner.queue / "20-verified" / "P1-fertig.md").write_text(
        PLAN_BODY, encoding="utf-8"
    )
    runner.status_path.write_text("FAIL reward-hacking\n", encoding="utf-8")
    base = g(repo, "rev-parse", "main").stdout

    assert runner._try_autoland("test") is False
    assert g(repo, "rev-parse", "main").stdout == base
    assert "passt nicht exakt" in runner.ledger_path.read_text(encoding="utf-8")


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


# ── Profile-/Pfad-Konsistenz ─────────────────────────────────────────────────

def test_default_paths_derive_from_hermes_home(monkeypatch):
    """DEFAULT_STATE_ROOT, CUSTOM_PACKS_DIR und NOTIFY_SCRIPT müssen aus
    get_hermes_home() kommen, nicht hartkodiert ~/.hermes verwenden."""
    import importlib

    from hermes_constants import get_hermes_home
    from loops import runner

    original_hermes_home = os.environ.get("HERMES_HOME")
    fake_home = Path("/tmp/fake-hermes-home-for-test")
    monkeypatch.setenv("HERMES_HOME", str(fake_home))
    try:
        importlib.reload(runner)
        assert runner.DEFAULT_STATE_ROOT == fake_home / "loops"
        assert runner.CUSTOM_PACKS_DIR == fake_home / "loops" / "packs-custom"
        assert runner.NOTIFY_SCRIPT == fake_home / "scripts" / "discord-notify.py"
    finally:
        # Modul wieder mit ursprünglichem HERMES_HOME laden, damit nachfolgende
        # Tests nicht mit dem Fake-Pfad laufen.
        if original_hermes_home is None:
            monkeypatch.delenv("HERMES_HOME", raising=False)
        else:
            monkeypatch.setenv("HERMES_HOME", original_hermes_home)
        importlib.reload(runner)


def _make_minimal_pack(packs_dir: Path, name: str, repo: Path) -> None:
    """Pack-Datei mit echter 'hermes'-Engine, damit load_pack ohne fake_engine funktioniert."""
    pack_dir = packs_dir / name
    pack_dir.mkdir(parents=True)
    prompt = pack_dir / "round.md"
    prompt.write_text("PHASE=round\n", encoding="utf-8")
    manifest = {
        "name": name,
        "type": "sweep",
        "repo": str(repo),
        "phases": {
            "round": {"engine": "hermes", "model": "reviewer", "timeout": 60, "prompt": "round.md"},
        },
    }
    (pack_dir / "pack.yaml").write_text(yaml.safe_dump(manifest, allow_unicode=True), encoding="utf-8")


def test_runner_rejects_missing_repo(tmp_path):
    missing = tmp_path / "nicht-da"
    _make_minimal_pack(tmp_path / "packs", "missing", missing)
    pack = load_pack(tmp_path / "packs", "missing")
    runner = LoopRunner(pack, state_root=tmp_path / "state")
    with pytest.raises(RuntimeError, match="existiert nicht"):
        runner._validate_repo()


def test_runner_rejects_non_git_repo(tmp_path):
    not_git = tmp_path / "kein-git"
    not_git.mkdir()
    _make_minimal_pack(tmp_path / "packs", "keingit", not_git)
    pack = load_pack(tmp_path / "packs", "keingit")
    runner = LoopRunner(pack, state_root=tmp_path / "state")
    with pytest.raises(RuntimeError, match="kein Git-Repository"):
        runner._validate_repo()


def test_status_survives_missing_repo(tmp_path):
    """Read-only status must not require a valid repo — construction and
    cmd_status run even when the pack repo is gone (config-drift resilience)."""
    missing = tmp_path / "nicht-da"
    _make_minimal_pack(tmp_path / "packs", "missing", missing)
    pack = load_pack(tmp_path / "packs", "missing")
    runner = LoopRunner(pack, state_root=tmp_path / "state")
    runner.cmd_status()  # must not raise


# ── Strukturiertes Ledger (ledger.jsonl) ─────────────────────────────────────

def test_ledger_event_appends_valid_jsonl(tmp_path, fake_engine):
    repo = init_repo(tmp_path / "repo")
    write_pack(tmp_path / "packs", "structured", "pipeline", repo)
    pack = load_pack(tmp_path / "packs", "structured")
    runner = LoopRunner(pack, state_root=tmp_path / "state")
    runner.ensure_dirs()

    runner.ledger_event(round=1, phase="build", verdict="ok", plan="P1-beispiel.md",
                         build_secs=12, verify_secs=None, reason=None)
    runner.ledger_event(round=2, phase="verify", verdict="fail", fail_kind="verify_fail")

    jsonl = runner.ledger_path.parent / "ledger.jsonl"
    lines = jsonl.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    events = [json.loads(line) for line in lines]
    assert events[0]["pack"] == "structured"
    assert events[0]["verdict"] == "ok"
    assert "verify_secs" not in events[0]  # None-Felder werden nicht geschrieben
    assert "reason" not in events[0]
    assert events[1]["fail_kind"] == "verify_fail"
    assert "ts" in events[0] and "ts" in events[1]
    # LEDGER.md selbst bleibt unangetastet (kein Text-Format-Drift)
    assert not runner.ledger_path.exists() or "verdict" not in runner.ledger_path.read_text(encoding="utf-8")


def test_ledger_event_is_best_effort_on_write_failure(tmp_path, monkeypatch, fake_engine):
    repo = init_repo(tmp_path / "repo")
    write_pack(tmp_path / "packs", "faulty", "pipeline", repo)
    pack = load_pack(tmp_path / "packs", "faulty")
    runner = LoopRunner(pack, state_root=tmp_path / "state")
    runner.ensure_dirs()

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "open", boom)
    runner.ledger_event(round=1, phase="build", verdict="ok")  # must not raise


def test_pipeline_happy_path_writes_structured_verified_event(tmp_path, fake_engine):
    behaviors, _ = fake_engine
    repo = init_repo(tmp_path / "repo")
    write_pack(tmp_path / "packs", "structured-happy", "pipeline", repo)
    pack = load_pack(tmp_path / "packs", "structured-happy")
    runner = LoopRunner(pack, state_root=tmp_path / "state")

    def plan_phase(kv, cwd):
        state = Path(kv["STATE"])
        (state / "queue" / "00-planned" / "P1-beispiel.md").write_text(PLAN_BODY, encoding="utf-8")
        (state / "last-status").write_text("PLANNED 1\n", encoding="utf-8")
        return engines.EngineResult(rc=0, output="", usage_limit=False)

    def build_phase(kv, cwd):
        commit_in(cwd, "t1")
        (Path(kv["STATE"]) / "last-status").write_text("BUILT fl-20260702-beispiel\n", encoding="utf-8")
        return engines.EngineResult(rc=0, output="", usage_limit=False)

    behaviors["plan"] = plan_phase
    behaviors["build"] = build_phase
    behaviors["verify"] = ok("PASS fl-20260702-beispiel")

    runner.cmd_night()

    jsonl = runner.ledger_path.parent / "ledger.jsonl"
    events = [json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines()]
    verified = [e for e in events if e["phase"] == "verify" and e["verdict"] == "ok"]
    assert len(verified) == 1
    assert verified[0]["build_secs"] is not None
    assert verified[0]["verify_secs"] is not None


def test_pipeline_verify_fail_writes_fail_and_bounced_events(tmp_path, fake_engine):
    behaviors, _ = fake_engine
    repo = init_repo(tmp_path / "repo")
    write_pack(tmp_path / "packs", "structured-bounce", "pipeline", repo)
    pack = load_pack(tmp_path / "packs", "structured-bounce")
    runner = LoopRunner(pack, state_root=tmp_path / "state")
    runner.ensure_dirs()
    (runner.queue / "00-planned" / "P1-beispiel.md").write_text(PLAN_BODY, encoding="utf-8")

    def build_phase(kv, cwd):
        commit_in(cwd, "t")
        (Path(kv["STATE"]) / "last-status").write_text("BUILT fl-20260702-beispiel\n", encoding="utf-8")
        return engines.EngineResult(rc=0, output="", usage_limit=False)

    behaviors["build"] = build_phase
    behaviors["verify"] = ok("FAIL tautologischer Test")

    runner.cmd_run()

    jsonl = runner.ledger_path.parent / "ledger.jsonl"
    events = [json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines()]
    fails = [e for e in events if e["verdict"] == "fail" and e["phase"] == "verify"]
    bounced = [e for e in events if e["verdict"] == "bounced"]
    stopped = [e for e in events if e["phase"] == "stop"]
    assert len(fails) == 2
    assert all(e["fail_kind"] == "verify_fail" for e in fails)
    assert len(bounced) == 1
    assert bounced[0]["fail_kind"] == "verify_fail"
    assert len(stopped) == 1
    assert stopped[0]["reason"] == "fail_streak"


# ── read_ledger_stats / read_all_ledger_stats ────────────────────────────────

def _write_jsonl(state_dir: Path, events: list[dict]) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "ledger.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        for event in events:
            fh.write(json.dumps(event) + "\n")
    return path


def test_read_ledger_stats_aggregates_realistic_fixture(tmp_path):
    # Feldform aus echtem /home/piet/.hermes/loops/dashboard-experience/LEDGER.md
    # geerntet: Pack "dashboard-experience", Plan-ID "P1-control-touch-targets".
    state_dir = tmp_path / "dashboard-experience"
    _write_jsonl(state_dir, [
        {"ts": "2026-07-10T00:58:00", "pack": "dashboard-experience", "round": 1,
         "phase": "build", "verdict": "fail", "plan": "P1-control-touch-targets.md",
         "fail_kind": "build_fail", "reason": "BUILD_FAIL frontend-gate-design-token-ratchet"},
        {"ts": "2026-07-10T01:08:00", "pack": "dashboard-experience", "round": 2,
         "plan": "P1-control-touch-targets.md", "phase": "fail", "verdict": "bounced",
         "fail_kind": "build_fail", "reason": "build: BUILD_FAIL"},
        {"ts": "2026-07-10T01:20:00", "pack": "dashboard-experience", "round": 3,
         "phase": "verify", "verdict": "ok", "plan": "P1-follow-up.md",
         "build_secs": 120, "verify_secs": 45},
    ])

    stats = read_ledger_stats(state_dir)
    # bounced follows the fail event for the same round -> not double-counted
    assert stats["rounds"] == 2
    assert stats["verified"] == 1
    assert stats["bounced"] == 1
    assert stats["fails_by_kind"] == {"build_fail": 1}
    assert stats["avg_build_secs"] == 120
    assert stats["avg_verify_secs"] == 45
    assert stats["last_ts"] == "2026-07-10T01:20:00"


def test_read_ledger_stats_tolerates_malformed_lines(tmp_path):
    state_dir = tmp_path / "flaky-pack"
    state_dir.mkdir(parents=True)
    path = state_dir / "ledger.jsonl"
    path.write_text(
        "not json at all\n"
        + json.dumps({"ts": "2026-07-10T00:00:00", "round": 1, "phase": "verify", "verdict": "ok"}) + "\n"
        + "\n"
        + "[1, 2, 3]\n"  # valides JSON, aber kein dict
        + json.dumps({"round": 2, "phase": "build", "verdict": "fail", "fail_kind": "build_fail"}) + "\n",
        encoding="utf-8",
    )

    stats = read_ledger_stats(state_dir)
    assert stats["rounds"] == 2
    assert stats["verified"] == 1
    assert stats["fails_by_kind"] == {"build_fail": 1}


def test_read_ledger_stats_missing_file_returns_zeroed(tmp_path):
    stats = read_ledger_stats(tmp_path / "nicht-vorhanden")
    assert stats == {
        "rounds": 0, "verified": 0, "fails_by_kind": {}, "blocked_by_kind": {},
        "bounced": 0, "avg_build_secs": None, "avg_verify_secs": None, "last_ts": None,
    }


def test_read_ledger_stats_counts_blocked_by_kind(tmp_path):
    state_dir = tmp_path / "blocked-pack"
    _write_jsonl(state_dir, [
        {"round": 1, "phase": "sweep", "verdict": "blocked", "fail_kind": "usage_limit"},
        {"round": 1, "phase": "sweep", "verdict": "blocked", "fail_kind": "usage_limit"},
        {"round": 2, "phase": "verify", "verdict": "blocked", "fail_kind": "build_fail"},
        {"round": 3, "phase": "sweep", "verdict": "blocked"},  # kein fail_kind -> "unknown"
    ])

    stats = read_ledger_stats(state_dir)
    assert stats["blocked_by_kind"] == {"usage_limit": 2, "build_fail": 1, "unknown": 1}
    # blocked events are rounds that produced an outcome, but not fails
    assert stats["fails_by_kind"] == {}
    assert stats["rounds"] == 4


def test_read_ledger_stats_rounds_is_outcome_event_count_not_distinct_round(tmp_path):
    # Append-only runs restart round numbering at R1 each invocation — a
    # distinct-round-number set collapses across runs. "rounds" must count
    # outcome events instead.
    state_dir = tmp_path / "restarting-pack"
    _write_jsonl(state_dir, [
        {"round": 1, "phase": "verify", "verdict": "ok"},
        {"round": 2, "phase": "verify", "verdict": "ok"},
        # second run restarts at round 1
        {"round": 1, "phase": "verify", "verdict": "ok"},
    ])

    stats = read_ledger_stats(state_dir)
    assert stats["rounds"] == 3
    assert stats["verified"] == 3


def test_read_ledger_stats_skips_wrong_typed_fields_without_discarding_all(tmp_path):
    state_dir = tmp_path / "poisoned-pack"
    state_dir.mkdir(parents=True)
    path = state_dir / "ledger.jsonl"
    path.write_text(
        json.dumps({"round": [], "phase": "build", "verdict": "fail", "fail_kind": ["x"]}) + "\n"
        + json.dumps({"round": 1, "phase": "verify", "verdict": "ok"}) + "\n"
        # absurd int duration: float conversion during averaging would overflow
        + json.dumps({"round": 2, "phase": "verify", "verdict": "ok", "build_secs": 10**400}) + "\n",
        encoding="utf-8",
    )

    stats = read_ledger_stats(state_dir)
    # the poisoned lines must not raise and must not wipe the good line's stats
    assert stats["verified"] == 2
    assert stats["avg_build_secs"] is None
    # the coerced fail line counts as an outcome round too (2 ok + 1 fail)
    assert stats["rounds"] == 3
    assert stats["fails_by_kind"] == {"unknown": 1}


def test_read_all_ledger_stats_maps_pack_name_to_stats(tmp_path):
    root = tmp_path / "loops-state"
    _write_jsonl(root / "pack-a", [{"round": 1, "phase": "verify", "verdict": "ok"}])
    _write_jsonl(root / "pack-b", [{"round": 1, "phase": "build", "verdict": "fail", "fail_kind": "build_fail"}])
    (root / "pack-c-empty").mkdir(parents=True)  # kein ledger.jsonl — muss übersprungen werden

    stats = read_all_ledger_stats(root)
    assert set(stats) == {"pack-a", "pack-b"}
    assert stats["pack-a"]["verified"] == 1
    assert stats["pack-b"]["fails_by_kind"] == {"build_fail": 1}


def test_read_all_ledger_stats_missing_root_returns_empty(tmp_path):
    assert read_all_ledger_stats(tmp_path / "nicht-da") == {}

# ── Nacht-Basis-Refresh (stale Worktree-Base) ───────────────────────────────
# 2026-07-10: der dashboard-experience-Worktree stand auf einem alten
# main-Stand, dessen Ratchet-Regression längst auf main gefixt war — der
# Build erbte den Defekt, klassifizierte ihn als "vorbestand" und stoppte
# mit Fail-Streak. cmd_night rebased seither VOR der Nacht auf main (gleiche
# Schienen wie beim Landen: nur clean, Anker-Tag, Konflikt → alte Basis).


def _night_refresh_setup(tmp_path, fake_engine, name, overrides_text=None):
    behaviors, calls = fake_engine
    repo = init_repo(tmp_path / "repo")
    write_pack(tmp_path / "packs", name, "pipeline", repo)
    pack = load_pack(tmp_path / "packs", name)
    state = tmp_path / "state" / name
    state.mkdir(parents=True)
    if overrides_text:
        (state / "overrides.env").write_text(overrides_text, encoding="utf-8")
    runner = LoopRunner(pack, state_root=tmp_path / "state")
    runner.ensure_dirs()
    runner.ensure_wt()  # Worktree entsteht auf dem AKTUELLEN main …
    # … dann läuft main weiter (der "Fix", den die Nacht erben muss).
    (repo / "fix_auf_main.py").write_text("fixed = True\n", encoding="utf-8")
    g(repo, "add", "-A")
    g(repo, "commit", "-m", "fix auf main nach worktree-erstellung")

    seen = {}

    def build_phase(kv, cwd):
        seen["fix_in_worktree"] = (Path(cwd) / "fix_auf_main.py").is_file()
        commit_in(cwd, "t1")
        (Path(kv["STATE"]) / "last-status").write_text(
            "BUILT fl-20260702-beispiel\n", encoding="utf-8"
        )
        return engines.EngineResult(rc=0, output="", usage_limit=False)

    (runner.queue / "00-planned").mkdir(parents=True, exist_ok=True)
    (runner.queue / "00-planned" / "P1-beispiel.md").write_text(
        PLAN_BODY, encoding="utf-8"
    )
    behaviors["build"] = build_phase
    behaviors["verify"] = ok("PASS fl-20260702-beispiel")
    return repo, runner, seen


def test_night_refreshes_stale_worktree_base(tmp_path, fake_engine):
    repo, runner, seen = _night_refresh_setup(tmp_path, fake_engine, "refresh")

    runner.cmd_night(skip_plan=True)

    assert seen.get("fix_in_worktree") is True, (
        "Build lief auf der stalen Basis — main-Fix fehlte im Worktree"
    )
    assert g(repo, "tag", "-l", "loop-rebase/*").stdout.strip(), "Rebase-Anker fehlt"
    assert "BASE-REFRESH" in runner.ledger_path.read_text(encoding="utf-8")


def test_night_base_refresh_override_skips(tmp_path, fake_engine):
    repo, runner, seen = _night_refresh_setup(
        tmp_path, fake_engine, "keinrefresh",
        overrides_text="SKIP_BASE_REFRESH=1\n",
    )

    runner.cmd_night(skip_plan=True)

    assert seen.get("fix_in_worktree") is False, (
        "Override gesetzt, aber es wurde trotzdem rebased"
    )
    assert g(repo, "tag", "-l", "loop-rebase/*").stdout.strip() == ""


def test_night_base_refresh_skips_dirty_worktree_but_runs(tmp_path, fake_engine):
    repo, runner, seen = _night_refresh_setup(tmp_path, fake_engine, "dirtyref")
    (runner.wt / "unfertig.txt").write_text("dirty\n", encoding="utf-8")

    runner.cmd_night(skip_plan=True)

    # Kein Rebase auf dirty Worktree — aber die Nacht läuft trotzdem.
    assert seen.get("fix_in_worktree") is False
    ledger = runner.ledger_path.read_text(encoding="utf-8")
    assert "BASE-REFRESH übersprungen" in ledger
    assert "dirty" in ledger
