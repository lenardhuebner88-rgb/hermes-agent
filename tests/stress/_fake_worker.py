#!/usr/bin/env python3
"""Fake worker process that exercises the real subprocess contract.

Reads HERMES_KANBAN_TASK from env, heartbeats periodically, does short
work, completes via the CLI. Designed to be spawned by the dispatcher
exactly the way `hermes chat -q` would be, minus the LLM cost.
"""

import json
import os
import subprocess
import time


def run_kanban_cli(args, *, expect_stdout: str):
    """Run the real CLI and fail loudly if it did not report success."""

    proc = subprocess.run(
        ["hermes", "kanban", *args],
        capture_output=True,
        text=True,
    )
    if proc.stdout:
        print(proc.stdout, end="")
    if proc.stderr:
        print(proc.stderr, end="")
    if proc.returncode != 0 or expect_stdout not in proc.stdout:
        raise RuntimeError(
            "kanban CLI failed: "
            f"args={args!r} rc={proc.returncode} "
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )


def main():
    tid = os.environ["HERMES_KANBAN_TASK"]
    workspace = os.environ.get("HERMES_KANBAN_WORKSPACE", "")

    # Announce via CLI (goes through real argparse + init_db + etc)
    run_kanban_cli(
        ["heartbeat", tid, "--note", "started"],
        expect_stdout="Heartbeat recorded",
    )

    # Simulate work with periodic heartbeats
    for i in range(3):
        time.sleep(0.3)
        run_kanban_cli(
            ["heartbeat", tid, "--note", f"progress {i+1}/3"],
            expect_stdout="Heartbeat recorded",
        )

    # Complete with structured handoff
    run_kanban_cli(
        [
            "complete", tid,
            "--summary", f"real-subprocess worker finished {tid}",
            "--metadata", json.dumps({
                "workspace": workspace,
                "worker_pid": os.getpid(),
                "iterations": 3,
            }),
        ],
        expect_stdout="Completed",
    )


if __name__ == "__main__":
    main()
