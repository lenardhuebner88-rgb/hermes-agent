"""TDD: TodoStore._validate must LOG when it coerces malformed input.

_validate silently turns garbage into plausible defaults (id='?', content=
'(no description)', status='pending'). That is the right *behavior* for a
planning aid that must not crash, but the silence means corruption leaves no
trail. These tests pin observability: a WARNING is emitted on each coercion
and (importantly) NOT emitted for a fully-valid item. Behavior is unchanged.
"""

from __future__ import annotations

import logging

from tools.todo_tool import TodoStore

LOGGER = "tools.todo_tool"


def _warnings(caplog) -> list:
    return [r for r in caplog.records if r.levelno >= logging.WARNING]


class TestValidateCoercionLogging:
    def test_warns_on_non_dict_item(self, caplog):
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            result = TodoStore._validate("garbage")  # type: ignore[arg-type]
        assert result == {"id": "?", "content": "(invalid item)", "status": "pending"}
        assert _warnings(caplog), "expected a WARNING for a non-dict item"

    def test_warns_on_missing_id(self, caplog):
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            result = TodoStore._validate({"content": "x", "status": "pending"})
        assert result["id"] == "?"
        assert _warnings(caplog), "expected a WARNING for a missing id"

    def test_warns_on_empty_content(self, caplog):
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            result = TodoStore._validate({"id": "1", "content": "  ", "status": "pending"})
        assert result["content"] == "(no description)"
        assert _warnings(caplog), "expected a WARNING for empty content"

    def test_warns_on_invalid_status(self, caplog):
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            result = TodoStore._validate({"id": "1", "content": "x", "status": "bogus"})
        assert result["status"] == "pending"
        assert _warnings(caplog), "expected a WARNING for an invalid status"

    def test_no_warning_for_valid_item(self, caplog):
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            result = TodoStore._validate({"id": "1", "content": "x", "status": "pending"})
        assert result == {"id": "1", "content": "x", "status": "pending"}
        assert not _warnings(caplog), "a valid item must not warn"
