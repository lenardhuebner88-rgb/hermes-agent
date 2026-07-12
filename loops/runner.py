"""Loop-Runner — führt Loop-Packs aus (Archetypen: pipeline | sweep).

CLI:
    python -m loops.runner --pack <name> --cmd plan|run|night|status
                           [--state-root PFAD] [--fresh] [--skip-plan]

Ein Pack (loops/packs/<name>/) beschreibt in pack.yaml WAS läuft (Phasen mit
Engine/Modell/Timeout/Prompt, Stop-Kriterien); der Runner liefert das WIE:
Worktree-Isolation, Datei-Queue, Ledger, deterministische Disposition
(Retry/Revert/Bounce), Locks, Usage-Limit-Stop, Discord-Notify.

Laufzeit-State: ~/.hermes/loops/<pack>/ (Override: --state-root, für Tests).
Der Runner pusht/deployt/merged standardmaessig NIE — Landung ist ein bewusster
Morgen-Schritt. Eine eng begrenzte, im Code allowlistete Ausnahme darf nach einem
unabhaengigen Pipeline-PASS deterministisch landen; spawned Modelle selbst erhalten
dadurch keine Push-/Merge-Befehle.

Portiert vom bewiesenen Bash-Harness ~/.hermes/fable-loop/ (2026-07-02); die
dort teuer gelernten Fallen sind hier Invarianten:
  * `git clean` nur auf Driver-Ebene (guard-Hook blockt es headless in-session)
  * Worktree-Checks über geparste Porcelain-Ausgabe (kein grep -q an
    pipefail-Pipes — SIGPIPE-Race)
  * Usage-/Session-Limit stoppt sofort (Regex in loops.engines)
  * Status-Wahrheit = last-status-Datei + Git-HEAD, nie Agent-Prosa
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

from hermes_constants import get_hermes_home
from loops import engines

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKS_DIR = REPO_ROOT / "loops" / "packs"
# Werkstatt-Substrat (v2.1): vom Operator/Dashboard angelegte Packs leben im State,
# nie im Repo — Browser-Edits dürfen den Live-Checkout nicht dirty machen.
_HERMES_HOME = get_hermes_home()
CUSTOM_PACKS_DIR = _HERMES_HOME / "loops" / "packs-custom"
DEFAULT_STATE_ROOT = _HERMES_HOME / "loops"
NOTIFY_SCRIPT = _HERMES_HOME / "scripts" / "discord-notify.py"

QUEUE_STAGES = ("00-planned", "10-building", "20-verified", "30-landed", "90-bounced")
DEFAULT_STOP = {"max_rounds": 12, "max_hours": 7, "fail_streak": 2, "dry_rounds": 2}

# Operator-Entscheid 2026-07-09 (Modell-Update 2026-07-12: Fable raus, Opus 4.8
# plant + verifiziert): genau dieser kuratierte Opus→Sol→Opus-Loop darf
# nach einem unabhaengigen PASS ueber die deterministische Landungsleiter selbst
# ff-mergen und nach piet-fork pushen. Die Autoritaet ist nicht nur an den Namen,
# sondern an Quelle, Live-Repo, Rollen/Modelle und exakte Manifest-/Prompt-Inhalte
# gebunden. Eine Pack-Kopie oder Manifest-Aenderung faellt dadurch fail-closed aus.
AUTOLAND_PACK_ALLOWLIST = frozenset({"dashboard-experience"})
AUTOLAND_EXPECTED_REPO = Path("/home/piet/.hermes/hermes-agent").resolve()
AUTOLAND_PHASE_CONTRACT = {
    "plan": ("claude", "claude-opus-4-8", "PLANNER-PROMPT.md"),
    "build": ("codex", "gpt-5.6-sol", "BUILDER-PROMPT.md"),
    "verify": ("claude", "claude-opus-4-8", "VERIFIER-PROMPT.md"),
}
AUTOLAND_PATH_PREFIXES = ("web/src/control/",)
# Werden zusammen mit den kuratierten Dateien aktualisiert. Der Loader prueft
# beide Ebenen: menschenlesbaren Rollenvertrag und bytegenaue Inhaltsbindung.
AUTOLAND_MANIFEST_SHA256 = {
    "dashboard-experience": "656a47081bc6e91fcb06c1346c93f93316a08a8db9588d1aa7604ee3e86e3eff",
}
AUTOLAND_PROMPT_SHA256 = {
    "dashboard-experience": {
        "PLANNER-PROMPT.md": "8a65ad96a5398d1eb3f01c00f1051ef5a6c580a46d978447307707f13303758b",
        "BUILDER-PROMPT.md": "55d09f80c724dcb8c8f55bc94a19fc9fd4d42291908cf697659abe7e7db736c0",
        "VERIFIER-PROMPT.md": "f6f4db9b95c55f6ebbfc0514a656c1b362e997d3a9f64344621c786de9ca94db",
    },
}

PHASES_BY_TYPE = {"pipeline": ("plan", "build", "verify"), "sweep": ("round",)}

RETRY_RE = re.compile(r"^retry:\s*(\d+)", re.MULTILINE)
PLAN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
PASS_STATUS_RE = re.compile(r"^PASS\s+([A-Za-z0-9][A-Za-z0-9._-]{0,127})$")


class ManifestError(ValueError):
    """pack.yaml ist unbrauchbar — Meldung nennt Pack und Feld."""


@dataclass
class PhaseCfg:
    engine: str
    model: str
    timeout: int
    prompt: str  # Dateiname relativ zum Pack-Ordner


@dataclass
class Pack:
    name: str
    type: str
    repo: Path
    pack_dir: Path
    phases: dict[str, PhaseCfg]
    stop: dict[str, int]
    description: str = ""
    stability: str = "experimental"
    notify: dict[str, str] = field(default_factory=dict)
    params: dict[str, str] = field(default_factory=dict)
    autoland: bool = False
    base_branch: str = "main"
    land_remote: str = "piet-fork"
    land_gates: list[str] | None = None
    land_push: bool = True

    @property
    def branch(self) -> str:
        return f"loop/{self.name}"


_PACK_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,63}$")


def resolve_packs_dir(
    name: str, primary: Path = PACKS_DIR, custom: Path = CUSTOM_PACKS_DIR
) -> Path:
    """Suchpfad Repo-Packs → Custom-Packs; Namens-Kollision ist ein harter Fehler
    (sonst würde ein Custom-Pack still ein kuratiertes Repo-Pack verschatten)."""
    if not _PACK_NAME_RE.match(name):
        raise ManifestError(f"Pack-Name ungültig: {name!r}")
    in_primary = (primary / name / "pack.yaml").is_file()
    in_custom = (custom / name / "pack.yaml").is_file()
    if in_primary and in_custom:
        raise ManifestError(
            f"Pack {name!r} existiert doppelt (Repo + packs-custom) — Custom-Pack umbenennen"
        )
    return custom if in_custom else primary


def load_pack(packs_dir: Path, name: str) -> Pack:
    # Charset-Whitelist vor jedem Pfad-Join (CLI und HTTP teilen sich diesen Loader).
    if not _PACK_NAME_RE.match(name):
        raise ManifestError(f"Pack-Name ungültig: {name!r}")
    pack_dir = packs_dir / name
    manifest = pack_dir / "pack.yaml"
    if not manifest.is_file():
        available = sorted(p.name for p in packs_dir.iterdir() if p.is_dir()) if packs_dir.is_dir() else []
        raise ManifestError(f"Pack {name!r}: {manifest} fehlt — vorhanden: {available}")
    try:
        raw = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ManifestError(f"Pack {name!r}: pack.yaml ist kein gültiges YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ManifestError(f"Pack {name!r}: pack.yaml muss ein Mapping sein")

    if raw.get("name") != name:
        raise ManifestError(f"Pack {name!r}: name-Feld ({raw.get('name')!r}) muss dem Ordnernamen entsprechen")
    ptype = raw.get("type")
    if ptype not in PHASES_BY_TYPE:
        raise ManifestError(f"Pack {name!r}: type muss pipeline|sweep sein, ist {ptype!r}")
    repo = raw.get("repo")
    if not isinstance(repo, str) or not repo.strip():
        raise ManifestError(f"Pack {name!r}: repo (Pfad zum Git-Repo) fehlt")

    phases_raw = raw.get("phases")
    required = PHASES_BY_TYPE[ptype]
    if not isinstance(phases_raw, dict) or set(phases_raw) != set(required):
        raise ManifestError(
            f"Pack {name!r}: type={ptype} braucht genau die Phasen {sorted(required)}, "
            f"hat {sorted(phases_raw) if isinstance(phases_raw, dict) else phases_raw!r}"
        )
    phases: dict[str, PhaseCfg] = {}
    for pname, pcfg in phases_raw.items():
        if not isinstance(pcfg, dict):
            raise ManifestError(f"Pack {name!r}: Phase {pname} muss ein Mapping sein")
        missing = {"engine", "model", "timeout", "prompt"} - set(pcfg)
        if missing:
            raise ManifestError(f"Pack {name!r}: Phase {pname} fehlt {sorted(missing)}")
        if pcfg["engine"] not in engines.ENGINES:
            raise ManifestError(
                f"Pack {name!r}: Phase {pname}: Engine {pcfg['engine']!r} unbekannt "
                f"(registriert: {sorted(engines.ENGINES)})"
            )
        # bool ist int-Subklasse: `timeout: true` wäre sonst ein 1s-Timeout.
        if not isinstance(pcfg["timeout"], int) or isinstance(pcfg["timeout"], bool) \
                or pcfg["timeout"] <= 0:
            raise ManifestError(f"Pack {name!r}: Phase {pname}: timeout muss positive Ganzzahl sein")
        if pcfg["model"] is None or not str(pcfg["model"]).strip():
            raise ManifestError(f"Pack {name!r}: Phase {pname}: model fehlt/leer")
        prompt_file = pack_dir / str(pcfg["prompt"])
        if not prompt_file.is_file():
            raise ManifestError(f"Pack {name!r}: Phase {pname}: Prompt-Datei fehlt: {prompt_file}")
        phases[pname] = PhaseCfg(
            engine=pcfg["engine"], model=str(pcfg["model"]),
            timeout=pcfg["timeout"], prompt=str(pcfg["prompt"]),
        )

    for section in ("stop", "params", "notify"):
        if raw.get(section) is not None and not isinstance(raw[section], dict):
            raise ManifestError(f"Pack {name!r}: {section} muss ein Mapping sein")
    stop = dict(DEFAULT_STOP)
    for key, val in (raw.get("stop") or {}).items():
        if key not in DEFAULT_STOP:
            raise ManifestError(f"Pack {name!r}: stop.{key} unbekannt (erlaubt: {sorted(DEFAULT_STOP)})")
        if not isinstance(val, int) or isinstance(val, bool) or val <= 0:
            raise ManifestError(f"Pack {name!r}: stop.{key} muss positive Ganzzahl sein")
        stop[key] = val

    autoland_raw = raw.get("autoland", False)
    if not isinstance(autoland_raw, bool):
        raise ManifestError(f"Pack {name!r}: autoland muss boolean sein")
    autoland = autoland_raw
    if autoland and name not in AUTOLAND_PACK_ALLOWLIST:
        raise ManifestError(
            f"Pack {name!r}: autoland nicht autorisiert; Allowlist="
            f"{sorted(AUTOLAND_PACK_ALLOWLIST)}"
        )
    if autoland and ptype != "pipeline":
        raise ManifestError(f"Pack {name!r}: autoland braucht type=pipeline")
    if autoland:
        expected_dir = (PACKS_DIR / name).resolve()
        if pack_dir.resolve() != expected_dir:
            raise ManifestError(
                f"Pack {name!r}: autoland ist nur aus dem kuratierten Repo-Pack "
                f"{expected_dir} erlaubt, nicht aus {pack_dir.resolve()}"
            )
        if Path(repo).expanduser().resolve() != AUTOLAND_EXPECTED_REPO:
            raise ManifestError(
                f"Pack {name!r}: autoland braucht das gebundene Live-Repo "
                f"{AUTOLAND_EXPECTED_REPO}, ist {Path(repo).expanduser().resolve()}"
            )
        actual_contract = {
            phase: (cfg.engine, cfg.model, cfg.prompt)
            for phase, cfg in phases.items()
        }
        if actual_contract != AUTOLAND_PHASE_CONTRACT:
            raise ManifestError(
                f"Pack {name!r}: autoland-Phasenvertrag weicht ab; "
                "erwartet Opus→Sol→Opus mit den kuratierten Prompts"
            )
        manifest_hash = hashlib.sha256(manifest.read_bytes()).hexdigest()
        if manifest_hash != AUTOLAND_MANIFEST_SHA256.get(name):
            raise ManifestError(
                f"Pack {name!r}: autoland-Manifestinhalt weicht vom kuratierten Hash ab"
            )
        expected_prompts = AUTOLAND_PROMPT_SHA256.get(name, {})
        actual_prompts = {
            cfg.prompt: hashlib.sha256((pack_dir / cfg.prompt).read_bytes()).hexdigest()
            for cfg in phases.values()
        }
        if actual_prompts != expected_prompts:
            raise ManifestError(
                f"Pack {name!r}: autoland-Promptinhalt weicht vom kuratierten Hash ab"
            )

    base_branch = raw.get("base_branch", "main")
    if not isinstance(base_branch, str) or not base_branch.strip():
        raise ManifestError(f"Pack {name!r}: base_branch muss ein nicht-leerer String sein")
    land_remote = raw.get("land_remote", "piet-fork")
    if not isinstance(land_remote, str) or not land_remote.strip():
        raise ManifestError(f"Pack {name!r}: land_remote muss ein nicht-leerer String sein")
    land_push = raw.get("land_push", True)
    if not isinstance(land_push, bool):
        raise ManifestError(f"Pack {name!r}: land_push muss boolean sein")
    land_gates_raw = raw.get("land_gates")
    if land_gates_raw is not None:
        if not isinstance(land_gates_raw, list) or not all(
            isinstance(c, str) and c.strip() for c in land_gates_raw
        ):
            raise ManifestError(
                f"Pack {name!r}: land_gates muss eine Liste nicht-leerer Strings sein"
            )
    land_gates = list(land_gates_raw) if land_gates_raw is not None else None

    params = {str(k): str(v) for k, v in (raw.get("params") or {}).items()}
    notify = {str(k): str(v) for k, v in (raw.get("notify") or {}).items()}

    return Pack(
        name=name, type=ptype, repo=Path(repo).expanduser(), pack_dir=pack_dir,
        phases=phases, stop=stop, description=str(raw.get("description", "")),
        stability=str(raw.get("stability", "experimental")), notify=notify,
        params=params, autoland=autoland,
        base_branch=base_branch, land_remote=land_remote,
        land_gates=land_gates, land_push=land_push,
    )


# ── reine Helfer (test-direkt) ───────────────────────────────────────────────

def parse_retry(plan_text: str) -> int:
    m = RETRY_RE.search(plan_text)
    return int(m.group(1)) if m else 0


def parse_plan_frontmatter(plan_text: str) -> dict:
    """Liest das Plan-Frontmatter fail-closed als Mapping."""
    if not plan_text.startswith("---\n"):
        return {}
    end = plan_text.find("\n---\n", 4)
    if end < 0:
        return {}
    try:
        frontmatter = yaml.safe_load(plan_text[4:end])
    except yaml.YAMLError:
        return {}
    return frontmatter if isinstance(frontmatter, dict) else {}


def parse_plan_id(plan_text: str) -> str:
    """Liest eine sichere, einzeilige Plan-ID aus YAML-Frontmatter.

    Auto-Landung bindet den Verifier-Status exakt an diese ID. Fehlendes oder
    komplexes Frontmatter ist kein implizites PASS, sondern liefert leer.
    """
    frontmatter = parse_plan_frontmatter(plan_text)
    plan_id = frontmatter.get("id")
    if not isinstance(plan_id, str) or not PLAN_ID_RE.fullmatch(plan_id):
        return ""
    return plan_id


def pass_status_matches_plan(status: str, plan_text: str) -> bool:
    plan_id = parse_plan_id(plan_text)
    match = PASS_STATUS_RE.fullmatch(status)
    return bool(plan_id and match and match.group(1) == plan_id)


def bump_retry(plan_path: Path) -> int:
    """Erhöht `retry: N` um 1; gibt neuen Wert zurück.

    Fehlt die retry-Zeile (Planner ist ein LLM, das Schema ist kein Garant),
    wird sie EINGEFÜGT — sonst wäre der Bump ein stiller No-Op und der Plan
    würde nie bouncen (Review-Blocker 2026-07-02).
    """
    text = plan_path.read_text(encoding="utf-8")
    new = parse_retry(text) + 1
    if RETRY_RE.search(text):
        text = RETRY_RE.sub(f"retry: {new}", text, count=1)
    elif text.startswith("---\n"):
        text = text.replace("---\n", f"---\nretry: {new}\n", 1)
    else:
        text = f"retry: {new}\n{text}"
    plan_path.write_text(text, encoding="utf-8")
    return new


def append_section(plan_path: Path, title: str, body: str) -> None:
    stamp = datetime.now().strftime("%F %H:%M")
    with plan_path.open("a", encoding="utf-8") as fh:
        fh.write(f"\n## {title} ({stamp})\n{body}\n")


def parse_worktree_paths(porcelain: str) -> list[str]:
    return [line[len("worktree "):] for line in porcelain.splitlines() if line.startswith("worktree ")]


def parse_overrides(path: Path) -> dict[str, str]:
    """overrides.env: KEY=VALUE pro Zeile; #-Kommentare und Leerzeilen erlaubt."""
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        out[key.strip()] = val.strip()
    return out


def read_ledger_stats(pack_state_dir: Path) -> dict:
    """Aggregate the structured ``ledger.jsonl`` for one pack (pure, read-only).

    Tolerant to a missing file and to malformed/partial lines — a broken line
    is skipped, never raises. Importable without instantiating ``LoopRunner``
    so the strategist/dashboard can read stats without touching pack state.
    """
    stats = {
        "rounds": 0,
        "verified": 0,
        "fails_by_kind": {},
        "blocked_by_kind": {},
        "bounced": 0,
        "avg_build_secs": None,
        "avg_verify_secs": None,
        "last_ts": None,
    }
    path = Path(pack_state_dir) / "ledger.jsonl"
    if not path.is_file():
        return stats
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return stats
    outcome_rounds = 0
    build_secs: list[float] = []
    verify_secs: list[float] = []
    last_ts: str | None = None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        try:
            verdict = event.get("verdict")
            if verdict == "ok":
                stats["verified"] += 1
                outcome_rounds += 1
            elif verdict == "fail":
                kind = event.get("fail_kind") or "unknown"
                if not isinstance(kind, str):
                    kind = "unknown"
                stats["fails_by_kind"][kind] = stats["fails_by_kind"].get(kind, 0) + 1
                outcome_rounds += 1
            elif verdict == "bounced":
                # A bounce always follows a fail event for the same round —
                # counting it again would double-count that round.
                stats["bounced"] += 1
            elif verdict == "blocked":
                kind = event.get("fail_kind") or "unknown"
                if not isinstance(kind, str):
                    kind = "unknown"
                stats["blocked_by_kind"][kind] = stats["blocked_by_kind"].get(kind, 0) + 1
                outcome_rounds += 1
            bsecs = event.get("build_secs")
            if isinstance(bsecs, (int, float)) and not isinstance(bsecs, bool):
                build_secs.append(bsecs)
            vsecs = event.get("verify_secs")
            if isinstance(vsecs, (int, float)) and not isinstance(vsecs, bool):
                verify_secs.append(vsecs)
            ts = event.get("ts")
            if isinstance(ts, str) and (last_ts is None or ts > last_ts):
                last_ts = ts
        except (TypeError, ValueError, AttributeError):
            # A single line with wrong-typed fields (e.g. list-valued fail_kind)
            # must not discard the entire pack's stats.
            continue
    stats["rounds"] = outcome_rounds
    try:
        stats["avg_build_secs"] = sum(build_secs) / len(build_secs) if build_secs else None
        stats["avg_verify_secs"] = sum(verify_secs) / len(verify_secs) if verify_secs else None
    except OverflowError:
        # Absurd integer durations from a corrupted line must not raise.
        stats["avg_build_secs"] = None
        stats["avg_verify_secs"] = None
    stats["last_ts"] = last_ts
    return stats


def read_all_ledger_stats(loops_state_root: Path) -> dict:
    """Map pack-name → ``read_ledger_stats`` for every pack under a state root."""
    root = Path(loops_state_root)
    out: dict = {}
    if not root.is_dir():
        return out
    for entry in sorted(root.iterdir()):
        if entry.is_dir() and (entry / "ledger.jsonl").is_file():
            out[entry.name] = read_ledger_stats(entry)
    return out


# ── Runner ───────────────────────────────────────────────────────────────────

class LoopRunner:
    def __init__(self, pack: Pack, state_root: Path | None = None):
        self.pack = pack
        self.state = (state_root or DEFAULT_STATE_ROOT) / pack.name
        self.wt = self.state / "wt"
        self.queue = self.state / "queue"
        self.ledger_path = self.state / "LEDGER.md"
        self.status_path = self.state / "last-status"
        self.stop_path = self.state / "STOP"
        self.visual_attestation_path = self.state / "visual-attestation.json"
        self.overrides = parse_overrides(self.state / "overrides.env")
        self.phase_secs: dict[str, int] = {}
        self._overrides_consumed = False
        self._repo_validated = False

    def _validate_repo(self) -> None:
        """Fail fast when the configured pack repo is missing or not a Git repo.

        Catches the most common configuration drift (moved/deleted repo path,
        typo in pack.yaml) before any destructive git operation runs. Called
        from the command path, NOT __init__, so read-only ``status`` still works
        against a moved/missing repo instead of crashing on construction.
        """
        if self._repo_validated:
            return
        if not self.pack.repo.is_dir():
            raise RuntimeError(f"Pack-Repo existiert nicht: {self.pack.repo}")
        res = subprocess.run(
            ["git", "-C", str(self.pack.repo), "rev-parse", "--git-dir"],
            capture_output=True, encoding="utf-8", errors="replace", check=False,
        )
        if res.returncode != 0:
            detail = res.stderr.strip() or "keine Fehlerdetails"
            raise RuntimeError(
                f"Pack-Repo {self.pack.repo} ist kein Git-Repository ({detail})"
            )
        self._repo_validated = True

    # ── Infrastruktur ──
    def say(self, msg: str) -> None:
        print(f"[{self.pack.name}] {msg}", flush=True)

    def ledger(self, msg: str) -> None:
        stamp = datetime.now().strftime("%F %H:%M")
        with self.ledger_path.open("a", encoding="utf-8") as fh:
            fh.write(f"- {stamp} {msg}\n")

    def ledger_event(self, **fields) -> None:
        """Append one structured JSON line to ``ledger.jsonl`` next to LEDGER.md.

        Best-effort: never raises, never alters LEDGER.md or any stop/continue
        decision. Consumed by the strategist/dashboard via ``read_ledger_stats``.
        """
        try:
            payload = {"ts": datetime.now().isoformat(timespec="seconds"), "pack": self.pack.name}
            payload.update({k: v for k, v in fields.items() if v is not None})
            path = self.ledger_path.parent / "ledger.jsonl"
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, sort_keys=True) + "\n")
        except Exception as exc:  # noqa: BLE001 — structured ledger never breaks a loop run
            logger.warning("ledger_event fehlgeschlagen: %s", exc)

    def notify(self, msg: str) -> None:
        channel = self.overrides.get("DISCORD_CHANNEL") or self.pack.notify.get("discord_channel", "")
        if not channel or not NOTIFY_SCRIPT.is_file():
            return
        try:
            subprocess.run(
                ["python3", str(NOTIFY_SCRIPT), "--channel", channel, "--stdin"],
                input=msg, encoding="utf-8", timeout=20, check=False,
                capture_output=True,
            )
        except Exception as exc:  # noqa: BLE001 — Notify ist nie lauf-kritisch
            logger.warning("Discord-Notify fehlgeschlagen: %s", exc)

    def consume_overrides(self) -> None:
        """overrides.env gilt nur für EINEN Lauf (Dashboard-Start-Override) —
        nach dem Start umbenennen, sonst wirkt z. B. SKIP_PLAN=1 für immer
        weiter. `self.overrides` bleibt für den laufenden Prozess in Kraft."""
        if self._overrides_consumed:
            return
        self._overrides_consumed = True
        path = self.state / "overrides.env"
        if not path.is_file():
            return
        consumed = self.state / "overrides.consumed.env"
        path.replace(consumed)
        self.say("overrides.env verbraucht (one-run) → overrides.consumed.env")

    def ensure_dirs(self) -> None:
        for stage in QUEUE_STAGES:
            (self.queue / stage).mkdir(parents=True, exist_ok=True)
        (self.state / "logs").mkdir(parents=True, exist_ok=True)

    @contextmanager
    def locked(self):
        """Pack-Lock + globaler Repo-Lock: nie zwei Loops aufs selbe Repo."""
        self.state.mkdir(parents=True, exist_ok=True)
        repo_key = hashlib.md5(str(self.pack.repo.resolve()).encode("utf-8")).hexdigest()[:8]
        repo_lock_path = self.state.parent / f".repo-{repo_key}.lock"
        with self.state.joinpath(".lock").open("w", encoding="utf-8") as pack_fh, \
                repo_lock_path.open("w", encoding="utf-8") as repo_fh:
            for fh, what in ((pack_fh, "Pack"), (repo_fh, "Repo")):
                # Einmal-Retry: die Dashboard-Running-Probe hält das Lock für µs —
                # ein Start exakt in dem Fenster soll nicht die Nacht kosten.
                for attempt in (1, 2):
                    try:
                        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if attempt == 2:
                            raise RuntimeError(f"{what}-Lock belegt — läuft schon ein Loop?") from None
                        time.sleep(1.0)
            yield

    # ── Git ──
    def git(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(cwd or self.wt), *args],
            capture_output=True, encoding="utf-8", errors="replace", check=False,
        )

    def rev_parse(self, ref: str = "HEAD") -> str:
        return self.git("rev-parse", ref).stdout.strip()

    def ensure_wt(self, fresh: bool = False) -> None:
        if fresh:
            self.say("FRESH → Worktree neu von main")
            self.git("worktree", "remove", "--force", str(self.wt), cwd=self.pack.repo)
            self.git("branch", "-D", self.pack.branch, cwd=self.pack.repo)
        listing = self.git("worktree", "list", "--porcelain", cwd=self.pack.repo)
        registered = str(self.wt.resolve()) in parse_worktree_paths(listing.stdout)
        if registered and self.wt.is_dir():
            return
        if registered:
            # Registriert, aber Verzeichnis weg (State manuell gelöscht) → Eintrag
            # aufräumen, sonst schlägt das erneute add dauerhaft fehl.
            self.git("worktree", "prune", cwd=self.pack.repo)
        self.wt.parent.mkdir(parents=True, exist_ok=True)
        res = self.git("worktree", "add", "-B", self.pack.branch, str(self.wt), "main", cwd=self.pack.repo)
        if res.returncode != 0:
            raise RuntimeError(f"worktree add fehlgeschlagen: {res.stderr.strip()}")

    def guard_clean(self) -> bool:
        """Loop-exklusiven Baum deterministisch säubern (Driver-Ebene, hook-frei)."""
        if not self.wt.is_dir():
            self.say(f"ABBRUCH: Worktree fehlt: {self.wt}")
            return False
        if not self.git("status", "--porcelain").stdout.strip():
            return True
        self.say("Worktree dirty — räume Phase-Reste auf")
        # Erst unstagen: Loop-Agenten stagen mit `git add -A` (Gate-Protokoll);
        # `checkout -- .` stellt aus dem INDEX her, gestagte Reste blieben sonst
        # unaufräumbar → ABBRUCH (live 2026-07-05).
        self.git("reset", "--quiet", "HEAD", "--", ".")
        self.git("checkout", "--", ".")
        self.git("clean", "-fd")
        left = self.git("status", "--porcelain").stdout.strip()
        if left:
            self.say(f"ABBRUCH: Worktree lässt sich nicht säubern:\n{left}")
            return False
        return True

    def revert_range(self, prehead: str) -> bool:
        res = self.git("revert", "--no-edit", f"{prehead}..HEAD")
        if res.returncode != 0:
            self.say(f"Revert fehlgeschlagen: {res.stderr.strip()}")
            self.ledger(f"⚠️ REVERT FEHLGESCHLAGEN ({prehead[:9]}..HEAD): {res.stderr.strip()[:200]}")
            return False
        return True

    # ── Phasen ──
    def _int_override(self, key: str, fallback: int) -> int:
        raw = self.overrides.get(key)
        if raw is None:
            return fallback
        try:
            return int(raw)
        except (TypeError, ValueError):
            # overrides.env kommt u.a. vom Dashboard — kaputter Wert darf keine
            # Runde crashen, nur zurückfallen und sichtbar sein.
            self.say(f"WARN: Override {key}={raw!r} ist keine Zahl — nutze {fallback}")
            return fallback

    def phase_cfg(self, name: str) -> PhaseCfg:
        cfg = self.pack.phases[name]
        up = name.upper()
        return PhaseCfg(
            engine=self.overrides.get(f"PHASE_{up}_ENGINE", cfg.engine),
            model=self.overrides.get(f"PHASE_{up}_MODEL", cfg.model),
            timeout=self._int_override(f"PHASE_{up}_TIMEOUT", cfg.timeout),
            prompt=cfg.prompt,
        )

    def stop_cfg(self, key: str) -> int:
        return self._int_override(key.upper(), self.pack.stop[key])

    def render_prompt(self, phase: str, **extra: str) -> str:
        text = (self.pack.pack_dir / self.pack.phases[phase].prompt).read_text(encoding="utf-8")
        params = dict(self.pack.params)
        for key in params:
            if key.upper() in self.overrides:
                params[key] = self.overrides[key.upper()]
        params_line = " ".join(f"{k.upper()}={v}" for k, v in sorted(params.items()))
        subst = {"STATE_DIR": str(self.state), "WT": str(self.wt), "PARAMS": params_line, **extra}
        for key, val in subst.items():
            text = text.replace("{{" + key + "}}", str(val))
        return text

    def _heartbeat(self, current: dict | None, done: dict | None = None) -> None:
        """heartbeat.json fürs Dashboard: aktive Phase + Dauer-Historie (best effort)."""
        hb_path = self.state / "heartbeat.json"
        try:
            data = json.loads(hb_path.read_text(encoding="utf-8")) if hb_path.is_file() else {}
        except (OSError, ValueError):
            data = {}
        history = [h for h in data.get("last", []) if isinstance(h, dict)]
        if done is not None:
            history = (history + [done])[-20:]
        try:
            hb_path.write_text(
                json.dumps({"current": current, "last": history}), encoding="utf-8"
            )
        except OSError:
            pass  # Telemetrie darf nie eine Runde kosten

    @contextmanager
    def _worker_environment(self, phase: str):
        """Markiert Modell-Subprozesse als Worker und nimmt Rechte nach Phase zurueck.

        Claudes globaler Guard blockiert in diesem Kontext Push/Deploy fail-closed.
        Fuer alle Engines (auch Codex) liegt zusaetzlich ein Push-Deny-Wrapper vor
        `git` im PATH; Git-Pushziele und Credentials werden im Prozess neutralisiert.
        Der Prompt bleibt eine weitere Schiene, nicht die einzige. Die Variablen
        werden auch bei Timeout/Exception exakt wiederhergestellt.
        """
        real_git = shutil.which("git")
        if not real_git:
            raise RuntimeError("git fehlt im PATH; Worker-Phase kann nicht sicher starten")
        worker_bin = self.state / "worker-bin"
        worker_bin.mkdir(parents=True, exist_ok=True)
        git_wrapper = worker_bin / "git"
        git_wrapper.write_text(
            "#!/bin/sh\n"
            "command_name=''\n"
            "skip_next=0\n"
            "inspect_alias=0\n"
            "for arg in \"$@\"; do\n"
            "  if [ \"$skip_next\" -eq 1 ]; then\n"
            "    if [ \"$inspect_alias\" -eq 1 ]; then\n"
            "      case \"$arg\" in alias.*) echo 'BLOCKED: loop worker darf keine git aliases setzen' >&2; exit 126 ;; esac\n"
            "    fi\n"
            "    skip_next=0; inspect_alias=0; continue\n"
            "  fi\n"
            "  case \"$arg\" in\n"
            "    -c|--config-env) skip_next=1; inspect_alias=1 ;;\n"
            "    -C|--git-dir|--work-tree|--namespace|--super-prefix)\n"
            "      skip_next=1 ;;\n"
            "    -calias.*|--config-env=alias.*)\n"
            "      echo 'BLOCKED: loop worker darf keine git aliases setzen' >&2; exit 126 ;;\n"
            "    --git-dir=*|--work-tree=*|--namespace=*|--super-prefix=*|--config-env=*|--*) ;;\n"
            "    -*) ;;\n"
            "    *) command_name=\"$arg\"; break ;;\n"
            "  esac\n"
            "done\n"
            "if [ \"$command_name\" = 'config' ]; then\n"
            "  for arg in \"$@\"; do\n"
            "    case \"$arg\" in alias.*) echo 'BLOCKED: loop worker darf keine git aliases setzen' >&2; exit 126 ;; esac\n"
            "  done\n"
            "fi\n"
            "case \"$command_name\" in\n"
            "  push|send-pack|receive-pack)\n"
            "    echo 'BLOCKED: loop worker darf git push/send-pack nicht ausfuehren' >&2\n"
            "    exit 126 ;;\n"
            "esac\n"
            f"exec {shlex.quote(real_git)} \"$@\"\n",
            encoding="utf-8",
        )
        git_wrapper.chmod(0o700)

        try:
            git_config_count = int(os.environ.get("GIT_CONFIG_COUNT", "0"))
        except ValueError:
            git_config_count = 0
        git_config = [
            ("push.default", "nothing"),
            ("remote.pushDefault", "__loop_worker_disabled__"),
            ("remote.origin.pushurl", "disabled://loop-worker"),
            ("remote.piet-fork.pushurl", "disabled://loop-worker"),
            ("credential.helper", ""),
            ("core.sshCommand", "/bin/false"),
        ]
        updates = {
            "HERMES_KANBAN_TASK": f"loop-{self.pack.name}-{phase}",
            "HERMES_LOOP_WORKER": "1",
            "PATH": f"{worker_bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "GIT_CONFIG_COUNT": str(git_config_count + len(git_config)),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "SSH_AUTH_SOCK": "",
        }
        for offset, (key, value) in enumerate(git_config):
            index = git_config_count + offset
            updates[f"GIT_CONFIG_KEY_{index}"] = key
            updates[f"GIT_CONFIG_VALUE_{index}"] = value
        previous = {key: os.environ.get(key) for key in updates}
        os.environ.update(updates)
        try:
            yield
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def run_phase(self, phase: str, **extra: str) -> engines.EngineResult:
        cfg = self.phase_cfg(phase)
        self.say(f"── Phase {phase} (engine={cfg.engine}, model={cfg.model}, timeout={cfg.timeout}s)")
        self.status_path.write_text("", encoding="utf-8")
        prompt = self.render_prompt(phase, **extra)
        started = time.time()
        started_iso = datetime.now().strftime("%FT%T")
        self._heartbeat({"phase": phase, "engine": cfg.engine, "model": cfg.model,
                         "started_at": started_iso, "timeout": cfg.timeout})
        with self._worker_environment(phase):
            result = engines.get_engine(cfg.engine)(cfg.model, prompt, self.wt, cfg.timeout)
        self.phase_secs[phase] = int(time.time() - started)
        self._heartbeat(None, done={"phase": phase, "engine": cfg.engine, "model": cfg.model,
                                    "secs": self.phase_secs[phase], "rc": result.rc,
                                    "at": started_iso})
        log_file = self.state / "logs" / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{phase}.log"
        log_file.write_text(result.output, encoding="utf-8")
        self.say(f"Phase {phase} fertig in {self.phase_secs[phase]}s (rc={result.rc})")
        return result

    def _secs(self, *phases: str) -> str:
        return " · ".join(f"{p} {self.phase_secs.get(p, 0)}s" for p in phases)

    def last_status(self) -> str:
        try:
            return self.status_path.read_text(encoding="utf-8").splitlines()[0].strip()
        except (FileNotFoundError, IndexError):
            return ""

    def stop_requested(self) -> bool:
        return self.stop_path.exists()

    def _validate_autoland_runtime(self, *, skip_plan: bool = False) -> None:
        """Autoland erlaubt nur den im Control-Startdialog sichtbaren Laufvertrag."""
        if not self.pack.autoland:
            return
        if skip_plan:
            raise RuntimeError(
                f"Pack {self.pack.name}: --skip-plan ist bei autoland nicht erlaubt"
            )

        # SKIP_BASE_REFRESH only skips the pre-night rebase — it does not
        # alter the land contract, so it stays allowed under autoland.
        allowed = {"MAX_ROUNDS", "MAX_HOURS", "SKIP_BASE_REFRESH"}
        for phase in self.pack.phases:
            prefix = f"PHASE_{phase.upper()}_"
            allowed.update({f"{prefix}ENGINE", f"{prefix}MODEL"})
        rejected = sorted(set(self.overrides) - allowed)
        if rejected:
            raise RuntimeError(
                f"Pack {self.pack.name}: nicht erlaubte Runtime-Overrides: {rejected}; "
                "Autoland akzeptiert nur Engine, Modell, MAX_ROUNDS und MAX_HOURS"
            )

        catalog_path = REPO_ROOT / "loops" / "models.yaml"
        catalog_data = yaml.safe_load(catalog_path.read_text(encoding="utf-8")) or {}
        catalog = catalog_data.get("engines", {})
        for phase in self.pack.phases:
            prefix = f"PHASE_{phase.upper()}_"
            if not ({f"{prefix}ENGINE", f"{prefix}MODEL"} & self.overrides.keys()):
                continue
            cfg = self.phase_cfg(phase)
            engine_entry = catalog.get(cfg.engine)
            if cfg.engine not in engines.ENGINES or not isinstance(engine_entry, dict):
                raise RuntimeError(
                    f"Pack {self.pack.name}: Phase {phase}: Engine {cfg.engine!r} unbekannt"
                )
            if cfg.model not in engine_entry.get("models", []):
                raise RuntimeError(
                    f"Pack {self.pack.name}: Phase {phase}: Modell {cfg.model!r} "
                    "ist nicht im UI-Katalog erlaubt"
                )
        limits = {"max_rounds": 50, "max_hours": 24}
        for key, maximum in limits.items():
            env_key = key.upper()
            raw = self.overrides.get(env_key)
            if raw is not None and not re.fullmatch(r"[1-9][0-9]*", raw):
                raise RuntimeError(
                    f"Pack {self.pack.name}: {env_key} muss eine ganze positive Zahl sein"
                )
            value = self.stop_cfg(key)
            if value <= 0 or value > maximum:
                raise RuntimeError(
                    f"Pack {self.pack.name}: {key} muss zwischen 1 und {maximum} liegen"
                )

    def _runtime_autoland_authorized(self) -> bool:
        """Nur der gebundene Phasenvertrag darf automatisch pushen; Budgets sind frei."""
        if not self.pack.autoland:
            return False
        phase_keys = {
            key for key in self.overrides
            if key.startswith("PHASE_") and key.endswith(("_ENGINE", "_MODEL"))
        }
        if not phase_keys:
            return True  # Das Pack-Manifest selbst wurde bereits fail-closed validiert.
        return all(
            (self.phase_cfg(phase).engine, self.phase_cfg(phase).model)
            == AUTOLAND_PHASE_CONTRACT[phase][:2]
            for phase in AUTOLAND_PHASE_CONTRACT
        )

    @property
    def manual_land_marker(self) -> Path:
        return self.state / "AUTOLAND_MANUAL"

    def _prepare_runtime_land_mode(self) -> None:
        if not self.pack.autoland:
            return
        if self._runtime_autoland_authorized():
            self.manual_land_marker.unlink(missing_ok=True)
            return
        self.manual_land_marker.parent.mkdir(parents=True, exist_ok=True)
        self.manual_land_marker.write_text(
            "UI-Phasenvertrag weicht vom gebundenen Auto-Land-Vertrag ab.\n",
            encoding="utf-8",
        )

    def _manual_land_required(self, context: str) -> bool:
        if not self.manual_land_marker.exists():
            return False
        note = f"AUTOLAND übersprungen ({context}): abweichender UI-Phasenvertrag"
        self.say(note)
        self.ledger(note)
        self.notify(f"ℹ️ {self.pack.name}: {note}; manuell prüfen und landen")
        return True

    # ── Queue-Disposition (pipeline) ──
    def qcount(self, stage: str) -> int:
        stage_dir = self.queue / stage
        return len(list(stage_dir.glob("*.md"))) if stage_dir.is_dir() else 0

    def pick_plan(self) -> Path | None:
        """Frische Pläne (retry 0) vor geretryten — sonst verbraucht ein einzelner
        schlechter Plan den `fail_streak`-Stop allein, während frische Pläne nie
        angefasst werden (Nachtlauf-Evidenz 2026-07-04)."""
        def retry_of(plan: Path) -> int:
            try:
                return parse_retry(plan.read_text(encoding="utf-8"))
            except OSError:
                return 0

        plans = sorted(
            (self.queue / "00-planned").glob("*.md"),
            key=lambda p: (retry_of(p), p.name),
        )
        return plans[0] if plans else None

    def handle_fail(self, plan: Path, reason: str, *, round_: int | None = None, fail_kind: str = "") -> str:
        """1 Retry (mit Feedback in der Plan-Datei), danach 90-bounced."""
        append_section(plan, "Loop-Fail", reason)
        if parse_retry(plan.read_text(encoding="utf-8")) >= 1:
            target = self.queue / "90-bounced" / plan.name
            if target.exists():  # Namens-Wiederverwendung: alte Evidenz nicht überschreiben
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                target = target.with_name(f"{target.stem}.{stamp}.md")
            plan.rename(target)
            self.ledger(f"bounced: {target.name} ({reason})")
            self.ledger_event(
                round=round_, phase="fail", verdict="bounced",
                plan=target.name, fail_kind=fail_kind or None, reason=reason,
            )
            return "bounced"
        bump_retry(plan)
        plan.rename(self.queue / "00-planned" / plan.name)
        return "retry"

    def _bounce_invalid_plans(self) -> None:
        """Pläne mit unparsierbarem Frontmatter/id koennen nie autolanden
        (pass_status_matches_plan bindet an genau diese ID) — vor Build+Verify
        verwerfen statt einen ganzen Zyklus zu verschwenden (2026-07-12: ein
        `title:` begann mit einem nackten Anführungszeichen und brach damit die
        YAML, `parse_plan_id` lieferte "", ein echter PASS wurde als
        PASS_ID_MISMATCH revertiert)."""
        for plan in sorted((self.queue / "00-planned").glob("*.md")):
            try:
                text = plan.read_text(encoding="utf-8")
            except OSError:
                continue
            if parse_plan_id(text):
                continue
            target = self.queue / "90-bounced" / plan.name
            if target.exists():  # Namens-Wiederverwendung: alte Evidenz nicht überschreiben
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                target = target.with_name(f"{target.stem}.{stamp}.md")
            plan.rename(target)
            self.ledger(f"plan-invalid: {target.name} (Frontmatter/id unparsierbar — vor Build verworfen)")

    # ── Kommandos ──
    def cmd_plan(self, fresh: bool = False) -> bool:
        self._validate_autoland_runtime()
        self.stop_path.unlink(missing_ok=True)
        self.ensure_dirs()
        self.ensure_wt(fresh=fresh)
        if not self.guard_clean():
            return False
        has_web = "1" if (self.wt / "web" / "node_modules").is_dir() else "0"
        result = self.run_phase("plan", HAS_WEB=has_web)
        if result.usage_limit:
            self.say("Usage-Limit im Planner — Stop.")
            self.notify(f"{self.pack.name}: Usage-Limit beim Planen — gestoppt.")
            return False
        self._bounce_invalid_plans()
        n = self.qcount("00-planned")
        status = "TIMEOUT" if result.timed_out else self.last_status()
        self.say(f"Planner fertig: status=[{status}], {n} Pläne in der Queue")
        self.ledger(f"PLAN: {n} Pläne (status={status})")
        self.notify(f"🌀 {self.pack.name} PLAN: {n} Pläne in der Queue (status={status})")
        return True

    def cmd_run(self, fresh: bool = False) -> None:
        self._validate_autoland_runtime()
        self.consume_overrides()
        self.stop_path.unlink(missing_ok=True)
        self.ensure_dirs()
        self.ensure_wt(fresh=fresh)
        if self.pack.type == "pipeline":
            self._run_pipeline()
        else:
            self._run_sweep()
        self.report()

    def _deadline(self) -> float:
        return time.time() + self.stop_cfg("max_hours") * 3600

    def _verifier_evidence_dirs(self) -> set[Path]:
        root = self.state / "evidence"
        if not root.is_dir():
            return set()
        return {path.resolve() for path in root.glob("*-verifier") if path.is_dir()}

    def _validate_visual_evidence_dir(
        self, plan_text: str, evidence_dir: Path, expected_commit: str
    ) -> tuple[bool, str, str]:
        """Prüft die sichtbare Drei-Viewport-Evidenz ohne Modell-Prosa.

        Der Verifier erzeugt die Artefakte, der Runner bindet Inhalt, Route,
        Viewports und Commit anschließend in einer eigenen Attestation.
        """
        evidence_root = (self.state / "evidence").resolve()
        resolved = evidence_dir.resolve()
        try:
            resolved.relative_to(evidence_root)
        except ValueError:
            return False, "Verifier-Evidenz liegt außerhalb des Loop-State", ""
        if not resolved.name.endswith("-verifier") or not resolved.is_dir():
            return False, "Verifier-Evidenzordner fehlt oder hat falschen Namen", ""

        frontmatter = parse_plan_frontmatter(plan_text)
        route = frontmatter.get("route")
        if not isinstance(route, str) or not (
            route == "/control" or route.startswith("/control/")
        ):
            return False, "Plan hat keine sichere /control-Route", ""

        summary_path = resolved / "summary.json"
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return False, f"summary.json unlesbar: {exc}", ""
        if not isinstance(summary, dict) or summary.get("ok") is not True:
            return False, "summary.ok ist nicht true", ""
        if summary.get("gitHead") != expected_commit:
            return False, "summary.gitHead passt nicht zum getesteten Commit", ""
        results = summary.get("results")
        if not isinstance(results, list) or len(results) != 3:
            return False, "summary braucht genau drei Viewport-Ergebnisse", ""

        widths: set[int] = set()
        result_pngs: set[Path] = set()
        result_aria: set[Path] = set()
        for result in results:
            if not isinstance(result, dict) or result.get("route") != route:
                return False, "Evidenzroute stimmt nicht mit dem Plan überein", ""
            viewport = result.get("viewport")
            if not isinstance(viewport, dict) or not isinstance(viewport.get("width"), int):
                return False, "Viewport-Metadaten fehlen", ""
            widths.add(viewport["width"])
            screenshot_path = result.get("screenshotPath")
            aria_path = result.get("ariaSnapshotPath")
            if not isinstance(screenshot_path, str) or not isinstance(aria_path, str):
                return False, "Summary-Artefaktpfade fehlen", ""
            screenshot = Path(screenshot_path)
            aria_snapshot = Path(aria_path)
            result_pngs.add(
                (screenshot if screenshot.is_absolute() else resolved / screenshot).resolve()
            )
            result_aria.add(
                (aria_snapshot if aria_snapshot.is_absolute() else resolved / aria_snapshot).resolve()
            )
            overflow = result.get("overflow")
            if (
                result.get("ok") is not True
                or result.get("consoleErrors") != []
                or result.get("pageErrors") != []
                or not isinstance(overflow, dict)
                or overflow.get("ok") is not True
                or result.get("ariaSnapshotError") is not None
            ):
                return False, f"Viewport {viewport['width']} ist nicht fehlerfrei", ""
        if widths != {390, 820, 1366}:
            return False, (
                "falsche Viewport-Breiten: erwartet [390, 820, 1366], "
                f"ist {sorted(widths)}"
            ), ""

        pngs = sorted(resolved.glob("*.png"))
        aria = sorted(resolved.glob("*.aria.yml"))
        artifacts = [summary_path, *pngs, *aria]
        if len(pngs) != 3 or len(aria) != 3:
            return False, f"erwartet 3 PNG/3 ARIA, ist {len(pngs)}/{len(aria)}", ""
        if result_pngs != {path.resolve() for path in pngs} or result_aria != {
            path.resolve() for path in aria
        }:
            return False, "Summary-Pfade passen nicht zu den Evidenzdateien", ""
        try:
            if any(not path.is_file() or path.stat().st_size == 0 for path in artifacts):
                return False, "Verifier-Evidenz enthält leere/fehlende Artefakte", ""
        except OSError as exc:
            return False, f"Verifier-Evidenz nicht lesbar: {exc}", ""

        digest = hashlib.sha256()
        try:
            for path in sorted(artifacts, key=lambda item: item.name):
                name = path.name.encode("utf-8")
                content_hash = hashlib.sha256(path.read_bytes()).digest()
                digest.update(len(name).to_bytes(4, "big"))
                digest.update(name)
                digest.update(content_hash)
        except OSError as exc:
            return False, f"Verifier-Evidenz nicht hashbar: {exc}", ""
        return True, f"visuelle Evidenz {route} @ 390/820/1366", digest.hexdigest()

    def _record_visual_attestation(
        self, plan_text: str, evidence_dir: Path
    ) -> tuple[bool, str]:
        expected_commit = self.rev_parse()
        ok, report, digest = self._validate_visual_evidence_dir(
            plan_text, evidence_dir, expected_commit
        )
        if not ok:
            return False, report
        plan_id = parse_plan_id(plan_text)
        if not plan_id:
            return False, "Plan-ID für Visual-Attestation fehlt"
        payload = {
            "plan_id": plan_id,
            "commit": expected_commit,
            "evidence_dir": str(evidence_dir.resolve()),
            "evidence_sha256": digest,
            "recorded_at": datetime.now().strftime("%FT%T"),
        }
        tmp = self.visual_attestation_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.visual_attestation_path)
        return True, report

    def _visual_attestation_ready(
        self, plan_text: str, expected_commit: str
    ) -> tuple[bool, str]:
        try:
            payload = json.loads(
                self.visual_attestation_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            return False, f"Visual-Attestation fehlt/unlesbar: {exc}"
        plan_id = parse_plan_id(plan_text)
        if not isinstance(payload, dict) or payload.get("plan_id") != plan_id:
            return False, "Visual-Attestation passt nicht zur Plan-ID"
        if payload.get("commit") != expected_commit:
            return False, "Visual-Attestation passt nicht zum Branch-Commit"
        raw_dir = payload.get("evidence_dir")
        if not isinstance(raw_dir, str):
            return False, "Visual-Attestation hat keinen Evidenzpfad"
        ok, report, digest = self._validate_visual_evidence_dir(
            plan_text, Path(raw_dir), expected_commit
        )
        if not ok:
            return False, report
        if payload.get("evidence_sha256") != digest:
            return False, "Verifier-Evidenz wurde nach Attestation verändert"
        return True, report

    def _run_pipeline(self) -> None:
        deadline = self._deadline()
        fails = verified = 0
        for rnd in range(1, self.stop_cfg("max_rounds") + 1):
            if self.stop_requested():
                self.say("STOP-Datei — sauberes Ende.")
                break
            if time.time() >= deadline:
                self.say("Wall-Clock-Deadline erreicht.")
                break
            if not self.guard_clean():
                break
            plan = self.pick_plan()
            if plan is None:
                self.say("Queue leer — fertig.")
                break
            building = self.queue / "10-building" / plan.name
            plan.rename(building)
            self.say(f"═══ Runde {rnd}: {building.name} ═══")
            prehead = self.rev_parse()
            if self.pack.autoland:
                self.visual_attestation_path.unlink(missing_ok=True)

            build = self.run_phase("build", PLAN_PATH=str(building))
            if build.usage_limit:
                # Invariante „Branch = nur verified-oder-reverted" auch hier halten:
                # existiert schon ein Commit, MUSS er als UNVERIFIED ausgewiesen werden
                # (Plan bleibt in 10-building); ohne Commit zurück in die Queue.
                if self.rev_parse() != prehead:
                    self.say("Usage-Limit im Build — Commit vorhanden, bleibt UNVERIFIZIERT (Plan in 10-building/).")
                    self.ledger(f"R{rnd} ⚠️ {building.name} Commit vorhanden aber UNVERIFIED (usage-limit im Build)")
                    self.notify(f"{self.pack.name}: Usage-Limit im Build — {building.name} unverifiziert, gestoppt.")
                    self.ledger_event(round=rnd, phase="build", verdict="blocked", plan=building.name,
                                       fail_kind="usage_limit", reason="commit vorhanden, unverified")
                else:
                    building.rename(self.queue / "00-planned" / building.name)
                    self.say("Usage-Limit — Stop.")
                    self.ledger(f"R{rnd} ⏸ {building.name} zurück in die Queue (usage-limit, kein Commit)")
                    self.notify(f"{self.pack.name}: Usage-Limit in Runde {rnd} — gestoppt ({verified} verified).")
                    self.ledger_event(round=rnd, phase="build", verdict="stopped", plan=building.name,
                                       fail_kind="usage_limit", reason="kein commit")
                break
            status = "TIMEOUT" if build.timed_out else self.last_status()
            build_ok = build.rc == 0 and status.startswith("BUILT")
            if self.rev_parse() == prehead or not build_ok:
                if build.rc != 0 and not build.timed_out:
                    status = f"ENGINE_RC_{build.rc} ({status or 'kein Status'})"
                self.say(f"BUILD_FAIL [{status}]")
                if not self.guard_clean():
                    break
                if self.rev_parse() != prehead and not self.revert_range(prehead):
                    break
                self.handle_fail(building, f"build: {status or 'kein Status'}", round_=rnd, fail_kind="build_fail")
                self.ledger(f"R{rnd} ❌ {building.name} build-fail: {status or '?'}")
                self.ledger_event(round=rnd, phase="build", verdict="fail", plan=building.name,
                                   fail_kind="build_fail", reason=status or "kein Status")
                fails += 1
                if fails >= self.stop_cfg("fail_streak"):
                    self.say("Fail-Streak — Stop für Human-Review.")
                    self.notify(f"{self.pack.name}: {fails}× Fail in Folge — gestoppt.")
                    self.ledger_event(round=rnd, phase="stop", verdict="stopped", reason="fail_streak")
                    break
                continue

            evidence_before = self._verifier_evidence_dirs()
            verify = self.run_phase("verify", PLAN_PATH=str(building), RANGE=f"{prehead}..HEAD")
            if verify.usage_limit:
                self.say("Usage-Limit im Verifier — Commit bleibt UNVERIFIZIERT (Plan in 10-building/).")
                self.ledger(f"R{rnd} ⚠️ {building.name} BUILT aber UNVERIFIED (usage-limit)")
                self.notify(f"{self.pack.name}: Usage-Limit im Verifier — {building.name} unverifiziert, gestoppt.")
                self.ledger_event(round=rnd, phase="verify", verdict="blocked", plan=building.name,
                                   fail_kind="usage_limit", reason="unverified", build_secs=self.phase_secs.get("build"))
                break
            status = "TIMEOUT" if verify.timed_out else self.last_status()
            if not self.guard_clean():
                break
            try:
                plan_text = building.read_text(encoding="utf-8")
            except OSError:
                plan_text = ""
            pass_matches = pass_status_matches_plan(status, plan_text)
            visual_ok = not self.pack.autoland
            visual_report = "für Review-only-Pack nicht erforderlich"
            if verify.rc == 0 and pass_matches and self.pack.autoland:
                evidence_after = self._verifier_evidence_dirs()
                fresh_evidence = sorted(evidence_after - evidence_before)
                if len(fresh_evidence) != 1:
                    visual_report = (
                        "genau ein frischer *-verifier-Evidenzordner erwartet, "
                        f"gefunden: {len(fresh_evidence)}"
                    )
                else:
                    visual_ok, visual_report = self._record_visual_attestation(
                        plan_text, fresh_evidence[0]
                    )
            if verify.rc == 0 and pass_matches and visual_ok:
                building.rename(self.queue / "20-verified" / building.name)
                verified += 1
                fails = 0
                sha = self.rev_parse()[:9]
                self.ledger(f"R{rnd} ✅ {building.name} verified ({sha}) [{self._secs('build', 'verify')}]")
                self.notify(f"✅ {self.pack.name} R{rnd}: {building.name} verified ({sha}) — {verified} gesamt")
                self.ledger_event(round=rnd, phase="verify", verdict="ok", plan=building.name,
                                   build_secs=self.phase_secs.get("build"), verify_secs=self.phase_secs.get("verify"))
            else:
                visual_fail = False
                if verify.rc != 0 and not verify.timed_out:
                    status = f"ENGINE_RC_{verify.rc} ({status or 'kein Status'})"
                elif status.startswith("PASS") and not pass_matches:
                    status = f"PASS_ID_MISMATCH ({status})"
                elif verify.rc == 0 and pass_matches and not visual_ok:
                    status = f"VISUAL_EVIDENCE_FAIL ({visual_report})"
                    visual_fail = True
                self.say(f"VERIFY_FAIL [{status}] — revert + retry/bounce")
                if not self.revert_range(prehead):
                    self.notify(f"{self.pack.name}: Revert fehlgeschlagen bei {building.name} — gestoppt.")
                    break
                verify_fail_kind = "verify_visual_fail" if visual_fail else "verify_fail"
                self.handle_fail(building, f"verify: {status}", round_=rnd, fail_kind=verify_fail_kind)
                self.ledger(f"R{rnd} ❌ {building.name} verify-fail: {status} (reverted)")
                self.notify(f"❌ {self.pack.name} R{rnd}: {building.name} verify-fail — {status}")
                self.ledger_event(round=rnd, phase="verify", verdict="fail", plan=building.name,
                                   fail_kind=verify_fail_kind, reason=status,
                                   build_secs=self.phase_secs.get("build"), verify_secs=self.phase_secs.get("verify"))
                fails += 1
                if fails >= self.stop_cfg("fail_streak"):
                    self.say("Fail-Streak — Stop für Human-Review.")
                    self.notify(f"{self.pack.name}: {fails}× Fail in Folge — gestoppt.")
                    self.ledger_event(round=rnd, phase="stop", verdict="stopped", reason="fail_streak")
                    break

    def _run_sweep(self) -> None:
        deadline = self._deadline()
        dry = blocked = 0
        for rnd in range(1, self.stop_cfg("max_rounds") + 1):
            if self.stop_requested():
                self.say("STOP-Datei — sauberes Ende.")
                break
            if time.time() >= deadline:
                self.say("Wall-Clock-Deadline erreicht.")
                break
            if not self.guard_clean():
                break
            self.say(f"═══ Runde {rnd} ═══")
            result = self.run_phase("round")
            if result.usage_limit:
                self.say("Usage-Limit — Stop.")
                self.notify(f"{self.pack.name}: Usage-Limit in Runde {rnd} — gestoppt.")
                self.ledger_event(round=rnd, phase="sweep", verdict="blocked", fail_kind="usage_limit")
                break
            status = "TIMEOUT" if result.timed_out else self.last_status()
            self.ledger(f"R{rnd} sweep status={status or '?'} [{self._secs('round')}]")
            if status.startswith("DRY"):
                dry, blocked = dry + 1, 0
                self.ledger_event(round=rnd, phase="sweep", verdict="ok", reason=status)
            elif status.startswith("BLOCKED") or status == "TIMEOUT":
                blocked, dry = blocked + 1, 0
                self.ledger_event(round=rnd, phase="sweep", verdict="blocked",
                                   fail_kind="blocked", reason=status)
            else:
                dry = blocked = 0
                self.ledger_event(round=rnd, phase="sweep", verdict="ok", reason=status)
            if dry >= self.stop_cfg("dry_rounds"):
                self.say("DRY-Konvergenz — Stop.")
                self.ledger_event(round=rnd, phase="stop", verdict="stopped", reason="dry_rounds")
                break
            if blocked >= self.stop_cfg("fail_streak"):
                self.say("Blocked-Streak — Stop für Human-Review.")
                self.notify(f"{self.pack.name}: {blocked}× BLOCKED in Folge — gestoppt.")
                self.ledger_event(round=rnd, phase="stop", verdict="stopped", reason="fail_streak")
                break

    def _autoland_queue_ready(self) -> tuple[bool, str]:
        """Fail-closed readiness contract for the allowlisted automatic landing.

        Exactly one independently verified plan must correspond to exactly one
        branch commit. Planned/unverified work blocks landing, so a stale queue or
        a builder that smuggles extra commits can never ride along.
        """
        planned = self.qcount("00-planned")
        building = self.qcount("10-building")
        verified = self.qcount("20-verified")
        ahead_raw = self.git(
            "rev-list", "--count", f"main..{self.pack.branch}", cwd=self.pack.repo
        ).stdout.strip()
        try:
            ahead = int(ahead_raw)
        except ValueError:
            return False, f"Branch-Commitzahl unlesbar: {ahead_raw!r}"
        if planned or building:
            return False, f"Queue nicht abgeschlossen: planned={planned}, building={building}"
        if verified != 1 or ahead != 1:
            return False, f"erwartet verified=1/ahead=1, ist verified={verified}/ahead={ahead}"
        scope = self.git(
            "diff", "--no-renames", "--name-only",
            f"main...{self.pack.branch}", cwd=self.pack.repo,
        )
        if scope.returncode != 0:
            return False, f"Commit-Scope nicht lesbar: {scope.stderr.strip()[:200]}"
        touched = [line.strip() for line in scope.stdout.splitlines() if line.strip()]
        outside = [
            path
            for path in touched
            if not any(path.startswith(prefix) for prefix in AUTOLAND_PATH_PREFIXES)
        ]
        if not touched or outside:
            return False, (
                "Commit-Scope außerhalb web/src/control/**: "
                + (", ".join(outside) if outside else "keine geänderten Dateien")
            )
        verified_plans = sorted((self.queue / "20-verified").glob("*.md"))
        try:
            plan_text = verified_plans[0].read_text(encoding="utf-8")
        except (IndexError, OSError):
            return False, "verifizierter Plan ist nicht lesbar"
        plan_id = parse_plan_id(plan_text)
        if not plan_id:
            return False, "verifizierter Plan hat keine sichere Frontmatter-ID"
        status = self.last_status()
        if not pass_status_matches_plan(status, plan_text):
            return False, (
                f"Verifier-Status passt nicht exakt zum Plan {plan_id}: {status or '?'}"
            )
        branch_head = self.git(
            "rev-parse", f"refs/heads/{self.pack.branch}", cwd=self.pack.repo
        ).stdout.strip()
        visual_ok, visual_report = self._visual_attestation_ready(
            plan_text, branch_head
        )
        if not visual_ok:
            return False, visual_report
        return True, (
            f"genau ein verifizierter Plan ({plan_id}), ein Commit und "
            f"{visual_report}"
        )

    def _autoland_pending(self) -> bool:
        if self.qcount("20-verified") > 0:
            return True
        # Netto-Diff statt reiner Commitzahl (2026-07-12): ein verify-fail wird
        # revertiert (Build-Commit + Revert-Commit), der Branch steht dann zwar
        # mit ahead>0 vor main, traegt aber netto NICHTS zu landen — reine
        # Commitzahl haette hier faelschlich "pending" gemeldet.
        net = self.git(
            "diff", "--name-only", f"main...{self.pack.branch}", cwd=self.pack.repo
        ).stdout.strip()
        return bool(net)

    def _try_autoland(self, context: str) -> bool:
        if self.stop_requested():
            self.say(f"AUTOLAND angehalten ({context}): STOP-Datei gesetzt")
            self.ledger(f"AUTOLAND angehalten ({context}): STOP-Datei gesetzt")
            return False
        ready, reason = self._autoland_queue_ready()
        if not ready:
            self.say(f"AUTOLAND BLOCKED ({context}): {reason}")
            self.ledger(f"AUTOLAND blocked ({context}): {reason}")
            self.notify(f"⛔ {self.pack.name} AUTOLAND blocked ({context}): {reason}")
            return False
        self.say(f"AUTOLAND bereit ({context}): {reason}")
        return self.cmd_land(push=True, require_push=True)

    def cmd_night(self, fresh: bool = False, skip_plan: bool = False) -> bool:
        # STOP ist auch beim Resume bindend: ein bereits verifizierter Commit darf
        # nicht an einer expliziten Operator-Sperre vorbei automatisch pushen.
        if self.pack.autoland and self.stop_requested():
            self.say("STOP-Datei — Auto-Land/Resume bleibt angehalten.")
            self.ledger("AUTOLAND angehalten: STOP-Datei gesetzt")
            return True
        # Ein frueherer Run kann nach PASS an einem voruebergehend dirty Live-
        # Checkout gescheitert sein. Erst diesen einen verifizierten Commit landen;
        # niemals weitere Arbeit darauf stapeln (Invariante bewusst beibehalten —
        # ein hier eingefuehrter queue-unfinished-Fall-through wuerde verifizierte
        # Arbeit stranden/ueberstapeln, Codex-Review 2026-07-12).
        # Deadlock-Fix 2026-07-12: der Wedge (revertierter Verify-Fail-Round →
        # ahead>0 ohne Landungsstoff PLUS Retry-Plan) wird bereits in
        # _autoland_pending() geloest (Netto-Diff statt Commitzahl → netto-leerer
        # Branch ist nicht mehr "pending", cmd_night faellt in den Round-Loop und
        # arbeitet den Retry ab). Hier braucht es KEINEN zusaetzlichen Guard.
        if self.pack.autoland and self._autoland_pending():
            if self._manual_land_required("resume"):
                return True
            return self._try_autoland("resume")
        self._validate_autoland_runtime(skip_plan=skip_plan)
        self._prepare_runtime_land_mode()
        self.consume_overrides()
        if not skip_plan and self.overrides.get("SKIP_PLAN", "").strip().lower() in ("1", "true", "yes"):
            skip_plan = True
            self.say("SKIP_PLAN-Override aktiv — Planung übersprungen.")
        # Basis-Refresh VOR der Nacht: ein Pack-Worktree, der auf einem alten
        # main-Stand steht, baut gegen dessen Defekte (2026-07-10: ratchet
        # 88>81 aus dem stale Base-Commit gebaut, obwohl der Fix längst auf
        # main lag → falsches "vorbestand", Fail-Streak-Stop). Gleiche
        # Schienen wie beim Landen (_auto_rebase: nur clean, Anker-Tag,
        # Konflikt → Abort, alter Stand bleibt); ein Fehlschlag blockiert die
        # Nacht nie — sie läuft dann bewusst auf der alten Basis weiter.
        # fresh=True resettet den Worktree ohnehin auf main.
        if (
            not fresh
            and self.wt.is_dir()
            and self.overrides.get("SKIP_BASE_REFRESH", "").strip().lower()
            not in ("1", "true", "yes")
        ):
            reb_ok, reb_msg = self._auto_rebase(self.pack.repo)
            first_line = reb_msg.splitlines()[0] if reb_msg else ""
            if reb_ok:
                self.ledger(f"BASE-REFRESH: {first_line}")
            else:
                self.ledger(f"BASE-REFRESH übersprungen: {first_line}")
        if self.pack.type == "pipeline" and not skip_plan:
            if not self.cmd_plan(fresh=fresh):
                return False
            fresh = False  # Worktree steht jetzt
            if self.qcount("00-planned") == 0:
                self.say("Keine Pläne — nichts zu bauen.")
                self.report()
                return True
        self.cmd_run(fresh=fresh)
        if self.pack.autoland and self._autoland_pending():
            if self._manual_land_required("night"):
                return True
            if self.stop_requested():
                self.say("STOP-Datei — verifizierte Arbeit bleibt ungelandet.")
                self.ledger("AUTOLAND angehalten (night): STOP-Datei gesetzt")
                return True
            return self._try_autoland("night")
        return True

    def report(self) -> None:
        commits = self.git("log", "--oneline", f"main..{self.pack.branch}").stdout.strip()
        counts = " · ".join(f"{self.qcount(s)} {s[3:]}" for s in QUEUE_STAGES) \
            if self.pack.type == "pipeline" else "(sweep)"
        if self.pack.autoland and self.manual_land_marker.exists():
            landing = "Landung: manuell — UI-Phasenvertrag weicht vom Auto-Land-Vertrag ab."
        elif self.pack.autoland:
            landing = "Landung: automatisch nach unabhaengigem PASS (allowlist + Schienen)."
        else:
            landing = "Landung: Morgen-Review (Design-Doc → Landung)."
        msg = (
            f"🌙 {self.pack.name} Bilanz: {counts}\n"
            f"Commits (main..{self.pack.branch}):\n{commits or '—'}\n"
            f"{landing}"
        )
        self.say(msg)
        self.notify(msg)

    # ── Landung (v2.3 Stufe 1 — operator-getriggert, mit Schienen) ──────────
    # Automatisiert die Morgen-Review-Mechanik; das URTEIL über die Commits bleibt
    # beim Menschen/Hauptagenten (Ledger + git log lesen kommt VOR dem land-Aufruf).

    def _land_gates(self, repo: Path, base: str) -> tuple[bool, str]:
        """Beweis nach dem ff-Merge: Collection-Sweep + affected Tests (+ Frontend,
        wenn web/ berührt). Seam für Tests."""
        py = repo / "venv" / "bin" / "python"
        steps: list[tuple[str, list[str], Path]] = [
            ("collection", [str(py), "-m", "pytest", "--co", "-q", "-p", "no:cacheprovider", "tests/"], repo),
            ("affected", ["bash", str(repo / "scripts" / "run-affected.sh"), base], repo),
        ]
        touched_web = bool(
            self.git("diff", "--name-only", f"{base}..HEAD", "--", "web/", cwd=repo).stdout.strip()
        )
        if touched_web:
            steps += [
                ("lint:control", ["npm", "run", "lint:control"], repo / "web"),
                ("tsc", ["npx", "tsc", "-b", "--noEmit"], repo / "web"),
                ("vitest", ["npx", "vitest", "run"], repo / "web"),
            ]
        for label, cmd, cwd in steps:
            try:
                res = subprocess.run(
                    cmd, cwd=str(cwd), capture_output=True,
                    encoding="utf-8", errors="replace", timeout=2400, check=False,
                )
            except (subprocess.TimeoutExpired, OSError) as exc:
                return False, f"{label}: {exc}"
            if res.returncode != 0:
                tail = "\n".join(((res.stdout or "") + (res.stderr or "")).splitlines()[-15:])
                return False, f"{label} rot (rc={res.returncode}):\n{tail}"
        return True, "collection + affected" + (" + frontend" if touched_web else "") + " grün"

    def _push(self, repo: Path) -> tuple[bool, str]:
        """Push NUR piet-fork, nur ff (kein --force). Seam für Tests."""
        res = self.git("push", "piet-fork", "main", cwd=repo)
        return res.returncode == 0, (res.stderr.strip() or res.stdout.strip())

    def _safe_land_rollback(
        self, repo: Path, base: str, expected_head: str
    ) -> tuple[bool, str]:
        """Rollt nur unseren unveraenderten ff-Merge zurueck.

        Ein fremder Commit auf main zwischen Merge/Gates/Push darf niemals durch
        den Loop-Fehlerpfad verworfen werden. In diesem Fall bleibt alles stehen
        und die Landung wird als manuell zu klaeren markiert.
        """
        current = self.git("rev-parse", "main", cwd=repo).stdout.strip()
        if current != expected_head:
            return False, (
                f"main ist parallel weitergelaufen ({current[:9]} statt "
                f"{expected_head[:9]}); kein automatischer Reset"
            )
        reset = self.git("reset", "--keep", base, cwd=repo)
        if reset.returncode != 0:
            return False, f"reset --keep fehlgeschlagen: {reset.stderr.strip()}"
        return True, f"rollback auf {base[:9]}"

    def _auto_rebase(self, repo: Path) -> tuple[bool, str]:
        """Loop-Branch im Pack-Worktree auf main rebasen — nur wenn sicher.

        Sicher heißt: Worktree existiert, steht auf dem Loop-Branch, ist clean,
        und der Rebase läuft konfliktfrei durch. Sonst (False, Grund) und der
        Branch bleibt unverändert (rebase --abort). Der alte Tip bleibt bei
        Erfolg als Tag loop-rebase/<pack>/<ts> erreichbar (Rollback-Anker,
        gleiche Konvention wie loop-land/…). NIEMALS ensure_wt(fresh=True)
        hier — das würde den Branch auf main resetten.
        """
        if not self.wt.is_dir():
            return False, f"Pack-Worktree fehlt ({self.wt}) — manuell rebasen"
        head = self.git("rev-parse", "--abbrev-ref", "HEAD", cwd=self.wt).stdout.strip()
        if head != self.pack.branch:
            return False, f"Worktree steht auf {head!r}, nicht {self.pack.branch!r}"
        if self.git("status", "--porcelain", cwd=self.wt).stdout.strip():
            return False, "Pack-Worktree ist dirty — manuell klären"
        anchor = f"loop-rebase/{self.pack.name}/{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        if self.git("tag", anchor, self.pack.branch, cwd=repo).returncode != 0:
            return False, "Rebase-Anker-Tag ließ sich nicht setzen"
        res = self.git("rebase", "main", cwd=self.wt)
        if res.returncode != 0:
            self.git("rebase", "--abort", cwd=self.wt)
            self.git("tag", "-d", anchor, cwd=repo)
            tail = "\n".join(((res.stdout or "") + (res.stderr or "")).splitlines()[-5:])
            return False, f"Auto-Rebase-Konflikt — manuell rebasen:\n{tail}"
        return True, f"auto-rebase auf main (Anker {anchor})"

    def cmd_land(self, push: bool = True, require_push: bool = False) -> bool:
        repo = self.pack.repo
        ahead = self.git("rev-list", "--count", f"main..{self.pack.branch}", cwd=repo).stdout.strip()
        if not ahead or ahead == "0":
            self.say("Nichts zu landen (Branch ist nicht vor main).")
            return True
        if self.qcount("10-building") > 0:
            self.say("ABBRUCH: 10-building/ ist nicht leer — UNVERIFIZIERTE Arbeit zuerst klären.")
            return False
        cur = self.git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo).stdout.strip()
        if cur != "main":
            self.say(f"ABBRUCH: Live-Checkout steht auf {cur!r}, nicht auf main.")
            return False
        dirty = self.git("status", "--porcelain", cwd=repo).stdout.strip()
        if dirty:
            self.say("ABBRUCH: Live-Checkout ist dirty (parallele Arbeit?) — Landung braucht einen sauberen Baum:\n"
                     + "\n".join(dirty.splitlines()[:10]))
            return False
        base = self.git("rev-parse", "main", cwd=repo).stdout.strip()
        tag = f"loop-land/{self.pack.name}/{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        if self.git("tag", tag, "main", cwd=repo).returncode != 0:
            self.say("ABBRUCH: Rollback-Anker-Tag konnte nicht gesetzt werden.")
            return False
        rebase_note = ""
        merge = self.git("merge", "--ff-only", self.pack.branch, cwd=repo)
        if merge.returncode != 0:
            reb_ok, reb_msg = self._auto_rebase(repo)
            if not reb_ok:
                self.git("tag", "-d", tag, cwd=repo)
                self.say(f"ABBRUCH: kein ff-Merge möglich, Auto-Rebase nicht sicher — {reb_msg}")
                self.ledger(f"LAND abgebrochen: {reb_msg.splitlines()[0]} (base {base[:9]})")
                return False
            self.say(f"main weitergelaufen → {reb_msg}")
            merge = self.git("merge", "--ff-only", self.pack.branch, cwd=repo)
            if merge.returncode != 0:
                self.git("tag", "-d", tag, cwd=repo)
                self.say(f"ABBRUCH: ff-Merge nach Auto-Rebase weiterhin unmöglich:\n{merge.stderr.strip()}")
                self.ledger(f"LAND abgebrochen: ff nach auto-rebase fehlgeschlagen (base {base[:9]})")
                return False
            rebase_note = f" · {reb_msg.splitlines()[0]}"
        merged_head = self.git("rev-parse", "main", cwd=repo).stdout.strip()
        ok, report = self._land_gates(repo, base)
        if not ok:
            # Baum war sauber, Merge war reiner ff → --keep rollt den Ref zurück,
            # ohne irgendetwas zu verwerfen (verweigert sonst; bewusst NICHT --hard).
            rolled_back, rollback_report = self._safe_land_rollback(
                repo, base, merged_head
            )
            if rolled_back:
                self.say(f"LAND zurückgerollt auf {base[:9]} — Gates rot:\n{report}")
                self.ledger(f"LAND rollback (Anker {tag}): {report.splitlines()[0]}")
                self.notify(
                    f"⛔ {self.pack.name} LAND: Gates rot → rollback auf "
                    f"{base[:9]} (Anker {tag})."
                )
                self.ledger_event(phase="land", verdict="blocked", fail_kind="land_gates_fail",
                                   reason=report.splitlines()[0])
            else:
                self.say(
                    f"LAND MANUELL KLÄREN — Gates rot; {rollback_report}:\n{report}"
                )
                self.ledger(
                    f"LAND MANUELL KLÄREN (Anker {tag}): {rollback_report}; "
                    f"{report.splitlines()[0]}"
                )
                self.notify(
                    f"⛔ {self.pack.name} LAND: Gates rot; {rollback_report}. "
                    f"Anker {tag}."
                )
                self.ledger_event(phase="land", verdict="blocked", fail_kind="land_gates_fail",
                                   reason=f"{rollback_report}; {report.splitlines()[0]}")
            return False
        pushed = ""
        if push:
            current = self.git("rev-parse", "main", cwd=repo).stdout.strip()
            if current != merged_head:
                reason = (
                    f"main ist vor dem Push parallel weitergelaufen ({current[:9]} "
                    f"statt {merged_head[:9]}); kein Push/Reset"
                )
                self.say(f"LAND MANUELL KLÄREN — {reason}")
                self.ledger(f"LAND MANUELL KLÄREN (Anker {tag}): {reason}")
                self.notify(f"⛔ {self.pack.name} LAND: {reason}. Anker {tag}.")
                return False
            p_ok, p_msg = self._push(repo)
            current = self.git("rev-parse", "main", cwd=repo).stdout.strip()
            if current != merged_head:
                reason = (
                    f"main ist während des Pushs parallel weitergelaufen "
                    f"({current[:9]} statt {merged_head[:9]}); kein Reset"
                )
                self.say(f"LAND MANUELL KLÄREN — {reason}")
                self.ledger(f"LAND MANUELL KLÄREN (Anker {tag}): {reason}")
                self.notify(f"⛔ {self.pack.name} LAND: {reason}. Anker {tag}.")
                return False
            if not p_ok and require_push:
                # Automatische Landung ist nur vollstaendig, wenn auch piet-fork
                # denselben Stand hat. Bei Push-Rot bleibt der verifizierte
                # Loop-Branch erhalten und main geht auf den Anker zurueck.
                rolled_back, rollback_report = self._safe_land_rollback(
                    repo, base, merged_head
                )
                if rolled_back:
                    self.say(
                        f"LAND zurückgerollt auf {base[:9]} — "
                        f"Pflicht-Push fehlgeschlagen: {p_msg}"
                    )
                    self.ledger(
                        f"LAND rollback (Anker {tag}): Pflicht-Push fehlgeschlagen"
                    )
                    self.notify(
                        f"⛔ {self.pack.name} LAND: piet-fork-Push fehlgeschlagen → "
                        f"rollback auf {base[:9]} (Anker {tag})."
                    )
                else:
                    self.say(
                        f"LAND MANUELL KLÄREN — Pflicht-Push fehlgeschlagen; "
                        f"{rollback_report}: {p_msg}"
                    )
                    self.ledger(
                        f"LAND MANUELL KLÄREN (Anker {tag}): Pflicht-Push "
                        f"fehlgeschlagen; {rollback_report}"
                    )
                    self.notify(
                        f"⛔ {self.pack.name} LAND: Pflicht-Push fehlgeschlagen; "
                        f"{rollback_report}. Anker {tag}."
                    )
                return False
            pushed = " · piet-fork gepusht" if p_ok else f" · PUSH FEHLGESCHLAGEN (Merge bleibt lokal): {p_msg}"
        # Verdaute Pläne archivieren + Pack frisch von neuem main ziehen
        landed_dir = self.queue / "30-landed"
        landed_dir.mkdir(parents=True, exist_ok=True)
        moved = 0
        for plan in sorted((self.queue / "20-verified").glob("*.md")):
            plan.rename(landed_dir / plan.name)
            moved += 1
        self.visual_attestation_path.unlink(missing_ok=True)
        self.ensure_wt(fresh=True)
        new_main = self.git("rev-parse", "--short", "main", cwd=repo).stdout.strip()
        self.ledger(
            f"LAND ✅ {ahead} Commits → main {new_main} "
            f"(Anker {tag}, {moved} Pläne archiviert){pushed}{rebase_note}"
        )
        self.say(f"LAND ✅ main={new_main} · Gates: {report}{pushed}{rebase_note}")
        self.notify(f"🛬 {self.pack.name} LAND: {ahead} Commits auf main ({new_main}); {report}{pushed}{rebase_note}")
        self.ledger_event(phase="land", verdict="landed", reason=f"main={new_main}")
        return True

    def cmd_status(self) -> None:
        print(f"{self.pack.name} [{self.pack.type}/{self.pack.stability}] @ {self.state}")
        if self.pack.type == "pipeline":
            print("  Queue: " + " · ".join(f"{self.qcount(s)} {s}" for s in QUEUE_STAGES))
        branch = self.git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() if self.wt.is_dir() else ""
        print(f"  Worktree: {self.wt} ({branch or 'fehlt'})")
        if branch:
            for line in self.git("log", "--oneline", f"main..{self.pack.branch}").stdout.splitlines():
                print(f"    {line}")
        if self.stop_path.exists():
            print("  ⚠️ STOP-Datei gesetzt")
        if self.ledger_path.is_file():
            tail = self.ledger_path.read_text(encoding="utf-8").splitlines()[-8:]
            print("  Ledger (letzte 8):")
            for line in tail:
                print(f"  {line}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Loop-Runner (pipeline|sweep Packs)")
    parser.add_argument("--pack", required=True)
    parser.add_argument("--cmd", required=True, choices=["plan", "run", "night", "status", "land"])
    parser.add_argument("--no-push", action="store_true", help="land: nur lokal mergen, nicht piet-fork pushen")
    parser.add_argument("--state-root", type=Path, default=None)
    parser.add_argument("--packs-dir", type=Path, default=None,
                        help="explizites Pack-Verzeichnis (default: Repo-Packs, dann ~/.hermes/loops/packs-custom)")
    parser.add_argument("--fresh", action="store_true", help="Worktree neu von main ziehen")
    parser.add_argument("--skip-plan", action="store_true", help="night: Planungsphase überspringen")
    args = parser.parse_args(argv)

    try:
        packs_dir = args.packs_dir or resolve_packs_dir(args.pack)
        pack = load_pack(packs_dir, args.pack)
    except ManifestError as exc:
        print(f"MANIFEST-FEHLER: {exc}", file=sys.stderr)
        return 2

    runner = LoopRunner(pack, state_root=args.state_root)
    if args.cmd == "status":
        runner.cmd_status()
        return 0
    try:
        runner._validate_repo()
        with runner.locked():
            runner.say(f"START cmd={args.cmd} {datetime.now().strftime('%F %H:%M:%S')}")
            rc = 0
            if args.cmd == "plan":
                runner.cmd_plan(fresh=args.fresh)
            elif args.cmd == "run":
                runner.cmd_run(fresh=args.fresh)
            elif args.cmd == "land":
                rc = 0 if runner.cmd_land(push=not args.no_push) else 4
            else:
                night_ok = runner.cmd_night(fresh=args.fresh, skip_plan=args.skip_plan)
                # Bestehende Review-only-Packs behalten ihre bisherigen Service-
                # Exitcodes. Nur die autorisierte Auto-Land-Pipeline meldet einen
                # unvollständigen Landungsversuch als harte Unit-Fehlfunktion.
                rc = 0 if night_ok or not pack.autoland else 4
            runner.say(f"ENDE cmd={args.cmd}")
            if rc:
                return rc
    except RuntimeError as exc:
        print(f"ABBRUCH: {exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
