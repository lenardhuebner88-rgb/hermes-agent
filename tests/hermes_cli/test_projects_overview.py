from __future__ import annotations

import fcntl
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import hermes_cli.control_loops as control_loops
import hermes_cli.kanban_db as kanban_db
import hermes_cli.projects_db as projects_db
from hermes_cli.projects_overview import (
    ProjectEntry,
    ProjectsRegistry,
    _attribute_project,
    _cached_agents_payload,
    _coordination_agents,
    _parse_coordination_note,
    _reset_projects_cache,
    build_agents_payload,
    build_commits_payload,
    build_project_detail,
    build_projects_payload,
    build_sessions_payload,
    load_projects_registry,
    register_projects_routes,
)


@pytest.fixture(autouse=True)
def _reset_projects_cache_between_tests() -> None:
    """Keep route TTL cache from leaking across tests (Stage 9)."""
    _reset_projects_cache()
    yield
    _reset_projects_cache()

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


def test_reserved_slug_agents_is_rejected(tmp_path: Path) -> None:
    """A project slugged 'agents' would shadow GET /api/projects/agents (Codex
    review #5) — the loader must reject it, keeping the valid sibling."""
    path = _write(
        tmp_path,
        """\
projects:
  - slug: agents
    name: Collides
    repo_path: /tmp/collides
  - slug: real
    name: Real
    repo_path: /tmp/real
""",
    )
    result = load_projects_registry(path)
    assert [p.slug for p in result.projects] == ["real"]
    assert any("reserved" in e and "agents" in e for e in result.errors)


def test_non_url_safe_slug_is_rejected(tmp_path: Path) -> None:
    """Slugs become URL path segments + React keys — reject unsafe ones."""
    path = _write(
        tmp_path,
        """\
projects:
  - slug: "bad/slug"
    name: Bad
    repo_path: /tmp/bad
  - slug: good-1
    name: Good
    repo_path: /tmp/good
""",
    )
    result = load_projects_registry(path)
    assert [p.slug for p in result.projects] == ["good-1"]
    assert any("URL-safe" in e for e in result.errors)


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
    assert kanban == {
        "open": 1,
        "running": 1,
        "blocked": 1,
        "review": 1,
        "done_7d": 1,
        "needs_input": 0,
    }


def test_kanban_source_needs_input_counts_by_block_kind(tmp_path: Path) -> None:
    """needs_input = tasks with block_kind='needs_input' (any status), board-scoped."""
    kdb = tmp_path / "kanban.db"
    pdb = tmp_path / "projects.db"
    _make_kanban_db(kdb)
    pid = _make_projects_db(pdb, name="Hermes Infra", board_slug="default")

    now = 1_700_100_000
    # Two needs_input tasks on this board (one blocked, one scheduled).
    _insert_task_full(
        kdb,
        task_id="ni1",
        title="wait on operator",
        status="blocked",
        project_id=pid,
        created_at=now - 100,
        block_kind="needs_input",
    )
    _insert_task_full(
        kdb,
        task_id="ni2",
        title="also waiting",
        status="scheduled",
        project_id=None,  # default-board legacy NULL project_id
        created_at=now - 90,
        block_kind="needs_input",
    )
    # Same board, other block_kind — must NOT count as needs_input.
    _insert_task_full(
        kdb,
        task_id="dep1",
        title="dependency park",
        status="blocked",
        project_id=pid,
        created_at=now - 80,
        block_kind="dependency",
    )
    # Different project_id — must not leak into default board scope.
    _insert_task_full(
        kdb,
        task_id="ni-other",
        title="other board",
        status="blocked",
        project_id="other-project",
        created_at=now - 70,
        block_kind="needs_input",
    )
    # Terminal task keeps its historic block_kind — must NOT count as a live
    # operator-waiting task (else archived rows inflate the attention ampel).
    _insert_task_full(
        kdb,
        task_id="ni-archived",
        title="resolved long ago",
        status="archived",
        project_id=pid,
        created_at=now - 500,
        block_kind="needs_input",
    )

    entry = _entry(kanban_project="default")
    registry = ProjectsRegistry(projects=[entry], errors=[])
    payload = build_projects_payload(
        registry, kanban_db_path=kdb, projects_db_path=pdb, now=now
    )

    kanban = payload["projects"][0]["kanban"]
    assert kanban is not None
    # ni1 (blocked) + ni2 (scheduled); the archived one is excluded.
    assert kanban["needs_input"] == 2
    # blocked still only status='blocked' (ni1 + dep1); ni2 is scheduled.
    assert kanban["blocked"] == 2


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


# ---------------------------------------------------------------------------
# Stage 3 — /api/projects/agents (tmux / coordination / kanban / loops)
# ---------------------------------------------------------------------------

# Real tmux pane fields in the current nine-column format. These panes predate
# correlation options, so the final four option fields are empty.
_REAL_TMUX_PANES_TEXT = """\
fable-babysit-3|0|claude|claude|/home/piet||||
work|0|claude|claude|/home/piet||||
work|1|codex|codex|/home/piet||||
work|2|kimi|kimi-code|/home/piet/.hermes/hermes-agent||||
work|3|grok|node|/home/piet||||
work|4|claude-agent|2.1.211|/home/piet/.hermes/hermes-agent||||
work|5|claude-agent-2|2.1.211|/home/piet/.hermes/hermes-agent||||
work|6|claude-agent-3|2.1.211|/home/piet/.hermes/hermes-agent||||
"""

# Real `tmux list-sessions -F '#{session_name}|#{session_created}'` output,
# same capture.
_REAL_TMUX_SESSIONS_TEXT = """\
fable-babysit-3|1784229852
work|1784140154
"""


def _hermes_infra_registry() -> ProjectsRegistry:
    hermes_infra = _entry(
        slug="hermes-infra",
        name="Hermes Infra",
        repo_path="/home/piet/.hermes/hermes-agent",
        parent=None,
    )
    diktat = _entry(
        slug="diktat",
        name="Diktat",
        repo_path="/home/piet/.hermes/hermes-agent",
        parent="hermes-infra",
    )
    return ProjectsRegistry(projects=[hermes_infra, diktat], errors=[])


def test_tmux_source_real_capture_kinds_labels_project_since(tmp_path: Path) -> None:
    registry = _hermes_infra_registry()
    kdb = tmp_path / "kanban.db"
    _make_kanban_db(kdb)

    payload = build_agents_payload(
        registry,
        tmux_panes_text=_REAL_TMUX_PANES_TEXT,
        tmux_sessions_text=_REAL_TMUX_SESSIONS_TEXT,
        coordination_dir=tmp_path / "no-coordination-here",
        kanban_db_path=kdb,
        projects_db_path=tmp_path / "projects.db",
        loops_state_root=tmp_path / "loops",
        pack_names=[],
        now=1_700_000_000,
    )

    assert payload["errors"] == []
    tmux_agents = [a for a in payload["agents"] if a["source"] == "tmux"]
    assert len(tmux_agents) == 8  # no plain-shell panes in the fixture

    by_label = {a["label"]: a for a in tmux_agents}

    # Frozen legacy shape: heuristic kind remains intact; only the additive
    # correlation fields appear, both null, and task remains null.
    for pane in tmux_agents:
        assert pane["task"] is None
        assert pane["session_id"] is None
        assert pane["task_id"] is None

    fable_pane = by_label["fable-babysit-3:0 claude"]
    assert fable_pane["kind"] == "claude"
    assert fable_pane["project"] is None  # /home/piet does not match the repo
    assert fable_pane["since"] == 1784229852
    # Structured kill target for the Projekte-Tab (never parse the label).
    assert fable_pane["tmux_session"] == "fable-babysit-3"
    assert fable_pane["tmux_window"] == "0"

    work_claude = by_label["work:0 claude"]
    assert work_claude["kind"] == "claude"
    assert work_claude["project"] is None
    assert work_claude["since"] == 1784140154

    work_codex = by_label["work:1 codex"]
    assert work_codex["kind"] == "codex"
    assert work_codex["project"] is None

    work_kimi = by_label["work:2 kimi"]
    assert work_kimi["kind"] == "kimi"
    # Path is exactly the shared repo_path; hermes-infra (no parent) wins over diktat.
    assert work_kimi["project"] == "hermes-infra"
    assert work_kimi["tmux_session"] == "work"
    assert work_kimi["tmux_window"] == "2"

    work_grok = by_label["work:3 grok"]
    assert work_grok["kind"] == "grok"  # window_name "grok" wins over command "node"
    assert work_grok["project"] is None

    for index, window_name in enumerate(("claude-agent", "claude-agent-2", "claude-agent-3"), start=4):
        pane = by_label[f"work:{index} {window_name}"]
        assert pane["kind"] == "claude"  # window name wins over command "2.1.211"
        assert pane["project"] == "hermes-infra"
        assert pane["since"] == 1784140154


def test_tmux_source_options_override_kind_and_join_task_title(tmp_path: Path) -> None:
    registry = _hermes_infra_registry()
    kdb = tmp_path / "kanban.db"
    _make_kanban_db(kdb)
    _insert_task(
        kdb,
        task_id="corr-task",
        status="todo",
        project_id=None,
        created_at=1_700_000_000,
    )
    panes = (
        "work|7|claude-looking-name|claude|/home/piet/.hermes/hermes-agent"
        "|grok|agent|corr-task|session-123\n"
        "work|8|codex|codex|/home/piet/.hermes/hermes-agent"
        "|codex|agent|missing-task|session-456\n"
    )

    payload = build_agents_payload(
        registry,
        tmux_panes_text=panes,
        tmux_sessions_text="work|1784140154\n",
        coordination_dir=tmp_path / "no-coordination-here",
        kanban_db_path=kdb,
        projects_db_path=tmp_path / "projects.db",
        loops_state_root=tmp_path / "loops",
        pack_names=[],
        now=1_700_000_000,
    )

    assert payload["errors"] == []
    by_label = {a["label"]: a for a in payload["agents"] if a["source"] == "tmux"}
    pane = by_label["work:7 claude-looking-name"]
    assert pane["kind"] == "grok"
    assert pane["session_id"] == "session-123"
    assert pane["task_id"] == "corr-task"
    assert pane["task"] == "task corr-task"

    missing = by_label["work:8 codex"]
    assert missing["task_id"] == "missing-task"
    assert missing["session_id"] == "session-456"
    assert missing["task"] is None


def test_tmux_source_no_server_running_yields_zero_agents_no_error(tmp_path: Path) -> None:
    registry = _hermes_infra_registry()
    kdb = tmp_path / "kanban.db"
    _make_kanban_db(kdb)

    payload = build_agents_payload(
        registry,
        tmux_panes_text="",  # simulates "tmux list-panes -a" with no server running
        tmux_sessions_text="",
        coordination_dir=tmp_path / "no-coordination-here",
        kanban_db_path=kdb,
        projects_db_path=tmp_path / "projects.db",
        loops_state_root=tmp_path / "loops",
        pack_names=[],
        now=1_700_000_000,
    )

    assert payload["errors"] == []
    assert [a for a in payload["agents"] if a["source"] == "tmux"] == []


# --- coordination source ------------------------------------------------------

# Verbatim copy of the real open check-in note
# vault/_agents/_coordination/2026-07-16_2333_claude_projekte-tab-nachtlauf.md
_REAL_COORDINATION_NOTE = """\
---
agent: claude
started: 2026-07-16T23:33:00+02:00
ended: null
task: "Projekte-Tab-Nachtlauf (Goal-Prompt vault/03-Agents/Claude/plans/2026-07-16-projekte-tab-nachtlauf-goal-prompt.md): Leitstand-Tab /control → Projekt-Karten + Agent-Sessions-Discovery, 12 Stufen, seriell gelandet. Baut in Worktree Branch projekte-tab-nacht."
touching:
  - /home/piet/.hermes/hermes-agent/hermes_cli/projects_overview.py (neu)
  - /home/piet/.hermes/hermes-agent/tests/hermes_cli/test_projects_overview*.py (neu)
  - /home/piet/.hermes/hermes-agent/web/src/control/views/ProjekteView.tsx (neu)
  - /home/piet/.hermes/hermes-agent/web/src/control/views/projekte/ (neu)
  - /home/piet/.hermes/hermes-agent/hermes_cli/web_server.py (NUR eine register_projects_routes-Zeile)
  - /home/piet/.hermes/hermes-agent/web/src/control/ControlPage.tsx (nur Tab-Registrierung)
  - /home/piet/.hermes/hermes-agent/web/src/control/components/ControlShell.tsx (nur ControlTab-Union + Nav-Eintrag)
  - /home/piet/.hermes/hermes-agent/web/src/control/hooks/useControlData.ts (nur neue Hooks angehängt)
  - /home/piet/.hermes/hermes-agent/web/src/control/lib/schemas.ts (nur neue Schemas angehängt)
  - /home/piet/.hermes/hermes-agent/web/src/control/lib/types.ts (nur neue Typen angehängt)
  - /home/piet/.hermes/hermes-agent/web/src/control/i18n/de.ts (nur neue Labels)
  - /home/piet/.hermes/projects.yaml (neu, Runtime-Config)
operator: Piet (Grill-Session 16.07., Roadmap Punkt 8; /goal-Start 23:33)
---

# Projekte-Tab-Nachtlauf — Check-IN

Tabu-Zonen respektiert: `AgentTerminalsView.tsx` + `agent_terminals.py` (frage-assistent-p0)
werden NICHT editiert — Terminal-Daten nur via Import/Aufruf bestehender Module.
`web_server.py`: nur EINE Registrierungszeile für register_projects_routes (Rebase-freundlich).
`useControlData.ts`: nur append neuer Hooks — Hinweis an godfile-split-Session (refactort die
Datei im isolierten Worktree): vor Mergeback auf frisches main rebasen.
Landen seriell mit Rebase auf frisches main, fast-forward piet-fork, Deploy nur bei
UI-Checkpoints (Stufen 4/6/9/12) in sauberem Fenster (keine fremden uncommitted .py).
"""

_CLOSED_COORDINATION_NOTE = """\
---
agent: claude
started: 2026-07-16T20:00:00+02:00
ended: 2026-07-16T23:00:00+02:00
task: "Vorheriger, längst abgeschlossener Auftrag."
touching:
  - /home/piet/.hermes/hermes-agent/hermes_cli/kanban_db.py
---

# Erledigt — Check-OUT
"""

_GARBAGE_COORDINATION_NOTE = """\
Kein Frontmatter hier, nur ein loser Notizzettel ohne --- Delimiter.
agent: claude
started: not-even-close-to-yaml: [
"""

_FENCELESS_COORDINATION_NOTE = """\
agent: codex
started: 2026-07-17T08:15:00+02:00
ended: null
task: "Fence-less worker note."
touching:
  - /home/piet/.hermes/hermes-agent/hermes_cli/projects_overview.py
operator: Piet

# Fence-less check-in
"""


def test_coordination_source_open_note_parsed_closed_and_garbage_skipped(
    tmp_path: Path,
) -> None:
    coordination_dir = tmp_path / "_coordination"
    coordination_dir.mkdir()
    (coordination_dir / "2026-07-16_2333_claude_projekte-tab-nachtlauf.md").write_text(
        _REAL_COORDINATION_NOTE, encoding="utf-8"
    )
    (coordination_dir / "2026-07-16_2000_claude_closed-note.md").write_text(
        _CLOSED_COORDINATION_NOTE, encoding="utf-8"
    )
    (coordination_dir / "2026-07-16_garbage.md").write_text(
        _GARBAGE_COORDINATION_NOTE, encoding="utf-8"
    )
    # Second open note — names sort after the closed note and before the real
    # open note so the parallel path's order-stability is actually exercised
    # (sorted glob order, not completion order).
    second_open = """\
---
agent: codex
started: 2026-07-16T22:00:00+02:00
ended: null
task: "Second open note for order-stable parallel scan."
touching:
  - /home/piet/.hermes/hermes-agent/hermes_cli/projects_overview.py
---

# Second open
"""
    (coordination_dir / "2026-07-16_2200_codex_second-open.md").write_text(
        second_open, encoding="utf-8"
    )

    registry = _hermes_infra_registry()
    kdb = tmp_path / "kanban.db"
    _make_kanban_db(kdb)
    payload = build_agents_payload(
        registry,
        tmux_panes_text="",
        coordination_dir=coordination_dir,
        kanban_db_path=kdb,
        projects_db_path=tmp_path / "projects.db",
        loops_state_root=tmp_path / "loops",
        pack_names=[],
        now=1_700_000_000,
    )

    assert payload["errors"] == []
    coordination_agents = [a for a in payload["agents"] if a["source"] == "coordination"]
    # Closed + garbage skipped; two open notes in sorted-glob order.
    assert [a["label"] for a in coordination_agents] == [
        "2026-07-16_2200_codex_second-open",
        "2026-07-16_2333_claude_projekte-tab-nachtlauf",
    ]

    note = coordination_agents[1]
    assert note["kind"] == "claude"
    assert note["label"] == "2026-07-16_2333_claude_projekte-tab-nachtlauf"
    assert "Projekte-Tab-Nachtlauf" in note["task"]
    assert note["project"] == "hermes-infra"
    expected_epoch = int(
        __import__("datetime")
        .datetime.fromisoformat("2026-07-16T23:33:00+02:00")
        .timestamp()
    )
    assert note["since"] == expected_epoch

    second = coordination_agents[0]
    assert second["kind"] == "codex"
    assert second["project"] == "hermes-infra"

    # Kill targets exist only on the tmux source — coordination notes are
    # claims, not processes, and must never become killable rows.
    for note_agent in coordination_agents:
        assert note_agent.get("tmux_session") is None
        assert note_agent.get("tmux_window") is None


def test_parse_coordination_note_open_closed_garbage(tmp_path: Path) -> None:
    """Unit: `_parse_coordination_note` open → dict, closed/garbage → None."""
    registry = _hermes_infra_registry()
    open_path = tmp_path / "open.md"
    open_path.write_text(_REAL_COORDINATION_NOTE, encoding="utf-8")
    closed_path = tmp_path / "closed.md"
    closed_path.write_text(_CLOSED_COORDINATION_NOTE, encoding="utf-8")
    garbage_path = tmp_path / "garbage.md"
    garbage_path.write_text(_GARBAGE_COORDINATION_NOTE, encoding="utf-8")

    parsed = _parse_coordination_note(open_path, registry)
    assert parsed is not None
    assert parsed["kind"] == "claude"
    assert parsed["label"] == "open"
    assert parsed["source"] == "coordination"
    assert parsed["project"] == "hermes-infra"
    assert parsed["session_id"] is None
    assert parsed["task_id"] is None

    assert _parse_coordination_note(closed_path, registry) is None
    assert _parse_coordination_note(garbage_path, registry) is None


def test_parse_coordination_note_correlation_keys_are_stripped(tmp_path: Path) -> None:
    note_path = tmp_path / "correlated.md"
    note_path.write_text(
        """\
---
agent: codex
started: 2026-07-17T22:00:00+02:00
ended: null
task: "B1"
session: "  session-123  "
task_id: "  task-456  "
touching:
  - /home/piet/.hermes/hermes-agent/hermes_cli/projects_overview.py
---
""",
        encoding="utf-8",
    )

    parsed = _parse_coordination_note(note_path, _hermes_infra_registry())
    assert parsed is not None
    assert parsed["session_id"] == "session-123"
    assert parsed["task_id"] == "task-456"


def test_coordination_source_fenceless_note_appears_in_agents_payload(tmp_path: Path) -> None:
    coordination_dir = tmp_path / "_coordination"
    coordination_dir.mkdir()
    (coordination_dir / "fenceless-open.md").write_text(
        _FENCELESS_COORDINATION_NOTE, encoding="utf-8"
    )

    registry = _hermes_infra_registry()
    kdb = tmp_path / "kanban.db"
    _make_kanban_db(kdb)
    payload = build_agents_payload(
        registry,
        tmux_panes_text="",
        coordination_dir=coordination_dir,
        kanban_db_path=kdb,
        projects_db_path=tmp_path / "missing-projects.db",
        loops_state_root=tmp_path / "loops",
        pack_names=[],
        now=1_700_000_000,
    )

    coordination_agents = [
        agent for agent in payload["agents"] if agent["source"] == "coordination"
    ]
    assert payload["errors"] == []
    assert [agent["label"] for agent in coordination_agents] == ["fenceless-open"]
    assert coordination_agents[0]["kind"] == "codex"
    assert coordination_agents[0]["project"] == "hermes-infra"


def test_coordination_source_broken_dir_is_isolated_error(tmp_path: Path) -> None:
    broken_dir = tmp_path / "not-a-dir"
    broken_dir.write_text("i am a file, not a directory", encoding="utf-8")

    registry = _hermes_infra_registry()
    payload = build_agents_payload(
        registry,
        tmux_panes_text="",
        coordination_dir=broken_dir,
        now=1_700_000_000,
    )

    assert any(e.startswith("coordination:") for e in payload["errors"])
    assert [a for a in payload["agents"] if a["source"] == "coordination"] == []


def test_coordination_one_pathological_note_does_not_kill_scan(tmp_path: Path) -> None:
    """Codex review #6: a deeply-nested YAML note raises RecursionError (not a
    YAMLError) inside the thread pool; it must skip only that note, not abort
    the whole scan and lose every valid coordination agent."""
    coord = tmp_path / "coord"
    coord.mkdir()
    (coord / "a-open.md").write_text(_REAL_COORDINATION_NOTE, encoding="utf-8")
    # A note whose frontmatter nests far past the recursion limit; safe_load of
    # this raises RecursionError, which is NOT a yaml.YAMLError.
    depth = sys.getrecursionlimit() + 200
    bomb = "---\nagent: claude\nstarted: 2026-07-17T00:00:00+02:00\nx: " + "[" * depth + "]" * depth + "\n---\n"
    (coord / "b-bomb.md").write_text(bomb, encoding="utf-8")

    registry = _hermes_infra_registry()
    agents, errors = _coordination_agents(coord, registry=registry)
    # The valid open note still comes through; the bomb is silently skipped.
    labels = [a["label"] for a in agents]
    assert "a-open" in labels
    assert errors == []


def test_attribute_project_rejects_dotdot_escape() -> None:
    """Codex review #7: a touching-path escaping the repo via `..` must not be
    attributed to that repo."""
    reg = ProjectsRegistry(
        projects=[_entry(slug="r", name="R", repo_path="/home/piet/repo")],
        errors=[],
    )
    assert _attribute_project(["/home/piet/repo/sub/file"], reg) == "r"
    assert _attribute_project(["/home/piet/repo/../outside/file"], reg) is None


# --- kanban source ------------------------------------------------------------


def test_kanban_source_running_tasks_attributed_by_project(tmp_path: Path) -> None:
    kdb = tmp_path / "kanban.db"
    pdb = tmp_path / "projects.db"
    _make_kanban_db(kdb)
    health_track_pid = _make_projects_db(pdb, name="Health Track", board_slug="health-track")
    conn = projects_db.connect(pdb)
    try:
        default_pid = projects_db.create_project(conn, name="Hermes Infra", board_slug="default")
    finally:
        conn.close()

    now = 1_700_100_000
    _insert_task(kdb, task_id="r1", status="running", project_id=None, created_at=now - 10)
    # The common live case: a task explicitly bound to the default board's
    # project row must attribute to the default project, not fall to None.
    _insert_task(
        kdb, task_id="r0", status="running", project_id=default_pid, created_at=now - 10
    )
    _insert_task(
        kdb, task_id="r2", status="running", project_id=health_track_pid, created_at=now - 10
    )
    _insert_task(
        kdb, task_id="r3", status="running", project_id="ghost-project-id", created_at=now - 10
    )
    conn = kanban_db.connect(kdb)
    try:
        conn.execute("UPDATE tasks SET started_at = ? WHERE id = 'r1'", (now - 500,))
        conn.execute("UPDATE tasks SET started_at = ? WHERE id = 'r2'", (now - 600,))
        conn.commit()
    finally:
        conn.close()

    registry = ProjectsRegistry(
        projects=[
            _entry(slug="hermes-infra", kanban_project="default"),
            _entry(slug="health-track", kanban_project="health-track"),
        ],
        errors=[],
    )
    payload = build_agents_payload(
        registry,
        tmux_panes_text="",
        coordination_dir=tmp_path / "no-coordination-here",
        kanban_db_path=kdb,
        projects_db_path=pdb,
        now=now,
    )

    kanban_agents = {a["label"]: a for a in payload["agents"] if a["source"] == "kanban"}
    assert set(kanban_agents) == {"r0", "r1", "r2", "r3"}
    assert kanban_agents["r0"]["project"] == "hermes-infra"
    assert kanban_agents["r1"]["project"] == "hermes-infra"
    assert kanban_agents["r1"]["since"] == now - 500
    assert kanban_agents["r1"]["kind"] == "kanban"
    assert kanban_agents["r2"]["project"] == "health-track"
    assert kanban_agents["r2"]["since"] == now - 600
    assert kanban_agents["r3"]["project"] is None


# --- loops source --------------------------------------------------------------


def _make_running_pack(state_root: Path, name: str) -> None:
    state_dir = state_root / name
    _write_heartbeat(
        state_dir,
        started_at="2026-07-16T21:37:59Z",
        last_at=["2026-07-16T19:25:36Z"],
    )
    lock = state_dir / ".lock"
    lock.write_text("", encoding="utf-8")


def test_loops_source_running_pack_attributed_project(tmp_path: Path) -> None:
    state_root = tmp_path / "loops"
    _make_running_pack(state_root, "dashboard-experience")
    _make_running_pack(state_root, "orphan-pack")
    kdb = tmp_path / "kanban.db"
    _make_kanban_db(kdb)

    lock1 = (state_root / "dashboard-experience" / ".lock").open("r+", encoding="utf-8")
    lock2 = (state_root / "orphan-pack" / ".lock").open("r+", encoding="utf-8")
    fcntl.flock(lock1, fcntl.LOCK_EX)
    fcntl.flock(lock2, fcntl.LOCK_EX)
    try:
        registry = ProjectsRegistry(
            projects=[_entry(slug="hermes-infra", loop_packs=["dashboard-experience"])],
            errors=[],
        )
        payload = build_agents_payload(
            registry,
            tmux_panes_text="",
            coordination_dir=tmp_path / "no-coordination-here",
            kanban_db_path=kdb,
            projects_db_path=tmp_path / "projects.db",
            loops_state_root=state_root,
            pack_names=["dashboard-experience", "orphan-pack", "never-ran-pack"],
            now=int(time.time()),
        )
    finally:
        fcntl.flock(lock1, fcntl.LOCK_UN)
        fcntl.flock(lock2, fcntl.LOCK_UN)
        lock1.close()
        lock2.close()

    loop_agents = {a["label"]: a for a in payload["agents"] if a["source"] == "loop"}
    assert set(loop_agents) == {"dashboard-experience", "orphan-pack"}  # never-ran-pack not running
    assert loop_agents["dashboard-experience"]["project"] == "hermes-infra"
    assert loop_agents["orphan-pack"]["project"] is None
    expected_epoch = int(
        __import__("datetime").datetime.fromisoformat("2026-07-16T21:37:59+00:00").timestamp()
    )
    assert loop_agents["dashboard-experience"]["since"] == expected_epoch
    assert payload["errors"] == []


# --- endpoint ------------------------------------------------------------------


def test_agents_endpoint_returns_200_frozen_shape_sources_isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("hermes_cli.projects_overview.get_hermes_home", lambda: tmp_path)
    _make_kanban_db(tmp_path / "kanban.db")

    broken_coordination_dir = tmp_path / "coordination-is-a-file"
    broken_coordination_dir.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(
        "hermes_cli.projects_overview._default_coordination_dir",
        lambda: broken_coordination_dir,
    )
    monkeypatch.setattr(
        "hermes_cli.projects_overview._run_tmux_command", lambda cmd: ("", None)
    )
    monkeypatch.setattr(control_loops, "_all_pack_names", lambda: [])

    app = FastAPI()
    register_projects_routes(app)
    client = TestClient(app)

    resp = client.get("/api/projects/agents")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["generated_at"], int)
    assert isinstance(body["agents"], list)
    assert body["agents"] == []
    assert any(e.startswith("coordination:") for e in body["errors"])
    assert not any(e.startswith("tmux:") for e in body["errors"])
    assert not any(e.startswith("kanban:") for e in body["errors"])
    assert not any(e.startswith("loops:") for e in body["errors"])


# ---------------------------------------------------------------------------
# Stage 6 — /api/projects/{slug} project drilldown
# ---------------------------------------------------------------------------


def _add_commits(repo: Path, commits: list[tuple[int, str, str]]) -> None:
    """Append commits as (epoch, message, relative_path content)."""
    import os

    for committed_at, message, relpath in commits:
        path = repo / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{message}\n", encoding="utf-8")
        _git(repo, "add", relpath)
        env = dict(os.environ)
        env["GIT_AUTHOR_DATE"] = f"{committed_at} +0000"
        env["GIT_COMMITTER_DATE"] = f"{committed_at} +0000"
        _git(repo, "commit", "-q", "-m", message, env=env)


def _insert_task_full(
    db_path: Path,
    *,
    task_id: str,
    title: str,
    status: str,
    project_id: str | None,
    created_at: int,
    priority: int = 0,
    block_kind: str | None = None,
) -> None:
    conn = kanban_db.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks "
            "(id, title, status, project_id, created_at, priority, block_kind) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_id, title, status, project_id, created_at, priority, block_kind),
        )
        conn.commit()
    finally:
        conn.close()


def test_detail_recent_commits_order_and_path_filters(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base = 1_700_000_000
    _init_repo_with_commit(repo, committed_at=base, message="c0: seed")
    _add_commits(
        repo,
        [
            (base + 100, "c1: sub touch", "sub/a.txt"),
            (base + 200, "c2: parent only", "README.md"),
            (base + 300, "c3: sub again", "sub/b.txt"),
            (base + 400, "c4: parent again", "other.txt"),
        ],
    )

    parent = _entry(slug="parent", repo_path=str(repo))
    sub = _entry(
        slug="sub",
        repo_path=str(repo),
        parent="parent",
        path_filters=["sub"],
    )
    registry = ProjectsRegistry(projects=[parent, sub], errors=[])
    now = base + 1000

    parent_detail = build_project_detail(
        parent,
        registry,
        kanban_db_path=tmp_path / "kanban.db",
        projects_db_path=tmp_path / "projects.db",
        loops_state_root=tmp_path / "loops",
        coordination_dir=tmp_path / "coord",
        tmux_panes_text="",
        pack_names=[],
        now=now,
    )
    assert len(parent_detail["recent_commits"]) == 5  # seed + 4
    assert parent_detail["recent_commits"][0]["message"] == "c4: parent again"
    assert parent_detail["recent_commits"][1]["message"] == "c3: sub again"
    assert all(len(c["hash"]) == 9 for c in parent_detail["recent_commits"])
    assert parent_detail["recent_commits"][0]["age_seconds"] == now - (base + 400)

    sub_detail = build_project_detail(
        sub,
        registry,
        kanban_db_path=tmp_path / "kanban.db",
        projects_db_path=tmp_path / "projects.db",
        loops_state_root=tmp_path / "loops",
        coordination_dir=tmp_path / "coord",
        tmux_panes_text="",
        pack_names=[],
        now=now,
    )
    messages = [c["message"] for c in sub_detail["recent_commits"]]
    assert messages == ["c3: sub again", "c1: sub touch"]


def test_detail_kanban_tasks_open_blocked_scoped_cap(tmp_path: Path) -> None:
    kdb = tmp_path / "kanban.db"
    pdb = tmp_path / "projects.db"
    _make_kanban_db(kdb)
    pid = _make_projects_db(pdb, name="Hermes Infra", board_slug="default")
    now = 1_700_100_000

    _insert_task_full(
        kdb,
        task_id="open1",
        title="Open task",
        status="todo",
        project_id=None,  # legacy default-board
        created_at=now - 50,
        priority=1,
    )
    _insert_task_full(
        kdb,
        task_id="blocked1",
        title="Blocked task",
        status="blocked",
        project_id=pid,
        created_at=now - 40,
        priority=5,
        block_kind="needs_input",
    )
    _insert_task_full(
        kdb,
        task_id="running1",
        title="Running task",
        status="running",
        project_id=pid,
        created_at=now - 30,
        priority=3,
    )
    _insert_task_full(
        kdb,
        task_id="done1",
        title="Done task",
        status="done",
        project_id=pid,
        created_at=now - 20,
        priority=9,
    )
    # Foreign board must not leak into default.
    other_pid = _make_projects_db(pdb, name="Other", board_slug="other-board")
    _insert_task_full(
        kdb,
        task_id="foreign",
        title="Foreign",
        status="todo",
        project_id=other_pid,
        created_at=now - 10,
        priority=99,
    )

    entry = _entry(slug="hermes-infra", kanban_project="default", repo_path=str(tmp_path))
    registry = ProjectsRegistry(projects=[entry], errors=[])
    detail = build_project_detail(
        entry,
        registry,
        kanban_db_path=kdb,
        projects_db_path=pdb,
        loops_state_root=tmp_path / "loops",
        coordination_dir=tmp_path / "coord",
        tmux_panes_text="",
        pack_names=[],
        now=now,
    )

    tasks = detail["kanban_tasks"]
    assert tasks is not None
    ids = [t["id"] for t in tasks]
    assert "done1" not in ids
    assert "foreign" not in ids
    assert set(ids) == {"open1", "blocked1", "running1"}
    # priority DESC then created_at DESC: blocked1 (5), running1 (3), open1 (1)
    assert ids == ["blocked1", "running1", "open1"]
    blocked = next(t for t in tasks if t["id"] == "blocked1")
    assert blocked["block_kind"] == "needs_input"
    assert blocked["status"] == "blocked"
    assert blocked["age_seconds"] == 40

    # Cap: 25 open tasks max.
    for i in range(30):
        _insert_task_full(
            kdb,
            task_id=f"cap{i}",
            title=f"Cap {i}",
            status="todo",
            project_id=pid,
            created_at=now - i,
            priority=0,
        )
    detail_cap = build_project_detail(
        entry,
        registry,
        kanban_db_path=kdb,
        projects_db_path=pdb,
        loops_state_root=tmp_path / "loops",
        coordination_dir=tmp_path / "coord",
        tmux_panes_text="",
        pack_names=[],
        now=now,
    )
    assert len(detail_cap["kanban_tasks"]) == 25


def test_detail_kanban_null_when_no_board(tmp_path: Path) -> None:
    entry = _entry(slug="no-board", kanban_project=None, repo_path=str(tmp_path))
    registry = ProjectsRegistry(projects=[entry], errors=[])
    detail = build_project_detail(
        entry,
        registry,
        kanban_db_path=tmp_path / "kanban.db",
        projects_db_path=tmp_path / "projects.db",
        loops_state_root=tmp_path / "loops",
        coordination_dir=tmp_path / "coord",
        tmux_panes_text="",
        pack_names=[],
        now=int(time.time()),
    )
    assert detail["kanban_tasks"] is None
    assert not any(e.startswith("kanban:") for e in detail["errors"])


def test_detail_loop_last_outcome_from_ledger_tail(tmp_path: Path) -> None:
    state_root = tmp_path / "loops"
    pack_dir = state_root / "dashboard-experience"
    pack_dir.mkdir(parents=True)
    ledger = pack_dir / "ledger.jsonl"
    # Mix of non-verdict usage lines + older verdict + newer verdict — last
    # verdict line wins. Real ledger mixes zoned and (older) naive ts; only
    # zoned ones parse via _parse_loop_iso_timestamp.
    lines = [
        json.dumps(
            {
                "phase": "verify",
                "verdict": "ok",
                "plan": "old-plan.md",
                "reason": "old",
                "ts": "2026-07-16T10:00:00Z",
            }
        ),
        json.dumps(
            {
                "event": "phase_usage",
                "phase": "verify",
                "secs": 12,
                "ts": "2026-07-16T11:00:00Z",
            }
        ),  # no verdict
        "not-json-at-all",
        json.dumps(
            {
                "phase": "land",
                "verdict": "landed",
                "plan": "P1-ship.md",
                "reason": "main=abc",
                "ts": "2026-07-16T12:00:00Z",
            }
        ),
        json.dumps({"event": "heartbeat_only", "ts": "2026-07-16T12:01:00Z"}),
    ]
    ledger.write_text("\n".join(lines) + "\n", encoding="utf-8")

    entry = _entry(slug="hermes-infra", loop_packs=["dashboard-experience"], repo_path=str(tmp_path))
    registry = ProjectsRegistry(projects=[entry], errors=[])
    detail = build_project_detail(
        entry,
        registry,
        kanban_db_path=tmp_path / "k.db",
        projects_db_path=tmp_path / "p.db",
        loops_state_root=state_root,
        coordination_dir=tmp_path / "coord",
        tmux_panes_text="",
        pack_names=["dashboard-experience"],
        now=int(time.time()),
    )

    assert len(detail["loops"]) == 1
    pack = detail["loops"][0]
    assert pack["name"] == "dashboard-experience"
    assert pack["running"] is False
    outcome = pack["last_outcome"]
    assert outcome is not None
    assert outcome["verdict"] == "landed"
    assert outcome["phase"] == "land"
    assert outcome["plan"] == "P1-ship.md"
    assert outcome["reason"] == "main=abc"
    expected_ts = int(
        __import__("datetime").datetime.fromisoformat("2026-07-16T12:00:00+00:00").timestamp()
    )
    assert outcome["ts"] == expected_ts


def test_detail_loop_missing_ledger_last_outcome_null(tmp_path: Path) -> None:
    state_root = tmp_path / "loops"
    (state_root / "never-ran").mkdir(parents=True)
    entry = _entry(slug="p", loop_packs=["never-ran"], repo_path=str(tmp_path))
    registry = ProjectsRegistry(projects=[entry], errors=[])
    detail = build_project_detail(
        entry,
        registry,
        loops_state_root=state_root,
        coordination_dir=tmp_path / "coord",
        tmux_panes_text="",
        pack_names=[],
        now=int(time.time()),
    )
    assert detail["loops"][0]["last_outcome"] is None
    assert not any(e.startswith("loops:") for e in detail["errors"])


def test_detail_broken_source_isolated(tmp_path: Path) -> None:
    # Git broken (missing repo), kanban board unresolvable, loops pack ok.
    state_root = tmp_path / "loops"
    pack_dir = state_root / "ok-pack"
    pack_dir.mkdir(parents=True)
    (pack_dir / "ledger.jsonl").write_text(
        json.dumps({"verdict": "ok", "phase": "verify", "ts": "2026-07-16T12:00:00Z"}) + "\n",
        encoding="utf-8",
    )
    kdb = tmp_path / "kanban.db"
    pdb = tmp_path / "projects.db"
    _make_kanban_db(kdb)
    _make_projects_db(pdb, name="Other", board_slug="other")

    entry = _entry(
        slug="broken-ish",
        repo_path=str(tmp_path / "no-such-repo"),
        kanban_project="ghost-board",
        loop_packs=["ok-pack"],
    )
    registry = ProjectsRegistry(projects=[entry], errors=[])
    detail = build_project_detail(
        entry,
        registry,
        kanban_db_path=kdb,
        projects_db_path=pdb,
        loops_state_root=state_root,
        coordination_dir=tmp_path / "coord",
        tmux_panes_text="",
        pack_names=["ok-pack"],
        now=int(time.time()),
    )

    assert detail["recent_commits"] == []
    assert detail["kanban_tasks"] is None
    assert any(e.startswith("git:") for e in detail["errors"])
    assert any(e.startswith("kanban:") for e in detail["errors"])
    assert detail["loops"][0]["last_outcome"]["verdict"] == "ok"
    assert not any(e.startswith("loops:") for e in detail["errors"])


def test_detail_unknown_slug_endpoint_404_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("hermes_cli.projects_overview.get_hermes_home", lambda: tmp_path)
    _write(
        tmp_path,
        """\
projects:
  - slug: known
    name: Known
    repo_path: /tmp/known
""",
    )
    app = FastAPI()
    register_projects_routes(app)
    client = TestClient(app)

    resp = client.get("/api/projects/does-not-exist")
    assert resp.status_code == 404
    body = resp.json()
    assert body == {"error": "unknown project", "slug": "does-not-exist"}


def test_detail_endpoint_returns_frozen_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    committed_at = 1_700_000_000
    _init_repo_with_commit(repo, committed_at=committed_at, message="feat: detail")
    _add_commits(repo, [(committed_at + 10, "feat: two", "two.txt"), (committed_at + 20, "feat: three", "three.txt")])

    kdb = tmp_path / "kanban.db"
    pdb = tmp_path / "projects.db"
    _make_kanban_db(kdb)
    pid = _make_projects_db(pdb, name="Hermes Infra", board_slug="default")
    _insert_task_full(
        kdb,
        task_id="t1",
        title="A task",
        status="todo",
        project_id=pid,
        created_at=committed_at,
        priority=2,
    )

    state_root = tmp_path / "loops"
    pack_dir = state_root / "builder-reviewer"
    pack_dir.mkdir(parents=True)
    (pack_dir / "ledger.jsonl").write_text(
        json.dumps(
            {
                "verdict": "landed",
                "phase": "land",
                "plan": "P1.md",
                "reason": "ok",
                "ts": "2026-07-16T12:00:00Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    yaml_text = f"""\
projects:
  - slug: hermes-infra
    name: Hermes Infra
    repo_path: {repo}
    kanban_project: default
    loop_packs:
      - builder-reviewer
    links:
      - label: Control
        url: /control
"""
    _write(tmp_path, yaml_text)
    # Production path uses get_hermes_home()/projects.yaml + default DB paths.
    monkeypatch.setattr("hermes_cli.projects_overview.get_hermes_home", lambda: tmp_path)
    # Loops state root is control_loops._state_root(); override via home layout
    # or by monkeypatching the detail builder's default — production resolves
    # via control_loops._state_root which uses HERMES_HOME/loops. Point it.
    monkeypatch.setattr(control_loops, "_state_root", lambda: state_root)
    monkeypatch.setattr(
        "hermes_cli.projects_overview._run_tmux_command", lambda cmd: ("", None)
    )
    monkeypatch.setattr(
        "hermes_cli.projects_overview._default_coordination_dir",
        lambda: tmp_path / "coord",
    )
    monkeypatch.setattr(control_loops, "_all_pack_names", lambda: [("builder-reviewer", "repo")])

    app = FastAPI()
    register_projects_routes(app)
    client = TestClient(app)

    resp = client.get("/api/projects/hermes-infra")
    assert resp.status_code == 200
    body = resp.json()
    assert body["slug"] == "hermes-infra"
    assert body["name"] == "Hermes Infra"
    assert body["parent"] is None
    assert body["links"] == [{"label": "Control", "url": "/control"}]
    assert len(body["recent_commits"]) == 3
    assert body["recent_commits"][0]["message"] == "feat: three"
    assert body["kanban_tasks"] is not None
    assert body["kanban_tasks"][0]["id"] == "t1"
    assert body["loops"][0]["last_outcome"]["verdict"] == "landed"
    assert isinstance(body["agents"], list)
    assert isinstance(body["errors"], list)
    assert isinstance(body["generated_at"], int)

    # agents path still works (not swallowed by {slug})
    agents_resp = client.get("/api/projects/agents")
    assert agents_resp.status_code == 200
    assert "agents" in agents_resp.json()


# ---------------------------------------------------------------------------
# Stage 9 — parallel coordination + short route TTL cache
# ---------------------------------------------------------------------------


def test_ttl_cache_agents_builds_once_within_ttl_rebuilds_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Injectable clock: two hits within TTL → one build; past TTL → rebuild."""
    monkeypatch.setattr("hermes_cli.projects_overview.get_hermes_home", lambda: tmp_path)
    _make_kanban_db(tmp_path / "kanban.db")
    monkeypatch.setattr(
        "hermes_cli.projects_overview._default_coordination_dir",
        lambda: tmp_path / "coord",
    )
    monkeypatch.setattr(
        "hermes_cli.projects_overview._run_tmux_command", lambda cmd: ("", None)
    )
    monkeypatch.setattr(control_loops, "_all_pack_names", lambda: [])

    calls = {"n": 0}
    real_build = build_agents_payload

    def counting_build(*args: object, **kwargs: object) -> dict:
        calls["n"] += 1
        return real_build(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        "hermes_cli.projects_overview.build_agents_payload", counting_build
    )

    fake_now = {"t": 1000.0}
    monkeypatch.setattr(
        "hermes_cli.projects_overview._clock", lambda: fake_now["t"]
    )

    app = FastAPI()
    register_projects_routes(app)
    client = TestClient(app)

    r1 = client.get("/api/projects/agents")
    r2 = client.get("/api/projects/agents")
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json() == r2.json()
    assert calls["n"] == 1  # second hit served from cache

    fake_now["t"] = 1000.0 + 10.1  # past default 10s TTL
    r3 = client.get("/api/projects/agents")
    assert r3.status_code == 200
    assert calls["n"] == 2


def test_ttl_cache_projects_builds_once_within_ttl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("hermes_cli.projects_overview.get_hermes_home", lambda: tmp_path)
    calls = {"n": 0}
    real_build = build_projects_payload

    def counting_build(*args: object, **kwargs: object) -> dict:
        calls["n"] += 1
        return real_build(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        "hermes_cli.projects_overview.build_projects_payload", counting_build
    )
    fake_now = {"t": 50.0}
    monkeypatch.setattr(
        "hermes_cli.projects_overview._clock", lambda: fake_now["t"]
    )

    app = FastAPI()
    register_projects_routes(app)
    client = TestClient(app)

    assert client.get("/api/projects").status_code == 200
    assert client.get("/api/projects").status_code == 200
    assert calls["n"] == 1
    fake_now["t"] = 61.0  # past default 10s TTL
    assert client.get("/api/projects").status_code == 200
    assert calls["n"] == 2


def test_detail_reuses_prebuilt_agents_payload_no_coordination_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """build_project_detail(agents_payload=...) must not re-scan coordination."""
    coordination_dir = tmp_path / "coord"
    coordination_dir.mkdir()
    (coordination_dir / "open.md").write_text(_REAL_COORDINATION_NOTE, encoding="utf-8")

    registry = _hermes_infra_registry()
    kdb = tmp_path / "kanban.db"
    _make_kanban_db(kdb)
    prebuilt = build_agents_payload(
        registry,
        tmux_panes_text="",
        coordination_dir=coordination_dir,
        kanban_db_path=kdb,
        projects_db_path=tmp_path / "projects.db",
        loops_state_root=tmp_path / "loops",
        pack_names=[],
        now=1_700_000_000,
    )
    assert any(a["source"] == "coordination" for a in prebuilt["agents"])
    correlated = next(a for a in prebuilt["agents"] if a["source"] == "coordination")
    correlated["session_id"] = "session-detail"
    correlated["task_id"] = "task-detail"

    parse_calls = {"n": 0}
    real_parse = _parse_coordination_note

    def spy_parse(path: Path, reg: ProjectsRegistry) -> dict | None:
        parse_calls["n"] += 1
        return real_parse(path, reg)

    monkeypatch.setattr(
        "hermes_cli.projects_overview._parse_coordination_note", spy_parse
    )

    entry = next(p for p in registry.projects if p.slug == "hermes-infra")
    detail = build_project_detail(
        entry,
        registry,
        kanban_db_path=kdb,
        projects_db_path=tmp_path / "projects.db",
        loops_state_root=tmp_path / "loops",
        coordination_dir=coordination_dir,
        tmux_panes_text="",
        pack_names=[],
        now=1_700_000_000,
        agents_payload=prebuilt,
    )
    assert parse_calls["n"] == 0  # coordination source not hit
    # Filtered to hermes-infra; project field dropped.
    assert all("project" not in a for a in detail["agents"])
    detail_coordination = next(a for a in detail["agents"] if a["source"] == "coordination")
    assert detail_coordination["session_id"] == "session-detail"
    assert detail_coordination["task_id"] == "task-detail"


def test_agents_cache_mutation_does_not_bleed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutating a returned agents payload must not corrupt the next cache hit."""
    monkeypatch.setattr("hermes_cli.projects_overview.get_hermes_home", lambda: tmp_path)
    _make_kanban_db(tmp_path / "kanban.db")
    coord = tmp_path / "coord"
    coord.mkdir()
    (coord / "open.md").write_text(_REAL_COORDINATION_NOTE, encoding="utf-8")
    monkeypatch.setattr(
        "hermes_cli.projects_overview._default_coordination_dir", lambda: coord
    )
    monkeypatch.setattr(
        "hermes_cli.projects_overview._run_tmux_command", lambda cmd: ("", None)
    )
    monkeypatch.setattr(control_loops, "_all_pack_names", lambda: [])
    monkeypatch.setattr("hermes_cli.projects_overview._clock", lambda: 1.0)

    # Direct accessor (not JSON round-trip) so we exercise the cache view itself.
    first = _cached_agents_payload()
    assert first["agents"]  # at least the coordination note
    original_len = len(first["agents"])
    first["agents"].clear()
    first["errors"] = ["poison"]
    first["generated_at"] = -1

    second = _cached_agents_payload()
    assert len(second["agents"]) == original_len
    assert second["errors"] == []
    assert second["generated_at"] != -1


def test_detail_route_reuses_cached_agents_and_filter_copy_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Detail route uses _cached_agents_payload; filtering does not corrupt cache."""
    # repo_path must match the coordination note's touching paths so the
    # agent attributes to hermes-infra and survives detail filtering.
    yaml_text = """\
projects:
  - slug: hermes-infra
    name: Hermes Infra
    repo_path: /home/piet/.hermes/hermes-agent
    kanban_project: null
    loop_packs: []
"""
    _write(tmp_path, yaml_text)
    monkeypatch.setattr("hermes_cli.projects_overview.get_hermes_home", lambda: tmp_path)
    # Avoid live-repo git scan in this cache-focused test.
    monkeypatch.setattr(
        "hermes_cli.projects_overview._project_recent_commits",
        lambda entry, *, now: ([], None),
    )
    coord = tmp_path / "coord"
    coord.mkdir()
    (coord / "open.md").write_text(_REAL_COORDINATION_NOTE, encoding="utf-8")
    monkeypatch.setattr(
        "hermes_cli.projects_overview._default_coordination_dir", lambda: coord
    )
    monkeypatch.setattr(
        "hermes_cli.projects_overview._run_tmux_command", lambda cmd: ("", None)
    )
    monkeypatch.setattr(control_loops, "_all_pack_names", lambda: [])
    monkeypatch.setattr(control_loops, "_state_root", lambda: tmp_path / "loops")
    monkeypatch.setattr("hermes_cli.projects_overview._clock", lambda: 10.0)

    build_calls = {"n": 0}
    real_build = build_agents_payload

    def counting_build(*args: object, **kwargs: object) -> dict:
        build_calls["n"] += 1
        return real_build(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        "hermes_cli.projects_overview.build_agents_payload", counting_build
    )

    app = FastAPI()
    register_projects_routes(app)
    client = TestClient(app)

    agents_body = client.get("/api/projects/agents").json()
    assert build_calls["n"] == 1
    assert any(a.get("project") == "hermes-infra" for a in agents_body["agents"])
    detail = client.get("/api/projects/hermes-infra").json()
    # Detail reuses cached agents — no second build.
    assert build_calls["n"] == 1
    assert detail["slug"] == "hermes-infra"
    assert any(a["source"] == "coordination" for a in detail["agents"])
    # Mutate detail agents list; cache must stay clean.
    detail["agents"].clear()

    agents_again = client.get("/api/projects/agents").json()
    assert build_calls["n"] == 1
    assert len(agents_again["agents"]) == len(agents_body["agents"])
    assert agents_again["agents"]  # still present



# ---------------------------------------------------------------------------
# Stage 10/11 — author on commits, assignee/operator on agents,
# /api/projects/sessions + /api/projects/commits
# ---------------------------------------------------------------------------


def test_git_last_commit_includes_author(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    committed_at = 1_700_000_000
    _init_repo_with_commit(repo, committed_at=committed_at, message="feat: author check")

    entry = _entry(repo_path=str(repo))
    payload = build_projects_payload(
        ProjectsRegistry(projects=[entry], errors=[]), now=committed_at + 60
    )

    last_commit = payload["projects"][0]["last_commit"]
    assert last_commit is not None
    # _init_repo_with_commit sets `git config user.name "Test User"`.
    assert last_commit["author"] == "Test User"


def test_detail_recent_commits_include_author(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base = 1_700_000_000
    _init_repo_with_commit(repo, committed_at=base, message="c0: seed")
    _add_commits(repo, [(base + 100, "c1: second", "b.txt")])

    entry = _entry(slug="proj", repo_path=str(repo))
    registry = ProjectsRegistry(projects=[entry], errors=[])
    detail = build_project_detail(
        entry,
        registry,
        kanban_db_path=tmp_path / "kanban.db",
        projects_db_path=tmp_path / "projects.db",
        loops_state_root=tmp_path / "loops",
        now=base + 200,
        agents_payload={"generated_at": base, "errors": [], "agents": []},
    )

    assert detail["errors"] == []
    assert [c["message"] for c in detail["recent_commits"]] == ["c1: second", "c0: seed"]
    assert all(c["author"] == "Test User" for c in detail["recent_commits"])


# --- commit feed (/api/projects/commits) ------------------------------------


def test_commits_feed_merges_projects_newest_first(tmp_path: Path) -> None:
    base = 1_700_000_000
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    _init_repo_with_commit(repo_a, committed_at=base, message="a0")
    _add_commits(repo_a, [(base + 300, "a1", "a.txt")])
    _init_repo_with_commit(repo_b, committed_at=base + 100, message="b0")
    _add_commits(repo_b, [(base + 200, "b1", "b.txt")])

    entry_a = _entry(slug="alpha", name="Alpha", repo_path=str(repo_a))
    entry_b = _entry(slug="beta", name="Beta", repo_path=str(repo_b))
    registry = ProjectsRegistry(projects=[entry_a, entry_b], errors=[])

    payload = build_commits_payload(registry, now=base + 400)

    assert payload["errors"] == []
    messages = [(c["project"], c["message"]) for c in payload["commits"]]
    assert messages == [
        ("alpha", "a1"),
        ("beta", "b1"),
        ("beta", "b0"),
        ("alpha", "a0"),
    ]
    for commit in payload["commits"]:
        assert commit["author"] == "Test User"
        assert commit["project_name"] in ("Alpha", "Beta")
        assert commit["age_seconds"] == base + 400 - commit["committed_at"]


def test_commits_feed_broken_repo_isolated(tmp_path: Path) -> None:
    base = 1_700_000_000
    good_repo = tmp_path / "good"
    _init_repo_with_commit(good_repo, committed_at=base, message="ok")

    broken = _entry(slug="broken", repo_path=str(tmp_path / "missing"))
    good = _entry(slug="good", name="Good", repo_path=str(good_repo))
    registry = ProjectsRegistry(projects=[broken, good], errors=[])

    payload = build_commits_payload(registry, now=base + 10)

    assert [c["message"] for c in payload["commits"]] == ["ok"]
    assert len(payload["errors"]) == 1
    assert payload["errors"][0].startswith("git: project 'broken':")


def test_commits_feed_cap_applies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = 1_700_000_000
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    _init_repo_with_commit(repo_a, committed_at=base, message="a0")
    _add_commits(repo_a, [(base + i, f"a{i}", f"a{i}.txt") for i in range(1, 5)])
    _init_repo_with_commit(repo_b, committed_at=base, message="b0")
    _add_commits(repo_b, [(base + i, f"b{i}", f"b{i}.txt") for i in range(1, 5)])

    registry = ProjectsRegistry(
        projects=[
            _entry(slug="alpha", repo_path=str(repo_a)),
            _entry(slug="beta", repo_path=str(repo_b)),
        ],
        errors=[],
    )
    monkeypatch.setattr("hermes_cli.projects_overview._FEED_COMMITS_LIMIT", 3)

    payload = build_commits_payload(registry, now=base + 100)

    assert len(payload["commits"]) == 3
    # Newest three across both repos, still strictly newest-first.
    committed = [c["committed_at"] for c in payload["commits"]]
    assert committed == sorted(committed, reverse=True)


# --- assignee / operator on agents ------------------------------------------


def test_kanban_running_agent_surfaces_assignee(tmp_path: Path) -> None:
    kdb = tmp_path / "kanban.db"
    _make_kanban_db(kdb)
    conn = kanban_db.connect(kdb)
    try:
        conn.execute(
            "INSERT INTO tasks (id, title, status, project_id, created_at, started_at, assignee) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("t_lane1", "lane task", "running", None, 1_700_000_000, 1_700_000_100, "premium"),
        )
        conn.execute(
            "INSERT INTO tasks (id, title, status, project_id, created_at, started_at, assignee) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("t_lane2", "no lane task", "running", None, 1_700_000_000, 1_700_000_100, None),
        )
        conn.commit()
    finally:
        conn.close()

    registry = _hermes_infra_registry()
    payload = build_agents_payload(
        registry,
        tmux_panes_text="",
        tmux_sessions_text="",
        coordination_dir=tmp_path / "no-coord",
        kanban_db_path=kdb,
        projects_db_path=tmp_path / "projects.db",
        loops_state_root=tmp_path / "loops",
        pack_names=[],
        now=1_700_000_200,
    )

    by_label = {a["label"]: a for a in payload["agents"] if a["source"] == "kanban"}
    assert by_label["t_lane1"]["assignee"] == "premium"
    assert by_label["t_lane2"]["assignee"] is None


def test_coordination_note_operator_parsed(tmp_path: Path) -> None:
    coord = tmp_path / "coord"
    coord.mkdir()
    (coord / "with-operator.md").write_text(_REAL_COORDINATION_NOTE, encoding="utf-8")
    note_without_operator = """\
---
agent: kimi
started: 2026-07-17T10:00:00+02:00
ended: null
task: "ohne operator-Feld"
touching:
  - /home/piet/.hermes/hermes-agent/web/src/control/views/ProjekteView.tsx
---
"""
    (coord / "without-operator.md").write_text(note_without_operator, encoding="utf-8")

    registry = _hermes_infra_registry()
    agents, errors = _coordination_agents(coord, registry=registry)

    assert errors == []
    by_label = {a["label"]: a for a in agents}
    assert (
        by_label["with-operator"]["operator"]
        == "Piet (Grill-Session 16.07., Roadmap Punkt 8; /goal-Start 23:33)"
    )
    assert by_label["without-operator"]["operator"] is None


def test_reserved_slugs_sessions_and_commits_are_rejected(tmp_path: Path) -> None:
    yaml_text = """\
projects:
  - slug: sessions
    name: Sessions
    repo_path: /tmp/x
  - slug: commits
    name: Commits
    repo_path: /tmp/y
  - slug: fine
    name: Fine
    repo_path: /tmp/z
"""
    registry = load_projects_registry(path=_write(tmp_path, yaml_text))

    assert [p.slug for p in registry.projects] == ["fine"]
    assert any("reserved" in e for e in registry.errors)


# --- sessions payload (/api/projects/sessions) -------------------------------

_SESSIONS_SCHEMA_SQL = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    display_name TEXT,
    title TEXT,
    model TEXT,
    model_config TEXT,
    parent_session_id TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    end_reason TEXT,
    message_count INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cwd TEXT,
    git_repo_root TEXT
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    timestamp REAL NOT NULL
);
"""


def _make_state_db(path: Path) -> None:
    import sqlite3

    conn = sqlite3.connect(path)
    try:
        conn.executescript(_SESSIONS_SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()


def _insert_session(
    db_path: Path,
    *,
    session_id: str,
    started_at: float,
    ended_at: float | None = None,
    end_reason: str | None = None,
    parent_session_id: str | None = None,
    model_config: str | None = None,
    display_name: str | None = None,
    title: str | None = None,
    source: str = "cli",
    model: str | None = "kimi-k2",
    cwd: str | None = None,
    message_count: int = 3,
    input_tokens: int = 100,
    output_tokens: int = 50,
    last_message_at: float | None = None,
) -> None:
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO sessions (id, source, display_name, title, model, model_config, "
            "parent_session_id, started_at, ended_at, end_reason, message_count, "
            "input_tokens, output_tokens, cwd) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                source,
                display_name,
                title,
                model,
                model_config,
                parent_session_id,
                started_at,
                ended_at,
                end_reason,
                message_count,
                input_tokens,
                output_tokens,
                cwd,
            ),
        )
        if last_message_at is not None:
            conn.execute(
                "INSERT INTO messages (session_id, role, timestamp) VALUES (?, 'user', ?)",
                (session_id, last_message_at),
            )
        conn.commit()
    finally:
        conn.close()


def test_sessions_payload_open_active_and_spawn_tree(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    _make_state_db(db)
    now = 1_700_000_000

    # Open root session, recently active → is_open + is_active.
    _insert_session(
        db,
        session_id="root1",
        started_at=now - 3600,
        display_name="Hauptsession",
        cwd="/home/piet/.hermes/hermes-agent",
        last_message_at=now - 60,
    )
    # Delegate child (spawned by root1 via marker).
    _insert_session(
        db,
        session_id="child1",
        started_at=now - 1800,
        parent_session_id="root1",
        model_config=json.dumps({"_delegate_from": "root1"}),
        title="Subagent Lauf",
        last_message_at=now - 900,
    )
    # Branch child.
    _insert_session(
        db,
        session_id="branch1",
        started_at=now - 1200,
        parent_session_id="root1",
        model_config=json.dumps({"_branched_from": "root1"}),
        ended_at=now - 600,
        end_reason="user_exit",
    )
    # Compression continuation of an ended root.
    _insert_session(
        db,
        session_id="root2",
        started_at=now - 7200,
        ended_at=now - 5400,
        end_reason="compression",
        title="Alte Session",
    )
    _insert_session(
        db,
        session_id="root2-cont",
        started_at=now - 5400,
        parent_session_id="root2",
        title="Alte Session (2)",
        last_message_at=now - 4000,
    )
    # Ended long ago AND outside the window → excluded entirely.
    _insert_session(
        db,
        session_id="ancient",
        started_at=now - 10 * 24 * 3600,
        ended_at=now - 9 * 24 * 3600,
        end_reason="user_exit",
    )
    # Open but started long ago → kept (open beats window), not active.
    _insert_session(
        db,
        session_id="old-open",
        started_at=now - 5 * 24 * 3600,
        title="Uralte offene Session",
    )

    registry = _hermes_infra_registry()
    payload = build_sessions_payload(registry, state_db_path=db, now=now)

    assert payload["errors"] == []
    by_id = {s["id"]: s for s in payload["sessions"]}
    assert set(by_id) == {"root1", "child1", "branch1", "root2", "root2-cont", "old-open"}

    root = by_id["root1"]
    assert root["is_open"] is True
    assert root["is_active"] is True  # last message 60s ago
    assert root["stale_open"] is False
    assert root["label"] == "Hauptsession"
    assert root["project"] == "hermes-infra"  # cwd attribution
    assert root["spawn_kind"] is None
    assert root["spawned_by_id"] is None
    assert root["tokens"] == 150

    child = by_id["child1"]
    assert child["spawn_kind"] == "delegate"
    assert child["spawned_by_id"] == "root1"
    assert child["spawned_by_label"] == "Hauptsession"
    assert child["is_open"] is True
    assert child["is_active"] is False  # last message 900s ago > 300s window
    assert child["stale_open"] is False  # but well inside the 24h horizon

    branch = by_id["branch1"]
    assert branch["spawn_kind"] == "branch"
    assert branch["is_open"] is False  # ended
    assert branch["is_active"] is False
    assert branch["stale_open"] is False

    cont = by_id["root2-cont"]
    assert cont["spawn_kind"] == "compression"
    assert cont["spawned_by_label"] == "Alte Session"

    old_open = by_id["old-open"]
    assert old_open["is_open"] is True
    assert old_open["is_active"] is False
    assert old_open["stale_open"] is True  # open for 5d with zero activity
    assert old_open["project"] is None  # no cwd/git_repo_root

    # Open-first ordering: unclosed sessions lead regardless of age (the row
    # cap must never swallow an old-but-open session), then newest started_at.
    ids_in_order = [s["id"] for s in payload["sessions"]]
    opens_in_order = [sid for sid in ids_in_order if by_id[sid]["is_open"]]
    assert opens_in_order == ["child1", "root1", "root2-cont", "old-open"]
    first_ended_index = next(
        index for index, sid in enumerate(ids_in_order) if not by_id[sid]["is_open"]
    )
    assert first_ended_index == len(opens_in_order)  # all opens lead the list
    ended_started = [
        s["started_at"] for s in payload["sessions"] if not s["is_open"]
    ]
    assert ended_started == sorted(ended_started, reverse=True)


def test_sessions_payload_parent_outside_window_still_resolved(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    _make_state_db(db)
    now = 1_700_000_000

    # Parent ended 4 days ago: outside the 36h window and closed → not a row,
    # but the child's spawned_by_label/end_reason must still resolve.
    _insert_session(
        db,
        session_id="parent-old",
        started_at=now - 5 * 24 * 3600,
        ended_at=now - 4 * 24 * 3600,
        end_reason="compression",
        title="Vorzeit-Elter",
    )
    _insert_session(
        db,
        session_id="child-new",
        started_at=now - 600,
        parent_session_id="parent-old",
        title="Frische Fortsetzung",
    )

    payload = build_sessions_payload(
        ProjectsRegistry(projects=[], errors=[]), state_db_path=db, now=now
    )

    assert payload["errors"] == []
    assert [s["id"] for s in payload["sessions"]] == ["child-new"]
    child = payload["sessions"][0]
    assert child["spawn_kind"] == "compression"  # via parent's end_reason lookup
    assert child["spawned_by_label"] == "Vorzeit-Elter"


def test_sessions_payload_orphaned_delegate_has_no_parent_link(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    _make_state_db(db)
    now = 1_700_000_000
    _insert_session(
        db,
        session_id="orphan",
        started_at=now - 100,
        model_config=json.dumps({"_delegate_from": "__orphaned__"}),
        title="Verwaister Subagent",
    )

    payload = build_sessions_payload(
        ProjectsRegistry(projects=[], errors=[]), state_db_path=db, now=now
    )

    orphan = payload["sessions"][0]
    assert orphan["spawn_kind"] == "delegate"
    assert orphan["spawned_by_id"] is None
    assert orphan["spawned_by_label"] is None


def test_sessions_payload_missing_state_db_is_empty_no_error(tmp_path: Path) -> None:
    payload = build_sessions_payload(
        ProjectsRegistry(projects=[], errors=[]),
        state_db_path=tmp_path / "no-state-here.db",
        now=1_700_000_000,
    )
    assert payload == {"generated_at": 1_700_000_000, "errors": [], "sessions": []}


def test_sessions_and_commits_endpoints_return_200_frozen_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("hermes_cli.projects_overview.get_hermes_home", lambda: tmp_path)
    _make_state_db(tmp_path / "state.db")
    _insert_session(
        tmp_path / "state.db",
        session_id="s1",
        started_at=1_700_000_000,
        title="Endpunkt-Session",
    )

    app = FastAPI()
    register_projects_routes(app)
    client = TestClient(app)

    sessions_resp = client.get("/api/projects/sessions")
    assert sessions_resp.status_code == 200
    sessions_body = sessions_resp.json()
    assert isinstance(sessions_body["generated_at"], int)
    assert sessions_body["errors"] == []
    assert [s["id"] for s in sessions_body["sessions"]] == ["s1"]
    assert sessions_body["sessions"][0]["label"] == "Endpunkt-Session"

    commits_resp = client.get("/api/projects/commits")
    assert commits_resp.status_code == 200
    commits_body = commits_resp.json()
    assert isinstance(commits_body["generated_at"], int)
    assert commits_body["errors"] == []
    assert commits_body["commits"] == []

    # Static routes are registered before /{slug}: they must NOT be captured
    # as project slugs (which would answer 404 unknown-project).
    assert client.get("/api/projects/sessions").status_code == 200
    assert client.get("/api/projects/commits").status_code == 200
