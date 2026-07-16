from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import hermes_cli.kanban_db as kanban_db
import hermes_cli.projects_db as projects_db
from hermes_cli.projects_overview import (
    ProjectEntry,
    ProjectsRegistry,
    build_projects_payload,
    load_projects_registry,
    register_projects_routes,
)

# Verbatim copy of the REAL ~/.hermes/projects.yaml content (2026-07-16) so the
# "valid" test exercises the exact on-disk format, not a synthetic simplification.
_REAL_PROJECTS_YAML = """\
# Projekt-Registry für den /control "Projekte"-Tab (Leitstand).
# Runtime-Config — NICHT im Repo. Gelesen von hermes_cli/projects_overview.py.
# Felder pro Projekt:
#   slug            eindeutiger Kurzname (Pflicht)
#   name            Anzeigename (Pflicht)
#   repo_path       Git-Checkout (Pflicht)
#   kanban_project  Board-Slug in ~/.hermes/projects.db ('default' = Hermes-Board) oder null
#   loop_packs      Loop-Pack-Namen unter ~/.hermes/loops/
#   links           [{label, url}] — optional
#   parent          slug des Elternprojekts (Unterprojekt) — optional
#   path_filters    Pfad-Präfixe/Dateien im Eltern-Repo, die zu diesem Unterprojekt gehören — optional
projects:
  - slug: hermes-infra
    name: Hermes Infra
    repo_path: /home/piet/.hermes/hermes-agent
    kanban_project: default
    loop_packs:
      - builder-reviewer
      - dashboard-experience
      - dashboard-polish
      - doc-sweep
      - error-sweep
      - loop-schmiede
      - loops-date-audit
      - test-stabiliser
      - xai-hard-gate
    links:
      - label: Control-Dashboard
        url: /control
  - slug: diktat
    name: Diktat
    repo_path: /home/piet/.hermes/hermes-agent
    parent: hermes-infra
    path_filters:
      - android/hermes-dictate
      - web/src/control/views/DiktatView.tsx
    kanban_project: null
    loop_packs: []
  - slug: health-track
    name: Health Track
    repo_path: /home/piet/projects/health-track
    kanban_project: health-track
    loop_packs:
      - health-track-ux
      - ht-defect-hunt
      - ht-perf
      - ht-ux-polish
  - slug: family-organizer
    name: Family Organizer
    repo_path: /home/piet/projects/family-organizer
    kanban_project: null
    loop_packs: []
  - slug: oma-galerie
    name: Oma-Galerie
    repo_path: /home/piet/projects/oma-galerie
    kanban_project: null
    loop_packs: []
  - slug: llm-wiki
    name: LLM-Wiki
    repo_path: /home/piet/llm-wiki
    kanban_project: null
    loop_packs: []
"""


def _write(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "projects.yaml"
    path.write_text(content, encoding="utf-8")
    return path


def test_valid_real_format_parses_all_entries(tmp_path: Path) -> None:
    path = _write(tmp_path, _REAL_PROJECTS_YAML)

    result = load_projects_registry(path)

    assert result.errors == []
    assert [p.slug for p in result.projects] == [
        "hermes-infra",
        "diktat",
        "health-track",
        "family-organizer",
        "oma-galerie",
        "llm-wiki",
    ]

    hermes_infra = next(p for p in result.projects if p.slug == "hermes-infra")
    assert "builder-reviewer" in hermes_infra.loop_packs
    assert hermes_infra.kanban_project == "default"
    assert len(hermes_infra.links) == 1
    assert hermes_infra.links[0].label == "Control-Dashboard"
    assert hermes_infra.links[0].url == "/control"

    diktat = next(p for p in result.projects if p.slug == "diktat")
    assert diktat.parent == "hermes-infra"
    assert diktat.path_filters == [
        "android/hermes-dictate",
        "web/src/control/views/DiktatView.tsx",
    ]
    assert diktat.kanban_project is None

    health_track = next(p for p in result.projects if p.slug == "health-track")
    assert health_track.kanban_project == "health-track"


def test_missing_file_returns_empty_no_error(tmp_path: Path) -> None:
    path = tmp_path / "does-not-exist.yaml"

    result = load_projects_registry(path)

    assert result.projects == []
    assert result.errors == []


def test_broken_yaml_returns_error_not_exception(tmp_path: Path) -> None:
    path = _write(tmp_path, "projects: [unclosed")

    result = load_projects_registry(path)

    assert result.projects == []
    assert len(result.errors) == 1


def test_top_level_plain_list_returns_error(tmp_path: Path) -> None:
    path = _write(tmp_path, "- slug: foo\n  name: Foo\n  repo_path: /tmp/foo\n")

    result = load_projects_registry(path)

    assert result.projects == []
    assert len(result.errors) == 1


def test_top_level_projects_wrong_type_returns_error(tmp_path: Path) -> None:
    path = _write(tmp_path, "projects: nope\n")

    result = load_projects_registry(path)

    assert result.projects == []
    assert len(result.errors) == 1


def test_invalid_entry_skipped_valid_entries_survive(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """\
projects:
  - slug: good-one
    name: Good One
    repo_path: /tmp/good-one
  - slug: broken
    name: Broken Entry
""",
    )

    result = load_projects_registry(path)

    assert [p.slug for p in result.projects] == ["good-one"]
    assert len(result.errors) == 1
    assert "broken" in result.errors[0]


def test_duplicate_slug_first_wins(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """\
projects:
  - slug: dup
    name: First
    repo_path: /tmp/first
  - slug: dup
    name: Second
    repo_path: /tmp/second
""",
    )

    result = load_projects_registry(path)

    assert [p.name for p in result.projects] == ["First"]
    assert len(result.errors) == 1
    assert "dup" in result.errors[0]


# ---------------------------------------------------------------------------
# Stage 2 — /api/projects payload (git / kanban / loops sources)
# ---------------------------------------------------------------------------


def _entry(**overrides: object) -> ProjectEntry:
    defaults: dict[str, object] = dict(
        slug="proj",
        name="Proj",
        repo_path="/nonexistent",
        kanban_project=None,
        loop_packs=[],
        links=[],
        parent=None,
        path_filters=[],
    )
    defaults.update(overrides)
    return ProjectEntry(**defaults)  # type: ignore[arg-type]


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


def _init_repo_with_commit(repo: Path, *, committed_at: int, message: str = "initial commit") -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    date_str = f"{committed_at} +0000"
    import os

    env = dict(os.environ)
    env["GIT_AUTHOR_DATE"] = date_str
    env["GIT_COMMITTER_DATE"] = date_str
    _git(repo, "commit", "-q", "-m", message, env=env)


def test_git_source_real_repo_reports_hash_message_age(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    committed_at = 1_700_000_000
    _init_repo_with_commit(repo, committed_at=committed_at, message="feat: real commit")

    entry = _entry(repo_path=str(repo))
    registry = ProjectsRegistry(projects=[entry], errors=[])
    payload = build_projects_payload(registry, now=committed_at + 120)

    project = payload["projects"][0]
    assert project["errors"] == []
    last_commit = project["last_commit"]
    assert last_commit is not None
    assert len(last_commit["hash"]) == 9
    assert last_commit["message"] == "feat: real commit"
    assert last_commit["committed_at"] == committed_at
    assert last_commit["age_seconds"] == 120


def test_git_source_missing_repo_path_is_isolated(tmp_path: Path) -> None:
    entry = _entry(repo_path=str(tmp_path / "does-not-exist"))
    registry = ProjectsRegistry(projects=[entry], errors=[])
    payload = build_projects_payload(registry, now=int(time.time()))

    project = payload["projects"][0]
    assert project["last_commit"] is None
    assert any(e.startswith("git:") for e in project["errors"])
    # Other fields must still be populated (no explosion of the whole entry).
    assert project["kanban"] is None
    assert project["loops"] == {"active": 0, "packs": []}


def test_git_source_path_filters_report_subproject_last_commit(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    parent_ts = 1_700_000_000
    _init_repo_with_commit(repo, committed_at=parent_ts, message="parent: unrelated change")

    # A later commit touching only the subproject's path.
    sub_dir = repo / "sub"
    sub_dir.mkdir()
    (sub_dir / "file.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "sub/file.txt")
    sub_ts = parent_ts + 3600
    import os

    env = dict(os.environ)
    env["GIT_AUTHOR_DATE"] = f"{sub_ts} +0000"
    env["GIT_COMMITTER_DATE"] = f"{sub_ts} +0000"
    _git(repo, "commit", "-q", "-m", "sub: touch subproject", env=env)

    # And a later still commit touching only the parent's other files.
    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    later_ts = sub_ts + 3600
    env2 = dict(os.environ)
    env2["GIT_AUTHOR_DATE"] = f"{later_ts} +0000"
    env2["GIT_COMMITTER_DATE"] = f"{later_ts} +0000"
    _git(repo, "commit", "-q", "-m", "parent: another unrelated change", env=env2)

    entry = _entry(repo_path=str(repo), path_filters=["sub"])
    registry = ProjectsRegistry(projects=[entry], errors=[])
    payload = build_projects_payload(registry, now=later_ts + 60)

    last_commit = payload["projects"][0]["last_commit"]
    assert last_commit is not None
    assert last_commit["message"] == "sub: touch subproject"
    assert last_commit["committed_at"] == sub_ts


# --- kanban source ----------------------------------------------------------


def _make_kanban_db(path: Path) -> None:
    kanban_db.init_db(db_path=path)


def _insert_task(
    db_path: Path,
    *,
    task_id: str,
    status: str,
    project_id: str | None,
    created_at: int,
    completed_at: int | None = None,
) -> None:
    conn = kanban_db.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks (id, title, status, project_id, created_at, completed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, f"task {task_id}", status, project_id, created_at, completed_at),
        )
        conn.commit()
    finally:
        conn.close()


def _make_projects_db(path: Path, *, name: str, board_slug: str) -> str:
    conn = projects_db.connect(path)
    try:
        pid = projects_db.create_project(conn, name=name, board_slug=board_slug)
    finally:
        conn.close()
    return pid


def test_kanban_source_default_board_buckets_and_done_7d_boundary(tmp_path: Path) -> None:
    kdb = tmp_path / "kanban.db"
    pdb = tmp_path / "projects.db"
    _make_kanban_db(kdb)
    pid = _make_projects_db(pdb, name="Hermes Infra", board_slug="default")

    now = 1_700_100_000
    seven_days = 7 * 24 * 3600

    # Legacy task with NULL project_id must count for the default board.
    _insert_task(kdb, task_id="t1", status="todo", project_id=None, created_at=now - 100)
    _insert_task(kdb, task_id="t2", status="running", project_id=pid, created_at=now - 100)
    _insert_task(kdb, task_id="t3", status="blocked", project_id=pid, created_at=now - 100)
    _insert_task(kdb, task_id="t4", status="review", project_id=pid, created_at=now - 100)
    # Completed 1 day ago -> counts.
    _insert_task(
        kdb, task_id="t5", status="done", project_id=pid,
        created_at=now - 200, completed_at=now - 86400,
    )
    # Completed 8 days ago -> must NOT count.
    _insert_task(
        kdb, task_id="t6", status="done", project_id=pid,
        created_at=now - 200, completed_at=now - (seven_days + 86400),
    )
    # A task from a different (unbound) project must not leak in.
    _insert_task(kdb, task_id="t7", status="todo", project_id="other-project", created_at=now - 50)

    entry = _entry(kanban_project="default")
    registry = ProjectsRegistry(projects=[entry], errors=[])
    payload = build_projects_payload(
        registry, kanban_db_path=kdb, projects_db_path=pdb, now=now
    )

    kanban = payload["projects"][0]["kanban"]
    assert kanban == {"open": 1, "running": 1, "blocked": 1, "review": 1, "done_7d": 1}


def test_kanban_source_named_board_scopes_by_project_id_only(tmp_path: Path) -> None:
    kdb = tmp_path / "kanban.db"
    pdb = tmp_path / "projects.db"
    _make_kanban_db(kdb)
    pid = _make_projects_db(pdb, name="Health Track", board_slug="health-track")

    now = 1_700_100_000
    _insert_task(kdb, task_id="a1", status="todo", project_id=pid, created_at=now - 10)
    # Legacy NULL project_id must NOT leak into a non-default board's counts.
    _insert_task(kdb, task_id="a2", status="todo", project_id=None, created_at=now - 10)

    entry = _entry(kanban_project="health-track")
    registry = ProjectsRegistry(projects=[entry], errors=[])
    payload = build_projects_payload(
        registry, kanban_db_path=kdb, projects_db_path=pdb, now=now
    )

    kanban = payload["projects"][0]["kanban"]
    assert kanban["open"] == 1


def test_kanban_project_none_yields_null_no_error(tmp_path: Path) -> None:
    entry = _entry(repo_path=str(tmp_path), kanban_project=None)
    registry = ProjectsRegistry(projects=[entry], errors=[])
    payload = build_projects_payload(
        registry,
        kanban_db_path=tmp_path / "kanban.db",
        projects_db_path=tmp_path / "projects.db",
        now=int(time.time()),
    )
    project = payload["projects"][0]
    assert project["kanban"] is None
    # kanban_project is None -> resolving it never touches the DB, so it must
    # not add a "kanban:" error on top of the (unrelated) non-git repo_path.
    assert not any(e.startswith("kanban:") for e in project["errors"])


def test_kanban_project_unresolvable_board_is_isolated(tmp_path: Path) -> None:
    kdb = tmp_path / "kanban.db"
    pdb = tmp_path / "projects.db"
    _make_kanban_db(kdb)
    # projects.db exists but has no project bound to "ghost-board".
    _make_projects_db(pdb, name="Something Else", board_slug="other-board")

    entry = _entry(kanban_project="ghost-board")
    registry = ProjectsRegistry(projects=[entry], errors=[])
    payload = build_projects_payload(
        registry, kanban_db_path=kdb, projects_db_path=pdb, now=int(time.time())
    )

    project = payload["projects"][0]
    assert project["kanban"] is None
    assert any(e.startswith("kanban:") for e in project["errors"])


# --- loops source ------------------------------------------------------------


def _write_heartbeat(state_dir: Path, *, started_at: str, last_at: list[str]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    heartbeat = {
        "current": {
            "phase": "build",
            "engine": "kimi",
            "model": "kimi-code/kimi-for-coding",
            "started_at": started_at,
            "timeout": 3600,
            "round": 1,
        },
        "last": [
            {"phase": "plan", "engine": "claude", "model": "claude-fable-5", "secs": 356, "rc": 0, "at": at}
            for at in last_at
        ],
    }
    (state_dir / "heartbeat.json").write_text(json.dumps(heartbeat), encoding="utf-8")


def test_loops_source_running_pack_reports_heartbeat(tmp_path: Path) -> None:
    state_root = tmp_path / "loops"
    pack_dir = state_root / "dashboard-experience"
    _write_heartbeat(
        pack_dir,
        started_at="2026-07-16T21:37:59Z",
        last_at=["2026-07-16T19:25:36Z", "2026-07-16T19:59:39Z"],
    )
    lock = pack_dir / ".lock"
    lock.write_text("", encoding="utf-8")

    import fcntl

    fh = lock.open("r+", encoding="utf-8")
    fcntl.flock(fh, fcntl.LOCK_EX)
    try:
        entry = _entry(loop_packs=["dashboard-experience"])
        registry = ProjectsRegistry(projects=[entry], errors=[])
        payload = build_projects_payload(
            registry, loops_state_root=state_root, now=int(time.time())
        )
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()

    loops = payload["projects"][0]["loops"]
    assert loops["active"] == 1
    pack = loops["packs"][0]
    assert pack["name"] == "dashboard-experience"
    assert pack["running"] is True
    expected_epoch = int(
        __import__("datetime").datetime.fromisoformat("2026-07-16T21:37:59+00:00").timestamp()
    )
    assert pack["last_heartbeat_at"] == expected_epoch


def test_loops_source_missing_state_dir_is_isolated(tmp_path: Path) -> None:
    state_root = tmp_path / "loops"
    entry = _entry(repo_path=str(tmp_path), loop_packs=["never-ran-pack"])
    registry = ProjectsRegistry(projects=[entry], errors=[])
    payload = build_projects_payload(registry, loops_state_root=state_root, now=int(time.time()))

    loops = payload["projects"][0]["loops"]
    assert loops == {
        "active": 0,
        "packs": [{"name": "never-ran-pack", "running": False, "last_heartbeat_at": None}],
    }
    # A missing state dir is a normal "never ran yet" state, not an error.
    assert not any(e.startswith("loops:") for e in payload["projects"][0]["errors"])


# --- endpoint ----------------------------------------------------------------


def test_endpoint_returns_200_with_empty_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "hermes_cli.projects_overview.get_hermes_home", lambda: tmp_path
    )
    app = FastAPI()
    register_projects_routes(app)
    client = TestClient(app)

    resp = client.get("/api/projects")
    assert resp.status_code == 200
    body = resp.json()
    assert body["projects"] == []
    assert body["registry_errors"] == []
    assert isinstance(body["generated_at"], int)


# --- isolation across multiple projects --------------------------------------


def test_isolation_one_broken_project_does_not_affect_others(tmp_path: Path) -> None:
    kdb = tmp_path / "kanban.db"
    pdb = tmp_path / "projects.db"
    _make_kanban_db(kdb)
    # No project bound to "ghost-board" -> unresolved.

    good_repo = tmp_path / "good-repo"
    committed_at = 1_700_000_000
    _init_repo_with_commit(good_repo, committed_at=committed_at, message="ok")

    broken = _entry(
        slug="broken",
        repo_path=str(tmp_path / "dead-repo"),
        kanban_project="ghost-board",
    )
    good = _entry(slug="good", repo_path=str(good_repo), kanban_project=None)

    registry = ProjectsRegistry(projects=[broken, good], errors=[])
    payload = build_projects_payload(
        registry, kanban_db_path=kdb, projects_db_path=pdb, now=committed_at + 10
    )

    assert len(payload["projects"]) == 2
    broken_out = next(p for p in payload["projects"] if p["slug"] == "broken")
    good_out = next(p for p in payload["projects"] if p["slug"] == "good")

    assert broken_out["last_commit"] is None
    assert broken_out["kanban"] is None
    assert len(broken_out["errors"]) >= 2  # git: + kanban:

    assert good_out["last_commit"] is not None
    assert good_out["last_commit"]["message"] == "ok"
    assert good_out["errors"] == []
