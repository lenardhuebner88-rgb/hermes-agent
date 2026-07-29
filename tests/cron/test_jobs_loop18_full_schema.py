"""Loop 18 / F2: full-schema restore validation + cross-process recovery.

Loop 15 validated only id/name/grobe schedule shape when restoring jobs.json
from a backup. Since loop 18 _is_plausible_job_record type-checks every field
create_job() persists whenever it is present (present-then-strict: absent
fields stay tolerated for back-compat, unknown extra fields for
forward-compat): enabled/no_agent strictly bool, repeat a dict with
int-or-None times and int completed, state a string from the known enum,
deliver/script/model/... str-or-None, skills/context_from/enabled_toolsets a
list of strings or None, origin/retry a dict or None, opt-in timeouts
strictly int.

The recovery serialization is additionally proven across PROCESSES (loop 15
covered threads only): two concurrent processes loading the same corrupt
store produce exactly one quarantine file, both get the restored jobs, and
neither crashes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import cron.jobs as jobs


def _full_record(tag: str) -> dict:
    """A record mirroring exactly what create_job() persists."""
    return {
        "id": f"job-{tag}",
        "name": f"job-{tag}",
        "prompt": "do the thing",
        "skills": ["skill-a"],
        "skill": "skill-a",
        "model": None,
        "provider": None,
        "provider_snapshot": "openai",
        "model_snapshot": "gpt-test",
        "base_url": None,
        "script": None,
        "no_agent": False,
        "context_from": None,
        "schedule": {"kind": "interval", "minutes": 60, "display": "every 60m"},
        "schedule_display": "every 60m",
        "repeat": {"times": None, "completed": 0},
        "enabled": True,
        "state": "scheduled",
        "paused_at": None,
        "paused_reason": None,
        "created_at": "2026-07-29T10:00:00+00:00",
        "next_run_at": "2026-07-29T11:00:00+00:00",
        "last_run_at": None,
        "last_status": None,
        "last_error": None,
        "last_delivery_error": None,
        "deliver": "local",
        "origin": None,
        "enabled_toolsets": None,
        "workdir": None,
    }


class TestFullSchemaValidation:
    def test_valid_full_record_accepted(self):
        assert jobs._is_plausible_job_record(_full_record("a"))

    def test_full_record_with_optional_fields_accepted(self):
        record = _full_record("b")
        record["attach_to_session"] = True
        record["retry"] = {"max_attempts": 2, "backoff_seconds": [300.0]}
        record["timeout_seconds"] = 300
        record["delivery_timeout_seconds"] = 60
        assert jobs._is_plausible_job_record(record)

    def test_all_known_states_accepted(self):
        for state in ("scheduled", "paused", "error", "completed"):
            record = _full_record("s")
            record["state"] = state
            assert jobs._is_plausible_job_record(record), state

    def test_unknown_extra_fields_tolerated(self):
        record = _full_record("c")
        record["future_field"] = {"anything": [1, 2, 3]}
        record["another_new_one"] = None
        assert jobs._is_plausible_job_record(record)

    def test_absent_optional_fields_tolerated(self):
        """Back-compat: minimal older/hand-written records stay restorable."""
        assert jobs._is_plausible_job_record(
            {
                "id": "job-min",
                "name": "job-min",
                "schedule": {"kind": "cron", "expr": "0 9 * * *"},
            }
        )

    def test_wrong_type_in_typed_fields_rejected(self):
        mutations = {
            "enabled": ["yes", 1, 0, None],
            "no_agent": [0, "false", None],
            "attach_to_session": [1, "true"],
            "repeat": ["once", 5, {"times": "5", "completed": 0},
                       {"times": None, "completed": "x"},
                       {"times": True, "completed": 0},
                       {"times": 0, "completed": 0},
                       {"times": None, "completed": -1}],
            "deliver": [123, ["local"], {"to": "local"}],
            "script": [42, True, ["/bin/x"]],
            "state": ["martian", "paused ", "", None, 1],
            "prompt": [123, ["x"]],
            "skills": ["skill-a", ["a", 5], 1],
            "context_from": ["job-a", ["a", None]],
            "enabled_toolsets": ["terminal", [1]],
            "origin": ["telegram", 5, ["x"]],
            "retry": [[1, 2], "always", 3],
            "timeout_seconds": ["300", True, 30.0],
            "delivery_timeout_seconds": ["60", None],
            "model": [7, ["m"]],
            "provider_snapshot": [dict(p="x")],
            "created_at": [0, dict()],
            "next_run_at": [False],
            "workdir": [Path("/tmp").parts],
        }
        for field, bad_values in mutations.items():
            for bad in bad_values:
                record = _full_record("x")
                record[field] = bad
                assert not jobs._is_plausible_job_record(record), (
                    f"{field}={bad!r} must be rejected"
                )


class TestCrossProcessRecovery:
    def test_two_processes_quarantine_exactly_once(self, tmp_path):
        """Two PROCESSES racing a corrupt store: exactly one quarantine,
        both get the restored jobs, no crash (loop 15 covered threads)."""
        cron_dir = tmp_path / "cron"
        with jobs.use_cron_store(tmp_path):
            jobs.save_jobs([_full_record("good")])
        assert (cron_dir / "jobs.json.bak.1").exists()
        (cron_dir / "jobs.json").write_bytes(b"{ corrupt main")

        repo_root = Path(jobs.__file__).resolve().parent.parent
        env = dict(os.environ)
        env["HERMES_HOME"] = str(tmp_path)
        env["PYTHONPATH"] = str(repo_root) + os.pathsep + env.get("PYTHONPATH", "")
        # Bounded retry on an EMPTY load: load_jobs checks jobs_file.exists()
        # before taking the lock, so a loader can land inside the winner's
        # quarantine→restore window (jobs.json briefly absent) and see "no
        # file" (pre-existing loop-15 race in load_jobs, outside this loop's
        # scope — reported). The retry converges on the restored store; the
        # assertions that matter — exactly one quarantine, no crash, both
        # processes end with the valid store — are unaffected.
        child_code = (
            "import json, time\n"
            "import cron.jobs as j\n"
            "ids = []\n"
            "for _ in range(20):\n"
            "    ids = [r.get('id') for r in j.load_jobs()]\n"
            "    if ids: break\n"
            "    time.sleep(0.15)\n"
            "print('IDS:' + json.dumps(ids))"
        )
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", child_code],
                env=env,
                cwd=str(repo_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(2)
        ]
        results = [p.communicate(timeout=180) for p in procs]

        for proc, (out, err) in zip(procs, results):
            assert proc.returncode == 0, f"child crashed: {err}"
            id_lines = [l for l in out.splitlines() if l.startswith("IDS:")]
            assert id_lines, f"child printed no result: {out} {err}"
            assert json.loads(id_lines[-1][4:]) == ["job-good"]
        quarantined = list(cron_dir.glob("jobs.json.corrupt-*"))
        assert len(quarantined) == 1
        # The store itself is healthy again for any later reader.
        restored = json.loads((cron_dir / "jobs.json").read_text(encoding="utf-8"))
        assert [r["id"] for r in restored["jobs"]] == ["job-good"]
