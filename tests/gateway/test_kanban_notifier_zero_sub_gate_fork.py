"""Fork-Gegenstueck zu Upstreams Zero-Subscription-Gate-Test.

Upstreams ``tests/gateway/test_kanban_notifier_zero_sub_gate.py::
test_zero_sub_board_is_never_opened_writable`` behauptet, ein Tick des Notifiers
oeffne fuer ein Board ohne Subscriptions **ueberhaupt keine** Verbindung
(``spy_connect.assert_not_called()``).

Dieser Fork erfuellt Upstreams eigentliche Zusage — der ZUSTELLPFAD ueberspringt
den schreibenden Open, ``count_notify_subs`` liefert 0 und ``_collect`` macht
``continue`` — aber nicht die Test-Zusicherung: unser Tick macht mehr als
Upstreams. ``gateway/kanban_watchers.py`` hat 11 ``_kb.connect(``-Stellen gegen
Upstreams 6, weil derselbe Tick zusaetzlich Alert-Regeln und den
Stalled-Tree-Flush faehrt. Diese Sweeps brauchen eine Verbindung unabhaengig von
Subscriptions.

Upstreams Test bleibt daher bewusst rot und ist im Landing-Report namentlich als
Divergenz gefuehrt. Dieser Test haelt fest, was von Upstreams Absicht gilt, damit
eine echte Regression im Zero-Sub-Gate nicht unbemerkt bleibt.
"""

from unittest.mock import patch

from hermes_cli import kanban_db as kb


def _make_completed_task_without_sub() -> None:
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="zero sub gate", assignee="worker")
        kb.complete_task(conn, tid, result="ok")
    finally:
        conn.close()


def test_zero_sub_board_reports_zero_and_is_skipped_by_the_probe(tmp_path, monkeypatch):
    """Der Read-only-Probe-Pfad, den Upstream eingefuehrt hat, greift.

    Das ist der Teil von Upstreams Zusage, den dieser Fork traegt: fuer ein
    Board ohne Subscriptions meldet ``count_notify_subs`` 0, und genau darauf
    steigt ``_collect`` vor dem schreibenden ``connect`` aus.
    """
    _make_completed_task_without_sub()

    # Read-only, legt die DB nicht an, faehrt keine Migration.
    assert kb.count_notify_subs(board="default") == 0

    # Und der Probe-Pfad selbst oeffnet die DB nicht schreibend.
    with patch.object(kb, "connect", wraps=kb.connect) as spy_connect:
        assert kb.count_notify_subs(board="default") == 0
    spy_connect.assert_not_called()


def test_subscribed_board_reports_nonzero(tmp_path, monkeypatch):
    """Gegenprobe: mit Subscription meldet die Probe > 0.

    Ohne diese Kontrolle wuerde ein ``count_notify_subs``, das immer 0 liefert,
    den Test oben stillschweigend gruen halten und das Gate waere blind.
    """
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="subscribed", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="123")
    finally:
        conn.close()

    assert kb.count_notify_subs(board="default") > 0
