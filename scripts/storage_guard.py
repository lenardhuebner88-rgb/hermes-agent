#!/usr/bin/env python3
"""Guard the storage layout: filesystem headroom plus bind-mount presence.

The host keeps its bulk data on a second disk (``/mnt/data``) that is mounted
with ``nofail``, so a boot without that disk succeeds silently.  Every bind
mount layered on top is then absent, and services happily write into the empty
underlay on ``/`` until the root filesystem is full again.  That failure is
invisible until something breaks, which is exactly what this guard exists to
prevent.

The set of expected bind mounts is read from ``/etc/fstab`` rather than kept in
a second list here: any bind entry whose source lives under the data root is
expected to be mounted.  Adding a bind mount therefore extends the monitoring
automatically, and the two can never drift apart.

Hermes cron convention: on success prints ``[SILENT] storage-guard OK ...`` and
exits 0 — the ``[SILENT]`` prefix suppresses Discord delivery for a healthy
run.  Anything else is delivered.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

DEFAULT_FSTAB = Path("/etc/fstab")
DEFAULT_MOUNTINFO = Path("/proc/self/mountinfo")
DEFAULT_DATA_ROOT = Path("/mnt/data")
# Watched filesystems.  /mnt/data is the escape valve, so it gets a headroom
# check of its own -- a full second disk would push writes back onto /.
DEFAULT_WATCHED = (Path("/"), DEFAULT_DATA_ROOT)
DEFAULT_WARN_PERCENT = 80.0
DEFAULT_ALARM_PERCENT = 90.0


@dataclass(frozen=True)
class Usage:
    path: Path
    percent: float
    free_bytes: int
    total_bytes: int


@dataclass(frozen=True)
class Finding:
    level: str  # "alarm" | "warn"
    message: str


def parse_fstab_binds(text: str, *, data_root: Path) -> list[tuple[str, str]]:
    """Return ``(source, target)`` for every bind entry sourced under ``data_root``.

    Only the data-root binds are returned: binds elsewhere are somebody else's
    concern and must not turn this guard red.
    """
    binds: list[tuple[str, str]] = []
    prefix = str(data_root).rstrip("/") + "/"
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) < 4:
            continue
        source, target, options = fields[0], fields[1], fields[3]
        if "bind" not in options.split(","):
            continue
        if source == str(data_root) or source.startswith(prefix):
            binds.append((source, target))
    return binds


def parse_mount_targets(text: str) -> set[str]:
    """Mount points currently present, from ``/proc/self/mountinfo``."""
    targets: set[str] = set()
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 5:
            targets.add(fields[4])
    return targets


def usage_for(path: Path) -> Usage | None:
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    if total <= 0:
        return None
    # Match df: usage is measured against the space actually available to
    # non-root users, not against the raw total.
    used = total - st.f_bfree * st.f_frsize
    denominator = used + free
    percent = 100.0 * used / denominator if denominator else 0.0
    return Usage(path, percent, free, total)


def evaluate(
    *,
    fstab_text: str,
    mountinfo_text: str,
    data_root: Path,
    watched: Sequence[Path],
    warn_percent: float,
    alarm_percent: float,
    usage_lookup=None,
) -> tuple[list[Finding], list[Usage]]:
    # Resolved at call time, not bound as a default: a default argument would
    # capture the original function object and silently ignore any replacement,
    # which made a test read the real filesystem instead of its fixture.
    if usage_lookup is None:
        usage_lookup = usage_for
    findings: list[Finding] = []
    mounted = parse_mount_targets(mountinfo_text)

    data_root_mounted = str(data_root) in mounted
    if not data_root_mounted:
        findings.append(
            Finding(
                "alarm",
                f"{data_root} is NOT mounted — it is fstab'd nofail, so writes are "
                f"landing on the root filesystem instead. Check the disk, then "
                f"`mount {data_root} && mount -a`.",
            )
        )

    for source, target in parse_fstab_binds(fstab_text, data_root=data_root):
        if target not in mounted:
            # A missing data root already explains every bind below it; do not
            # bury that one actionable line under a list of consequences.
            if data_root_mounted:
                findings.append(
                    Finding("alarm", f"bind mount missing: {target} (source {source})")
                )

    usages: list[Usage] = []
    for path in watched:
        usage = usage_lookup(path)
        if usage is None:
            findings.append(Finding("warn", f"cannot stat filesystem {path}"))
            continue
        usages.append(usage)
        if usage.percent >= alarm_percent:
            findings.append(
                Finding(
                    "alarm",
                    f"{path} is {usage.percent:.0f}% full "
                    f"({usage.free_bytes / 2**30:.1f} GB free)",
                )
            )
        elif usage.percent >= warn_percent:
            findings.append(
                Finding(
                    "warn",
                    f"{path} is {usage.percent:.0f}% full "
                    f"({usage.free_bytes / 2**30:.1f} GB free)",
                )
            )
    return findings, usages


def _render(findings: Iterable[Finding], usages: Iterable[Usage]) -> str:
    summary = " ".join(
        f"{usage.path}={usage.percent:.0f}%/{usage.free_bytes / 2**30:.0f}GB-free"
        for usage in usages
    )
    findings = list(findings)
    tag = "[ALARM]" if any(f.level == "alarm" for f in findings) else "[ENTSCHEIDUNG]"
    lines = [f"{tag} storage-guard: {summary}"]
    for finding in sorted(findings, key=lambda f: f.level != "alarm"):
        lines.append(f"  [{finding.level.upper()}] {finding.message}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--fstab", type=Path, default=DEFAULT_FSTAB)
    parser.add_argument("--mountinfo", type=Path, default=DEFAULT_MOUNTINFO)
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--watch", type=Path, action="append", dest="watched")
    parser.add_argument("--warn-percent", type=float, default=DEFAULT_WARN_PERCENT)
    parser.add_argument("--alarm-percent", type=float, default=DEFAULT_ALARM_PERCENT)
    args = parser.parse_args(argv)

    try:
        fstab_text = args.fstab.read_text()
        mountinfo_text = args.mountinfo.read_text()
    except OSError as error:
        print(f"storage-guard: cannot read mount state: {error}", file=sys.stderr)
        return 2

    findings, usages = evaluate(
        fstab_text=fstab_text,
        mountinfo_text=mountinfo_text,
        data_root=args.data_root,
        watched=tuple(args.watched or DEFAULT_WATCHED),
        warn_percent=args.warn_percent,
        alarm_percent=args.alarm_percent,
    )

    if not findings:
        summary = " ".join(
            f"{usage.path}={usage.percent:.0f}%" for usage in usages
        )
        print(f"[SILENT] storage-guard OK {summary}")
        return 0

    print(_render(findings, usages))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
