"""PROPOSED — Coord-Profile Variante des Discord Stop-Code Inbound-Gate Tests.

Status (Sprint 2P, 2026-05-16):
    Diese Datei liegt im Vault unter ``vault/03-Agents/Hermes/_proposed/`` und ist
    NICHT live. Sie gehoert nach Abschluss von Sprint-2-Main T1
    (Coord-Profile Stop-Code-Gate-Symmetrie, OI-13) nach::

        ~/.hermes/profiles/coordinator/tests/gateway/test_discord_stop_code_gate.py

    bzw. — falls die Coord-Side den shared ``~/.hermes/hermes-agent/`` nutzt —
    als Sister-Datei neben dem Hub-Test::

        ~/.hermes/hermes-agent/tests/gateway/test_coord_discord_stop_code_gate.py

    Move-Anleitung: siehe ``vault/03-Agents/Hermes/_proposed/README-test-coord-gate.md``.

Hintergrund:
    - Spec : vault/03-Agents/Hermes/playbooks/hub-coordinator-reporting-v1.md §6.1 + §6.3 + §6.4
    - RCA  : vault/03-Agents/Hermes/analyses/bot-loop-rca-and-architecture-2026-05-16T1748.md
    - Hub-Vorbild: ~/.hermes/hermes-agent/tests/gateway/test_discord_stop_code_gate.py (14 cases)

Kern-Unterschied Hub vs Coord:
    Hub erwartet im Body **Inbound-Codes** (§6.2, 6 Codes) — diese kommen aus
    Coord-Richtung. Coord empfaengt aus Hub-Richtung **Outbound-Codes** (§6.1,
    8 Codes). Coord-Side L3 muss daher mit env-Override::

        HERMES_DISCORD_INBOUND_STOP_CODES=
          approval-missing,operator-lock-missing,dispatch-rejected,
          scope-violation,gate-blocked,hub-unavailable,
          poll-timeout,coord-stuck-60s

    laufen. ``inbound`` im Variablen-Namen ist relativ zum jeweiligen Listener —
    fuer Coord ist die ``Outbound``-Code-Liste die ``inbound``-Liste.
"""

import os
import re
import unittest
from unittest.mock import MagicMock, patch


COORD_INBOUND_CODES = {
    # 8 Outbound-Codes aus playbooks/hub-coordinator-reporting-v1.md §6.1
    "approval-missing",
    "operator-lock-missing",
    "dispatch-rejected",
    "scope-violation",
    "gate-blocked",
    "hub-unavailable",
    "poll-timeout",
    "coord-stuck-60s",
}

COORD_INBOUND_CSV = ",".join(sorted(COORD_INBOUND_CODES))

HUB_INBOUND_CODES = {
    # zum Negativ-Test "wrong-lane": Hub-Inbound darf NICHT bei Coord passieren
    "hub-plan-ready",
    "hub-budget-warn",
    "hub-memory-warn",
    "hub-shutdown",
    "coord-ack-required",
    "reviewer-rejected",
}


def _make_author(*, bot=False, is_self=False):
    a = MagicMock()
    a.bot = bot
    a.id = 99999 if is_self else 12345
    a.name = "TestBot" if bot else "TestUser"
    a.display_name = a.name
    return a


def _make_message(*, author=None, content="hello", mentions=None, is_dm=False):
    msg = MagicMock()
    msg.author = author or _make_author()
    msg.content = content
    msg.attachments = []
    msg.mentions = mentions or []
    msg.webhook_id = None
    msg.application_id = None
    if is_dm:
        import discord
        msg.channel = MagicMock(spec=discord.DMChannel)
        msg.channel.id = 111
        msg.guild = None
    else:
        msg.channel = MagicMock()
        msg.channel.id = 222
        msg.channel.name = "test-channel"
        msg.channel.guild = MagicMock()
        msg.guild = msg.channel.guild
        type(msg.channel).__name__ = "TextChannel"
    return msg


def _stop_code_gate(message, client_user, allow_bots="mentions"):
    """Spiegelt discord.py:on_message Stop-Code-Logik.

    Returns True wenn die Message am Bot/Stop-Code-Gate vorbei kommt,
    False wenn sie verworfen wird. Wie im Hub-Test, nur mit Coord-Default
    fuer ``HERMES_DISCORD_INBOUND_STOP_CODES`` (Outbound-Code-Liste).
    """
    if message.author == client_user:
        return False  # own messages

    is_dm = message.guild is None
    self_mentioned = bool(client_user and client_user in message.mentions)
    author_is_bot = getattr(message.author, "bot", False) is True
    app_authored = author_is_bot or message.webhook_id or message.application_id

    if not app_authored:
        return True  # humans pass

    allow = (allow_bots or "none").lower().strip()
    if not is_dm and not self_mentioned:
        return False
    if allow == "none":
        return False
    elif allow == "mentions":
        if not self_mentioned:
            return False

    # L3 — Stop-Code-Gate (Coord-Side default = Outbound-Codes)
    if not is_dm:
        stop_codes_csv = os.getenv(
            "HERMES_DISCORD_INBOUND_STOP_CODES",
            COORD_INBOUND_CSV,
        )
        inbound_codes = {
            c.strip().lower() for c in stop_codes_csv.split(",") if c.strip()
        }
        if "*" not in inbound_codes:
            body = getattr(message, "content", "") or ""
            m = re.search(r"\[STOP-CODE:\s*([a-zA-Z0-9_-]+)\]", body)
            if not m or m.group(1).lower() not in inbound_codes:
                return False

    return True


class TestCoordStopCodeGate(unittest.TestCase):
    """Coord-Profile Variante des Discord Stop-Code Inbound-Gates.

    14 cases (Auftrag Sprint 2P / T9p): 8 valid accepts (eine pro Outbound-Code),
    1 wrong-lane reject (Hub-Inbound-Code wird bei Coord rejected),
    1 plain reject, 1 DM bypass, 1 env-override `*`, 1 human bypass,
    1 malformed reject.
    """

    def setUp(self):
        os.environ.pop("HERMES_DISCORD_INBOUND_STOP_CODES", None)

    # --- 8 Accept-Cases: jeder Outbound-Code aus §6.1 ---

    def _accept_helper(self, code):
        self_user = _make_author(is_self=True)
        bot = _make_author(bot=True)
        msg = _make_message(
            author=bot,
            content=f"[STOP-CODE: {code}] context line",
            mentions=[self_user],
        )
        self.assertTrue(
            _stop_code_gate(msg, client_user=self_user),
            f"Outbound code {code!r} should pass Coord-L3-Gate",
        )

    def test_01_accept_approval_missing(self):
        self._accept_helper("approval-missing")

    def test_02_accept_operator_lock_missing(self):
        self._accept_helper("operator-lock-missing")

    def test_03_accept_dispatch_rejected(self):
        self._accept_helper("dispatch-rejected")

    def test_04_accept_scope_violation(self):
        self._accept_helper("scope-violation")

    def test_05_accept_gate_blocked(self):
        self._accept_helper("gate-blocked")

    def test_06_accept_hub_unavailable(self):
        self._accept_helper("hub-unavailable")

    def test_07_accept_poll_timeout(self):
        self._accept_helper("poll-timeout")

    def test_08_accept_coord_stuck_60s(self):
        self._accept_helper("coord-stuck-60s")

    # --- 6 Reject-/Bypass-Cases ---

    def test_09_reject_inbound_wrong_lane(self):
        """Hub-Inbound-Code (z.B. hub-plan-ready) darf bei Coord NICHT passieren."""
        self_user = _make_author(is_self=True)
        bot = _make_author(bot=True)
        msg = _make_message(
            author=bot,
            content="[STOP-CODE: hub-plan-ready] wrong lane for coord",
            mentions=[self_user],
        )
        self.assertFalse(
            _stop_code_gate(msg, client_user=self_user),
            "Hub-Inbound-Code must be rejected by Coord-L3-Gate",
        )

    def test_10_reject_plain_message_no_stop_code(self):
        """Bot-Body ohne Marker → silent drop."""
        self_user = _make_author(is_self=True)
        bot = _make_author(bot=True)
        msg = _make_message(
            author=bot, content="just a chatty status", mentions=[self_user]
        )
        self.assertFalse(_stop_code_gate(msg, client_user=self_user))

    def test_11_dm_bypasses_gate(self):
        """DM (kein shared channel) umgeht das Gate."""
        self_user = _make_author(is_self=True)
        bot = _make_author(bot=True)
        msg = _make_message(author=bot, content="hi", is_dm=True)
        self.assertTrue(_stop_code_gate(msg, client_user=self_user, allow_bots="all"))

    def test_12_env_override_wildcard_disables_gate(self):
        """HERMES_DISCORD_INBOUND_STOP_CODES=* deaktiviert L3 komplett."""
        self_user = _make_author(is_self=True)
        bot = _make_author(bot=True)
        msg = _make_message(author=bot, content="[idle]", mentions=[self_user])
        with patch.dict(os.environ, {"HERMES_DISCORD_INBOUND_STOP_CODES": "*"}):
            self.assertTrue(_stop_code_gate(msg, client_user=self_user))

    def test_13_human_bypasses_gate(self):
        """Human-Author wird vom L3-Gate nicht beruehrt (L3 nur fuer Bot/App)."""
        self_user = _make_author(is_self=True)
        human = _make_author(bot=False)
        msg = _make_message(author=human, content="normal text without marker")
        self.assertTrue(_stop_code_gate(msg, client_user=self_user))

    def test_14_malformed_marker_rejected(self):
        """Falsch geformter Marker (z.B. ohne []) wird abgelehnt."""
        self_user = _make_author(is_self=True)
        bot = _make_author(bot=True)
        msg = _make_message(
            author=bot,
            content="STOP-CODE: approval-missing  (no brackets, malformed)",
            mentions=[self_user],
        )
        self.assertFalse(_stop_code_gate(msg, client_user=self_user))


if __name__ == "__main__":
    unittest.main()
