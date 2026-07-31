"""Deterministic inventory, cleanup, and landing for ``loop/*`` branches.

The module owns every landing decision.  The generic loop runner only executes
this entry point, records its stdout, and propagates its exit code.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Literal

from hermes_constants import get_hermes_home
from hermes_cli.control_loops import (
    get_landing_automation_enabled,
    write_landing_runtime_status,
)
from hermes_cli.vision_metrics import (
    read_gate_records,
    read_landing_gate_records,
    record_landing_gate_pass,
)
from loops.runner import _land_gates

Action = Literal["landed", "cleaned", "parked"]
GateRunner = Callable[[Path, str], tuple[bool, str]]
BaselineRecords = Callable[[], list[dict[str, object]]]
LandingEvidenceWriter = Callable[[str, tuple[str, ...], str, str], object]

_STASH_MESSAGE = "landing-loop: fremde Basis-Änderungen (autostash)"
# Ein Stash, den wir selbst angelegt haben und nicht mehr zurückholen
# konnten, ist der einzige Zustand, den der Loop erzeugt und nicht selbst
# aufräumen kann — dafür stoppt die Queue (siehe _diagnose_outcome).
_STASH_LOST_MARKER = "NICHT zurückgeholt"
_COLLECTION_RELEVANT_NAMES = {
    "__init__.py",
    "conftest.py",
    "pyproject.toml",
    "uv.lock",
}


class FailureClass(str, Enum):
    CANDIDATE_REGRESSION = "candidate_regression"
    MERGE_CONFLICT = "merge_conflict"
    MAIN_RED = "main_red"
    INFRA = "infra"
    UNCLEAR = "unclear"
    CLEAN_LAND = "clean_land"
    HELD_ESCALATED = "held_escalated"


class GateStage(str, Enum):
    BASELINE = "baseline"
    AFFECTED = "affected"
    FULL = "full"
    POST_MERGE = "post_merge"


@dataclass(frozen=True)
class LL2Candidate:
    task_or_branch_id: str
    candidate_commit: str
    failing_gate: str = "pending"
    failure_class: FailureClass = FailureClass.UNCLEAR

    @property
    def fingerprint(self) -> str:
        payload = "|".join(
            (
                self.task_or_branch_id,
                self.candidate_commit,
                self.failing_gate,
                self.failure_class.value,
            )
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class BaselineProbe:
    baseline_sha: str
    green: bool
    failure_class: FailureClass
    reason: str
    source: Literal["nightly", "landing", "none"] = "none"

    @staticmethod
    def sha_matches(record_sha: str, baseline_sha: str) -> bool:
        """Prefix-tolerant SHA equality; the ledger historically stores 9-char
        short SHAs while the baseline is always the full 40-char main SHA.

        Fail-closed: empty or sub-7-char values only match on full equality,
        so an ambiguous fragment can never produce a green baseline.
        """
        record = record_sha.strip().lower()
        baseline = baseline_sha.strip().lower()
        if not record or not baseline:
            return False
        if len(record) < 7 or len(baseline) < 7:
            return record == baseline
        return record.startswith(baseline) or baseline.startswith(record)

    @classmethod
    def from_records(
        cls,
        baseline_sha: str,
        nightly_records: list[dict[str, object]],
        landing_records: list[dict[str, object]] | None = None,
    ) -> BaselineProbe:
        nightly_pass = any(
            cls.sha_matches(str(record.get("head_sha", "")), baseline_sha)
            and str(record.get("result", "")).lower() == "pass"
            for record in nightly_records
        )
        if nightly_pass:
            return cls(
                baseline_sha,
                True,
                FailureClass.CLEAN_LAND,
                "Nightly-Full-Gate-Nachweis (präfix-eindeutig) für aktuellen main-SHA",
                "nightly",
            )
        landing_pass = any(
            cls.sha_matches(str(record.get("head_sha", "")), baseline_sha)
            and str(record.get("result", "")).lower() == "pass"
            and str(record.get("source", "")) == "landing_loop"
            for record in (landing_records or [])
        )
        if landing_pass:
            return cls(
                baseline_sha,
                True,
                FailureClass.CLEAN_LAND,
                "Landing-Gate-Nachweis (präfix-eindeutig) für aktuellen main-SHA",
                "landing",
            )
        return cls(
            baseline_sha,
            False,
            FailureClass.MAIN_RED,
            "Weder Nightly- noch Landing-Gate-Nachweis für den aktuellen main-SHA",
            "none",
        )


@dataclass(frozen=True)
class RunPlan:
    baseline: BaselineProbe
    candidates: tuple[LL2Candidate, ...]
    gate_stages: tuple[GateStage, ...]
    isolate: tuple[str, ...]
    stop_after: str | None
    stop_rest: bool


@dataclass(frozen=True)
class CandidateOutcome:
    candidate: LL2Candidate
    action: Action
    reason: str
    stop_rest: bool = False

    @property
    def failure_class(self) -> FailureClass:
        return self.candidate.failure_class

    @property
    def fingerprint(self) -> str:
        return self.candidate.fingerprint


def classify_failure(
    gate_name: str,
    output: str,
    baseline_sha: str,
    candidate_sha: str,
) -> FailureClass:
    """Classify a gate failure conservatively; unknown evidence stops the queue."""
    del candidate_sha  # reserved for stronger commit-specific evidence in LL2-S2
    text = f"{gate_name} {output}".lower()
    if gate_name.lower() == GateStage.BASELINE.value or (
        baseline_sha.lower() in text and ("main" in text or "baseline" in text)
    ):
        return FailureClass.MAIN_RED
    if any(token in text for token in ("held", "escalat")):
        return FailureClass.HELD_ESCALATED
    if any(
        token in text
        for token in (
            "timed out",
            "timeout",
            "no space left",
            "command not found",
            "connection reset",
            "connection refused",
            "gate-ausnahme",
            "killed",
        )
    ):
        return FailureClass.INFRA
    if any(
        token in text
        for token in ("failed ", " failed", "failure", "assertion", "tests/", " rot")
    ):
        return FailureClass.CANDIDATE_REGRESSION
    return FailureClass.UNCLEAR


def plan_queue(
    candidates: tuple[LL2Candidate, ...],
    baseline: BaselineProbe,
) -> RunPlan:
    ordered = tuple(sorted(candidates, key=lambda item: item.task_or_branch_id))
    isolated: list[str] = []
    planned: list[LL2Candidate] = []
    stop_after: str | None = None
    if baseline.green:
        for candidate in ordered:
            planned.append(candidate)
            if candidate.failure_class is FailureClass.CANDIDATE_REGRESSION:
                isolated.append(candidate.task_or_branch_id)
            elif candidate.failure_class in (
                FailureClass.MAIN_RED,
                FailureClass.INFRA,
                FailureClass.UNCLEAR,
            ) and candidate.failing_gate != "pending":
                stop_after = candidate.task_or_branch_id
                break
    return RunPlan(
        baseline=baseline,
        candidates=tuple(planned),
        gate_stages=(
            GateStage.BASELINE,
            GateStage.AFFECTED,
            GateStage.FULL,
            GateStage.POST_MERGE,
        ),
        isolate=tuple(isolated),
        stop_after=stop_after,
        stop_rest=not baseline.green or stop_after is not None,
    )


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
    plan: RunPlan
    diagnostics: tuple[CandidateOutcome, ...]
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
        baseline_records: BaselineRecords | None = None,
        landing_baseline_records: BaselineRecords | None = None,
        landing_evidence_writer: LandingEvidenceWriter | None = None,
        automation_enabled: Callable[[], bool] | None = None,
        recovery_request: Callable[[LL2Candidate], str | bool] | None = None,
        excluded_branches: Iterable[str] = (),
        state_dir: Path | None = None,
        stop_path: Path | None = None,
        now: Callable[[], datetime] | None = None,
    ):
        self.repo = repo.resolve()
        self.loops_root = loops_root.expanduser().resolve()
        self.ledger_dir = ledger_dir.expanduser().resolve()
        self.base = base
        self.dry_run = dry_run
        self.gate_runner = gate_runner
        self.baseline_records = baseline_records or read_gate_records
        self.landing_baseline_records = (
            landing_baseline_records or read_landing_gate_records
        )
        self.landing_evidence_writer = landing_evidence_writer or (
            lambda head, gates, candidate, ts: record_landing_gate_pass(
                head, gates, candidate, ts=ts
            )
        )
        self.automation_enabled = automation_enabled or (lambda: True)
        self.recovery_request = recovery_request
        self.excluded_branches = frozenset(excluded_branches)
        self.state_dir = state_dir
        self.stop_path = stop_path
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._collection_proven = False

    def _automation_checkpoint(self) -> str | None:
        if self.dry_run:
            return None
        if self.stop_path is not None and self.stop_path.exists():
            return "kooperativer STOP angefordert"
        try:
            enabled = self.automation_enabled()
        except Exception:
            enabled = False
        if not enabled:
            return "Landing-Automatik deaktiviert"
        return None

    def _write_runtime_status(
        self,
        *,
        baseline: BaselineProbe,
        inventory: tuple[BranchInventory, ...],
        outcomes: tuple[BranchOutcome, ...],
    ) -> None:
        if self.state_dir is None or self.dry_run:
            return
        write_landing_runtime_status(
            {
                "baseline_sha": baseline.baseline_sha,
                "baseline_ok": baseline.green,
                "queue_summary": {
                    "total": len(inventory),
                    "landed": sum(item.action == "landed" for item in outcomes),
                    "cleaned": sum(item.action == "cleaned" for item in outcomes),
                    "parked": sum(item.action == "parked" for item in outcomes),
                },
                "last_result": "green"
                if all(item.action != "parked" for item in outcomes)
                else "held",
                "candidates": [
                    {
                        "branch": item.branch,
                        "head": item.head,
                        "ahead": item.ahead,
                        "behind": item.behind,
                    }
                    for item in inventory
                ],
                "updated_at": self.now().isoformat(),
            },
            state_dir=self.state_dir,
        )

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
        return tuple(
            sorted(
                line
                for line in result.stdout.splitlines()
                if line and line not in self.excluded_branches
            )
        )

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

    def _main_branch_problem(self) -> str | None:
        branch = self._git(
            "symbolic-ref",
            "--quiet",
            "--short",
            "HEAD",
        )
        if branch.returncode != 0 or branch.stdout.strip() != self.base:
            current = branch.stdout.strip() or "detached"
            return f"Basis-Worktree steht auf {current}, erwartet {self.base}"
        return None

    @staticmethod
    def _parse_porcelain_paths(blob: str) -> list[str]:
        """All affected paths from ``git status --porcelain -z``.

        Rename/copy entries carry two NUL fields (new path first, source
        second); BOTH are returned, because a stash pathspec without the
        source leaves the rename half-applied.
        """
        fields = blob.split("\0")
        paths: list[str] = []
        index = 0
        while index < len(fields):
            entry = fields[index]
            index += 1
            if not entry:
                continue
            status_code, path = entry[:2], entry[3:]
            if "R" in status_code or "C" in status_code:
                source = fields[index] if index < len(fields) else ""
                index += 1
                if source:
                    paths.append(source)
            if path:
                paths.append(path)
        return paths

    def _main_dirty_paths(self) -> list[str] | None:
        status = self._git("status", "--porcelain", "-z")
        if status.returncode != 0:
            return None
        return self._parse_porcelain_paths(status.stdout)

    def _merge_paths(self, branch: str) -> list[str] | None:
        """Paths the merge would touch; ``None`` when undeterminable."""
        merge_base = self._git("merge-base", self.base, branch)
        if merge_base.returncode != 0:
            return None
        diff = self._git("diff", "--name-only", "-z", merge_base.stdout.strip(), branch)
        if diff.returncode != 0:
            return None
        return [path for path in diff.stdout.split("\0") if path]

    @staticmethod
    def _dirty_overlap(dirty_paths: list[str], merge_paths: list[str]) -> list[str]:
        """Dirty paths the merge would actually touch.

        Untracked directories (trailing ``/``) match any merge path beneath
        them; everything else matches exactly. A merge path that merely adds a
        sibling file next to dirty work does NOT overlap.
        """
        merge_set = set(merge_paths)
        hits = []
        for path in dirty_paths:
            if path.endswith("/"):
                if any(candidate.startswith(path) for candidate in merge_set):
                    hits.append(path)
            elif path in merge_set:
                hits.append(path)
        return sorted(hits)

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
        branch_problem = self._main_branch_problem()
        if branch_problem:
            return self._park(item, branch_problem)
        dirty_paths = self._main_dirty_paths()
        if dirty_paths is None:
            return self._park(item, "Status des Basis-Worktrees nicht lesbar")
        overlap: list[str] = []
        if dirty_paths:
            merge_paths = self._merge_paths(item.branch)
            if merge_paths is None:
                return self._park(
                    item, "Merge-Umfang nicht bestimmbar (merge-base/diff rot)"
                )
            overlap = self._dirty_overlap(dirty_paths, merge_paths)
        if overlap:
            shown = ", ".join(overlap[:5])
            suffix = f" (+{len(overlap) - 5} weitere)" if len(overlap) > 5 else ""
            return self._park(
                item,
                "Fremde Änderungen im Basis-Worktree kreuzen den Merge: "
                f"{shown}{suffix}",
            )
        if self.dry_run:
            conflict = self._preview_merge_conflict(item.branch)
            if conflict:
                return self._park(item, f"dry-run: Merge würde scheitern ({conflict})")
            stash_note = (
                f"; {len(dirty_paths)} fremde Dirty-Pfade disjunkt → Stash-Schutz"
                if dirty_paths
                else ""
            )
            return BranchOutcome(
                item.branch,
                "landed",
                f"dry-run: würde {ahead} Commit(s) mergen und _land_gates fahren "
                f"(frisch behind={behind}{stash_note})",
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

        stash_problem, stash_sha = self._stash_foreign_dirt(dirty_paths)
        if stash_problem is not None:
            return self._park(item, stash_problem)
        outcome: BranchOutcome | None = None
        thrown: BaseException | None = None
        try:
            outcome = self._merge_and_prove(item)
        except BaseException as exc:  # noqa: BLE001 - Pop muss trotzdem laufen
            thrown = exc
        pop_problem: str | None = None
        if stash_sha is not None:
            pop_problem = self._pop_foreign_stash(stash_sha, dirty_paths)
        if thrown is not None:
            if pop_problem is not None:
                print(
                    "Landing-Loop: Stash-Rückholung nach Ausnahme fehlgeschlagen: "
                    f"{pop_problem}",
                    file=sys.stderr,
                )
            raise thrown
        assert outcome is not None
        if pop_problem is not None:
            # Original-Reason als Präfix behalten: der Prefix-Dispatch in
            # _diagnose_outcome (Gate rot / Merge-Konflikt) muss intakt
            # bleiben, sonst entwertet der Pop-Fehler die Klassifikation.
            return self._park(
                item,
                f"{outcome.reason}; fremde Änderungen {_STASH_LOST_MARKER}: "
                f"{pop_problem}",
            )
        if stash_sha is not None and outcome.action == "landed":
            outcome = BranchOutcome(
                outcome.branch,
                outcome.action,
                f"{outcome.reason}; {len(dirty_paths)} fremde Dirty-Pfade "
                "gestasht und zurückgeholt",
            )
        return outcome

    def _stash_foreign_dirt(
        self, dirty_paths: list[str]
    ) -> tuple[str | None, str | None]:
        """Park foreign dirty paths in an anchored stash.

        Returns ``(problem, stash_sha)``. ``stash_sha`` is the anchored stash
        commit to pop later — ``None`` when git created no entry (a foreign
        session committed its work between our status read and the push;
        git exits 0 without an entry in that window). The sha, never a
        positional ``stash@{0}``, authorizes the later pop.
        """
        if not dirty_paths:
            return None, None
        # Kein Pathspec: die Dirty-Liste IST der komplette Schmutz des
        # Checkouts, und ein Pathspec scheitert an geloeschten Seiten
        # gestagter Renames ("did not match any files").
        before = self._git("rev-parse", "-q", "--verify", "refs/stash").stdout.strip()
        stash = self._git(
            "stash",
            "push",
            "--include-untracked",
            "--quiet",
            "-m",
            _STASH_MESSAGE,
        )
        if stash.returncode != 0:
            return (
                "Stash fremder Änderungen fehlgeschlagen: "
                f"{_one_line(stash.stderr or stash.stdout)}"
            ), None
        after = self._git("rev-parse", "-q", "--verify", "refs/stash").stdout.strip()
        stash_sha: str | None = None
        if after and after != before:
            # Nicht dem Spitzenwert vertrauen: eine Fremdsession kann im
            # Fenster push→rev-parse selbst gestasht haben. Der Anker ist
            # der neueste Eintrag mit UNSEREM Betreff.
            stash_sha = self._find_own_stash()
            if stash_sha is None:
                return (
                    f"fremde Änderungen {_STASH_LOST_MARKER}: Stash-Anker "
                    "nicht verifizierbar (Betreff-Match fehlgeschlagen) — "
                    "fail-closed, `git stash list` manuell prüfen"
                ), None
        residual = self._git("status", "--porcelain")
        if residual.returncode != 0 or residual.stdout:
            if stash_sha is not None:
                pop_problem = self._pop_foreign_stash(stash_sha, dirty_paths)
                if pop_problem is not None:
                    return (
                        f"Basis-Worktree nach Stash nicht sauber; fremde "
                        f"Änderungen {_STASH_LOST_MARKER} ({pop_problem}) — "
                        "MANUELL prüfen, Stash-Eintrag bleibt erhalten"
                    ), None
            return ("Basis-Worktree nach Stash nicht sauber; Landung ausgesetzt"), None
        return None, stash_sha

    def _find_own_stash(self) -> str | None:
        """SHA of the newest stash entry carrying OUR message (newest first)."""
        listing = self._git("stash", "list", "--format=%H%x09%gs")
        for line in listing.stdout.splitlines():
            sha, _tab, subject = line.partition("\t")
            if subject.strip() == f"On {self.base}: {_STASH_MESSAGE}":
                return sha.strip() or None
        return None

    def _pop_foreign_stash(self, stash_sha: str, dirty_paths: list[str]) -> str | None:
        """Restore exactly OUR anchored stash entry; never a positional guess.

        ``apply --index <sha>`` is position-independent (verified: git accepts
        a raw stash-commit sha) and restores the foreign session's staged
        state as staged. Only afterwards is the entry dropped — by its
        re-verified position. Every failure leaves the entry untouched.
        """
        current_dirty = self._main_dirty_paths()
        if current_dirty is None:
            return (
                "Status des Basis-Worktrees vor dem Pop nicht lesbar — "
                "Pop ausgesetzt, Stash bleibt erhalten"
            )
        rewritten = sorted(set(current_dirty) & set(dirty_paths))
        if rewritten:
            return (
                "fremde Session hat zwischenzeitlich erneut geschrieben "
                f"({', '.join(rewritten[:5])}) — Stash bleibt erhalten, "
                "manuell zusammenführen"
            )
        apply = self._git("stash", "apply", "--index", "--quiet", stash_sha)
        if apply.returncode != 0:
            return (
                "Stash-Apply des eigenen Eintrags fehlgeschlagen — Eintrag "
                f"bleibt erhalten: {_one_line(apply.stderr or apply.stdout)}"
            )
        listing = self._git("stash", "list", "--format=%H")
        entries = [
            line.strip() for line in listing.stdout.splitlines() if line.strip()
        ]
        if stash_sha not in entries:
            return None  # Eintrag bereits weg — angewendet ist angewendet
        drop = self._git(
            "stash", "drop", "--quiet", f"stash@{{{entries.index(stash_sha)}}}"
        )
        if drop.returncode != 0:
            return (
                "Stash angewendet, aber Eintrag nicht entfernbar "
                f"({_one_line(drop.stderr or drop.stdout)}) — manuell: "
                "`git stash list`"
            )
        return None

    def _merge_and_prove(self, item: BranchInventory) -> BranchOutcome:
        pre_merge_head = self._git("rev-parse", "HEAD", check=True).stdout.strip()
        merge = self._git("merge", "--no-edit", item.branch)
        if merge.returncode != 0:
            # Inhaltskonflikt (kandidaten-lokal, recoverable) vs. alles andere
            # (Infra/fremder Merge/index.lock — konservativ klassifizieren).
            unmerged = self._git("ls-files", "-u")
            self._git("merge", "--abort")
            detail = _one_line(merge.stderr or merge.stdout or "keine Fehlerdetails")
            if unmerged.returncode == 0 and unmerged.stdout.strip():
                return self._park(item, f"Merge-Konflikt: {detail}")
            return self._park(item, f"Merge-Fehler: {detail}")

        changed = self._git(
            "diff", "--name-only", f"{pre_merge_head}..HEAD", check=True
        ).stdout.splitlines()
        include_collection = not self._collection_proven or any(
            Path(path).name in _COLLECTION_RELEVANT_NAMES for path in changed
        )
        try:
            if self.gate_runner is None:
                green, gate_report = _land_gates(
                    self.repo,
                    pre_merge_head,
                    include_collection=include_collection,
                )
            else:
                green, gate_report = self.gate_runner(self.repo, pre_merge_head)
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

        if include_collection:
            self._collection_proven = True

        landed_head = self._git("rev-parse", "HEAD", check=True).stdout.strip()
        gates = (("collection",) if include_collection else ()) + ("affected",)
        if "frontend" in gate_report.lower():
            gates += ("frontend",)
        try:
            self.landing_evidence_writer(
                landed_head,
                gates,
                item.branch,
                self.now().isoformat(),
            )
        except Exception as exc:  # noqa: BLE001 - missing proof is fail-closed
            rolled_back, rollback_report = self._rollback(pre_merge_head)
            if rolled_back:
                return self._park(
                    item,
                    "Landing-Evidenz nicht schreibbar; "
                    f"{rollback_report}: {_one_line(str(exc))}",
                )
            return self._park(
                item,
                "Landing-Evidenz nicht schreibbar; "
                f"ROLLBACK FEHLGESCHLAGEN ({rollback_report}): {_one_line(str(exc))}",
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

    def _diagnose_outcome(
        self,
        item: BranchInventory,
        result: BranchOutcome,
        baseline_sha: str,
    ) -> CandidateOutcome:
        if result.action in ("landed", "cleaned"):
            failure_class = FailureClass.CLEAN_LAND
            failing_gate = "none"
        elif "Gate rot" in result.reason:
            failing_gate = GateStage.POST_MERGE.value
            failure_class = classify_failure(
                failing_gate,
                result.reason,
                baseline_sha,
                item.head,
            )
        elif result.reason.startswith("Merge-Konflikt"):
            # Candidate-local: a content conflict neither proves a regression
            # nor authorizes stopping the rest of the queue. It is recoverable
            # (rebase by a worker), so it gets its own class instead of the
            # generic policy hold.
            failing_gate = "merge"
            failure_class = FailureClass.MERGE_CONFLICT
        elif result.reason.startswith("Merge-Fehler"):
            # Kein Inhaltskonflikt (index.lock, fremder Merge, …): die
            # generische Klassifikation entscheidet — INFRA/UNCLEAR stoppen
            # die Queue, alles andere bleibt kandidaten-lokal.
            failing_gate = "merge"
            failure_class = classify_failure(
                failing_gate,
                result.reason,
                baseline_sha,
                item.head,
            )
        else:
            # Policy holds stay candidate-local and do not invent a new action.
            failure_class = FailureClass.HELD_ESCALATED
            failing_gate = "policy"
        candidate = LL2Candidate(
            item.branch,
            item.head,
            failing_gate,
            failure_class,
        )
        return CandidateOutcome(
            candidate,
            result.action,
            result.reason,
            stop_rest=failure_class
            in (FailureClass.MAIN_RED, FailureClass.INFRA, FailureClass.UNCLEAR)
            or _STASH_LOST_MARKER in result.reason,
        )

    def run(
        self,
        *,
        after_inventory: Callable[[tuple[BranchInventory, ...]], None] | None = None,
    ) -> LandingRun:
        self._validate()
        started_at = self.now()
        inventory = self.inventory()
        baseline_sha = self._git("rev-parse", self.base, check=True).stdout.strip()
        baseline = BaselineProbe.from_records(
            baseline_sha,
            self.baseline_records(),
            self.landing_baseline_records(),
        )
        candidates = tuple(LL2Candidate(item.branch, item.head) for item in inventory)
        plan = plan_queue(candidates, baseline)
        if after_inventory is not None:
            after_inventory(inventory)
        outcomes_list: list[BranchOutcome] = []
        diagnostics_list: list[CandidateOutcome] = []
        stop_reason: str | None = baseline.reason if plan.stop_rest else None
        for item in inventory:
            checkpoint_reason = self._automation_checkpoint()
            if checkpoint_reason is not None:
                stop_reason = checkpoint_reason
            # Ein Stopp gated nur das LANDEN (Merge + Gates). Das Bereinigen
            # eines Leerstands (ahead == 0) bewegt ausschließlich den
            # Loop-Branch auf die Basis — kein Merge, keine Gates, keine
            # Abhängigkeit zur Baseline. Es läuft daher auch bei rotem
            # main weiter; einziger harter Halt ist der Operator-Checkpoint
            # (Automatik aus / STOP-Datei).
            if stop_reason is not None and (
                checkpoint_reason is not None or item.ahead != 0
            ):
                result = self._park(item, f"Rest-Queue gestoppt: {stop_reason}")
                failure_class = (
                    FailureClass.MAIN_RED
                    if not baseline.green
                    else FailureClass.HELD_ESCALATED
                )
                candidate = LL2Candidate(
                    item.branch,
                    item.head,
                    GateStage.BASELINE.value if not baseline.green else "queue",
                    failure_class,
                )
                diagnosed = CandidateOutcome(
                    candidate,
                    result.action,
                    result.reason,
                    stop_rest=not baseline.green,
                )
            else:
                result = self._cleanup(item) if item.ahead == 0 else self._land(item)
                diagnosed = self._diagnose_outcome(item, result, baseline_sha)
                if (
                    not self.dry_run
                    and diagnosed.failure_class
                    in (FailureClass.CANDIDATE_REGRESSION, FailureClass.MERGE_CONFLICT)
                    and self.recovery_request is not None
                    and self._automation_checkpoint() is None
                ):
                    try:
                        recovery_result = self.recovery_request(diagnosed.candidate)
                    except Exception as exc:  # noqa: BLE001 - recovery darf den Lauf nie abbrechen
                        recovery_result = f"error:{exc}"
                    if recovery_result in (False, "exhausted"):
                        result = BranchOutcome(
                            result.branch,
                            result.action,
                            f"{result.reason}; Recovery held_escalated",
                        )
                        diagnosed = CandidateOutcome(
                            diagnosed.candidate,
                            diagnosed.action,
                            result.reason,
                            stop_rest=diagnosed.stop_rest,
                        )
                    elif isinstance(
                        recovery_result, str
                    ) and recovery_result.startswith("error:"):
                        result = BranchOutcome(
                            result.branch,
                            result.action,
                            f"{result.reason}; Recovery fehlgeschlagen "
                            f"({recovery_result[6:]})",
                        )
                        diagnosed = CandidateOutcome(
                            diagnosed.candidate,
                            diagnosed.action,
                            result.reason,
                            stop_rest=diagnosed.stop_rest,
                        )
                if diagnosed.stop_rest:
                    stop_reason = (
                        f"{diagnosed.failure_class.value} bei {item.branch} "
                        f"({diagnosed.fingerprint[:12]})"
                    )
            outcomes_list.append(result)
            diagnostics_list.append(diagnosed)
        outcomes = tuple(outcomes_list)
        diagnostics = tuple(diagnostics_list)
        if not self.dry_run:
            outcomes = tuple(
                self._freshen_completed(item, result)
                for item, result in zip(inventory, outcomes, strict=True)
            )
        self._write_runtime_status(
            baseline=baseline,
            inventory=inventory,
            outcomes=outcomes,
        )
        run = LandingRun(
            started_at=started_at,
            finished_at=self.now(),
            dry_run=self.dry_run,
            inventory=inventory,
            outcomes=outcomes,
            plan=plan,
            diagnostics=diagnostics,
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
            plan=run.plan,
            diagnostics=run.diagnostics,
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
    lines += ["", "## LL-2 Diagnose", ""]
    lines.append(
        f"- Baseline `{run.plan.baseline.baseline_sha}`: "
        f"**{'green' if run.plan.baseline.green else 'main_red'}** — "
        f"{run.plan.baseline.reason}"
    )
    lines.append(
        "- Gate-Matrix: "
        + " → ".join(stage.value for stage in run.plan.gate_stages)
    )
    for item in run.diagnostics:
        lines.append(
            f"- `{item.fingerprint}` {item.candidate.task_or_branch_id}: "
            f"**{item.failure_class.value}** ({item.candidate.failing_gate})"
        )
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


def _resolve_recovery_task_id(conn, recovery_key: str) -> str | None:
    """Find the OPEN recovery card for this key, if one exists.

    Terminal cards (done/archived — kanban has no other terminal statuses)
    never absorb a new request: a card that was closed while the branch is
    still broken must not silently collect request events nobody picks up.
    """
    row = conn.execute(
        "SELECT id FROM tasks WHERE idempotency_key = ? "
        "AND status NOT IN ('done', 'archived') "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (recovery_key,),
    ).fetchone()
    return str(row["id"]) if row is not None else None


def _task_status(conn, task_id: str) -> str | None:
    row = conn.execute(
        "SELECT status FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    return str(row["status"]) if row is not None else None


def _create_recovery_task(conn, candidate: LL2Candidate, recovery_key: str) -> str:
    """Materialize the kanban card that repairs a pack-named loop branch.

    Idempotent per branch+fingerprint: repeated runs attach to the same open
    card. ``create_task`` deduplicates against every non-archived card —
    including terminal ``done`` ones — so when it hands back a terminal card
    we mint a fresh one with a version suffix instead of hanging the request
    on a closed card. No ``branch_name``/worktree binding: worker isolation
    provisions its own chain worktree and would overwrite the binding
    anyway; the branch is repaired where it lives (its loop worktree).
    """
    from hermes_cli import kanban_db

    branch = candidate.task_or_branch_id
    pack = branch.removeprefix("loop/")

    def create(key: str) -> str:
        return kanban_db.create_task(
            conn,
            title=f"Landing-Recovery: {branch} landbar machen",
            body=(
                f"Der Landing-Loop konnte `{branch}` nicht landen "
                f"({candidate.failure_class.value}, Fingerprint "
                f"{candidate.fingerprint[:12]}).\n\n"
                f"Reparatur dort, wo der Branch lebt — im Loop-Worktree "
                f"`~/.hermes/loops/{pack}/wt` (dort ist `{branch}` ausgecheckt):\n"
                "1. Worktree-Status prüfen (muss sauber sein)\n"
                f"2. `~/.hermes/loops/{pack}/STOP` und Pack-Lock respektieren\n"
                "3. `git rebase main`, Konflikte auflösen\n"
                "4. `scripts/run-affected.sh main` im Loop-Worktree grün fahren\n"
                "Danach landet der nächste Landing-Loop-Lauf den Branch selbst."
            ),
            assignee="coder",
            created_by="landing_loop",
            idempotency_key=key,
            skills=["loop-branch-repair"],
        )

    task_id = create(recovery_key)
    if _task_status(conn, task_id) not in ("done", "archived"):
        return task_id
    version = 2
    while True:
        versioned_key = f"{recovery_key}:v{version}"
        existing = _resolve_recovery_task_id(conn, versioned_key)
        if existing is not None:
            return existing
        candidate_id = create(versioned_key)
        if _task_status(conn, candidate_id) not in ("done", "archived"):
            return candidate_id
        version += 1


def request_candidate_recovery(candidate: LL2Candidate) -> str:
    """Request one idempotent same-card recovery for a candidate.

    Task resolution order: a task-encoded branch suffix (``loop/t_<id>``),
    then the open recovery card for this branch+fingerprint, then a newly
    created one for pack-named loop branches.
    """
    branch = candidate.task_or_branch_id
    task_id = branch.rsplit("/", 1)[-1]
    from hermes_cli import kanban_db

    with kanban_db.connect() as conn:
        if re.fullmatch(r"t_[a-z0-9]+", task_id) is None:
            recovery_key = f"landing-recovery:{branch}:{candidate.fingerprint[:12]}"
            task_id = _resolve_recovery_task_id(conn, recovery_key)
            if task_id is None:
                task_id = _create_recovery_task(conn, candidate, recovery_key)
        if candidate.failure_class is FailureClass.MERGE_CONFLICT:
            blocking_findings = [
                f"Merge-Konflikt mit main bei {candidate.candidate_commit}: "
                "Rebase und Konfliktauflösung erforderlich"
            ]
            required_verification = ["merge", GateStage.AFFECTED.value]
        else:
            blocking_findings = [
                f"Candidate regression in {candidate.failing_gate} at "
                f"{candidate.candidate_commit}"
            ]
            required_verification = [candidate.failing_gate]
        created = kanban_db.request_landing_recovery(
            conn,
            task_id,
            fingerprint=candidate.fingerprint,
            candidate_commit=candidate.candidate_commit,
            failure_class=candidate.failure_class.value,
            failing_gate=candidate.failing_gate,
            blocking_findings=blocking_findings,
            required_verification=required_verification,
        )
        if created:
            return "requested"
        rows = conn.execute(
            "SELECT kind, payload FROM task_events "
            "WHERE task_id = ? AND kind = 'landing_recovery_exhausted' "
            "ORDER BY id DESC LIMIT 10",
            (task_id,),
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["payload"] or "{}")
            except (TypeError, ValueError):
                continue
            if payload.get("fingerprint") == candidate.fingerprint:
                return "exhausted"
    return "deduplicated"


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

    state_dir_value = os.environ.get("HERMES_LOOP_STATE_DIR")
    stop_path_value = os.environ.get("HERMES_LOOP_STOP_PATH")
    state_dir = Path(state_dir_value) if state_dir_value else None
    stop_path = Path(stop_path_value) if stop_path_value else None

    loop = LandingLoop(
        args.repo,
        args.loops_root,
        args.ledger_dir,
        base=args.base,
        dry_run=args.dry_run,
        automation_enabled=(
            (lambda: get_landing_automation_enabled(state_dir))
            if state_dir is not None
            else None
        ),
        recovery_request=None if args.dry_run else request_candidate_recovery,
        excluded_branches=(f"loop/{state_dir.name}",) if state_dir else (),
        state_dir=state_dir,
        stop_path=stop_path,
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
