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
