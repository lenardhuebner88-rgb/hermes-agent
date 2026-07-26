"""Regression coverage for worker-only Langfuse correlation forwarding."""


def test_non_worker_context_is_empty(monkeypatch):
    from hermes_cli.observability_context import resolve_observability_context

    for name in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_PROFILE"):
        monkeypatch.delenv(name, raising=False)

    assert resolve_observability_context() == {}


def test_worker_context_resolves_and_normalizes_dimensions(monkeypatch):
    import hermes_cli.observability_context as mod

    monkeypatch.setenv("HERMES_KANBAN_TASK", " t-child ")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", " 77 ")
    monkeypatch.setenv("HERMES_PROFILE", " coder ")
    monkeypatch.setattr(mod, "_resolve_chain_and_lane", lambda task_id: (" t-root ", " coder "))

    assert mod.resolve_observability_context() == {
        "task_run_id": "77",
        "chain_id": "t-root",
        "lane": "coder",
    }