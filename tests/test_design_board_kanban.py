from hermes_cli import design_board_kanban as dbk


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
