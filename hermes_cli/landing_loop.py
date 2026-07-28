"""Deterministic inventory, cleanup, and landing for ``loop/*`` branches.

The module owns every landing decision.  The generic loop runner only executes
this entry point, records its stdout, and propagates its exit code.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from hermes_constants import get_hermes_home
from loops.runner import _land_gates

Action = Literal["landed", "cleaned", "parked"]
GateRunner = Callable[[Path, str], tuple[bool, str]]


class LandingLoopError(RuntimeError):
    """Fatal infrastructure/configuration error for the whole run."""


@dataclass(frozen=True)
class BranchInventory:
    branch: str
    ahead: int
    behind: int
    worktree: Path
    head: str


@dataclass(frozen=True)
class BranchOutcome:
    branch: str
    action: Action
    reason: str


@dataclass(frozen=True)
class LandingRun:
    started_at: datetime
    finished_at: datetime
    dry_run: bool
    inventory: tuple[BranchInventory, ...]
    outcomes: tuple[BranchOutcome, ...]
    ledger_path: Path | None = None

    def count(self, action: Action) -> int:
        return sum(item.action == action for item in self.outcomes)


class LandingLoop:
    """Purely deterministic landing policy around real Git repositories."""

    def __init__(
        self,
        repo: Path,
        loops_root: Path,
        ledger_dir: Path,
        *,
        base: str = "main",
        dry_run: bool = False,
        gate_runner: GateRunner | None = None,
        now: Callable[[], datetime] | None = None,
    ):
        self.repo = repo.resolve()
        self.loops_root = loops_root.expanduser().resolve()
        self.ledger_dir = ledger_dir.expanduser().resolve()
        self.base = base
        self.dry_run = dry_run
        self.gate_runner = gate_runner
        self.now = now or (lambda: datetime.now(timezone.utc))

    def _git(
        self,
        *args: str,
        cwd: Path | None = None,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", "-C", str(cwd or self.repo), *args],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if check and result.returncode != 0:
            detail = _one_line(result.stderr or result.stdout or "keine Fehlerdetails")
            raise LandingLoopError(
                f"git {' '.join(args)} fehlgeschlagen (rc={result.returncode}): {detail}"
            )
        return result

    def _validate(self) -> None:
        if not self.repo.is_dir():
            raise LandingLoopError(f"Repository fehlt: {self.repo}")
        self._git("rev-parse", "--git-dir", check=True)
        self._git("rev-parse", "--verify", self.base, check=True)

    def _branches(self) -> tuple[str, ...]:
        result = self._git(
            "for-each-ref",
            "--format=%(refname:short)",
            "refs/heads/loop/",
            check=True,
        )
        return tuple(sorted(line for line in result.stdout.splitlines() if line))

    def _counts(self, branch: str) -> tuple[int, int]:
        result = self._git(
            "rev-list",
            "--left-right",
            "--count",
            f"{self.base}...{branch}",
            check=True,
        )
        try:
            behind_text, ahead_text = result.stdout.split()
            return int(ahead_text), int(behind_text)
        except (TypeError, ValueError) as exc:
            raise LandingLoopError(
                f"Ungültige rev-list-Ausgabe für {branch}: {result.stdout!r}"
            ) from exc

    def _worktree(self, branch: str) -> Path:
        pack_name = branch.removeprefix("loop/")
        return self.loops_root / pack_name / "wt"

    def inventory(self) -> tuple[BranchInventory, ...]:
        items = []
        for branch in self._branches():
            ahead, behind = self._counts(branch)
            head = self._git("rev-parse", branch, check=True).stdout.strip()
            items.append(
                BranchInventory(
                    branch=branch,
                    ahead=ahead,
                    behind=behind,
                    worktree=self._worktree(branch),
                    head=head,
                )
            )
        return tuple(items)

    def _worktree_status(self, worktree: Path) -> str | None:
        if not worktree.is_dir():
            return None
        result = self._git("status", "--porcelain", cwd=worktree)
        if result.returncode != 0:
            return None
        return result.stdout

    def _dirty_reason(self, status: str) -> str:
        untracked = any(line.startswith("??") for line in status.splitlines())
        tracked = any(not line.startswith("??") for line in status.splitlines())
        if tracked and untracked:
            return "Loop-Worktree enthält uncommittete und untrackte Dateien"
        if untracked:
            return "Loop-Worktree enthält untrackte Dateien"
        return "Loop-Worktree enthält uncommittete Änderungen"

    def _park(self, item: BranchInventory, reason: str) -> BranchOutcome:
        return BranchOutcome(item.branch, "parked", _one_line(reason))

    def _cleanup(self, item: BranchInventory) -> BranchOutcome:
        # Deliberately fresh: neither the inventory counts nor an earlier status
        # read authorize the destructive reset.
        ahead, behind = self._counts(item.branch)
        if ahead != 0:
            return self._park(
                item,
                f"ahead seit Inventur {item.ahead}→{ahead}; Reset ausgesetzt",
            )
        status = self._worktree_status(item.worktree)
        if status is None:
            return self._park(item, f"Loop-Worktree fehlt: {item.worktree}")
        if status:
            return self._park(item, self._dirty_reason(status))
        # Dritte Bedingung: der Worktree muss auch WIRKLICH auf item.branch
        # stehen. "reset --hard" bewegt den ausgecheckten Branch, nicht den
        # gemeinten -- steht dort ein anderer, vernichtet der Reset dessen
        # Commits, und die Nachpruefung unten meldet trotzdem Erfolg, weil sie
        # nur item.branch gegen base vergleicht. Fail-closed: ein detached HEAD
        # oder ein unlesbarer Ref parkt ebenfalls.
        head = self._git("symbolic-ref", "--quiet", "--short", "HEAD", cwd=item.worktree)
        checked_out = head.stdout.strip()
        if head.returncode != 0 or checked_out != item.branch:
            return self._park(
                item,
                f"Loop-Worktree steht auf {checked_out or 'detached HEAD'}, "
                f"erwartet {item.branch}; Reset ausgesetzt",
            )
        if self.dry_run:
            return BranchOutcome(
                item.branch,
                "cleaned",
                f"dry-run: würde Branch auf {self.base} zurücksetzen "
                f"(frisch ahead=0, behind={behind}, Worktree sauber)",
            )
        result = self._git("reset", "--hard", self.base, cwd=item.worktree)
        if result.returncode != 0:
            return self._park(
                item,
                f"Reset auf {self.base} fehlgeschlagen: "
                f"{result.stderr or result.stdout}",
            )
        branch_head = self._git("rev-parse", item.branch, check=True).stdout.strip()
        base_head = self._git("rev-parse", self.base, check=True).stdout.strip()
        if branch_head != base_head or self._worktree_status(item.worktree):
            return self._park(item, "Reset-Nachprüfung fehlgeschlagen")
        return BranchOutcome(
            item.branch,
            "cleaned",
            f"auf {self.base} zurückgesetzt (vorher behind={behind})",
        )

    def _main_is_ready(self) -> str | None:
        branch = self._git(
            "symbolic-ref",
            "--quiet",
            "--short",
            "HEAD",
        )
        if branch.returncode != 0 or branch.stdout.strip() != self.base:
            current = branch.stdout.strip() or "detached"
            return f"Basis-Worktree steht auf {current}, erwartet {self.base}"
        status = self._git("status", "--porcelain")
        if status.returncode != 0:
            return "Status des Basis-Worktrees nicht lesbar"
        if status.stdout:
            return "Basis-Worktree ist nicht sauber"
        return None

    def _preview_merge_conflict(self, branch: str) -> str | None:
        merge_base = self._git("merge-base", self.base, branch, check=True).stdout.strip()
        preview = self._git("merge-tree", merge_base, self.base, branch)
        if preview.returncode != 0:
            return _one_line(preview.stderr or preview.stdout or "merge-tree fehlgeschlagen")
        if "<<<<<<<" in preview.stdout:
            return "merge-tree meldet Inhaltskonflikte"
        return None

    def _rollback(self, pre_merge_head: str) -> tuple[bool, str]:
        result = self._git("reset", "--hard", pre_merge_head)
        if result.returncode != 0:
            return False, _one_line(result.stderr or result.stdout)
        current = self._git("rev-parse", "HEAD", check=True).stdout.strip()
        clean = not self._git("status", "--porcelain", check=True).stdout
        if current != pre_merge_head or not clean:
            return False, "HEAD/Worktree stimmt nach Rollback nicht mit dem Anker überein"
        return True, "Merge zurückgerollt"

    def _land(self, item: BranchInventory) -> BranchOutcome:
        ahead, behind = self._counts(item.branch)
        current_head = self._git("rev-parse", item.branch, check=True).stdout.strip()
        if current_head != item.head:
            return self._park(
                item,
                f"Branch-Head seit Inventur geändert ({item.head[:9]}→{current_head[:9]})",
            )
        status = self._worktree_status(item.worktree)
        if status is None:
            return self._park(item, f"Loop-Worktree fehlt: {item.worktree}")
        if status:
            return self._park(item, self._dirty_reason(status))
        main_problem = self._main_is_ready()
        if main_problem:
            return self._park(item, main_problem)
        if self.dry_run:
            conflict = self._preview_merge_conflict(item.branch)
            if conflict:
                return self._park(item, f"dry-run: Merge würde scheitern ({conflict})")
            return BranchOutcome(
                item.branch,
                "landed",
                f"dry-run: würde {ahead} Commit(s) mergen und _land_gates fahren "
                f"(frisch behind={behind})",
            )
        if ahead == 0:
            reset = self._git("reset", "--hard", self.base, cwd=item.worktree)
            if reset.returncode != 0:
                return self._park(
                    item,
                    "Commits bereits durch vorherige Landung enthalten, "
                    f"Branch-Reset aber fehlgeschlagen: {reset.stderr or reset.stdout}",
                )
            return BranchOutcome(
                item.branch,
                "landed",
                "Commits bereits durch vorherige Landung in main enthalten",
            )

        pre_merge_head = self._git("rev-parse", "HEAD", check=True).stdout.strip()
        merge = self._git("merge", "--no-edit", item.branch)
        if merge.returncode != 0:
            self._git("merge", "--abort")
            detail = _one_line(merge.stderr or merge.stdout or "keine Fehlerdetails")
            return self._park(item, f"Merge-Konflikt/Fehler: {detail}")

        gate_runner = self.gate_runner or _land_gates
        try:
            green, gate_report = gate_runner(self.repo, pre_merge_head)
        except Exception as exc:  # noqa: BLE001 - a gate crash is a red gate
            green, gate_report = False, f"Gate-Ausnahme: {exc}"
        if not green:
            rolled_back, rollback_report = self._rollback(pre_merge_head)
            if rolled_back:
                return self._park(item, f"Gate rot; {rollback_report}: {gate_report}")
            return self._park(
                item,
                f"Gate rot; ROLLBACK FEHLGESCHLAGEN ({rollback_report}): {gate_report}",
            )

        # The loop worktree was clean immediately before the merge and the repo
        # lock excludes official loop writers. Re-check both reset conditions
        # against the newly advanced base before freshening the branch.
        fresh_ahead, _fresh_behind = self._counts(item.branch)
        fresh_status = self._worktree_status(item.worktree)
        if fresh_ahead != 0 or fresh_status is None or fresh_status:
            return self._park(
                item,
                "Commits gelandet, Branch aber nicht bereinigbar "
                f"(ahead={fresh_ahead}, status={'fehlt' if fresh_status is None else 'dirty'})",
            )
        reset = self._git("reset", "--hard", self.base, cwd=item.worktree)
        if reset.returncode != 0:
            return self._park(
                item,
                f"Commits gelandet, Branch-Reset fehlgeschlagen: "
                f"{reset.stderr or reset.stdout}",
            )
        return BranchOutcome(item.branch, "landed", _one_line(gate_report))

    def _freshen_completed(
        self,
        item: BranchInventory,
        result: BranchOutcome,
    ) -> BranchOutcome:
        """Align every completed branch to the final base produced by this run."""
        if result.action == "parked":
            return result
        ahead, behind = self._counts(item.branch)
        status = self._worktree_status(item.worktree)
        if ahead != 0 or status is None or status:
            return self._park(
                item,
                f"{result.action} abgeschlossen, finaler Reset aber ausgesetzt "
                f"(ahead={ahead}, behind={behind}, "
                f"status={'fehlt' if status is None else 'dirty'})",
            )
        reset = self._git("reset", "--hard", self.base, cwd=item.worktree)
        if reset.returncode != 0:
            return self._park(
                item,
                f"{result.action} abgeschlossen, finaler Reset fehlgeschlagen: "
                f"{reset.stderr or reset.stdout}",
            )
        return result

    def run(
        self,
        *,
        after_inventory: Callable[[tuple[BranchInventory, ...]], None] | None = None,
    ) -> LandingRun:
        self._validate()
        started_at = self.now()
        inventory = self.inventory()
        if after_inventory is not None:
            after_inventory(inventory)
        outcomes = tuple(
            self._cleanup(item) if item.ahead == 0 else self._land(item)
            for item in inventory
        )
        if not self.dry_run:
            outcomes = tuple(
                self._freshen_completed(item, result)
                for item, result in zip(inventory, outcomes, strict=True)
            )
        run = LandingRun(
            started_at=started_at,
            finished_at=self.now(),
            dry_run=self.dry_run,
            inventory=inventory,
            outcomes=outcomes,
        )
        if self.dry_run:
            return run
        ledger_path = self._write_ledger(run)
        return LandingRun(
            started_at=run.started_at,
            finished_at=run.finished_at,
            dry_run=False,
            inventory=run.inventory,
            outcomes=run.outcomes,
            ledger_path=ledger_path,
        )

    def _write_ledger(self, run: LandingRun) -> Path:
        self.ledger_dir.mkdir(parents=True, exist_ok=True)
        stamp = run.started_at.astimezone(timezone.utc).strftime(
            "%Y-%m-%dT%H%M%S.%fZ"
        )
        path = self.ledger_dir / f"{stamp}-landing-loop-receipt.md"
        path.write_text(render_receipt(run), encoding="utf-8")
        return path


def _one_line(value: object) -> str:
    return " ".join(str(value).split())


def render_discord(run: LandingRun) -> str:
    prefix = "Landing-Loop dry-run" if run.dry_run else "Landing-Loop"
    lines = [
        f"{prefix}: {run.count('landed')} gelandet · "
        f"{run.count('cleaned')} bereinigt · {run.count('parked')} geparkt"
    ]
    parked = [item for item in run.outcomes if item.action == "parked"]
    for item in parked[:9]:
        lines.append(f"- {item.branch}: {_one_line(item.reason)}")
    if len(parked) > 9:
        overflow = ", ".join(item.branch for item in parked[9:])
        lines[-1] += f" · weitere nur im Ledger: {overflow}"
    return "\n".join(lines)


def render_receipt(run: LandingRun) -> str:
    mode = "dry-run" if run.dry_run else "live"
    lines = [
        "---",
        'title: "Landing-Loop receipt"',
        "type: receipt",
        f"created: {run.started_at.isoformat()}",
        f"mode: {mode}",
        "---",
        "",
        "# Landing-Loop",
        "",
        "## Inventur",
        "",
        "| Branch | ahead | behind | Worktree |",
        "|---|---:|---:|---|",
    ]
    for item in run.inventory:
        lines.append(
            f"| {item.branch} | {item.ahead} | {item.behind} | {item.worktree} |"
        )
    if not run.inventory:
        lines.append("| — | 0 | 0 | — |")
    lines += ["", "## Ergebnis", ""]
    for item in run.outcomes:
        lines.append(f"- {item.branch}: **{item.action}** — {item.reason}")
    if not run.outcomes:
        lines.append("- Keine `loop/*`-Branches vorhanden.")
    lines += ["", "## Discord", "", render_discord(run), ""]
    return "\n".join(lines)


def notify_discord(
    message: str,
    *,
    channel: str,
    notify_script: Path,
) -> bool:
    if not channel or not notify_script.is_file():
        return False
    result = subprocess.run(
        ["python3", str(notify_script), "--channel", channel, "--stdin"],
        input=message,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        check=False,
    )
    return result.returncode == 0


def main(argv: list[str] | None = None) -> int:
    hermes_home = get_hermes_home()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path("/home/piet/.hermes/hermes-agent"),
    )
    parser.add_argument("--base", default="main")
    parser.add_argument("--loops-root", type=Path, default=hermes_home / "loops")
    parser.add_argument(
        "--ledger-dir",
        type=Path,
        default=Path.home() / "vault" / "03-Agents" / "Hermes" / "receipts",
    )
    parser.add_argument("--discord-channel", default="")
    parser.add_argument(
        "--notify-script",
        type=Path,
        default=hermes_home / "scripts" / "discord-notify.py",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    loop = LandingLoop(
        args.repo,
        args.loops_root,
        args.ledger_dir,
        base=args.base,
        dry_run=args.dry_run,
    )
    try:
        result = loop.run()
    except LandingLoopError as exc:
        print(f"Landing-Loop abgebrochen: {exc}", file=sys.stderr)
        return 2
    summary = render_discord(result)
    if args.dry_run:
        print(render_receipt(result), end="")
    else:
        print(summary)
        notify_discord(
            summary,
            channel=args.discord_channel,
            notify_script=args.notify_script,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
