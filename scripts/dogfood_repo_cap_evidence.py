#!/usr/bin/env python3
"""READ-ONLY evidence collector for the per-repo concurrency cap (LT2 dogfood).

Polls the live dashboard plugin-API to capture evidence for the 4 dogfood
scenarios described in the runbook (docs/dogfood_repo_cap_runbook.md):

  1. Echte Parallelität: workers/active count==2, both integration_merged,
     main has both commits, no repo_serialized between them.
  2. No-Op-Falle: cap=2 but same-lane/per_profile=1 -> repo_serialized.
  3. Overlap-Konflikt: second task -> integration_rebase_conflict -> blocked.
  4. Rollback: cap->1 -> serialisation returns (repo_serialized at N+1).

Data sources (all READ-ONLY):
  - GET /api/plugins/kanban/workers/active  (max concurrent worker count)
  - GET /api/plugins/kanban/tasks/{id}/activity  (integration_* / repo_serialized events)
  - git -C <repo> log --oneline main  (landed commits)

Auth: password-login via CookieJar — identical pattern to
scripts/smoke_health_status_auth.py.  Passwords, tokens, and cookies are
NEVER printed or written to the receipt.

Usage:
  # Full collection (requires HERMES_DASHBOARD_USERNAME / HERMES_DASHBOARD_PASSWORD)
  python3 scripts/dogfood_repo_cap_evidence.py \\
      --task-ids t_xxx t_yyy \\
      --repo /home/piet/.hermes/hermes-agent \\
      --receipt-dir /home/piet/vault/03-Agents/Claude-Code/receipts

  # Dry-run (no auth, no API calls — validates args + receipt template)
  python3 scripts/dogfood_repo_cap_evidence.py --dry-run

  # Single-scenario capture
  python3 scripts/dogfood_repo_cap_evidence.py \\
      --task-ids t_xxx --scenario S1 --repo /home/piet/.hermes/hermes-agent
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_URL = "http://127.0.0.1:9119"
DEFAULT_REPO = "/home/piet/.hermes/hermes-agent"
DEFAULT_RECEIPT_DIR = "/home/piet/vault/03-Agents/Claude-Code/receipts"

# Event kinds we care about (from kanban_worktrees.py + kanban_db.py)
EVENT_INTEGRATION_MERGED = "integration_merged"
EVENT_INTEGRATION_REBASE_CONFLICT = "integration_rebase_conflict"
EVENT_REPO_SERIALIZED = "repo_serialized"
EVENT_SKIPPED_REPO_SERIALIZED = "skipped_repo_serialized"
EVENT_INTEGRATION_CLEAN = "integration_clean"
EVENT_INTEGRATION_PARKED = "integration_parked"

RELEVANT_EVENT_KINDS = frozenset({
    EVENT_INTEGRATION_MERGED,
    EVENT_INTEGRATION_REBASE_CONFLICT,
    EVENT_REPO_SERIALIZED,
    EVENT_SKIPPED_REPO_SERIALIZED,
    EVENT_INTEGRATION_CLEAN,
    EVENT_INTEGRATION_PARKED,
})

# Fields that must NEVER appear in receipts (secrets hygiene)
_REDACTED_MARKERS = ("password", "token", "cookie", "secret", "api_key", "session")


class CollectorError(RuntimeError):
    """User-facing collection failure."""


# ---------------------------------------------------------------------------
# HTTP helpers (auth pattern from smoke_health_status_auth.py)
# ---------------------------------------------------------------------------

def _base_url(raw: str) -> str:
    parsed = urllib.parse.urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        raise CollectorError(f"invalid dashboard URL: {raw!r}")
    return raw.rstrip("/")


def _read_password(env_name: str, *, no_prompt: bool) -> str:
    value = os.environ.get(env_name, "")
    if value:
        return value
    if no_prompt:
        raise CollectorError(f"{env_name} is not set")
    import getpass
    return getpass.getpass(f"{env_name}: ")


def _json_request(
    opener: urllib.request.OpenerDirector,
    method: str,
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout: float,
) -> dict[str, Any]:
    data = None
    headers: dict[str, str] = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            status = response.status
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise CollectorError(
            f"{method} {url} returned HTTP {exc.code}: {body[:300]}"
        ) from exc
    except urllib.error.URLError as exc:
        raise CollectorError(f"{method} {url} failed: {exc.reason}") from exc

    if status < 200 or status >= 300:
        raise CollectorError(f"{method} {url} returned HTTP {status}")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CollectorError(f"{method} {url} returned invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise CollectorError(f"{method} {url} returned a non-object JSON payload")
    return parsed


def _authenticate(
    base: str,
    *,
    provider: str,
    username: str,
    password: str,
    timeout: float,
) -> urllib.request.OpenerDirector:
    """Login via /auth/password-login, return an opener with session cookie."""
    jar = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    login = _json_request(
        opener,
        "POST",
        f"{base}/auth/password-login",
        payload={
            "provider": provider,
            "username": username,
            "password": password,
            "next": "/api/plugins/kanban/workers/active",
        },
        timeout=timeout,
    )
    if login.get("ok") is not True:
        raise CollectorError("password login did not return ok=true")
    return opener


# ---------------------------------------------------------------------------
# API collectors
# ---------------------------------------------------------------------------

def collect_workers_active(
    opener: urllib.request.OpenerDirector,
    base: str,
    *,
    board: Optional[str],
    timeout: float,
) -> dict[str, Any]:
    """GET /api/plugins/kanban/workers/active — snapshot of currently-running workers.

    Returns the full JSON payload: {workers: [...], count: N, cap: N, ...}
    """
    url = f"{base}/api/plugins/kanban/workers/active"
    if board:
        url += f"?board={urllib.parse.quote(board)}"
    return _json_request(opener, "GET", url, timeout=timeout)


def collect_task_activity(
    opener: urllib.request.OpenerDirector,
    base: str,
    task_id: str,
    *,
    board: Optional[str],
    timeout: float,
    limit: int = 50,
) -> dict[str, Any]:
    """GET /api/plugins/kanban/tasks/{id}/activity — recent task events.

    Returns {task_id: ..., events: [{id, run_id, kind, note, at}, ...]}
    """
    url = f"{base}/api/plugins/kanban/tasks/{urllib.parse.quote(task_id)}/activity"
    params = {"limit": str(limit)}
    if board:
        params["board"] = board
    url += "?" + urllib.parse.urlencode(params)
    return _json_request(opener, "GET", url, timeout=timeout)


def filter_relevant_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only integration_* / repo_serialized events."""
    return [e for e in events if e.get("kind") in RELEVANT_EVENT_KINDS]


def collect_git_log(repo: str, *, branch: str = "main", max_count: int = 20) -> list[str]:
    """Read `git -C <repo> log --oneline <ref>` — landed commits.

    ``branch`` defaults to ``main`` (the Hermes repo's default branch).  For
    ``--repo`` sandboxes whose default branch is NOT ``main`` (e.g. ``trunk`` or
    ``master``), an unresolvable branch transparently falls back to ``HEAD`` so
    evidence is still captured instead of an empty ``[git error]`` line.
    """
    ref = branch
    try:
        probe = subprocess.run(
            ["git", "-C", repo, "rev-parse", "--verify", "--quiet", f"{branch}^{{commit}}"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if probe.returncode != 0:
            ref = "HEAD"  # requested branch missing -> fall back to current HEAD
    except subprocess.TimeoutExpired:
        return ["[git rev-parse timed out]"]
    except FileNotFoundError:
        return ["[git not found]"]

    try:
        result = subprocess.run(
            ["git", "-C", repo, "log", "--oneline", "-n", str(max_count), ref],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            return [f"[git error: {result.stderr.strip()[:200]}]"]
        return [line for line in result.stdout.strip().splitlines() if line]
    except subprocess.TimeoutExpired:
        return ["[git log timed out]"]
    except FileNotFoundError:
        return ["[git not found]"]


# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------

def poll_workers_active(
    opener: urllib.request.OpenerDirector,
    base: str,
    *,
    board: Optional[str],
    timeout: float,
    poll_interval: float,
    poll_duration: float,
) -> list[dict[str, Any]]:
    """Poll workers/active every poll_interval seconds for poll_duration seconds.

    Returns a list of snapshots, each with a timestamp.
    """
    snapshots: list[dict[str, Any]] = []
    deadline = time.monotonic() + poll_duration
    while time.monotonic() < deadline:
        snap = collect_workers_active(opener, base, board=board, timeout=timeout)
        snap["_captured_at"] = datetime.now(timezone.utc).isoformat()
        snapshots.append(snap)
        if time.monotonic() < deadline:
            time.sleep(poll_interval)
    return snapshots


def max_concurrent_workers(snapshots: list[dict[str, Any]]) -> int:
    """Extract the maximum concurrent worker count across all snapshots.

    The peak is the live number of running workers — the headline evidence
    metric for the dogfood scenarios.  The endpoint reports ``count`` as a
    redundant mirror of ``len(workers)`` (plugin_api ``/workers/active``), so we
    take whichever is larger per snapshot: if a snapshot ever omits ``count`` (or
    reports it stale / non-int), we must NOT silently drop to 0 and undercount —
    the live ``workers`` list is the source of truth.
    """
    peak = 0
    for snap in snapshots:
        count = snap.get("count")
        count_val = count if isinstance(count, int) and not isinstance(count, bool) else 0
        workers = snap.get("workers")
        workers_len = len(workers) if isinstance(workers, list) else 0
        concurrent = max(count_val, workers_len)
        if concurrent > peak:
            peak = concurrent
    return peak


# ---------------------------------------------------------------------------
# Secrets hygiene
# ---------------------------------------------------------------------------

def _scrub_secrets(obj: Any) -> Any:
    """Recursively remove any dict keys that look like secrets."""
    if isinstance(obj, dict):
        return {
            k: ("[REDACTED]" if any(m in k.lower() for m in _REDACTED_MARKERS) else _scrub_secrets(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_scrub_secrets(item) for item in obj]
    return obj


def _json_block(obj: Any, max_chars: int) -> str:
    """Serialize ``obj`` to JSON that is ALWAYS valid, even when it must shrink.

    The previous collector embedded ``json.dumps(...)[:max_chars]`` directly in a
    ```json fence; for bulky snapshots that hard slice cut mid-token, producing
    an unparseable block.  Instead, when the full serialization exceeds
    ``max_chars`` we keep the largest *whole-item* prefix of a list (so the
    result stays valid JSON) and annotate how many items were omitted.
    """
    text = json.dumps(obj, indent=2, ensure_ascii=False)
    if len(text) <= max_chars:
        return text
    if isinstance(obj, list):
        kept: list[Any] = []
        for item in obj:
            if len(json.dumps(kept + [item], indent=2, ensure_ascii=False)) > max_chars:
                break
            kept.append(item)
        wrapper = {
            "_truncated": (
                f"showing {len(kept)} of {len(obj)} items "
                f"(full payload {len(text)} chars > {max_chars} cap)"
            ),
            "items": kept,
        }
        return json.dumps(wrapper, indent=2, ensure_ascii=False)
    return json.dumps(
        {"_truncated": f"payload {len(text)} chars exceeds {max_chars} cap"},
        indent=2,
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# Receipt writer
# ---------------------------------------------------------------------------

def write_receipt(
    receipt_path: str,
    *,
    scenario: str,
    task_ids: list[str],
    workers_snapshots: list[dict[str, Any]],
    peak_concurrent: int,
    task_activities: dict[str, dict[str, Any]],
    git_log: list[str],
    repo: str,
    started_at: str,
    finished_at: str,
    notes: str,
    branch: str = "main",
) -> None:
    """Write a structured Markdown receipt with evidence for one scenario."""

    # Build per-task event summary
    task_summaries = []
    for tid in task_ids:
        activity = task_activities.get(tid, {})
        all_events = activity.get("events", [])
        relevant = filter_relevant_events(all_events)
        event_kinds = [e["kind"] for e in relevant]
        kind_counts = Counter(event_kinds)
        task_summaries.append({
            "task_id": tid,
            "total_events": len(all_events),
            "relevant_events": len(relevant),
            "event_kinds": dict(kind_counts),
            "events": [
                {
                    "kind": e.get("kind"),
                    "at": e.get("at"),
                    "note": e.get("note"),
                }
                for e in relevant
            ],
        })

    # Scrub any accidental secrets from all collected data
    scrubbed_snapshots = _scrub_secrets(workers_snapshots)
    scrubbed_activities = _scrub_secrets(task_activities)

    receipt = f"""# Dogfood Receipt: {scenario}

**Date:** {started_at} – {finished_at}
**Repo:** `{repo}`
**Task IDs:** {", ".join(task_ids) or "(none)"}
**Peak concurrent workers:** {peak_concurrent}

## Evidence Summary

| Metric | Value |
|--------|-------|
| Snapshots collected | {len(workers_snapshots)} |
| Peak concurrent count | {peak_concurrent} |
| Tasks monitored | {len(task_ids)} |

## Workers/Active Snapshots

```json
{_json_block(scrubbed_snapshots, 4000)}
```

## Per-Task Activity (filtered: {", ".join(sorted(RELEVANT_EVENT_KINDS))})

"""
    for ts in task_summaries:
        receipt += f"""### {ts['task_id']}

- Total events: {ts['total_events']}
- Relevant events: {ts['relevant_events']}
- Event kind counts: {json.dumps(ts['event_kinds'], ensure_ascii=False)}

"""
        for ev in ts["events"]:
            receipt += f"  - `{ev['kind']}` at `{ev['at']}`"
            if ev.get("note"):
                receipt += f" — {ev['note']}"
            receipt += "\n"
        receipt += "\n"

    receipt += f"""## Git Log (`{branch}`, last {len(git_log)} commits)

```
{chr(10).join(git_log)}
```

## Notes

{notes or "(none)"}

## Scenario Expectations

"""
    expectations = {
        "S1": (
            "workers/active count==2 in at least one snapshot, both tasks have "
            "integration_merged, main has both commits, no repo_serialized between them."
        ),
        "S2": (
            "cap=2 but same-lane/per_profile=1: NOT parallel. "
            "repo_serialized event present -> belegt die Activation-Caveat."
        ),
        "S3": (
            "Overlap-Konflikt: second task -> integration_rebase_conflict -> blocked. "
            "main only has the winner."
        ),
        "S4": (
            "Rollback: cap->1 + gateway restart -> Serialisierung kehrt zurück "
            "(repo_serialized wieder bei N+1)."
        ),
    }
    for sid, desc in expectations.items():
        marker = "← THIS SCENARIO" if sid == scenario else ""
        receipt += f"- **{sid}:** {desc} {marker}\n"

    receipt += f"""
## Verification Checklist

- [ ] Collector script ran WITHOUT printing tokens/passwords/cookies
- [ ] workers/active endpoint returned valid JSON
- [ ] task activity endpoint returned valid JSON for all task IDs
- [ ] git log returned commit history for main
- [ ] Receipt written to `{receipt_path}`

---
*Generated by `scripts/dogfood_repo_cap_evidence.py`*
"""

    parent = os.path.dirname(receipt_path)
    if parent:  # empty when receipt_path is a bare filename (e.g. --receipt-dir '')
        os.makedirs(parent, exist_ok=True)
    with open(receipt_path, "w", encoding="utf-8") as f:
        f.write(receipt)


# ---------------------------------------------------------------------------
# Dry-run receipt template
# ---------------------------------------------------------------------------

def write_dry_run_template(receipt_dir: str) -> str:
    """Write a minimal template receipt for dry-run validation."""
    receipt_dir = receipt_dir or "."  # empty --receipt-dir '' -> current directory
    path = os.path.join(receipt_dir, "dogfood_repo_cap_TEMPLATE.md")
    template = """# Dogfood Receipt: TEMPLATE (dry-run)

Replace this with actual collected data. See docs/dogfood_repo_cap_runbook.md
for the operator procedure to produce real evidence for each scenario.

## Scenarios

- S1: Echte Parallelität (cap=2, lane-spread, 2 disjunkte tasks)
- S2: No-Op-Falle (cap=2, same-lane, per_profile=1)
- S3: Overlap-Konflikt (2 tasks on same file)
- S4: Rollback (cap->1, gateway restart)
"""
    os.makedirs(receipt_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(template)
    return path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "READ-ONLY evidence collector for the per-repo concurrency cap (LT2 dogfood). "
            "Polls the live dashboard API, collects workers/active + task activity + git log, "
            "and writes a structured receipt. NEVER logs tokens/passwords/cookies."
        ),
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("HERMES_DASHBOARD_URL", DEFAULT_URL),
        help=f"Dashboard base URL (default: {DEFAULT_URL}, or HERMES_DASHBOARD_URL).",
    )
    parser.add_argument(
        "--provider",
        default=os.environ.get("HERMES_DASHBOARD_AUTH_PROVIDER", "basic"),
        help="Password-auth provider name (default: basic).",
    )
    parser.add_argument(
        "--username",
        default=os.environ.get("HERMES_DASHBOARD_USERNAME", ""),
        help="Dashboard username (default: HERMES_DASHBOARD_USERNAME).",
    )
    parser.add_argument(
        "--password-env",
        default="HERMES_DASHBOARD_PASSWORD",
        help="Environment variable containing the dashboard password.",
    )
    parser.add_argument(
        "--board",
        default=None,
        help="Kanban board slug (omit for current board).",
    )
    parser.add_argument(
        "--task-ids",
        nargs="*",
        default=[],
        help="Task IDs to collect activity for.",
    )
    parser.add_argument(
        "--scenario",
        default="S1",
        help="Scenario label for the receipt (S1, S2, S3, S4).",
    )
    parser.add_argument(
        "--repo",
        default=DEFAULT_REPO,
        help=f"Repository path for git log (default: {DEFAULT_REPO}).",
    )
    parser.add_argument(
        "--branch",
        default="main",
        help=(
            "Branch to read the git log from (default: main). For --repo sandboxes "
            "whose default branch is not 'main', the collector falls back to HEAD "
            "automatically when this branch does not resolve."
        ),
    )
    parser.add_argument(
        "--receipt-dir",
        default=DEFAULT_RECEIPT_DIR,
        help=f"Directory for the receipt file (default: {DEFAULT_RECEIPT_DIR}).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="HTTP timeout per request in seconds (default: 10).",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=5.0,
        help="Seconds between workers/active polls (default: 5).",
    )
    parser.add_argument(
        "--poll-duration",
        type=float,
        default=30.0,
        help="Total seconds to poll workers/active (default: 30).",
    )
    parser.add_argument(
        "--no-prompt",
        action="store_true",
        help="Fail instead of prompting when the password is not in the environment.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip all API calls and git; write a template receipt to validate setup.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(argv or sys.argv[1:]))

    # Dry-run: validate args + write template
    if args.dry_run:
        template_path = write_dry_run_template(args.receipt_dir)
        print(f"[dry-run] template receipt written to: {template_path}")
        print(f"[dry-run] repo: {args.repo}")
        print(f"[dry-run] scenario: {args.scenario}")
        print(f"[dry-run] task_ids: {args.task_ids}")
        print("[dry-run] no API calls made, no secrets accessed")
        return 0

    started_at = datetime.now(timezone.utc).isoformat()
    try:
        base = _base_url(args.url)
        username = args.username
        if not username:
            if args.no_prompt:
                raise CollectorError("HERMES_DASHBOARD_USERNAME is not set")
            username = input("HERMES_DASHBOARD_USERNAME: ").strip()
        password = _read_password(args.password_env, no_prompt=args.no_prompt)

        # Authenticate
        print(f"[auth] logging in to {base} as {username}...", file=sys.stderr)
        opener = _authenticate(
            base,
            provider=args.provider,
            username=username,
            password=password,
            timeout=args.timeout,
        )
        print("[auth] login successful", file=sys.stderr)

        # Poll workers/active
        print(
            f"[poll] polling workers/active every {args.poll_interval}s "
            f"for {args.poll_duration}s...",
            file=sys.stderr,
        )
        snapshots = poll_workers_active(
            opener,
            base,
            board=args.board,
            timeout=args.timeout,
            poll_interval=args.poll_interval,
            poll_duration=args.poll_duration,
        )
        peak = max_concurrent_workers(snapshots)
        print(f"[poll] peak concurrent workers: {peak}", file=sys.stderr)

        # Collect task activity
        task_activities: dict[str, dict[str, Any]] = {}
        for tid in args.task_ids:
            print(f"[activity] collecting activity for {tid}...", file=sys.stderr)
            activity = collect_task_activity(
                opener,
                base,
                tid,
                board=args.board,
                timeout=args.timeout,
            )
            relevant = filter_relevant_events(activity.get("events", []))
            print(
                f"[activity] {tid}: {len(relevant)} relevant events "
                f"({Counter(e['kind'] for e in relevant)})",
                file=sys.stderr,
            )
            task_activities[tid] = activity

        # Collect git log
        print(
            f"[git] reading git log for {args.branch} in {args.repo}...",
            file=sys.stderr,
        )
        git_log = collect_git_log(args.repo, branch=args.branch)
        print(f"[git] {len(git_log)} commits read", file=sys.stderr)

        # Write receipt
        finished_at = datetime.now(timezone.utc).isoformat()
        date_tag = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        receipt_filename = f"{date_tag}-dogfood-repo-cap-{args.scenario.lower()}-receipt.md"
        receipt_path = os.path.join(args.receipt_dir, receipt_filename)

        write_receipt(
            receipt_path,
            scenario=args.scenario,
            task_ids=args.task_ids,
            workers_snapshots=snapshots,
            peak_concurrent=peak,
            task_activities=task_activities,
            git_log=git_log,
            repo=args.repo,
            started_at=started_at,
            finished_at=finished_at,
            notes="",
            branch=args.branch,
        )
        print(f"\n[done] receipt written to: {receipt_path}", file=sys.stderr)
        print(f"[done] peak concurrent workers: {peak}", file=sys.stderr)
        return 0

    except CollectorError as exc:
        print(f"collection failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
