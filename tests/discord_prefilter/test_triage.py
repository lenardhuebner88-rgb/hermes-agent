"""Unit tests for the pre-filter triage parser + noise heuristic.

The triage core is the one piece of pure logic in the Discord pre-filter
bridge, so it is the piece worth testing in isolation. Everything else
(discord.py I/O, subprocess spawning of ``claude -p`` / ``hermes``) is thin
glue exercised by the live end-to-end checks in the plan.

Contract under test:
  * ``parse_triage_output`` turns the ``claude -p --output-format json``
    envelope into a ``TriageDecision`` and is **fail-open**: anything it
    cannot confidently read becomes ``ESCALATE`` (never silently dropped).
  * ``heuristic_noise`` catches obvious noise *before* a model is spawned.
"""

import json

from bridges.discord_prefilter.triage import (
    Bucket,
    TriageDecision,
    build_noise_matchers,
    heuristic_noise,
    parse_triage_output,
)


def _envelope(inner: str) -> str:
    """Wrap model text in the shape ``claude -p --output-format json`` emits."""
    return json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": inner,
            "session_id": "abc",
        }
    )


# --- parse_triage_output: happy paths -------------------------------------


def test_trivial_with_reply():
    out = _envelope(json.dumps({"bucket": "trivial", "reply": "Alles läuft. ✅"}))
    d = parse_triage_output(out)
    assert d.bucket is Bucket.TRIVIAL
    assert d.reply == "Alles läuft. ✅"
    assert d.source == "model"


def test_escalate_null_reply():
    out = _envelope(json.dumps({"bucket": "escalate", "reply": None}))
    d = parse_triage_output(out)
    assert d.bucket is Bucket.ESCALATE
    assert d.reply is None


def test_noise():
    out = _envelope(json.dumps({"bucket": "noise", "reply": None}))
    d = parse_triage_output(out)
    assert d.bucket is Bucket.NOISE


# --- parse_triage_output: robustness --------------------------------------


def test_inner_json_wrapped_in_code_fence():
    inner = "```json\n" + json.dumps({"bucket": "trivial", "reply": "hi"}) + "\n```"
    d = parse_triage_output(_envelope(inner))
    assert d.bucket is Bucket.TRIVIAL
    assert d.reply == "hi"


def test_inner_json_with_surrounding_prose():
    inner = 'Sure! Here you go: {"bucket": "noise", "reply": null} — done.'
    d = parse_triage_output(_envelope(inner))
    assert d.bucket is Bucket.NOISE


def test_raw_text_without_envelope_is_still_parsed():
    # If --output-format text was used (or the envelope is absent), the raw
    # stdout IS the model text. Parser must still find the inner JSON.
    raw = json.dumps({"bucket": "escalate", "reply": None})
    d = parse_triage_output(raw)
    assert d.bucket is Bucket.ESCALATE


# --- parse_triage_output: fail-open to ESCALATE ---------------------------


def test_broken_json_fails_open_to_escalate():
    d = parse_triage_output("not json at all <<>>")
    assert d.bucket is Bucket.ESCALATE
    assert d.source == "fallback"


def test_empty_output_fails_open_to_escalate():
    d = parse_triage_output("")
    assert d.bucket is Bucket.ESCALATE
    assert d.source == "fallback"


def test_unknown_bucket_value_fails_open_to_escalate():
    out = _envelope(json.dumps({"bucket": "delete_everything", "reply": "x"}))
    d = parse_triage_output(out)
    assert d.bucket is Bucket.ESCALATE


def test_trivial_with_blank_reply_is_escalated():
    # A "trivial" verdict with no usable answer cannot be answered cheaply;
    # better to escalate than to post an empty message.
    out = _envelope(json.dumps({"bucket": "trivial", "reply": "   "}))
    d = parse_triage_output(out)
    assert d.bucket is Bucket.ESCALATE


def test_error_envelope_fails_open_to_escalate():
    out = json.dumps({"type": "result", "is_error": True, "result": "rate limited"})
    d = parse_triage_output(out)
    assert d.bucket is Bucket.ESCALATE
    assert d.source == "fallback"


# --- heuristic_noise -------------------------------------------------------


def test_heuristic_matches_configured_pattern():
    matchers = build_noise_matchers([r"^\s*gm\b", r"^\s*\+1\s*$"])
    assert heuristic_noise("gm everyone", matchers) is True
    assert heuristic_noise("+1", matchers) is True
    assert heuristic_noise("can you build the report?", matchers) is False


def test_heuristic_empty_patterns_never_matches():
    matchers = build_noise_matchers([])
    assert heuristic_noise("anything", matchers) is False


def test_heuristic_ignores_invalid_regex():
    # A bad pattern in config must not crash the bridge; it is skipped.
    matchers = build_noise_matchers([r"(unclosed", r"^\s*ok\s*$"])
    assert heuristic_noise("ok", matchers) is True


def test_triage_decision_is_frozen_dataclass():
    d = TriageDecision(bucket=Bucket.NOISE, reply=None)
    assert d.bucket is Bucket.NOISE
    assert d.reply is None


# --- run_triage: spawn-cmd contract ----------------------------------------


def test_run_triage_cmd_excludes_memsearch(monkeypatch):
    """The per-message classifier must not load the memsearch memory plugin
    (Planspec 2026-06-12 memsearch-voll-rollout, T3): targeted --settings
    disable on the cmd + MEMSEARCH_NO_WATCH belt in the env."""
    from types import SimpleNamespace
    from unittest import mock

    from bridges.discord_prefilter import triage as triage_mod
    from bridges.discord_prefilter.config import PrefilterConfig

    config = PrefilterConfig(
        discord_token="x",
        channel_id=1,
        model="haiku",
        claude_bin="/usr/bin/true",
        noise_matchers=build_noise_matchers([]),
    )

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env", {})
        return SimpleNamespace(returncode=0, stdout=_envelope(
            json.dumps({"bucket": "escalate", "reply": None})
        ), stderr="")

    with mock.patch.object(triage_mod.subprocess, "run", fake_run):
        triage_mod.run_triage("echte Aufgabe bitte", config)

    cmd = captured["cmd"]
    s_idx = cmd.index("--settings")
    settings = json.loads(cmd[s_idx + 1])
    assert settings["enabledPlugins"]["memsearch@memsearch-plugins"] is False
    assert "--bare" not in cmd
    assert captured["env"]["MEMSEARCH_NO_WATCH"] == "1"
