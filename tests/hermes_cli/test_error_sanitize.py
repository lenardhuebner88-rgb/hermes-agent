import logging

from hermes_cli.error_sanitize import safe_detail, scrub_detail


def test_scrub_detail_strips_posix_absolute_path():
    assert scrub_detail("failed at /var/log/hermes/agent.log") == "failed at <path>"


def test_scrub_detail_strips_home_path():
    assert scrub_detail("missing /home/piet/.hermes/config.yaml") == "missing <path>"


def test_scrub_detail_strips_windows_absolute_path():
    assert scrub_detail(r"missing C:\Users\piet\.hermes\config.yaml") == "missing <path>"


def test_scrub_detail_traceback_or_multiline_collapses_to_empty_for_generic():
    assert scrub_detail("Traceback (most recent call last)\n  File /tmp/x.py") == ""
    assert scrub_detail("line one\nline two") == ""


def test_scrub_detail_clean_message_passes_through():
    assert scrub_detail("gateway probe exploded") == "gateway probe exploded"


def test_scrub_detail_keeps_api_path():
    assert scrub_detail("GET /api/x failed") == "GET /api/x failed"


def test_scrub_detail_caps_length():
    assert len(scrub_detail("x" * 400)) == 300


def test_safe_detail_logs_and_returns_scrubbed_message(caplog):
    log = logging.getLogger("tests.error_sanitize")
    caplog.set_level(logging.ERROR, logger=log.name)

    try:
        raise RuntimeError("failed at /home/piet/.hermes/config.yaml")
    except RuntimeError as exc:
        detail = safe_detail(exc, "Operation failed", log=log)

    assert detail == "failed at <path>"
    assert "Operation failed" in caplog.text
    assert "/home/piet/.hermes/config.yaml" in caplog.text


def test_scrub_detail_strips_single_segment_system_dirs():
    # Depth-1 absolute paths to known system roots leak sensitive location info
    # (e.g. PermissionError: "[Errno 13] Permission denied: '/root'").
    for d in ("root", "etc", "opt", "srv", "boot", "mnt", "usr", "proc", "sys", "dev"):
        out = scrub_detail(f"Permission denied: /{d}")
        assert out == "Permission denied: <path>", (d, out)


def test_scrub_detail_strips_double_slash_system_path():
    # Valid double-slash POSIX forms must still be redacted, not leaked.
    assert "root" not in scrub_detail("Permission denied: //root")
    assert "<path>" in scrub_detail("Permission denied: //root")
    assert "passwd" not in scrub_detail("leak //etc/passwd here")


def test_scrub_detail_does_not_scrub_nonsystem_single_segment():
    # The allowlist — not the slash count — decides a depth-1 redaction, so a
    # bare /segment that is not a known system root is left untouched.
    assert scrub_detail("see /nonsystem leaf") == "see /nonsystem leaf"
    assert scrub_detail("token /abc123") == "token /abc123"


def test_scrub_detail_preserves_urls():
    # Extending the allowlist must not regress URL handling: scheme URLs with a
    # bare host or a double-slash path segment stay byte-identical.
    assert scrub_detail("Error fetching http://example.com") == "Error fetching http://example.com"
    assert (
        scrub_detail("GET https://example.com//health failed")
        == "GET https://example.com//health failed"
    )


def test_scrub_detail_no_overscrub_word_internal_slashes():
    for s in ("use and/or here", "ratio 24/7 uptime", "read/write access denied"):
        assert scrub_detail(s) == s, s


def test_safe_detail_uses_generic_for_traceback_like_message(caplog):
    log = logging.getLogger("tests.error_sanitize.generic")
    caplog.set_level(logging.ERROR, logger=log.name)

    try:
        raise RuntimeError("line one\nline two")
    except RuntimeError as exc:
        detail = safe_detail(exc, "Generic failure", log=log)

    assert detail == "Generic failure"
    assert "Generic failure" in caplog.text
