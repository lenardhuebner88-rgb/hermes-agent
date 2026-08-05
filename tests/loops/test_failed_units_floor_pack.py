from __future__ import annotations

import os
import subprocess
from pathlib import Path

from loops.runner import DeterministicPhaseCfg, PACKS_DIR, load_pack


PACK_NAME = "failed-units-floor"
REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "loop_failed_units_floor.sh"
ALLOWLIST = REPO_ROOT / "scripts" / "loop_failed_units_allowlist.txt"


def _run_floor(tmp_path: Path, failed_units: list[str]) -> subprocess.CompletedProcess[str]:
    fake_systemctl = tmp_path / "systemctl"
    fake_systemctl.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$*" >>"$SYSTEMCTL_LOG"\n'
        "while IFS= read -r unit; do\n"
        '  [ -z "$unit" ] || printf "%s loaded failed failed\\n" "$unit"\n'
        'done <<<"$FAILED_UNITS"\n'
    )
    fake_systemctl.chmod(0o755)

    notifier = tmp_path / "notify.py"
    notifier.write_text(
        "import os, sys\n"
        "with open(os.environ['NOTIFY_LOG'], 'a') as handle:\n"
        "    handle.write(sys.stdin.read() + '\\n---\\n')\n"
    )

    env = {
        **os.environ,
        "FAILED_UNITS": "\n".join(failed_units),
        "FAILED_UNITS_FLOOR_NOTIFY": str(notifier),
        "FAILED_UNITS_FLOOR_SYSTEMCTL": str(fake_systemctl),
        "HERMES_LOOP_STATE_DIR": str(tmp_path / "state"),
        "NOTIFY_LOG": str(tmp_path / "notify.log"),
        "SYSTEMCTL_LOG": str(tmp_path / "systemctl.log"),
    }
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_manifest_loads_as_one_deterministic_run_phase():
    pack = load_pack(PACKS_DIR, PACK_NAME)

    assert pack.type == "deterministic"
    assert list(pack.phases) == ["run"]
    run_phase = pack.phases["run"]
    assert isinstance(run_phase, DeterministicPhaseCfg)
    assert run_phase.command == (
        "bash",
        "scripts/loop_failed_units_floor.sh",
    )
    assert pack.autoland is False


def test_allowlisted_failures_are_silent_and_never_restarted(tmp_path: Path):
    assert set(ALLOWLIST.read_text().splitlines()) == {
        "nachtwache-selftest.service",
        "pa-backup.service",
    }

    result = _run_floor(
        tmp_path,
        ["nachtwache-selftest.service", "pa-backup.service"],
    )

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "notify.log").exists()
    assert (tmp_path / "systemctl.log").read_text().splitlines() == [
        "--user list-units --state=failed --plain --no-legend"
    ]


def test_new_failure_notifies_once_across_followup_runs(tmp_path: Path):
    failed_units = [
        "nachtwache-selftest.service",
        "new-regression.service",
        "pa-backup.service",
    ]

    first = _run_floor(tmp_path, failed_units)
    second = _run_floor(tmp_path, failed_units)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    notifications = (tmp_path / "notify.log").read_text()
    assert notifications.count("\n---\n") == 1
    assert "new-regression.service" in notifications
    assert "nachtwache-selftest.service" not in notifications
    assert "pa-backup.service" not in notifications
    assert (tmp_path / "systemctl.log").read_text().splitlines() == [
        "--user list-units --state=failed --plain --no-legend",
        "--user list-units --state=failed --plain --no-legend",
    ]
