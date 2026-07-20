"""Runtime smoke test for the Docker tini compatibility shim (#34192).

Build the real image and verify:

  1. /usr/bin/tini exists and is a symlink to /init (the compat shim
     for orchestration templates that still reference /usr/bin/tini)
  2. The configured ENTRYPOINT is the UID guard, not /usr/bin/tini
  3. The guard execs /init so s6-overlay still becomes PID 1
"""
from __future__ import annotations

import json
import subprocess


def test_tini_compat_symlink_exists(built_image: str) -> None:
    """/usr/bin/tini must exist as a symlink to /init.

    Regression for #34192: orchestration templates (e.g. Hostinger's
    'Hermes WebUI' catalog) still pin /usr/bin/tini as the entrypoint.
    The shim symlinks it to /init so legacy wrappers exec the right
    PID-1 reaper without behavior change.
    """
    r = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "sh",
         built_image, "-c",
         'test -L /usr/bin/tini && '
         'test "$(readlink -f /usr/bin/tini)" = "/init"'],
        capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, (
        f"/usr/bin/tini is not a symlink to /init: {r.stderr[-500:]}"
    )


def test_entrypoint_guard_execs_init_not_tini(built_image: str) -> None:
    """The UID guard must be the entrypoint and hand PID 1 to /init.

    The tini shim is only for legacy external wrappers; the image's own
    runtime must validate --user before starting the canonical /init.
    """
    r = subprocess.run(
        ["docker", "inspect", built_image,
         "--format", "{{json .Config.Entrypoint}}"],
        capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 0, f"docker inspect failed: {r.stderr}"
    entrypoint = json.loads(r.stdout)
    guard_path = "/opt/hermes/docker/pre-init-uid-guard.sh"
    assert entrypoint == [guard_path], (
        f"ENTRYPOINT does not use the UID guard: {entrypoint!r}"
    )

    guard = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "cat",
         built_image, guard_path],
        capture_output=True, text=True, timeout=30,
    )
    assert guard.returncode == 0, f"could not inspect UID guard: {guard.stderr}"
    assert (
        'exec /init /opt/hermes/docker/main-wrapper.sh "$@"'
        in guard.stdout.splitlines()
    ), "UID guard must exec the canonical /init + main-wrapper chain"
