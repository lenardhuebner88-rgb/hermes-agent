from __future__ import annotations

from pathlib import Path

from scripts import storage_guard as sg

DATA_ROOT = Path("/mnt/data")

FSTAB = """\
# a comment
UUID=abc  /  ext4  defaults  0 1
UUID=def  /mnt/data  ext4  defaults,noatime,nofail  0 2
/mnt/data/hermes/backups /home/piet/.hermes/backups none bind,nofail 0 0
/mnt/data/hermes/loops /home/piet/.hermes/loops none bind,nofail,x-systemd.requires=/mnt/data 0 0
/srv/elsewhere /home/piet/elsewhere none bind 0 0
"""


def _mountinfo(targets: list[str]) -> str:
    return "\n".join(
        f"{i} 1 0:{i} / {target} rw,relatime - ext4 /dev/sda1 rw"
        for i, target in enumerate(targets, start=20)
    )


def _usage(percent_by_path: dict[str, float]):
    def lookup(path: Path):
        percent = percent_by_path.get(str(path))
        if percent is None:
            return None
        total = 100 * 2**30
        free = int(total * (100 - percent) / 100)
        return sg.Usage(path, percent, free, total)

    return lookup


def _evaluate(mounted: list[str], percents: dict[str, float], **kwargs):
    return sg.evaluate(
        fstab_text=kwargs.pop("fstab_text", FSTAB),
        mountinfo_text=_mountinfo(mounted),
        data_root=DATA_ROOT,
        watched=(Path("/"), DATA_ROOT),
        warn_percent=80.0,
        alarm_percent=90.0,
        usage_lookup=_usage(percents),
        **kwargs,
    )


HEALTHY = ["/", "/mnt/data", "/home/piet/.hermes/backups", "/home/piet/.hermes/loops"]


def test_only_data_root_binds_are_expected():
    binds = sg.parse_fstab_binds(FSTAB, data_root=DATA_ROOT)
    assert binds == [
        ("/mnt/data/hermes/backups", "/home/piet/.hermes/backups"),
        ("/mnt/data/hermes/loops", "/home/piet/.hermes/loops"),
    ]
    # a bind that has nothing to do with the data disk must not be watched
    assert all(target != "/home/piet/elsewhere" for _source, target in binds)


def test_healthy_layout_reports_nothing():
    findings, usages = _evaluate(HEALTHY, {"/": 40.0, "/mnt/data": 12.0})
    assert findings == []
    assert {str(u.path) for u in usages} == {"/", "/mnt/data"}


def test_missing_bind_mount_is_an_alarm():
    mounted = [t for t in HEALTHY if t != "/home/piet/.hermes/backups"]
    findings, _ = _evaluate(mounted, {"/": 40.0, "/mnt/data": 12.0})
    assert [f.level for f in findings] == ["alarm"]
    assert "/home/piet/.hermes/backups" in findings[0].message


def test_absent_data_root_reports_one_cause_not_every_consequence():
    """Without the disk every bind is missing too; the operator needs the one
    actionable line, not a list of symptoms burying it."""
    findings, _ = _evaluate(["/"], {"/": 40.0, "/mnt/data": 40.0})
    assert len(findings) == 1
    assert findings[0].level == "alarm"
    assert "NOT mounted" in findings[0].message


def test_headroom_thresholds():
    warn, _ = _evaluate(HEALTHY, {"/": 85.0, "/mnt/data": 12.0})
    assert [f.level for f in warn] == ["warn"]

    alarm, _ = _evaluate(HEALTHY, {"/": 93.0, "/mnt/data": 12.0})
    assert [f.level for f in alarm] == ["alarm"]

    # the escape valve filling up is itself a problem
    data_full, _ = _evaluate(HEALTHY, {"/": 40.0, "/mnt/data": 91.0})
    assert [f.level for f in data_full] == ["alarm"]
    assert "/mnt/data" in data_full[0].message


def test_main_is_silent_when_healthy(tmp_path, capsys, monkeypatch):
    fstab = tmp_path / "fstab"
    fstab.write_text(FSTAB)
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(_mountinfo(HEALTHY))
    monkeypatch.setattr(sg, "usage_for", _usage({"/": 40.0, "/mnt/data": 12.0}))

    rc = sg.main(["--fstab", str(fstab), "--mountinfo", str(mountinfo)])

    assert rc == 0
    assert capsys.readouterr().out.startswith("[SILENT] storage-guard OK")


def test_main_speaks_up_when_a_bind_is_gone(tmp_path, capsys, monkeypatch):
    fstab = tmp_path / "fstab"
    fstab.write_text(FSTAB)
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(_mountinfo([t for t in HEALTHY if "loops" not in t]))
    monkeypatch.setattr(sg, "usage_for", _usage({"/": 40.0, "/mnt/data": 12.0}))

    rc = sg.main(["--fstab", str(fstab), "--mountinfo", str(mountinfo)])

    out = capsys.readouterr().out
    assert rc == 1
    assert "[SILENT]" not in out
    assert "/home/piet/.hermes/loops" in out


# ---------------------------------------------------------------------------
# Boundary + contract pinning
# ---------------------------------------------------------------------------

def test_usage_and_finding_are_frozen():
    """The snapshot records are immutable — a guard that can mutate its
    own findings could silently downgrade an alarm to a warning."""
    import pytest as _pytest

    usage = sg.Usage(Path("/"), 50.0, 1, 2)
    finding = sg.Finding("alarm", "x")
    with _pytest.raises(AttributeError):
        usage.percent = 99.0
    with _pytest.raises(AttributeError):
        finding.level = "warn"


def test_fstab_line_with_exactly_four_fields_is_parsed():
    """A minimal 4-field fstab line (no dump/pass columns) still carries
    source/target/options — dropping it would silently unwatch the bind."""
    binds = sg.parse_fstab_binds(
        "/mnt/data/hermes/x /home/piet/x none bind\n", data_root=DATA_ROOT
    )
    assert binds == [("/mnt/data/hermes/x", "/home/piet/x")]


def test_mountinfo_line_with_exactly_five_fields_counts():
    """A minimal 5-field mountinfo line still names a live mount target."""
    assert sg.parse_mount_targets("1 2 3 4 /target\n") == {"/target"}


def test_thresholds_are_inclusive_at_warn_and_alarm():
    """Both thresholds compare '>=': a filesystem at EXACTLY 80% warns and
    at EXACTLY 90% alarms — a strict comparison would miss the boundary
    night and alert one cycle late."""
    warn, _ = _evaluate(HEALTHY, {"/": 80.0, "/mnt/data": 12.0})
    assert [f.level for f in warn] == ["warn"]

    alarm, _ = _evaluate(HEALTHY, {"/": 90.0, "/mnt/data": 12.0})
    assert [f.level for f in alarm] == ["alarm"]


def test_render_lists_alarms_before_warnings():
    """Alarms sort first — the first line an operator reads must be the
    worst one, even if the warn finding was produced earlier."""
    findings = [sg.Finding("warn", "w-message"), sg.Finding("alarm", "a-message")]
    usages = [sg.Usage(Path("/"), 90.0, 2**30, 10 * 2**30)]
    lines = sg._render(findings, usages).splitlines()
    assert lines[1] == "  [ALARM] a-message"
    assert lines[2] == "  [WARN] w-message"
