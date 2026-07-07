from hermes_cli import design_board_kanban as dbk
from hermes_cli import design_board_store as store


def test_terminal_set():
    assert dbk.TERMINAL == {"done", "archived"}


def test_task_facets_skips_missing(monkeypatch):
    class FakeTask:
        def __init__(self, id, status, assignee):
            self.id, self.status, self.assignee = id, status, assignee

    def fake_get_task(conn, tid):
        return FakeTask(tid, "running", "coder") if tid == "t_ok" else None

    monkeypatch.setattr(dbk, "_get_task", fake_get_task)
    monkeypatch.setattr(dbk, "_open_ro", lambda: _Dummy())
    # _open_ro is monkeypatched, so no real DB is touched.
    facets = dbk.task_facets(["t_ok", "t_missing"])
    assert facets == [{"id": "t_ok", "status": "running", "assignee": "coder", "terminal": False}]


class _Dummy:
    def __enter__(self): return object()
    def __exit__(self, *a): return False

class _FakeRow:
    def __init__(self, id, status, assignee):
        self._data = {"id": id, "status": status, "assignee": assignee}

    def __getitem__(self, key):
        return self._data[key]

class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, _query, _params=None):
        return self

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

def test_batch_task_facets(monkeypatch):
    rows = [
        _FakeRow("t_1", "done", "coder"),
        _FakeRow("t_2", "running", None),
    ]
    monkeypatch.setattr(dbk, "_open_ro", lambda: _FakeConn(rows))
    facets = dbk.batch_task_facets(["t_1", "t_2", "t_missing"])
    assert facets == {
        "t_1": {"id": "t_1", "status": "done", "assignee": "coder", "terminal": True},
        "t_2": {"id": "t_2", "status": "running", "assignee": None, "terminal": False},
    }


def test_batch_task_facets_empty():
    assert dbk.batch_task_facets([]) == {}


def test_batch_task_facets_dedupes_ids(monkeypatch):
    calls = []

    class _TrackingConn:
        def __init__(self, rows):
            self._rows = rows

        def execute(self, query, params=None):
            calls.append(params)
            return self

        def fetchall(self):
            return self._rows

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    rows = [_FakeRow("t_1", "done", "coder")]
    monkeypatch.setattr(dbk, "_open_ro", lambda: _TrackingConn(rows))
    facets = dbk.batch_task_facets(["t_1", "t_1", "t_1"])
    assert facets == {
        "t_1": {"id": "t_1", "status": "done", "assignee": "coder", "terminal": True},
    }
    assert len(calls) == 1
    assert calls[0] == ["t_1"]


def test_batch_task_facets_chunks_large_id_lists(monkeypatch):
    calls = []

    class _TrackingConn:
        def execute(self, query, params=None):
            calls.append(list(params) if params else [])
            return self

        def fetchall(self):
            return []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(dbk, "_open_ro", lambda: _TrackingConn())
    ids = [f"t_{i}" for i in range(1500)]
    assert dbk.batch_task_facets(ids) == {}
    assert len(calls) == 2
    assert len(calls[0]) == 900
    assert len(calls[1]) == 600


def test_after_screenshot_attaches_system_entry(monkeypatch):
    card_id = store.create_card(
        kind="bug",
        title="Gap",
        target={"view": "/control/fleet"},
    )
    store.link_task(card_id, "t_done")

    monkeypatch.setattr(dbk, "_render_dashboard_view", lambda card: b"png")

    entries = dbk.attach_after_screenshots_for_task("t_done", status="done")

    assert len(entries) == 1
    updated = store.get_card(card_id)
    entry = updated["entries"][0]
    assert entry["author"] == "system"
    assert entry["kind"] == "screenshot"
    assert entry["asset"].endswith("after-t_done.png")
    assert entry["note"] == "after-screenshot task:t_done"


def test_completion_receipt_attaches_system_comment(monkeypatch):
    card_id = store.create_card(
        kind="bug",
        title="Gap",
        target={"view": "/control/fleet"},
    )
    store.link_task(card_id, "t_done")
    monkeypatch.setattr(dbk, "_completion_receipt_metadata", lambda task_id, run_id=None: (1735689600, "abc123"))

    entries = dbk.attach_completion_receipts_for_task("t_done", status="done", run_id=42)

    assert len(entries) == 1
    updated = store.get_card(card_id)
    assert updated is not None
    entry = updated["entries"][0]
    assert entry["author"] == "system"
    assert entry["kind"] == "comment"
    assert entry["note"] == "task-receipt task:t_done completed_at:2025-01-01T00:00:00Z commit:abc123"


def test_completion_receipt_is_idempotent_per_task(monkeypatch):
    card_id = store.create_card(
        kind="bug",
        title="Gap",
        target={"view": "/control/fleet"},
    )
    store.link_task(card_id, "t_done")
    monkeypatch.setattr(dbk, "_completion_receipt_metadata", lambda task_id, run_id=None: (1735689600, "abc123"))

    assert len(dbk.attach_completion_receipts_for_task("t_done", status="done")) == 1
    assert dbk.attach_completion_receipts_for_task("t_done", status="done") == []


def test_completion_receipt_skips_non_terminal(monkeypatch):
    card_id = store.create_card(
        kind="bug",
        title="Gap",
        target={"view": "/control/fleet"},
    )
    store.link_task(card_id, "t_running")
    monkeypatch.setattr(dbk, "_completion_receipt_metadata", lambda task_id, run_id=None: (1735689600, "abc123"))

    assert dbk.attach_completion_receipts_for_task("t_running", status="running") == []
    updated = store.get_card(card_id)
    assert updated is not None
    assert updated["entries"] == []


def test_after_screenshot_degrades_to_comment_on_render_error(monkeypatch):
    card_id = store.create_card(
        kind="bug",
        title="Gap",
        target={"view": "/control/fleet"},
    )
    store.link_task(card_id, "t_done")

    def fail(_card):
        raise RuntimeError("chromium missing")

    monkeypatch.setattr(dbk, "_render_dashboard_view", fail)

    entries = dbk.attach_after_screenshots_for_task("t_done", status="done")

    assert len(entries) == 1
    entry = store.get_card(card_id)["entries"][0]
    assert entry["author"] == "system"
    assert entry["kind"] == "comment"
    assert "chromium missing" in entry["note"]


def test_after_screenshot_is_idempotent_per_task(monkeypatch):
    card_id = store.create_card(
        kind="bug",
        title="Gap",
        target={"view": "/control/fleet"},
    )
    store.link_task(card_id, "t_done")

    calls = []
    monkeypatch.setattr(dbk, "_render_dashboard_view", lambda card: calls.append(card) or b"png")

    assert len(dbk.attach_after_screenshots_for_task("t_done", status="done")) == 1
    assert dbk.attach_after_screenshots_for_task("t_done", status="done") == []
    assert len(calls) == 1


def test_dashboard_url_for_target_view(monkeypatch):
    monkeypatch.setenv("HERMES_DESIGN_BOARD_DASHBOARD_BASE_URL", "http://127.0.0.1:9119/")

    assert dbk._dashboard_url_for_card({"target": {"view": "/control/fleet"}}) == "http://127.0.0.1:9119/control/fleet"
    assert dbk._dashboard_url_for_card({"target": {"view": "control/fleet"}}) == "http://127.0.0.1:9119/control/fleet"
    assert dbk._dashboard_url_for_card({"target": {"view": "https://example.test/x"}}) == "https://example.test/x"
