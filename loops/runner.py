"""Loop-Runner — führt Loop-Packs aus (Archetypen: pipeline | sweep | deterministic).

CLI:
    python -m loops.runner --pack <name> --cmd plan|run|night|status
                           [--state-root PFAD] [--fresh] [--skip-plan]

Ein Pack (loops/packs/<name>/) beschreibt in pack.yaml WAS läuft (LLM-Phasen
mit Engine/Modell/Timeout/Prompt oder eine deterministische Kommando-Phase);
der Runner liefert das WIE:
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Mapping, NoReturn

import yaml

from hermes_cli.gate_leaker import build_runner, isolate_from_logs
from hermes_constants import get_hermes_home
from loops import engines

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKS_DIR = REPO_ROOT / "loops" / "packs"
# Kuratierter Engine/Modell-Katalog (Dashboard-Dropdown). Modul-Konstante, damit
# Tests einen Temp-Katalog einspielen können; der Autoland-Runtime-Check liest
# hierüber, dass JEDES Default- UND Override-Modell im Katalog liegt.
MODELS_FILE = REPO_ROOT / "loops" / "models.yaml"
# Werkstatt-Substrat (v2.1): vom Operator/Dashboard angelegte Packs leben im State,
# nie im Repo — Browser-Edits dürfen den Live-Checkout nicht dirty machen.
_HERMES_HOME = get_hermes_home()
CUSTOM_PACKS_DIR = _HERMES_HOME / "loops" / "packs-custom"
DEFAULT_STATE_ROOT = _HERMES_HOME / "loops"
NOTIFY_SCRIPT = _HERMES_HOME / "scripts" / "discord-notify.py"

QUEUE_STAGES = ("00-planned", "10-building", "20-verified", "30-landed", "90-bounced")

# Wartefenster auf den Repo-Lock bei der Landung. Grosszuegig bemessen, weil
# eine fremde Landung ihn ueber ihre gesamten Gates haelt (zwei Laeufe a 2400 s
# Timeout) und ein deterministischer Sammellauf sogar ueber seinen ganzen Lauf.
# Warten ist hier immer besser als Abbrechen: der verifizierte Commit ist fertig
# und wuerde sonst bis zur naechsten Nacht liegenbleiben.
LAND_LOCK_WAIT_S = 5400.0

# Kurzes Fenster fuer das Ziehen der Basis (BASE-REFRESH). Es braucht den Lock
# nur, um NICHT mitten in eine fremde Landung hineinzulesen — laeuft eine, ist
# Weiterarbeiten auf der alten Basis die richtige Antwort, nicht Warten.
BASE_SAMPLE_WAIT_S = 120.0
DEFAULT_STOP = {
    "max_rounds": 12,
    "max_hours": 7,
    "fail_streak": 2,
    "dry_rounds": 2,
    # N pipeline-nights ending DRY in a row before ledger+notify. 0 = off.
    "dry_streak_alert": 3,
}


def _utc_iso() -> str:
    """Unambiguous wire timestamp for dashboard/state consumers."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _disable_user_timer(unit: str) -> subprocess.CompletedProcess[str]:
    """Systemd control boundary kept mockable for retirement tests."""
    return subprocess.run(
        ["systemctl", "--user", "disable", "--now", unit],
        capture_output=True,
        text=True,
        check=False,
    )


# Operator-Entscheid 2026-07-18: dieselbe deterministische Drei-Phasen-
# Landungsleiter wird pro Pack vertraglich vorbereitet. hermes-hardening folgt
# nach 3, hermes-feature-forge nach 5 sauberen manuellen Landungen. Der Flip
# braucht jeweils Allowlist-Eintrag + autoland:true + echte Safety-/Prompt-SHAs.
# Bis dahin bleiben beide neuen Vertraege fail-closed. Quelle, Live-Repo,
# Rollen/Engines und exakte Sicherheits-/Prompt-Inhalte bleiben Teil der
# Autoritaetsbindung — das konkrete Modell floatet (Katalog-geprueft).
AUTOLAND_PACK_ALLOWLIST = frozenset({"dashboard-experience"})
AUTOLAND_EXPECTED_REPO = Path("/home/piet/.hermes/hermes-agent").resolve()
# Die Landungsautorität bindet Engine-ROLLE + Prompt pro Phase — NICHT das
# konkrete Modell. Modell und Budgets dürfen zur Laufzeit floaten (Operator wählt
# im Control-Startdialog), solange sie im Katalog liegen; Prompt, Repo,
# Pack-Quelle, Pfad-/Visual-/exact-one-commit-/clean-/ff-only-Gates bleiben
# fail-closed. Keine konkreten Modellnamen hier oder im Fehlertext (sonst kippt
# ein Modell-Update die Autorität zurück in die modellgebundene Regression).
#
# Operator-Entscheid 2026-07-25: die Engine-ROLLE ist NICHT mehr Teil der
# Landungsautorität. Sie floatet ab jetzt wie das Modell — ein Engine-Swap
# (etwa build auf eine Fremd-Engine) erzwingt kein manual-land mehr. Damit
# entfällt bewusst die Garantie Bauer≠Prüfer; was die Landung objektiv hält,
# sind Verify-PASS, Visual-Gate und die land_gates (Collection-Sweep +
# run-affected + Frontend-Gate), plus die unten weiterhin gebundenen Prompts,
# Pack-Quelle, Live-Repo und Landungsziel.
AUTOLAND_PHASE_CONTRACT = {
    "plan": "PLANNER-PROMPT.md",
    "build": "BUILDER-PROMPT.md",
    "verify": "VERIFIER-PROMPT.md",
}


def _autoland_safety_hash(raw: dict) -> str:
    """Hash der Landungsautorität — schließt Laufzeit-Modell, -Engine und -Budgets
    bewusst aus, bindet aber Prompt-Zuordnung, Repo, Params, Notify und die
    Landungsziel-/Gate-Parameter (base_branch, land_remote, land_push, land_gates).

    So bleiben Modell-Swap (gpt-5.6-sol → gpt-5.5) UND Engine-Swap (Operator-
    Entscheid 2026-07-25) OHNE Manifest-Drift, während jede Änderung an Prompt,
    Repo, Scope-Params ODER am Push-Ziel/den Land-Gates fail-closed auffällt.
    `stop`/Budgets nur über die Schlüssel gebunden (Werte floaten). Push-Ziel und
    Gates dürfen NICHT floaten: land_remote=origin oder land_gates=[] müssen den
    Hash drehen (sonst nicht fail-closed)."""
    phases = raw.get("phases") or {}
    projection = {
        "name": raw.get("name"),
        "type": raw.get("type"),
        "repo": raw.get("repo"),
        "stability": raw.get("stability"),
        "phases": {
            phase: {"prompt": cfg.get("prompt")}
            for phase, cfg in phases.items()
        },
        "stop_keys": sorted((raw.get("stop") or {}).keys()),
        "params": raw.get("params") or {},
        "notify": raw.get("notify") or {},
        "autoland": raw.get("autoland", False),
        # Landungsziel + Gate-Ausführung binden — dieselben Defaults wie load_pack,
        # damit ein Manifest ohne diese Keys stabil hasht, jede Abweichung (origin,
        # land_push=false, leere/andere Gate-Liste) aber Drift erzeugt.
        "base_branch": raw.get("base_branch", "main"),
        "land_remote": raw.get("land_remote", "piet-fork"),
        "land_push": raw.get("land_push", True),
        "land_gates": raw.get("land_gates"),
    }
    if "goal" in raw:
        # A retirement target grants authority to disable a timer, so an
        # autoland pack must bind it. Omitting the key preserves legacy hashes.
        projection["goal"] = raw.get("goal")
    if "plan_skip_when_queue_full" in raw:
        # Gleiche Bauart wie `goal`, gleicher Grund: der Schalter entscheidet,
        # ob im UNBEAUFSICHTIGTEN Nachtlauf ueberhaupt geplant wird. Ein Flip
        # auf false in einem Vertrags-Autoland-Pack aendert dessen Verhalten
        # ohne Zuschauer und muss deshalb den Hash drehen.
        # Nur bei GESETZTEM Key aufnehmen: ein zusaetzlicher Projektions-Key
        # dreht den Hash sonst auch mit Default (json.dumps sieht ihn), und
        # genau das haette die kuratierten SHAs gebrochen. Das war die reale
        # Sorge hinter dem urspruenglichen Auslassen (2b3f0d3fbc) — sie ist mit
        # diesem Muster geloest, statt die Bindung ganz aufzugeben.
        projection["plan_skip_when_queue_full"] = raw.get(
            "plan_skip_when_queue_full"
        )
    canonical = json.dumps(
        projection, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


# --- Operator-Autoland (2026-07-28) --------------------------------------
# Zwei Klassen von Autoland, bewusst getrennt:
#
#   1. VERTRAGS-Autoland  — `autoland: true` im Manifest, Allowlist + Safety-/
#      Prompt-SHA. Das ist die Autorität für UNBEAUFSICHTIGTE Läufe (Timer,
#      Cron, Nachtlauf): niemand schaut zu, also muss der Vertrag halten.
#      Unverändert.
#   2. OPERATOR-Autoland  — pro Lauf im Startdialog eingeschaltet, für JEDES
#      pipeline-Pack, ohne SHA-Vertrag. Der Operator startet den Lauf selbst
#      und trägt ihn; der Zweitschlüssel (AUTOLAND_ACK = exakter Pack-Name)
#      belegt, dass das Einschalten Absicht war und kein Durchklicken.
#
# Was in BEIDEN Fällen unverändert hart bleibt: Verify-PASS, Visual-Gate,
# land_gates (Collection-Sweep + run-affected + Frontend-Gate), exact-one-commit,
# sauberer Worktree, ff-only und das Push-Ziel (nie origin).
AUTOLAND_RUNTIME_KEY = "AUTOLAND"
AUTOLAND_RUNTIME_ACK_KEY = "AUTOLAND_ACK"

# Pfad-Vertrag für Packs OHNE kuratierten Eintrag. Bewusst weit im Erlauben
# (ein beliebiges Pack weiß der Runner nicht vorher, was es anfasst), aber
# gleich streng im Verbieten wie die kuratierten Verträge: Auth, Board-Spine
# und das Root-Lockfile landet nie unbeaufsichtigt.
OPERATOR_AUTOLAND_DENY_PREFIXES = (
    "hermes_cli/auth.py",
    "hermes_cli/dashboard_auth/",
    "hermes_cli/kanban_db.py",
    "web/package.json",
    "package-lock.json",
    "loops/runner.py",
    ".github/",
    "scripts/deploy_dashboard.sh",
)


@dataclass(frozen=True)
class AutolandContract:
    path_prefixes: tuple[str, ...]
    deny_prefixes: tuple[str, ...]
    # SHA-256 der modellagnostischen Sicherheitsprojektion (_autoland_safety_hash),
    # NICHT des rohen Manifests — Modell/Budgets sind bewusst nicht Teil davon.
    safety_sha256: str | None
    prompt_sha256: dict[str, str] | None
    require_visual: Literal["always", "if_web_touched"]
    # NUR für Operator-Autoland: der Pfad-Scope eines beliebigen Packs ist nicht
    # vorab kuratierbar, also tritt an die Stelle der Allow-Liste die Deny-Liste.
    # Muss explizit sein: ein leeres ``path_prefixes`` heißt weiterhin "NICHTS
    # erlaubt" (fail-closed) und darf nie als "alles erlaubt" durchgehen.
    allow_any_path: bool = False


_HERMES_REPO_PREFIXES = ("web/src/control/", "hermes_cli/", "tests/")
_HERMES_REPO_DENIES = (
    "hermes_cli/auth.py",
    "hermes_cli/dashboard_auth/",
    "hermes_cli/kanban_db.py",
    "web/package.json",
    "package-lock.json",
)

# Werden zusammen mit den kuratierten Dateien aktualisiert. Der Loader prueft
# beide Ebenen: menschenlesbaren Rollenvertrag und bytegenaue Inhaltsbindung.
AUTOLAND_CONTRACTS: dict[str, AutolandContract] = {
    "dashboard-experience": AutolandContract(
        path_prefixes=("web/src/control/",),
        deny_prefixes=(),
        safety_sha256="cedbcf5ea93cdb72f7f70f82b258a212fcca0380a76239d3c8f7c7e787e9207f",
        # Re-signiert 2026-07-28 (Operator-Freigabe): VERIFIER benennt
        # {{STATE_DIR}}/last-status jetzt als Datei statt nur als Namen (Vorfall
        # hermes-hardening R1 vom 28.07.), PLANNER traegt statt "(Opus 4.8)" die
        # Platzhalter seiner Schwesterphasen. BUILDER unveraendert.
        prompt_sha256={
        "PLANNER-PROMPT.md": "00ec377ef1d1a753d4ea01f593b1f6ee65b171e383136416f78f5312061027da",
        "BUILDER-PROMPT.md": "4a6eb2e5e558901a0d3e06a0f5785b30295e56c84c51d3f0321a7eed06ec8d93",
        "VERIFIER-PROMPT.md": "b1b7c2702820e8974491b3173e5594949534239818c17dff201f233ec1cbb972",
        },
        require_visual="always",
    ),
    "hermes-feature-forge": AutolandContract(
        path_prefixes=_HERMES_REPO_PREFIXES,
        deny_prefixes=_HERMES_REPO_DENIES,
        safety_sha256=None,
        prompt_sha256=None,
        require_visual="if_web_touched",
    ),
    "hermes-hardening": AutolandContract(
        path_prefixes=_HERMES_REPO_PREFIXES,
        deny_prefixes=_HERMES_REPO_DENIES,
        safety_sha256=None,
        prompt_sha256=None,
        require_visual="if_web_touched",
    ),
}

PHASES_BY_TYPE = {
    "pipeline": ("plan", "build", "verify"),
    "sweep": ("round",),
    "deterministic": ("run",),
}

RETRY_RE = re.compile(r"^retry:\s*(\d+)", re.MULTILINE)
PLAN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
PASS_STATUS_RE = re.compile(r"^PASS\s+([A-Za-z0-9][A-Za-z0-9._-]{0,127})$")
# Einzeilige Frontmatter-Skalare am Zeilenanfang (kein Indent) — Fallback bei
# YAMLError/Nicht-Mapping. Mehrzeilige Felder (done_when, anti_scope, …) werden
# bewusst nicht rekonstruiert; der Runner braucht nur id/title/priority/…
_FRONTMATTER_SCALAR_RE = re.compile(r"^([a-z_]+):\s*(.+)$")


class ManifestError(ValueError):
    """pack.yaml ist unbrauchbar — Meldung nennt Pack und Feld."""


@dataclass
class PhaseCfg:
    engine: str
    model: str
    timeout: int
    prompt: str  # Dateiname relativ zum Pack-Ordner
    # Reasoning-Effort dieser Phase; None = Engine-Default (kein CLI-Flag).
    # Gültige Stufen kommen aus der Engine selbst (engines.effort_levels_for),
    # nicht aus einer zweiten Liste hier.
    effort: str | None = None


@dataclass
class DeterministicPhaseCfg:
    command: tuple[str, ...]
    timeout: int

    # Read-only compatibility projection for existing pack catalog consumers.
    # These are display sentinels, never accepted from pack.yaml and never
    # resolved through an LLM engine.
    @property
    def engine(self) -> str:
        return "deterministic"

    @property
    def model(self) -> str:
        return "command"

    @property
    def prompt(self) -> str:
        return "pack.yaml"

    @property
    def effort(self) -> None:
        return None


@dataclass(frozen=True)
class GoalCfg:
    reached_status: str
    reason: str


@dataclass
class Pack:
    name: str
    type: str
    repo: Path
    pack_dir: Path
    phases: dict[str, PhaseCfg | DeterministicPhaseCfg]
    stop: dict[str, int]
    description: str = ""
    stability: str = "experimental"
    notify: dict[str, str] = field(default_factory=dict)
    params: dict[str, str] = field(default_factory=dict)
    autoland: bool = False
    plan_skip_when_queue_full: bool = True
    base_branch: str = "main"
    land_remote: str = "piet-fork"
    land_gates: list[str] | None = None
    land_push: bool = True
    goal: GoalCfg | None = None

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
        raise ManifestError(
            f"Pack {name!r}: type muss pipeline|sweep|deterministic sein, ist {ptype!r}"
        )
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
    phases: dict[str, PhaseCfg | DeterministicPhaseCfg] = {}
    for pname, pcfg in phases_raw.items():
        if not isinstance(pcfg, dict):
            raise ManifestError(f"Pack {name!r}: Phase {pname} muss ein Mapping sein")
        if ptype == "deterministic":
            missing = {"command", "timeout"} - set(pcfg)
            extra = set(pcfg) - {"command", "timeout"}
            if missing or extra:
                detail = []
                if missing:
                    detail.append(f"fehlt {sorted(missing)}")
                if extra:
                    detail.append(f"kennt nicht {sorted(extra)}")
                raise ManifestError(
                    f"Pack {name!r}: deterministische Phase {pname} "
                    + ", ".join(detail)
                )
            command_raw = pcfg["command"]
            if not isinstance(command_raw, str) or not command_raw.strip():
                raise ManifestError(
                    f"Pack {name!r}: Phase {pname}: command muss "
                    "ein nicht-leerer String sein"
                )
            try:
                command = tuple(shlex.split(command_raw))
            except ValueError as exc:
                raise ManifestError(
                    f"Pack {name!r}: Phase {pname}: command ungültig: {exc}"
                ) from exc
            if not command:
                raise ManifestError(
                    f"Pack {name!r}: Phase {pname}: command ist leer"
                )
            if (
                not isinstance(pcfg["timeout"], int)
                or isinstance(pcfg["timeout"], bool)
                or pcfg["timeout"] <= 0
            ):
                raise ManifestError(
                    f"Pack {name!r}: Phase {pname}: timeout muss "
                    "positive Ganzzahl sein"
                )
            phases[pname] = DeterministicPhaseCfg(
                command=command,
                timeout=pcfg["timeout"],
            )
            continue
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
        # `effort` ist optional. Gesetzt wird er fail-closed gegen das
        # Transport-Set der Engine geprüft — ein Effort, den das CLI nicht
        # annimmt, darf nicht bis zum Lauf durchrutschen.
        try:
            phase_effort = engines.validate_effort(
                str(pcfg["engine"]), pcfg.get("effort")
            )
        except engines.EffortUnsupported as exc:
            raise ManifestError(f"Pack {name!r}: Phase {pname}: {exc}") from None
        phases[pname] = PhaseCfg(
            engine=pcfg["engine"], model=str(pcfg["model"]),
            timeout=pcfg["timeout"], prompt=str(pcfg["prompt"]),
            effort=phase_effort,
        )

    for section in ("stop", "params", "notify", "goal"):
        if raw.get(section) is not None and not isinstance(raw[section], dict):
            raise ManifestError(f"Pack {name!r}: {section} muss ein Mapping sein")
    stop = dict(DEFAULT_STOP)
    for key, val in (raw.get("stop") or {}).items():
        if key not in DEFAULT_STOP:
            raise ManifestError(f"Pack {name!r}: stop.{key} unbekannt (erlaubt: {sorted(DEFAULT_STOP)})")
        if not isinstance(val, int) or isinstance(val, bool):
            raise ManifestError(f"Pack {name!r}: stop.{key} muss Ganzzahl sein")
        # dry_streak_alert: 0 schaltet die Eskalation ab; alle anderen stop-Keys > 0.
        if key == "dry_streak_alert":
            if val < 0:
                raise ManifestError(
                    f"Pack {name!r}: stop.{key} muss nicht-negative Ganzzahl sein"
                )
        elif val <= 0:
            raise ManifestError(f"Pack {name!r}: stop.{key} muss positive Ganzzahl sein")
        stop[key] = val

    autoland_raw = raw.get("autoland", False)
    if not isinstance(autoland_raw, bool):
        raise ManifestError(f"Pack {name!r}: autoland muss boolean sein")
    autoland = autoland_raw
    # Opt-out (Default an): Packs, deren Vertrag jede Nacht frische Pläne
    # verlangt, setzen plan_skip_when_queue_full: false. Der Key IST an die
    # Autoland-Sicherheitsprojektion gebunden, aber nur wenn ein Manifest ihn
    # setzt (_autoland_safety_hash, Muster wie `goal`) — so dreht ein Flip auf
    # false den Hash fail-closed, ohne die kuratierten SHAs der Packs zu
    # brechen, die den Key gar nicht führen.
    plan_skip_raw = raw.get("plan_skip_when_queue_full", True)
    if not isinstance(plan_skip_raw, bool):
        raise ManifestError(
            f"Pack {name!r}: plan_skip_when_queue_full muss boolean sein"
        )
    autoland_contract = AUTOLAND_CONTRACTS.get(name)
    if name in AUTOLAND_PACK_ALLOWLIST and (
        autoland_contract is None
        or autoland_contract.safety_sha256 is None
        or autoland_contract.prompt_sha256 is None
    ):
        raise ManifestError(
            f"Pack {name!r}: Allowlist-Vertrag ist unvollständig (echte SHAs fehlen)"
        )
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
        # Prompt-Vertrag: Prompt pro Phase, KEIN Modell und (seit 2026-07-25)
        # KEINE Engine — beide floaten bewusst und fallen hier nicht durch.
        actual_contract = {
            phase: cfg.prompt
            for phase, cfg in phases.items()
        }
        if actual_contract != AUTOLAND_PHASE_CONTRACT:
            raise ManifestError(
                f"Pack {name!r}: autoland-Phasenvertrag weicht ab; "
                "erwartet die kuratierte Prompt-Zuordnung pro Phase"
            )
        # Modell-/engineagnostische Sicherheitsprojektion statt Vollmanifest-Bytes —
        # Modell, Engine und Budget-Werte sind ausgeschlossen, alles übrige gebunden.
        if _autoland_safety_hash(raw) != autoland_contract.safety_sha256:
            raise ManifestError(
                f"Pack {name!r}: autoland-Sicherheitsprojektion weicht vom "
                "kuratierten Hash ab"
            )
        actual_prompts = {
            cfg.prompt: hashlib.sha256((pack_dir / cfg.prompt).read_bytes()).hexdigest()
            for cfg in phases.values()
        }
        if actual_prompts != autoland_contract.prompt_sha256:
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

    goal_raw = raw.get("goal")
    goal = None
    if goal_raw is not None:
        expected_goal_keys = {"reached_status", "reason"}
        if set(goal_raw) != expected_goal_keys:
            raise ManifestError(
                f"Pack {name!r}: goal braucht genau {sorted(expected_goal_keys)}"
            )
        for key in expected_goal_keys:
            value = goal_raw[key]
            if not isinstance(value, str) or not value.strip() or "\n" in value:
                raise ManifestError(
                    f"Pack {name!r}: goal.{key} muss ein nicht-leerer Einzeiler sein"
                )
        goal = GoalCfg(
            reached_status=goal_raw["reached_status"].strip(),
            reason=goal_raw["reason"].strip(),
        )

    params = {str(k): str(v) for k, v in (raw.get("params") or {}).items()}
    notify = {str(k): str(v) for k, v in (raw.get("notify") or {}).items()}

    return Pack(
        name=name, type=ptype, repo=Path(repo).expanduser(), pack_dir=pack_dir,
        phases=phases, stop=stop, description=str(raw.get("description", "")),
        stability=str(raw.get("stability", "experimental")), notify=notify,
        params=params, autoland=autoland,
        plan_skip_when_queue_full=plan_skip_raw,
        base_branch=base_branch, land_remote=land_remote,
        land_gates=land_gates, land_push=land_push, goal=goal,
    )


# ── reine Helfer (test-direkt) ───────────────────────────────────────────────

def parse_retry(plan_text: str) -> int:
    m = RETRY_RE.search(plan_text)
    return int(m.group(1)) if m else 0


def _unquote_frontmatter_scalar(value: str) -> str:
    """Trim + strip one matching quote layer if the whole value is quoted."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _frontmatter_scalar_fallback(block: str) -> dict:
    """Zeilenbasierter Fallback: nur `key: value`-Skalare ohne Indent.

    Rettet Build-Slots bei Tippfehler-Titles (unquoted Colon → YAMLError),
    ohne komplexe/eingerückte Werte zu raten. parse_plan_id validiert die id
    weiter via PLAN_ID_RE (fail-closed für Auto-Land).
    """
    result: dict[str, str] = {}
    for line in block.splitlines():
        match = _FRONTMATTER_SCALAR_RE.match(line)
        if not match:
            continue
        result[match.group(1)] = _unquote_frontmatter_scalar(match.group(2))
    return result


def parse_plan_frontmatter(plan_text: str) -> dict:
    """Liest das Plan-Frontmatter fail-closed als Mapping.

    Primärpfad: yaml.safe_load. Bei YAMLError oder Nicht-Mapping greift ein
    strenger zeilenbasierter Skalar-Fallback (nur einzeilige Keys am
    Zeilenanfang) — typischer Auslöser: unquoted Doppelpunkt im title.
    """
    if not plan_text.startswith("---\n"):
        return {}
    end = plan_text.find("\n---\n", 4)
    if end < 0:
        return {}
    block = plan_text[4:end]
    try:
        frontmatter = yaml.safe_load(block)
    except yaml.YAMLError:
        return _frontmatter_scalar_fallback(block)
    if isinstance(frontmatter, dict):
        return frontmatter
    return _frontmatter_scalar_fallback(block)


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


def pass_status_matches_plan(
    status: str, plan_text: str, plan_path: Path | str | None = None
) -> bool:
    """PASS-Status muss an die Plan-ID binden — sonst kein Auto-Land.

    Baseline: exakte YAML-`id`. Zusätzlich toleriert (belegter Vorfall
    2026-07-13): Builder/Verifier melden trotz Prompt-Anweisung den
    Dateinamen-Stamm statt der YAML-ID (`PASS P1-settings-controls-first`
    statt `PASS HT-UX-P1-SETTINGS-CONTROLS-FIRST`). Ein Stamm-Treffer zählt
    nur, wenn `plan_path` übergeben ist UND die YAML-ID selbst eine
    präfigierte Form dieses Stamms ist (`<präfix>-<stamm>`, casefolded) —
    kein beliebiger Substring-Match, sonst würde ein PASS für einen anderen
    Plan mit zufällig gleichem Stamm durchrutschen.
    """
    plan_id = parse_plan_id(plan_text)
    if not plan_id:
        return False
    match = PASS_STATUS_RE.fullmatch(status)
    if not match:
        return False
    status_id = match.group(1)
    if status_id == plan_id:
        return True
    if plan_path is None:
        return False
    stem = Path(plan_path).stem
    if status_id.casefold() != stem.casefold():
        return False
    return plan_id.casefold().endswith("-" + stem.casefold())


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


def select_test_python(repo: Path) -> tuple[Path | None, str]:
    """Interpreter fuer den Collection-Sweep — dieselbe Wahrheit wie die Testskripte.

    Bis 2026-07-28 stand hier hart ``repo/venv/bin/python``. Nach dem venv-Split
    traegt ``venv`` kein pytest mehr (Release-Env ohne [dev]), es liegt in
    ``.venv`` — der Sweep starb mit "No module named pytest" und damit rollte
    JEDE default-gegatete Landung in diesem Repo zurueck, unabhaengig davon, wie
    gut der Diff war (live belegt beim Landeversuch hermes-hardening,
    2026-07-28 20:17, rc=1 nach sauberem ff-Merge).

    Die Auswahl gehoert deshalb an ``scripts/lib/select_test_python.sh``: der
    Helper beantwortet dieselbe Frage schon fuer run_tests.sh/collect_check.sh
    und sortiert dabei den leeren ``uv venv`` und den Hermes-Release-Env aus.
    Eine zweite Regel hier waere genau die Drift, gegen die dieser Helper
    ueberhaupt angelegt wurde.

    Rueckgabe ``(pfad, "")`` bei Erfolg, ``(None, grund)`` sonst — der Grund
    kommt vom Helper und nennt das Reparaturkommando woertlich. Fehlt der Helper
    (fremdes Pack-Repo ohne die Fork-Skripte), bleibt es beim alten Pfad.
    """
    helper = repo / "scripts" / "lib" / "select_test_python.sh"
    if not helper.is_file():
        return repo / "venv" / "bin" / "python", ""
    try:
        res = subprocess.run(
            ["bash", "-c", '. "$1"; select_test_python "$2"', "_", str(helper), str(repo)],
            capture_output=True, encoding="utf-8", errors="replace",
            timeout=120, check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return None, f"{helper}: {exc}"
    picked = (res.stdout or "").strip()
    if res.returncode != 0 or not picked:
        return None, (res.stderr or "").strip() or f"{helper}: rc={res.returncode}"
    return Path(picked), ""


# Auch die LANDUNGS-Gates laufen unter dem Loop-Lastdeckel. Sie erben sonst das
# `HERMES_TEST_WORKERS=8` aus dem systemd-User-Environment und liefen mit dem
# doppelten Gewicht der Bau-Gates — waehrend daneben andere Packs bauen und
# Kanban-Worker arbeiten. Denselben Hebel wie `loops/gate.sh`, damit der
# Operator beides mit einer Variable anhebt.
def _land_gate_env() -> dict[str, str]:
    return {**os.environ, "HERMES_TEST_WORKERS": os.environ.get(
        "HERMES_LOOP_TEST_WORKERS", "4")}


def _isolatable_gate(command: list[str], label: str = "") -> str | None:
    """Return the per-file gate kind, or ``None`` when rerunning is unsafe."""
    if label == "affected":
        return "python"
    if label == "vitest":
        return "vitest"
    command_text = " ".join(command).lower()
    if "vitest" in command_text:
        return "vitest"
    if any(token in command_text for token in ("run-affected.sh", "run_tests.sh", "pytest")):
        return "python"
    return None


def _is_load_artifact(repo: Path, gate: str | None, output: str) -> tuple[bool, str]:
    """Prove a red per-file gate harmless by rerunning every red file alone.

    ``isolate_from_logs`` deliberately fails closed for an empty/unparseable
    failed-file list, capped reruns, timeouts, and reproduced failures.
    """
    if gate is None:
        return False, ""
    result = isolate_from_logs(
        [(gate, output)],
        runner_factory=lambda name: build_runner(name, repo),
    )
    if not result["leaker_only"]:
        return False, ""
    files = [entry.removeprefix(f"{gate}: ") for entry in result["leakers"]]
    return (
        True,
        f"Lastartefakt: {gate} rot unter Last; isoliert grün: {', '.join(files)}",
    )


def _land_gates(
    repo: Path,
    base: str,
    land_gates: list[str] | None = None,
    *,
    include_collection: bool = True,
) -> tuple[bool, str]:
    """Shared post-merge landing proof.

    This is the single gate implementation used by both ``LoopRunner`` and the
    deterministic landing loop. Keeping it outside ``LoopRunner`` makes the
    proven gate sequence reusable without constructing a model-driven runner.
    """
    if land_gates is not None:
        load_artifacts: list[str] = []
        for command in land_gates:
            argv = shlex.split(command)
            try:
                res = subprocess.run(
                    argv,
                    cwd=str(repo),
                    capture_output=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=2400,
                    check=False,
                    env=_land_gate_env(),
                )
            except (subprocess.TimeoutExpired, OSError) as exc:
                return False, f"{command}: {exc}"
            if res.returncode != 0:
                output = (res.stdout or "") + (res.stderr or "")
                if res.returncode == 1:
                    is_artifact, detail = _is_load_artifact(
                        repo, _isolatable_gate(argv), output
                    )
                    if is_artifact:
                        load_artifacts.append(detail)
                        continue
                tail = "\n".join(output.splitlines()[-15:])
                return False, f"{command} rot (rc={res.returncode}):\n{tail}"
        report = "land_gates grün (" + ", ".join(land_gates) + ")"
        if load_artifacts:
            report += "\n" + "\n".join(load_artifacts)
        return True, report

    steps: list[tuple[str, list[str], Path]] = []
    if include_collection:
        py, why = select_test_python(repo)
        if py is None:
            return False, f"kein Test-Python fuer den Collection-Sweep:\n{why}"
        steps.append(
            (
                "collection",
                [
                    str(py),
                    "-m",
                    "pytest",
                    "--co",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    "tests/",
                ],
                repo,
            )
        )
    steps.append(
        (
            "affected",
            ["bash", str(repo / "scripts" / "run-affected.sh"), base],
            repo,
        )
    )
    touched = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "diff",
            "--name-only",
            f"{base}..HEAD",
            "--",
            "web/",
        ],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    touched_web = bool(touched.stdout.strip())
    if touched_web:
        # WORTGLEICH wie vor der Herausloesung aus LoopRunner. Diese drei
        # Schritte sind der Landungsbeweis JEDES Nacht-Packs, nicht nur des
        # Landing-Loops -- ein Wechsel auf scripts/gate-frontend.sh aendert
        # ihn still mit: das Skript bringt einen check-branch-age-Preflight
        # (Exit 3) und einen Build mit, also koennte eine Landung rot werden,
        # die heute gruen ist. Dieser Slice loest die Funktion nur heraus; ob
        # der kanonische Gate hier der bessere ist, ist eine eigene
        # Entscheidung mit eigener Messung.
        steps += [
            ("lint:control", ["npm", "run", "lint:control"], repo / "web"),
            ("tsc", ["npx", "tsc", "-b", "--noEmit"], repo / "web"),
            ("vitest", ["npx", "vitest", "run"], repo / "web"),
        ]
    load_artifacts: list[str] = []
    for label, command, cwd in steps:
        try:
            res = subprocess.run(
                command,
                cwd=str(cwd),
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=2400,
                check=False,
                env=_land_gate_env(),
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return False, f"{label}: {exc}"
        if res.returncode != 0:
            output = (res.stdout or "") + (res.stderr or "")
            tail = "\n".join(output.splitlines()[-15:])
            if label == "affected" and res.returncode == 3:
                return False, f"affected nicht gelaufen (rc=3):\n{tail}"
            if label == "affected" and res.returncode == 4:
                return False, f"affected unmapped (rc=4):\n{tail}"
            if res.returncode == 1:
                is_artifact, detail = _is_load_artifact(
                    repo, _isolatable_gate(command, label), output
                )
                if is_artifact:
                    load_artifacts.append(detail)
                    continue
            return False, f"{label} rot (rc={res.returncode}):\n{tail}"
    suffix = " + frontend" if touched_web else ""
    prefix = "collection + affected" if include_collection else "affected"
    batched = " (Collection im Lauf bereits bewiesen)" if not include_collection else ""
    report = f"{prefix}{suffix} grün{batched}"
    if load_artifacts:
        report += "\n" + "\n".join(load_artifacts)
    return True, report


# Night-Overrides: nur PHASE_[A-Z]+_(ENGINE|MODEL|EFFORT). Persistent (nicht
# konsumiert). Präzedenz: One-Shot (overrides.env) > Night (night-overrides.env)
# > pack.yaml. EFFORT gehört in dieselbe Klasse wie ENGINE/MODEL: er beschreibt
# die Route, nicht den Auftrag, und dreht deshalb auch den Autoland-Safety-Hash
# nicht (der projiziert je Phase nur den Prompt).
_NIGHT_OVERRIDE_KEY_RE = re.compile(r"^PHASE_[A-Z]+_(ENGINE|MODEL|EFFORT)$")
NIGHT_OVERRIDES_FILENAME = "night-overrides.env"


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


def parse_night_overrides(path: Path) -> dict[str, str]:
    """night-overrides.env: nur PHASE_*_(ENGINE|MODEL); ungültige Keys fail-closed."""
    if not path.is_file():
        return {}
    data = parse_overrides(path)
    bad = sorted(k for k in data if not _NIGHT_OVERRIDE_KEY_RE.fullmatch(k))
    if bad:
        raise RuntimeError(
            "night-overrides.env: nur PHASE_[A-Z]+_(ENGINE|MODEL) erlaubt; "
            f"ungültig: {', '.join(bad)}"
        )
    return data


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
        except ValueError as exc:
            logger.warning("malformed ledger event skipped: %s", exc)
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
        self.night_overrides = parse_night_overrides(self.state / NIGHT_OVERRIDES_FILENAME)
        self.phase_secs: dict[str, int] = {}
        self._overrides_consumed = False
        self._repo_validated = False
        self._repo_lock_depth = 0
        self.autoland_runtime = self._read_runtime_autoland()

    def _read_runtime_autoland(self) -> bool:
        """Operator-Autoland aus den One-Shot-Overrides — mit Zweitschlüssel.

        ``AUTOLAND=1`` allein reicht NICHT: ``AUTOLAND_ACK`` muss den exakten
        Pack-Namen wiederholen. Ein fehlender/falscher ACK ist ein harter
        Fehler, kein stilles Ignorieren — sonst startete ein Lauf, den der
        Operator als landend gemeint hat, still als nicht-landend (oder
        umgekehrt), und genau diese Zweideutigkeit soll der Schlüssel killen.
        """
        raw = str(self.overrides.get(AUTOLAND_RUNTIME_KEY, "")).strip().lower()
        if raw in ("", "0", "false", "no"):
            return False
        if raw not in ("1", "true", "yes"):
            raise RuntimeError(
                f"Pack {self.pack.name}: {AUTOLAND_RUNTIME_KEY}={raw!r} ungültig "
                "(erlaubt: 1/0)"
            )
        ack = str(self.overrides.get(AUTOLAND_RUNTIME_ACK_KEY, "")).strip()
        if ack != self.pack.name:
            raise RuntimeError(
                f"Pack {self.pack.name}: Operator-Autoland braucht den Zweitschlüssel "
                f"{AUTOLAND_RUNTIME_ACK_KEY}=<Pack-Name>; erhalten: {ack!r}"
            )
        if self.pack.type != "pipeline":
            raise RuntimeError(
                f"Pack {self.pack.name}: Autoland braucht type=pipeline "
                f"(ist {self.pack.type})"
            )
        return True

    @property
    def autoland_active(self) -> bool:
        """Landet dieser LAUF? Manifest-Vertrag ODER Operator-Zweitschlüssel."""
        return bool(self.pack.autoland or self.autoland_runtime)

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
            payload = {"ts": _utc_iso(), "pack": self.pack.name}
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

    def _acquire_flock(self, fh, what: str, *, blocking: bool, timeout: float) -> None:
        if blocking:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError(
                            f"{what}-Lock nach {timeout:g}s weiter belegt"
                        ) from None
                    time.sleep(0.05)
        # Einmal-Retry: die Dashboard-Running-Probe hält das Lock für µs —
        # ein Start exakt in dem Fenster soll nicht die Nacht kosten.
        for attempt in (1, 2):
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if attempt == 2:
                    raise RuntimeError(f"{what}-Lock belegt — läuft schon ein Loop?") from None
                time.sleep(1.0)

    @contextmanager
    def locked(self, *, blocking: bool = False, timeout: float = 0.0):
        """Pack-Lock: nie zwei Läufe DESSELBEN Packs gleichzeitig.

        Der frühere globale Repo-Lock ist hier weg — er umschloss den GANZEN
        Lauf (Plan/Build/Verify arbeiten im pack-eigenen Worktree und brauchen
        den geteilten Live-Checkout gar nicht) und verhinderte dadurch, dass
        zwei Packs desselben Repos je gleichzeitig liefen. Der Repo-Lock lebt
        jetzt eng um ``cmd_land`` (siehe ``repo_locked``), wo der geteilte
        Live-Checkout tatsächlich exklusiv gebraucht wird.

        Deterministische Sammelläufe (``blocking=True``) warten weiterhin
        zusätzlich auf den Repo-Lock — mit demselben begrenzten Wartefenster
        wie zuvor —, damit sie einer gerade laufenden Landung nicht
        dazwischenfahren; das Fenster ist jetzt nur die Land-Dauer statt der
        gesamten Nacht.
        """
        self.state.mkdir(parents=True, exist_ok=True)
        with self.state.joinpath(".lock").open("w", encoding="utf-8") as pack_fh:
            self._acquire_flock(pack_fh, "Pack", blocking=blocking, timeout=timeout)
            if blocking:
                with self.repo_locked(blocking=True, timeout=timeout):
                    yield
            else:
                yield

    @contextmanager
    def repo_locked(self, *, blocking: bool = False, timeout: float = 0.0):
        """Repo-Lock: exklusiver Zugriff auf den GETEILTEN Live-Checkout
        (``self.pack.repo``) — umschließt ausschließlich ``cmd_land``
        (Sauberkeits-Check, ff-Merge, Landungs-Gates, Push, Rollback).
        ``_land_gates`` startet Collection-Sweep + affected Tests mit
        ``cwd=repo`` und läuft Minuten — das Exklusivfenster muss also bis
        zum Ende der Gates reichen, nicht nur bis zum ``merge --ff-only``:
        sonst kann eine zweite, ebenfalls saubere Landung dazwischenmergen und
        die laufenden Gates lesen Dateien, die unter ihnen weggeschrieben
        werden (nicht-deterministisches Gate-Ergebnis, siehe cmd_land).

        BASE-REFRESH in ``cmd_night`` läuft bewusst NICHT hierunter: der
        eigentliche Rebase passiert im pack-eigenen Worktree, gegen ``repo``
        gehen dort nur rev-list/diff/tag-Lesevorgänge, und die
        Rebase-Anker-Tags tragen den Pack-Namen als Präfix — keine Kollision
        zwischen Packs.

        Re-entrant innerhalb desselben ``LoopRunner``: ein deterministischer
        Pack, der über ``locked(blocking=True)`` den Repo-Lock schon hält,
        blockiert sich in seinem eigenen ``cmd_land`` nicht selbst.
        """
        if self._repo_lock_depth > 0:
            self._repo_lock_depth += 1
            try:
                yield
            finally:
                self._repo_lock_depth -= 1
            return
        self.state.mkdir(parents=True, exist_ok=True)
        repo_key = hashlib.md5(str(self.pack.repo.resolve()).encode("utf-8")).hexdigest()[:8]
        repo_lock_path = self.state.parent / f".repo-{repo_key}.lock"
        with repo_lock_path.open("w", encoding="utf-8") as repo_fh:
            self._acquire_flock(repo_fh, "Repo", blocking=blocking, timeout=timeout)
            # Halter hinterlegen: wer den Lock hält, ist von außen sonst nicht
            # feststellbar. Das Dashboard leitete den Blockierer bisher aus
            # "welches Pack läuft gerade" ab — seit Bauen parallel erlaubt ist,
            # ist ein laufendes Pack aber KEIN Beleg fürs Lock-Halten, und die
            # Anzeige benannte dann das falsche. Nur gültig, solange der Lock
            # steht: `open("w")` truncatet, ein freigegebener Lock ist leer.
            try:
                repo_fh.write(f"{self.pack.name}\n{os.getpid()}\n")
                repo_fh.flush()
            except OSError:
                pass  # Die Landung darf an einer Anzeige-Notiz nicht scheitern.
            self._repo_lock_depth += 1
            try:
                yield
            finally:
                self._repo_lock_depth -= 1

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
        res = self.git("worktree", "add", "-B", self.pack.branch, str(self.wt), self.pack.base_branch, cwd=self.pack.repo)
        if res.returncode != 0:
            raise RuntimeError(f"worktree add fehlgeschlagen: {res.stderr.strip()}")

    def _fail_guard_clean(self, reason: str) -> NoReturn:
        """Guard-Abbruch einmalig protokollieren und bis zur Unit tragen."""
        message = f"GUARD-CLEAN: {reason}"
        self.say(f"ABBRUCH: {reason}")
        self.ledger(f"⚠️ {message}")
        self.notify(f"{self.pack.name}: {message}")
        raise RuntimeError(message)

    def guard_clean(self) -> bool:
        """Loop-exklusiven Baum deterministisch säubern (Driver-Ebene, hook-frei)."""
        if not self.wt.is_dir():
            self._fail_guard_clean(f"Worktree fehlt: {self.wt}")
        head = self.git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        if head != self.pack.branch:
            # Drift (live 2026-07-31 + 08-03: Baum stand auf 'master',
            # BASE-REFRESH fiel aus, Reverts schlugen mit "Invalid revision
            # range" fehl). Erst säubern, dann zurück auf den Loop-Branch —
            # niemals still auf dem fremden Branch weiterbauen.
            self.say(
                f"Worktree driftete auf {head!r} — heile auf {self.pack.branch!r}"
            )
            self.git("reset", "--quiet", "HEAD", "--", ".")
            self.git("checkout", "--", ".")
            self.git("clean", "-fd")
            res = self.git("checkout", self.pack.branch)
            if res.returncode != 0:
                detail = res.stderr.strip() or "keine Fehlerdetails"
                self._fail_guard_clean(
                    f"Rückkehr auf {self.pack.branch!r} fehlgeschlagen "
                    f"(war {head!r}): {detail}"
                )
            self.ledger(f"WORKTREE-DRIFT geheilt: {head} → {self.pack.branch}")
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
            self._fail_guard_clean(
                f"Worktree lässt sich nicht säubern:\n{left}"
            )
        return True

    def revert_range(self, prehead: str) -> bool:
        # Vorschalt-Guard: der Revert darf nur laufen, wenn prehead auflösbar
        # und Vorfahre von HEAD ist. Im gedrifteten Baum (live 2026-07-31/
        # 08-03) ist prehead unauflösbar ("Invalid revision range") — oder
        # schlimmer: ein auflösbarer Nicht-Vorfahre ließe `git revert
        # prehead..HEAD` still die GESAMTE Branch-Historie revertieren.
        resolvable = (
            self.git("rev-parse", "--verify", "--quiet", f"{prehead}^{{commit}}").returncode == 0
        )
        if not resolvable or self.git(
            "merge-base", "--is-ancestor", prehead, "HEAD"
        ).returncode != 0:
            self.say(f"Revert verweigert: {prehead[:9]} ist unauflösbar oder kein Vorfahre von HEAD")
            self.ledger(
                f"⚠️ REVERT VERWEIGERT ({prehead[:9]}..HEAD): prehead unauflösbar "
                "oder kein Vorfahre von HEAD — Baum gedriftet?"
            )
            return False
        res = self.git("revert", "--no-edit", f"{prehead}..HEAD")
        if res.returncode == 0:
            return True
        err = res.stderr.strip()
        self.say(f"Revert fehlgeschlagen: {err}")
        self.git("revert", "--abort")  # halben Konflikt-Zustand aufräumen (best effort)
        # Fail-safe statt liegenlassen (live 2026-07-31/08-02: der unverifizierte
        # Commit blieb stehen, danach blockte der Autoland-Wächter jede Nacht).
        # prehead wurde zu Rundenbeginn gemessen und ist oben als Vorfahre
        # bewiesen — alles in prehead..HEAD ist unverifizierte Arbeit genau
        # dieser Runde; Verifiziertes liegt nie hinter prehead. Der harte
        # Reset ist damit verlustfrei für verifizierte Arbeit.
        if self.rev_parse() != prehead:
            reset = self.git("reset", "--hard", prehead)
            if reset.returncode == 0:
                self.ledger(
                    f"⚠️ REVERT FEHLGESCHLAGEN ({prehead[:9]}..HEAD) — "
                    f"fail-safe reset --hard auf {prehead[:9]}: {err[:200]}"
                )
                return True
            err = f"{err} | reset: {reset.stderr.strip()}"
        self.ledger(f"⚠️ REVERT FEHLGESCHLAGEN ({prehead[:9]}..HEAD): {err[:200]}")
        return False

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
        """Phase-Config: One-Shot > Night > pack.yaml (ENGINE/MODEL); TIMEOUT nur One-Shot."""
        cfg = self.pack.phases[name]
        if not isinstance(cfg, PhaseCfg):
            raise RuntimeError(
                f"Pack {self.pack.name}: Phase {name} ist deterministisch "
                "und hat keine Engine-/Modell-Konfiguration"
            )
        up = name.upper()
        eng_key = f"PHASE_{up}_ENGINE"
        mod_key = f"PHASE_{up}_MODEL"
        if eng_key in self.overrides:
            engine = self.overrides[eng_key]
        elif eng_key in self.night_overrides:
            engine = self.night_overrides[eng_key]
        else:
            engine = cfg.engine
        if mod_key in self.overrides:
            model = self.overrides[mod_key]
        elif mod_key in self.night_overrides:
            model = self.night_overrides[mod_key]
        else:
            model = cfg.model
        # Effort folgt derselben Präzedenz wie Engine/Modell (One-Shot > Night >
        # pack.yaml) und wird gegen die EFFEKTIVE Engine validiert — nach einem
        # Engine-Swap gilt das Set der neuen Engine, nicht das der alten.
        eff_key = f"PHASE_{up}_EFFORT"
        if eff_key in self.overrides:
            effort_raw = self.overrides[eff_key]
        elif eff_key in self.night_overrides:
            effort_raw = self.night_overrides[eff_key]
        else:
            effort_raw = cfg.effort if engine == cfg.engine else None
        return PhaseCfg(
            engine=engine,
            model=model,
            timeout=self._int_override(f"PHASE_{up}_TIMEOUT", cfg.timeout),
            prompt=cfg.prompt,
            effort=engines.validate_effort(engine, effort_raw),
        )

    def stop_cfg(self, key: str) -> int:
        return self._int_override(key.upper(), self.pack.stop[key])

    def render_prompt(self, phase: str, **extra: str) -> str:
        """Render a pack prompt with effective route placeholders.

        ``ENGINE``/``MODEL`` always come from ``phase_cfg(phase)`` after One-Shot
        and Night overrides. When the pack has a ``build`` phase,
        ``BUILD_ENGINE``/``BUILD_MODEL`` expose that same resolution for the
        writer route (used by verifiers). Missing build-phase placeholders stay
        unreplaced — never invent a fallback model name.
        """
        cfg = self.phase_cfg(phase)
        text = (self.pack.pack_dir / cfg.prompt).read_text(encoding="utf-8")
        params = dict(self.pack.params)
        for key in params:
            if key.upper() in self.overrides:
                params[key] = self.overrides[key.upper()]
        params_line = " ".join(f"{k.upper()}={v}" for k, v in sorted(params.items()))
        subst: dict[str, str] = {
            "STATE_DIR": str(self.state),
            "WT": str(self.wt),
            "PARAMS": params_line,
            "PHASE": phase,
            "ENGINE": cfg.engine,
            "MODEL": cfg.model,
            **extra,
        }
        if "build" in self.pack.phases:
            build_cfg = self.phase_cfg("build")
            subst.setdefault("BUILD_ENGINE", build_cfg.engine)
            subst.setdefault("BUILD_MODEL", build_cfg.model)
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
            # Lastdeckel für den GANZEN Agent-Turn, nicht nur für loops/gate.sh.
            # Ein Pack-Prompt ruft `scripts/gate-frontend.sh` und teils auch
            # `scripts/run_tests.sh` direkt — die erbten sonst weiter das
            # `HERMES_TEST_WORKERS=8` aus dem systemd-User-Environment und den
            # vitest-Default von 4 Workern. Seit mehrere Packs parallel bauen,
            # summiert sich das: am 2026-08-04 riss ein jsdom-Render-Test bei
            # Load 9,2 seinen bereits auf 30 s angehobenen Timeout, isoliert
            # lief dieselbe Datei in 1,8 s durch. Timeouts weiter hochzudrehen
            # ist Whack-a-Mole (die Warnung steht in web/vite.config.ts) — die
            # Last gehört gedeckelt, nicht die Uhr verstellt.
            # vitest-Worker wiegen schwerer als pytest-Worker (je eine
            # jsdom-Umgebung), daher der eigene, niedrigere Hebel.
            "HERMES_TEST_WORKERS": os.environ.get("HERMES_LOOP_TEST_WORKERS", "4"),
            "GATE_FRONTEND_MAX_WORKERS": os.environ.get(
                "HERMES_LOOP_FRONTEND_WORKERS", "2"
            ),
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

    def run_phase(
        self, phase: str, *, round_: int | None = None, **extra: str
    ) -> engines.EngineResult:
        cfg = self.phase_cfg(phase)
        effort_note = f", effort={cfg.effort}" if cfg.effort else ""
        self.say(
            f"── Phase {phase} (engine={cfg.engine}, model={cfg.model}"
            f"{effort_note}, timeout={cfg.timeout}s)"
        )
        self.status_path.write_text("", encoding="utf-8")
        prompt = self.render_prompt(phase, **extra)
        started = time.time()
        started_iso = _utc_iso()
        current = {"phase": phase, "engine": cfg.engine, "model": cfg.model,
                   "effort": cfg.effort,
                   "started_at": started_iso, "timeout": cfg.timeout}
        if round_ is not None:
            current["round"] = round_
        self._heartbeat(current)
        with self._worker_environment(phase):
            result = engines.get_engine(cfg.engine)(
                cfg.model, prompt, self.wt, cfg.timeout, cfg.effort
            )
        self.phase_secs[phase] = int(time.time() - started)
        done = {"phase": phase, "engine": cfg.engine, "model": cfg.model,
                "effort": cfg.effort,
                "secs": self.phase_secs[phase], "rc": result.rc,
                "at": started_iso}
        if round_ is not None:
            done["round"] = round_
        self._heartbeat(None, done=done)
        log_file = self.state / "logs" / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{phase}.log"
        log_file.write_text(result.output, encoding="utf-8")
        subscription_engine = cfg.engine in {
            "alibaba-token-plan",
            "claude",
            "codex",
            "kimi",
            "neuralwatt",
            "xai",
        }
        self.ledger_event(
            event="phase_usage",
            round=round_,
            phase=phase,
            engine=cfg.engine,
            model=cfg.model,
            secs=self.phase_secs[phase],
            rc=result.rc,
            input_tokens=result.input_tokens,
            cached_input_tokens=result.cached_input_tokens,
            output_tokens=result.output_tokens,
            reasoning_tokens=result.reasoning_tokens,
            total_tokens=result.total_tokens,
            provenance_path=result.provenance_path,
            billing="subscription" if subscription_engine else "unknown",
            metered_cost_eur=0.0 if subscription_engine else None,
        )
        self.say(f"Phase {phase} fertig in {self.phase_secs[phase]}s (rc={result.rc})")
        return result

    def _secs(self, *phases: str) -> str:
        return " · ".join(f"{p} {self.phase_secs.get(p, 0)}s" for p in phases)

    def last_status(self) -> str:
        try:
            return self.status_path.read_text(encoding="utf-8").splitlines()[0].strip()
        except (FileNotFoundError, IndexError):
            return ""

    def _goal_reached(self, status: str | None = None) -> bool:
        goal = self.pack.goal
        return goal is not None and (status or self.last_status()) == goal.reached_status

    def _retire_if_goal_reached(self) -> bool:
        goal = self.pack.goal
        if goal is None or not self._goal_reached():
            return False
        timer = f"hermes-loop@{self.pack.name}.timer"
        retired_at = _utc_iso()
        result = _disable_user_timer(timer)
        if result.returncode != 0:
            detail = " ".join(
                (result.stderr or result.stdout or "kein systemctl-Detail").splitlines()
            )
            self.ledger(
                f"PACK-RETIREMENT-FAILED timer={timer} reason={goal.reason} "
                f"attempted_at={retired_at} detail={detail}"
            )
            raise RuntimeError(
                f"Pack-Ziel erreicht, aber Timer {timer} konnte nicht deaktiviert werden: "
                f"{detail}"
            )
        self.ledger(
            f"PACK-RETIREMENT timer={timer} reason={goal.reason} retired_at={retired_at}; "
            "Reaktivierung ist Operator-Handlung"
        )
        return True

    @property
    def dry_streak_path(self) -> Path:
        """Persistent DRY-night counter for this pack (not LEDGER.md)."""
        return self.state / "dry-streak.json"

    def _read_dry_streak(self) -> int:
        """Best-effort read of consecutive DRY nights. Corrupt/missing → 0."""
        try:
            raw = json.loads(self.dry_streak_path.read_text(encoding="utf-8"))
            n = raw.get("streak", 0) if isinstance(raw, dict) else 0
            if not isinstance(n, int) or isinstance(n, bool) or n < 0:
                return 0
            return n
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
            logger.warning("dry-streak read failed; treating counter as zero: %s", exc)
            return 0

    def _write_dry_streak(self, streak: int) -> None:
        """Best-effort write of the DRY-night counter. Never raises."""
        try:
            self.state.mkdir(parents=True, exist_ok=True)
            payload = {"streak": int(streak), "updated_at": _utc_iso()}
            self.dry_streak_path.write_text(
                json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001 — counter must not crash the loop
            logger.warning("dry-streak write fehlgeschlagen: %s", exc)

    def _reset_dry_streak(self) -> None:
        """Clear the counter after a non-DRY productive end (plans/build)."""
        try:
            if self._read_dry_streak() == 0 and not self.dry_streak_path.exists():
                return
            self._write_dry_streak(0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("dry-streak reset fehlgeschlagen: %s", exc)

    def _note_dry_end(self, status: str) -> None:
        """Increment DRY streak; from threshold on, ledger + Discord notify.

        Never raises. Return value of the night stay True — escalation reports,
        it does not abort.
        """
        try:
            streak = self._read_dry_streak() + 1
            self._write_dry_streak(streak)
            threshold = self.stop_cfg("dry_streak_alert")
            if threshold <= 0 or streak < threshold:
                return
            # Operator-Klartext (Discord): Pack, Nächte, last-status wörtlich, Bitte.
            msg = (
                f"{self.pack.name}: DRY-Streak {streak} Nächte in Folge "
                f"(last-status: {status}) — bitte Loop prüfen oder stilllegen"
            )
            self.ledger(
                f"DRY-STREAK: {streak} Nächte in Folge (status={status})"
            )
            self.ledger_event(
                phase="plan",
                verdict="dry_streak",
                streak=streak,
                threshold=threshold,
                reason=status,
            )
            self.notify(msg)
        except Exception as exc:  # noqa: BLE001 — same house style as ledger_event
            logger.warning("dry-streak note fehlgeschlagen: %s", exc)

    def stop_requested(self) -> bool:
        return self.stop_path.exists()

    def _validate_effective_phase_catalog(self) -> None:
        """Fail-closed: effektive One-Shot/Night/pack.yaml-Paare müssen im Katalog stehen.

        Gilt für alle Packs (nicht nur Autoland).
        """
        catalog_data = yaml.safe_load(MODELS_FILE.read_text(encoding="utf-8")) or {}
        # Dieselbe Funktion, aus der das Dashboard sein Dropdown baut — sonst
        # böte das UI Modelle an, die der Start hier ablehnt.
        from loops.model_catalog import catalog_with_dynamic_models

        catalog = catalog_with_dynamic_models(catalog_data.get("engines", {}))
        for phase, raw_cfg in self.pack.phases.items():
            if isinstance(raw_cfg, DeterministicPhaseCfg):
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
            # phase_cfg validiert den Effort bereits gegen die effektive Engine;
            # hier bleibt der Aufruf als expliziter fail-closed-Punkt stehen,
            # damit ein künftiger Pfad an phase_cfg vorbei nicht still durchläuft.
            engines.validate_effort(cfg.engine, cfg.effort)

    def _validate_autoland_runtime(self, *, skip_plan: bool = False) -> None:
        """Validiert effektive Phase-Paare; Autoland zusätzlich den sichtbaren Laufvertrag."""
        # Effektive Engine/Model-Paarung gilt fail-closed für alle Packs.
        self._validate_effective_phase_catalog()
        if not self.autoland_active:
            return
        if skip_plan:
            raise RuntimeError(
                f"Pack {self.pack.name}: --skip-plan ist bei autoland nicht erlaubt"
            )

        # SKIP_BASE_REFRESH only skips the pre-night rebase — it does not
        # alter the land contract, so it stays allowed under autoland.
        allowed = {
            "MAX_ROUNDS", "MAX_HOURS", "SKIP_BASE_REFRESH",
            # Der Operator-Schalter und sein Zweitschlüssel sind selbst Overrides
            # und müssen sich hier nicht als "unerlaubt" wiederfinden.
            AUTOLAND_RUNTIME_KEY, AUTOLAND_RUNTIME_ACK_KEY,
        }
        for phase in self.pack.phases:
            prefix = f"PHASE_{phase.upper()}_"
            # EFFORT floatet wie ENGINE/MODEL: er ist Teil der Route, nicht der
            # Landungsautorität. Stünde er nicht hier, würde jede Effort-Wahl im
            # Startdialog das Autoland eines Packs abschießen.
            allowed.update({f"{prefix}ENGINE", f"{prefix}MODEL", f"{prefix}EFFORT"})
        rejected = sorted(set(self.overrides) - allowed)
        if rejected:
            raise RuntimeError(
                f"Pack {self.pack.name}: nicht erlaubte Runtime-Overrides: {rejected}; "
                "Autoland akzeptiert nur Engine, Modell, Effort, MAX_ROUNDS und MAX_HOURS"
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
        """Modell, Engine UND Budgets floaten frei (Operator-Entscheid 2026-07-25).

        Vorher war die Engine-ROLLE pro Phase Teil der Landungsautorität: ein
        Engine-Swap über PHASE_*_ENGINE (One-Shot oder Night) setzte den
        manual-land-Marker und der Runner loggte `AUTOLAND übersprungen`. Das ist
        entfallen — die Landung hängt jetzt nur noch am beim Laden fail-closed
        validierten Manifest-Vertrag (Prompts, Pack-Quelle, Live-Repo,
        Landungsziel) sowie an Verify-PASS, Visual-Gate und den land_gates.
        """
        return bool(self.autoland_active)

    @property
    def manual_land_marker(self) -> Path:
        return self.state / "AUTOLAND_MANUAL"

    def _prepare_runtime_land_mode(self) -> None:
        if not self.autoland_active:
            return
        if self.autoland_runtime and not self.pack.autoland:
            # Zweitschlüssel-Autoland ist eine Operator-Entscheidung pro Lauf und
            # steht deshalb nachweisbar im Ledger — sonst wäre hinterher nicht
            # unterscheidbar, ob ein Pack laut Manifest landet oder weil jemand
            # den Schalter umgelegt hat.
            self.ledger(
                f"AUTOLAND per Operator-Zweitschlüssel aktiviert "
                f"({AUTOLAND_RUNTIME_ACK_KEY}={self.pack.name}); "
                "Deny-Präfixe + Gates unverändert bindend"
            )
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

    def _autoland_contract(self) -> AutolandContract:
        """Return the already loader-validated per-pack landing contract.

        Operator-Autoland (Zweitschlüssel, kein kuratierter Vertrag) bekommt
        einen Default: Pfade offen — der Runner kann für ein beliebiges Pack
        nicht vorher wissen, was es anfasst — aber dieselbe Deny-Liste wie die
        kuratierten Verträge, plus Visual-Pflicht sobald `web/src/control/`
        berührt ist. Die SHA-Felder bleiben None: sie gehören zum
        Manifest-Vertrag, den dieser Pfad bewusst nicht führt.
        """
        contract = AUTOLAND_CONTRACTS.get(self.pack.name)
        if contract is None and self.autoland_runtime:
            return AutolandContract(
                path_prefixes=(),
                deny_prefixes=OPERATOR_AUTOLAND_DENY_PREFIXES,
                safety_sha256=None,
                prompt_sha256=None,
                require_visual="if_web_touched",
                allow_any_path=True,
            )
        if contract is None:
            raise RuntimeError(
                f"Pack {self.pack.name}: kein per-Pack-Autoland-Vertrag vorhanden"
            )
        return contract

    def _autoland_touched_paths(self) -> tuple[list[str] | None, str]:
        scope = self.git(
            "diff", "--no-renames", "--name-only",
            f"main...{self.pack.branch}", cwd=self.pack.repo,
        )
        if scope.returncode != 0:
            return None, f"Commit-Scope nicht lesbar: {scope.stderr.strip()[:200]}"
        return [line.strip() for line in scope.stdout.splitlines() if line.strip()], ""

    def _autoland_visual_required(self, touched: list[str] | None = None) -> bool:
        contract = self._autoland_contract()
        if contract.require_visual == "always":
            return True
        if touched is None:
            touched, _ = self._autoland_touched_paths()
        # Ein unlesbarer Scope darf nie als vermeintlicher Backend-only-Diff die
        # visuelle Pflicht umgehen.
        return touched is None or any(
            path.startswith("web/src/control/") for path in touched
        )

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

    def _expire_stale_building(self) -> None:
        """10-building verfällt beim Pipeline-Start: was dort liegt, stammt aus
        einem früheren Lauf (der Runner läuft ein-instanzig, ein aktiver Build
        kann es nicht geben). Verwaist heißt bounced mit Protokollzeile — sonst
        blockt der Eintrag _autoland_queue_ready() dauerhaft (live 2026-08-04:
        zwei Pläne seit 31.07./02.08. → 'Queue nicht abgeschlossen')."""
        stage = self.queue / "10-building"
        if not stage.is_dir():
            return
        bounced = False
        for plan in sorted(stage.glob("*.md")):
            target = self.queue / "90-bounced" / plan.name
            if target.exists():  # Namens-Wiederverwendung: alte Evidenz nicht überschreiben
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                target = target.with_name(f"{target.stem}.{stamp}.md")
            plan.rename(target)
            self.ledger(
                f"building-expired: {target.name} "
                "(verwaist aus früherem Lauf — bounced)"
            )
            self.ledger_event(
                phase="expire", verdict="bounced", plan=target.name,
                fail_kind="stale_building", reason="verwaist in 10-building",
            )
            bounced = True
        if bounced:
            self._discard_expired_build_commits()

    def _discard_expired_build_commits(self) -> None:
        """Räumt den unverifizierten Commit ab, der zu einem verfallenen
        10-building-Plan gehört (usage-limit im Build/Verify lässt Plan UND
        Commit absichtlich liegen, :2164/:2209). Würde nur der Plan verfallen,
        bliebe der Netto-Diff bestehen: _autoland_pending() wahr, aber
        _autoland_queue_ready() blockt für immer (verified=0/ahead=1) — der
        Live-Wedge von dashboard-experience (27.07.–04.08.2026).

        Verlustfrei für Verifiziertes: ein Commit gilt erst nach PASS als
        verifiziert, und genau dann wandert sein Plan nach 20-verified
        (:2301-2303). qcount("20-verified") == 0 heißt also: der gesamte
        Netto-Diff ist unverifiziert oder netto-null (Build+Revert-Paare).
        Liegt doch Verifiziertes vor (gemischter Stand, z. B. Runde 1 PASS,
        Runde 2 usage-limit), wird der Branch NICHT angefasst, sondern
        alarmiert — das ist eine Menschen-Entscheidung, keine Runner-Automatik.
        Der Anker-Tag macht den Reset selbst reversibel (Muster wie
        _auto_rebase)."""
        if self.qcount("20-verified") > 0:
            self.ledger(
                "expire: gemischter Stand (20-verified nicht leer + verworfener "
                "Build) — Loop-Branch bleibt unangetastet, manuell klären"
            )
            self.notify(
                f"⛔ {self.pack.name}: verwaisten Build verworfen, aber "
                "20-verified ist nicht leer — Loop-Branch manuell prüfen."
            )
            return
        net = self.git(
            "diff", "--name-only",
            f"{self.pack.base_branch}...{self.pack.branch}", cwd=self.pack.repo,
        ).stdout.strip()
        if not net:
            return
        anchor = (
            f"loop-expire/{self.pack.name}/"
            f"{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        )
        if self.git("tag", anchor, self.pack.branch, cwd=self.pack.repo).returncode != 0:
            self.ledger(
                "expire: Anker-Tag ließ sich nicht setzen — Branch bleibt "
                "unangetastet (Autoland-Wächter blockiert weiter)"
            )
            return
        self.ensure_wt()
        if not self.guard_clean():
            self.ledger(
                "expire: Branch-Räumung abgebrochen (Worktree nicht heilbar) — "
                f"Anker {anchor}, Autoland-Wächter blockiert weiter"
            )
            return
        res = self.git("reset", "--hard", self.pack.base_branch)
        if res.returncode != 0:
            self.ledger(
                f"⚠️ EXPIRE-RESET FEHLGESCHLAGEN "
                f"({self.pack.branch} → {self.pack.base_branch}, Anker {anchor}): "
                f"{res.stderr.strip()[:200]}"
            )
            return
        self.ledger(
            f"expire-reset: unverifizierte Commits verworfen "
            f"({self.pack.branch} → {self.pack.base_branch}, Anker {anchor}): "
            + ", ".join(net.splitlines()[:5])
        )

    def base_stale_blocks_gate(self) -> bool:
        """Würde der branch-age-Preflight das Gate im Pack-Worktree blocken?

        Objektive Probe am echten Skript statt an Agent-Prosa: `check-branch-age.sh`
        ist die einzige Quelle seiner eigenen Schwelle, damit sie nicht in zwei
        Stellen driftet. Läuft es rot, kann `run-affected.sh` (und damit
        `loops/gate.sh`) gar keinen Test ausgeführt haben — ein Verifier-FAIL in
        diesem Zustand ist eine Aussage über die Basis, nicht über den Kandidaten.

        Fehlt das Skript oder lässt es sich nicht starten, lautet die Antwort
        False: eine unbeantwortbare Probe darf keine Runde entwerten.
        """
        script = self.wt / "scripts" / "check-branch-age.sh"
        if not script.is_file():
            return False
        try:
            res = subprocess.run(
                [str(script)], cwd=str(self.wt), capture_output=True,
                encoding="utf-8", errors="replace", timeout=60, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return res.returncode != 0

    def _auto_rebase_on_settled_base(self, **kw) -> tuple[bool, str]:
        """``_auto_rebase``, aber nie mitten in eine fremde Landung hinein.

        Der Rebase selbst läuft im pack-eigenen Worktree — er LIEST aber
        ``main`` im Live-Checkout, und genau dort ist ``main`` während einer
        fremden Landung unfertig: nach dem ff-Merge, aber vor dem Urteil der
        Landungs-Gates. Fallen die rot, rollt ``_safe_land_rollback`` main per
        ``reset --keep`` zurück — ein Pack, das in diesem Fenster rebast hat,
        trägt die zurückgerollten Commits danach in seinem Branch und
        ff-merged sie bei seiner eigenen Landung wieder nach main.

        Deshalb kurz den Repo-Lock nehmen. Läuft die fremde Landung länger,
        ist Weiterarbeiten auf der ALTEN Basis die richtige Antwort — ein
        Base-Refresh darf die Nacht nie blockieren (bestehender Vertrag).
        """
        try:
            with self.repo_locked(blocking=True, timeout=BASE_SAMPLE_WAIT_S):
                return self._auto_rebase(self.pack.repo, **kw)
        except RuntimeError as exc:
            return False, f"fremde Landung hält den Repo-Lock ({exc})"

    def _refresh_base_if_stale(self, why: str) -> bool:
        """Basis nachziehen, wenn sie das Gate blocken würde. True = wieder frisch."""
        if not self.base_stale_blocks_gate():
            return True
        ok, msg = self._auto_rebase_on_settled_base(collapse_net_zero=True)
        first = msg.splitlines()[0] if msg else ""
        if ok:
            self.ledger(f"BASE-REFRESH ({why}): {first}")
        else:
            self.ledger(f"BASE-REFRESH ({why}) übersprungen: {first}")
        return ok and not self.base_stale_blocks_gate()

    # ── Frontend-Deps ──
    def _plan_prompt_reads_has_web(self) -> bool:
        """Liest der Plan-Prompt dieses Packs {{HAS_WEB}} überhaupt aus?

        Bewusst aus dem Prompt abgeleitet statt als neuer ``pack.yaml``-Key:
        die autoland-Sicherheitsprojektion (``_manifest_hash``) hasht Manifest
        UND Prompts, ein neuer Manifest-Key würde die kuratierten Hashes
        brechen und autoland-Packs fail-closed stellen.
        """
        cfg = self.pack.phases.get("plan")
        prompt = getattr(cfg, "prompt", None)
        if not prompt:
            return False
        try:
            text = (self.pack.pack_dir / prompt).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        return "{{HAS_WEB}}" in text

    # Die beiden node_modules, die gate-frontend.sh worktree-lokal verlangt.
    _DEDICATED_NM_LINKS = ("node_modules", "web/node_modules")

    def dedicated_deps_tree(self) -> Path:
        """Spiegelt die Deps-Identität aus ``scripts/gate-frontend.sh``.

        Das Gate bildet sie als ``basename(<physischer repo_root>)-sha256[:16]``
        unter ``HERMES_WORKTREE_DEPS_ROOT``. Die Formel darf nicht driften —
        ``test_dedicated_deps_tree_matches_gate_frontend_formula`` rechnet sie
        gegen das echte Shell-Skript nach.
        """
        root = os.environ.get("HERMES_WORKTREE_DEPS_ROOT") or "/mnt/data/hermes-worktree-deps"
        canonical = self.wt.resolve(strict=False)
        digest = hashlib.sha256(str(canonical).encode("utf-8")).hexdigest()[:16]
        return Path(os.path.abspath(root)) / f"{canonical.name}-{digest}"

    def _plant_dedicated_deps_links(self) -> str | None:
        """Symlinks in den exklusiven Deps-Baum pflanzen; Fehlergrund oder None.

        ``gate-frontend.sh`` bereitet den dedizierten Baum nur vor, wenn schon
        ein solcher Symlink existiert (``_gate_has_dedicated_nm_link``) — in
        einem frischen Worktree also nie. Ohne diesen Anstoß installiert
        ``npm ci`` ~1,5 GB als echte Verzeichnisse dorthin, wo der Worktree
        liegt: bei den Loop-Packs auf die Systemplatte statt auf die SSD.
        """
        tree = self.dedicated_deps_tree()
        for rel in self._DEDICATED_NM_LINKS:
            link = self.wt / rel
            target = tree / rel
            if link.is_symlink():
                if Path(os.path.abspath(link.readlink())) != target:
                    return f"{rel} zeigt auf einen fremden Baum ({link.readlink()})"
                continue
            if link.exists():
                return f"{rel} ist ein echtes Verzeichnis — erst entfernen"
            try:
                target.mkdir(parents=True, exist_ok=True)
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(target)
            except OSError as exc:
                return f"{rel}: {exc}"
        return None

    def ensure_frontend_deps(self) -> str:
        """HAS_WEB ehrlich beantworten, statt blind auf ``node_modules`` zu proben.

        Seit der Worktree-Deps-Isolation legt erst ``scripts/gate-frontend.sh``
        den ``node_modules``-Symlink in den exklusiven Deps-Baum unter
        ``HERMES_WORKTREE_DEPS_ROOT`` — ein frischer Loop-Worktree hat ihn nie.
        Die frühere nackte ``is_dir()``-Probe meldete darum dauerhaft
        ``HAS_WEB=0``; Packs mit hartem Frontend-Kontrakt brachen im Planner ab
        und kamen so nie an ein Gate, das die Deps angelegt hätte
        (dashboard-polish: 7 Nächte DRY, 2026-07-29 bis 2026-08-04).

        Nur Packs, deren Plan-Prompt ``HAS_WEB`` überhaupt liest, zahlen die
        Provisionierung; ein Fehlschlag kostet nie die Nacht, er fällt auf
        ``"0"`` zurück und wird im Ledger benannt.
        """
        web = self.wt / "web"
        if (web / "node_modules").is_dir():
            return "1"
        if not web.is_dir() or not self._plan_prompt_reads_has_web():
            return "0"
        gate = self.wt / "scripts" / "gate-frontend.sh"
        if not gate.is_file():
            return "0"
        self.say("Frontend-Deps fehlen im Worktree — Preflight provisioniert sie einmalig …")
        planted = self._plant_dedicated_deps_links()
        if planted is not None:
            self.ledger(f"FRONTEND-DEPS Isolation nicht herstellbar: {planted}")
            return "0"
        env = {
            **os.environ,
            "GATE_FRONTEND_PREFLIGHT_ONLY": "1",
            # Der Preflight installiert nur Deps und behauptet nichts über
            # Grün — der branch-age-Wächter davor würde ihn auf einem noch
            # nicht rebaseten Baum blocken, ohne dass je ein Test liefe.
            "HERMES_GATE_STALE_OK": "1",
        }
        try:
            res = subprocess.run(
                [str(gate)], cwd=str(self.wt), env=env, capture_output=True,
                encoding="utf-8", errors="replace", timeout=1800, check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self.ledger(f"FRONTEND-DEPS Provisionierung fehlgeschlagen: {exc}")
            return "0"
        if res.returncode != 0 or not (web / "node_modules").is_dir():
            tail = [
                line for line in ((res.stderr or "") + "\n" + (res.stdout or "")).splitlines()
                if line.strip()
            ]
            reason = tail[-1].strip() if tail else "kein Output"
            self.ledger(
                f"FRONTEND-DEPS Provisionierung fehlgeschlagen (rc={res.returncode}): {reason}"
            )
            return "0"
        self.ledger("FRONTEND-DEPS provisioniert (gate-frontend Preflight)")
        return "1"

    # ── Kommandos ──
    def cmd_plan(self, fresh: bool = False) -> bool:
        self._validate_autoland_runtime()
        self.stop_path.unlink(missing_ok=True)
        self.ensure_dirs()
        self.ensure_wt(fresh=fresh)
        if not self.guard_clean():
            return False
        has_web = self.ensure_frontend_deps()
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
        # Statuskontrakt: Planner muss last-status auf PLANNED… oder DRY… setzen.
        # Leerer/sonstiger Status (False-DRY, Timeout, Turn vor Sweep-Ende) ist
        # eine Anomalie — sichtbar im Ledger; cmd_night retryt einmal.
        if not (status.startswith("PLANNED") or status.startswith("DRY")):
            self.ledger(
                f"PLAN-ANOMALIE: status=[{status}] — "
                "Planner-Turn ohne Statuskontrakt beendet"
            )
        self.notify(f"🌀 {self.pack.name} PLAN: {n} Pläne in der Queue (status={status})")
        return True

    def _run_deterministic(self) -> int:
        cfg = self.pack.phases["run"]
        if not isinstance(cfg, DeterministicPhaseCfg):
            raise RuntimeError(
                f"Pack {self.pack.name}: deterministische run-Phase fehlt"
            )
        self.say(
            f"── Phase run (command={shlex.join(cfg.command)}, "
            f"timeout={cfg.timeout}s)"
        )
        started = time.time()
        try:
            result = subprocess.run(
                list(cfg.command),
                cwd=str(self.wt),
                env={
                    **os.environ,
                    "HERMES_LOOP_STATE_DIR": str(self.state),
                    "HERMES_LOOP_STOP_PATH": str(self.stop_path),
                },
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=cfg.timeout,
                check=False,
            )
            rc = result.returncode
            stdout = result.stdout or ""
            stderr = result.stderr or ""
        except subprocess.TimeoutExpired as exc:
            rc = 124
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            stderr += f"\nTimeout nach {cfg.timeout}s"
        except OSError as exc:
            rc = 126
            stdout = ""
            stderr = str(exc)

        secs = int(time.time() - started)
        self.phase_secs["run"] = secs
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        (self.state / "logs" / f"{stamp}-run.log").write_text(
            stdout + stderr,
            encoding="utf-8",
        )
        lines = stdout.splitlines()
        if lines:
            for line in lines:
                self.ledger(f"RUN stdout: {line}")
        else:
            self.ledger("RUN stdout: (leer)")
        self.ledger(f"RUN exit={rc} [{secs}s]")
        self.ledger_event(
            event="phase_usage",
            phase="run",
            secs=secs,
            rc=rc,
            billing="deterministic",
            metered_cost_eur=0.0,
        )
        self.say(f"Phase run fertig in {secs}s (rc={rc})")
        if stderr.strip():
            self.say("stderr:\n" + "\n".join(stderr.splitlines()[-15:]))
        return rc

    def cmd_run(self, fresh: bool = False) -> int:
        self._validate_autoland_runtime()
        self.consume_overrides()
        self.stop_path.unlink(missing_ok=True)
        self.ensure_dirs()
        self.ensure_wt(fresh=fresh)
        if self._goal_reached():
            self._retire_if_goal_reached()
            if self.pack.type != "deterministic":
                self.report()
            return 0
        if self.pack.type == "deterministic":
            rc = self._run_deterministic()
            if rc == 0:
                self._retire_if_goal_reached()
            return rc
        if self.pack.type == "pipeline":
            self._run_pipeline()
        else:
            self._run_sweep()
        self._retire_if_goal_reached()
        self.report()
        return 0

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
            "recorded_at": _utc_iso(),
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
        fails = verified = stale_requeues = 0
        self._expire_stale_building()
        for rnd in range(1, self.stop_cfg("max_rounds") + 1):
            if self.stop_requested():
                self.say("STOP-Datei — sauberes Ende.")
                break
            if time.time() >= deadline:
                self.say("Wall-Clock-Deadline erreicht.")
                break
            if not self.guard_clean():
                break
            # Vorbeugung: main läuft unter uns weiter (parallele Sessions,
            # Kanban-Merges, andere Packs). Driftet die Basis über die
            # branch-age-Schwelle, stellt der Preflight das Gate rot, OHNE dass
            # ein Test läuft — und der Verifier verwirft dann einen guten Commit.
            # Zwischen den Runden ist kein Commit in Arbeit, hier ist Nachziehen
            # gefahrlos.
            self._refresh_base_if_stale(f"Runde {rnd}, Basis stale")
            plan = self.pick_plan()
            if plan is None:
                self.say("Queue leer — fertig.")
                break
            building = self.queue / "10-building" / plan.name
            plan.rename(building)
            self.say(f"═══ Runde {rnd}: {building.name} ═══")
            prehead = self.rev_parse()
            if self.autoland_active:
                self.visual_attestation_path.unlink(missing_ok=True)

            build = self.run_phase("build", round_=rnd, PLAN_PATH=str(building))
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
            verify = self.run_phase(
                "verify",
                round_=rnd,
                PLAN_PATH=str(building),
                RANGE=f"{prehead}..HEAD",
                BUILD_PROVENANCE=build.provenance_path or "",
            )
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
            # Statuskontrakt-Anomalie (leer, weder PASS noch FAIL, nicht TIMEOUT):
            # Verify ist read-only/idempotent → genau 1 Retry, dann fail-closed.
            # Explizites FAIL / PASS / TIMEOUT bleiben unverändert (kein Retry).
            # Status-Wahrheit = last-status-Datei, nie Agent-Prosa (Doktrin runner.py:24).
            if (
                not status.startswith("PASS")
                and not status.startswith("FAIL")
                and status != "TIMEOUT"
            ):
                self.ledger(f"VERIFY-RETRY nach Anomalie (status=[{status}])")
                self.say(
                    f"Verify-Anomalie (status=[{status}]) — genau 1 Verify-Retry."
                )
                evidence_before = self._verifier_evidence_dirs()
                verify = self.run_phase(
                    "verify",
                    round_=rnd,
                    PLAN_PATH=str(building),
                    RANGE=f"{prehead}..HEAD",
                    BUILD_PROVENANCE=build.provenance_path or "",
                )
                if verify.usage_limit:
                    self.say(
                        "Usage-Limit im Verifier — Commit bleibt UNVERIFIZIERT "
                        "(Plan in 10-building/)."
                    )
                    self.ledger(
                        f"R{rnd} ⚠️ {building.name} BUILT aber UNVERIFIED (usage-limit)"
                    )
                    self.notify(
                        f"{self.pack.name}: Usage-Limit im Verifier — "
                        f"{building.name} unverifiziert, gestoppt."
                    )
                    self.ledger_event(
                        round=rnd, phase="verify", verdict="blocked",
                        plan=building.name, fail_kind="usage_limit",
                        reason="unverified",
                        build_secs=self.phase_secs.get("build"),
                    )
                    break
                status = "TIMEOUT" if verify.timed_out else self.last_status()
                if not self.guard_clean():
                    break
                if (
                    not status.startswith("PASS")
                    and not status.startswith("FAIL")
                    and status != "TIMEOUT"
                ):
                    self.ledger(
                        f"VERIFY-ANOMALIE: status=[{status}] — "
                        "Verifier-Turn 2x ohne Statuskontrakt"
                    )
                    self.notify(
                        f"{self.pack.name}: Verify-Phase 2x ohne Statuskontrakt "
                        "— fail-closed Revert, bitte Verifier-Log pruefen"
                    )
                    # Fall-through: fail-closed Revert wie bisher (status anomal).
            try:
                plan_text = building.read_text(encoding="utf-8")
            except OSError:
                plan_text = ""
            pass_matches = pass_status_matches_plan(status, plan_text, building)
            visual_required = (
                self.autoland_active and self._autoland_visual_required()
            )
            visual_ok = not visual_required
            visual_report = (
                "für Review-only-Pack nicht erforderlich"
                if not self.autoland_active
                else "für Backend-only-Diff nicht erforderlich"
            )
            if verify.rc == 0 and pass_matches and visual_required:
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
            elif (
                # Nur ein Verifier, der wirklich GELAUFEN ist und ein Urteil
                # abgegeben hat, kann am stale Gate gescheitert sein. TIMEOUT
                # und Engine-Abbrüche (rc != 0) haben andere Ursachen — sie
                # hier mitzufangen würde echte Fehler als Basisproblem tarnen.
                verify.rc == 0
                and not verify.timed_out
                and self.base_stale_blocks_gate()
            ):
                # Kein Urteil über den Kandidaten: der branch-age-Preflight hat
                # das Gate rotgestellt, bevor ein Test lief (live 2026-08-04 an
                # dashboard-polish — parallele main-Merges während des Laufs).
                # Hier wird bewusst NICHT gebounct und NICHT auf fail_streak
                # gezählt; der Plan geht unverbraucht zurück in die Queue und
                # die nächste Runde baut ihn auf frischer Basis neu.
                self.say(
                    "VERIFY UNGÜLTIG — Basis ist stale, das Gate lief nicht. "
                    "Kein Urteil über den Plan."
                )
                if not self.revert_range(prehead):
                    self.notify(
                        f"{self.pack.name}: Revert fehlgeschlagen bei {building.name} — gestoppt."
                    )
                    break
                stale_requeues += 1
                building.rename(self.queue / "00-planned" / building.name)
                self.ledger(
                    f"R{rnd} ⏭ {building.name}: Verify ungültig (Basis stale, Gate lief nicht) "
                    f"— Plan neu eingereiht, kein Fail"
                )
                self.ledger_event(round=rnd, phase="verify", verdict="blocked",
                                   plan=building.name, fail_kind="base_stale", reason=status,
                                   build_secs=self.phase_secs.get("build"),
                                   verify_secs=self.phase_secs.get("verify"))
                if not self._refresh_base_if_stale("nach ungültigem Verify"):
                    # Die Basis lässt sich nicht heilen (Konflikt, dirty). Ohne
                    # diesen Deckel liefe der Pack alle max_rounds durch und
                    # baute jede Runde denselben Plan neu, ohne ihn je bewerten
                    # zu können — fail_streak greift hier ja bewusst nicht.
                    if stale_requeues >= 2:
                        self.say("Basis bleibt stale — Stop für Human-Review.")
                        self.notify(
                            f"{self.pack.name}: Basis bleibt stale (Rebase scheitert) — gestoppt."
                        )
                        self.ledger_event(round=rnd, phase="stop", verdict="stopped",
                                           reason="base_stale_unhealable")
                        break
                continue
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
            result = self.run_phase("round", round_=rnd)
            if result.usage_limit:
                self.say("Usage-Limit — Stop.")
                self.notify(f"{self.pack.name}: Usage-Limit in Runde {rnd} — gestoppt.")
                self.ledger_event(round=rnd, phase="sweep", verdict="blocked", fail_kind="usage_limit")
                break
            status = "TIMEOUT" if result.timed_out else self.last_status()
            required_artifacts = tuple(
                name.strip()
                for name in str(self.pack.params.get("required_artifacts", "")).split(",")
                if name.strip()
            )
            if status.startswith("PREPARED") and required_artifacts:
                unsafe = [
                    name
                    for name in required_artifacts
                    if Path(name).is_absolute() or Path(name).name != name or name in {".", ".."}
                ]
                if unsafe:
                    status = f"BLOCKED PREPARED-Artefaktnamen ungültig: {', '.join(unsafe)}"
                    self.status_path.write_text(status + "\n", encoding="utf-8")
                else:
                    missing = [
                        name
                        for name in required_artifacts
                        if not (self.state / name).is_file()
                        or (self.state / name).stat().st_size == 0
                    ]
                    if missing:
                        status = f"BLOCKED PREPARED-Artefakte fehlen: {', '.join(missing)}"
                        self.status_path.write_text(status + "\n", encoding="utf-8")
                    else:
                        artifacts = ", ".join(required_artifacts)
                        self.ledger(f"PREPARED: Artefakte {artifacts}")
                        self.notify(f"{self.pack.name}: PREPARED — {artifacts}")
            # "?" las sich als "Status unbekannt, vermutlich ok". Der Grund ist
            # bekannt: die Runde hat den Statuskontrakt nicht erfüllt.
            self.ledger(
                f"R{rnd} sweep status={status or 'kein Status (Kontrakt verletzt)'} "
                f"[{self._secs('round')}]"
            )
            if self._goal_reached(status):
                self.ledger_event(
                    round=rnd, phase="stop", verdict="stopped", reason="goal_reached"
                )
                break
            if status.startswith("DRY"):
                dry, blocked = dry + 1, 0
                self.ledger_event(round=rnd, phase="sweep", verdict="ok", reason=status)
            elif status.startswith("BLOCKED") or status == "TIMEOUT" or not status:
                # Leerer Status = der Agent hat den Kontrakt nicht erfüllt (Turn
                # abgebrochen, Datei nie geschrieben). Er fiel bisher in den
                # else-Zweig und zählte damit als ERFOLGREICHE Runde: beide
                # Zähler auf 0, fail_streak konnte nie greifen, ein toter Sweep
                # lief alle max_rounds durch. Der Pipeline-Pfad kennt diesen
                # Statuskontrakt bereits (run_phase "plan"), der Sweep nicht.
                blocked, dry = blocked + 1, 0
                self.ledger_event(round=rnd, phase="sweep", verdict="blocked",
                                   fail_kind="blocked", reason=status or "kein Status")
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
        contract = self._autoland_contract()
        touched, scope_error = self._autoland_touched_paths()
        if touched is None:
            return False, scope_error
        denied = [
            path
            for path in touched
            if any(path.startswith(prefix) for prefix in contract.deny_prefixes)
        ]
        if denied:
            return False, "Commit-Scope trifft Deny-Präfix: " + ", ".join(denied)
        outside = [] if contract.allow_any_path else [
            path
            for path in touched
            if not any(path.startswith(prefix) for prefix in contract.path_prefixes)
        ]
        if not touched or outside:
            scope_label = (
                "web/src/control/**"
                if self.pack.name == "dashboard-experience"
                else f"erlaubter Präfixe {contract.path_prefixes}"
            )
            return False, (
                f"Commit-Scope außerhalb {scope_label}: "
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
        if not pass_status_matches_plan(status, plan_text, verified_plans[0]):
            return False, (
                f"Verifier-Status passt nicht exakt zum Plan {plan_id}: {status or '?'}"
            )
        branch_head = self.git(
            "rev-parse", f"refs/heads/{self.pack.branch}", cwd=self.pack.repo
        ).stdout.strip()
        if self._autoland_visual_required(touched):
            visual_ok, visual_report = self._visual_attestation_ready(
                plan_text, branch_head
            )
            if not visual_ok:
                return False, visual_report
        else:
            visual_report = "Visual-Attestation für Backend-only-Diff nicht erforderlich"
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
        if self.autoland_active and self.stop_requested():
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
        # Verfall auch auf dem Resume-Pfad, BEVOR _autoland_queue_ready()
        # urteilt: ein verwaister 10-building-Eintrag (usage-limit lässt Plan
        # UND Commit liegen) blockte den Wächter sonst jede Nacht erneut — der
        # Verfall lief nur in _run_pipeline und wurde dort nie erreicht,
        # solange _autoland_pending() wahr war (Live-Wedge 27.07.–04.08.2026,
        # Codex-Review 04.08.).
        if self.autoland_active:
            self._expire_stale_building()
        if self.autoland_active and self._autoland_pending():
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
            reb_ok, reb_msg = self._auto_rebase_on_settled_base(
                collapse_net_zero=True
            )
            first_line = reb_msg.splitlines()[0] if reb_msg else ""
            if reb_ok:
                self.ledger(f"BASE-REFRESH: {first_line}")
            else:
                self.ledger(f"BASE-REFRESH übersprungen: {first_line}")
        # Plan-Phase sparen, wenn die Queue ohnehin voll ist: cmd_plan wuerde
        # nur Modellzeit (Rate-Limit-Kontingent) fuer Plaene verbrennen, fuer
        # die kein Round-Slot frei ist. Opt-out per Manifest fuer Packs, deren
        # Vertrag jede Nacht frische Plaene verlangt. fresh bleibt unberuehrt:
        # ohne cmd_plan steht der Worktree noch nicht, cmd_run uebernimmt.
        #
        # `max_rounds` ist hier die RICHTIGE und einzige Schranke. Die
        # naheliegende Verschaerfung auf min(max_rounds, params.max_plans) ist
        # am 2026-08-05 geprueft und VERWORFEN worden: `max_plans` deckelt, was
        # der Planner PRO TURN neu schreiben darf, nicht den Queue-Bestand.
        # Beleg gegen die eigene Annahme — builder-reviewer (max_plans=8,
        # max_rounds=12) stand real bei 8 und 9 wartenden Plaenen
        # ("PLAN: 9 Pläne (status=PLANNED 4)", LEDGER 05.07.); eine Schwelle
        # bei 8 haette dort geplant, obwohl 3-4 der 12 Rundenslots frei waren.
        # Wer das nachmisst: die Queue-Tiefe steht in "PLAN: N Pläne", NICHT im
        # "planned=N" der Autoland-Resume-Zeile (die emittiert nur
        # dashboard-experience, max_rounds=1 — daher sieht sie nie mehr als 1
        # und liest sich faelschlich wie die globale Obergrenze).
        #
        # Nachgemessen 2026-08-05 gegen ~/.hermes/loops/*/LEDGER.md (die
        # Commit-Message von 2b3f0d3fbc nannte 16 DRY-Laeufe / ~2 h; das war der
        # Topf ALLER ertraglosen Plan-Phasen): wegen belegtem Slot liefen 5
        # Naechte ertraglos (dashboard-experience, 16./30./31.07., 01./05.08.),
        # bei Ø ~484 s Plan-Phase rund 40 min Modellzeit. Der groessere Block —
        # 12 Naechte dashboard-polish — war NICHT die Queue, sondern
        # "DRY web fehlt" und ist seit 0e92dc7f58 (ensure_frontend_deps, 04.08.)
        # geheilt: in der Nacht darauf 3 verifizierte Runden statt DRY.
        queue_full_plan_skip = (
            self.pack.type == "pipeline"
            and not skip_plan
            and self.pack.plan_skip_when_queue_full
            and self.qcount("00-planned") >= self.stop_cfg("max_rounds")
        )
        if queue_full_plan_skip:
            planned = self.qcount("00-planned")
            max_rounds = self.stop_cfg("max_rounds")
            self.ledger(
                f"PLAN übersprungen: Queue voll ({planned} ≥ max_rounds={max_rounds})"
            )
            self.ledger_event(
                phase="plan", verdict="skipped", reason="queue_full",
                planned=planned, max_rounds=max_rounds,
            )
            self.say(
                f"Queue voll ({planned} ≥ max_rounds={max_rounds}) — "
                "Planung übersprungen."
            )
        if (
            self.pack.type == "pipeline"
            and not skip_plan
            and not queue_full_plan_skip
        ):
            if not self.cmd_plan(fresh=fresh):
                return False
            fresh = False  # Worktree steht jetzt
            if self.qcount("00-planned") == 0:
                status = self.last_status()
                if self._goal_reached(status):
                    self._retire_if_goal_reached()
                    self.report()
                    return True
                if status.startswith("DRY"):
                    # Echter DRY-Kontrakt: nichts zu bauen, still-ok Ende.
                    # Zählt für die DRY-Streak-Eskalation (Serie = toter Loop).
                    self.say("Keine Pläne — nichts zu bauen.")
                    self._note_dry_end(status)
                    self.report()
                    return True
                # Anomalie (leer, TIMEOUT, sonst ohne Statuskontrakt): genau 1 Retry.
                self.ledger("PLAN-RETRY nach Anomalie")
                self.say("Plan-Anomalie (0 Pläne, kein DRY) — genau 1 Plan-Retry.")
                if not self.cmd_plan(fresh=False):
                    return False
                if self.qcount("00-planned") == 0:
                    status2 = self.last_status()
                    if self._goal_reached(status2):
                        self._retire_if_goal_reached()
                        self.report()
                        return True
                    if status2.startswith("DRY"):
                        # Auch der Plan-Retry-DRY-Ausgang zählt (beide DRY-Exits).
                        self.say("Keine Pläne — nichts zu bauen.")
                        self._note_dry_end(status2)
                        self.report()
                        return True
                    msg = (
                        f"{self.pack.name}: Plan-Phase 2× ohne Statuskontrakt "
                        f"beendet (status=[{status2}]) — Run gestoppt, "
                        "bitte Planner-Log prüfen"
                    )
                    self.say(msg)
                    self.ledger(
                        f"PLAN-STOP: Plan-Phase 2× ohne Statuskontrakt "
                        f"(status=[{status2}])"
                    )
                    self.notify(msg)
                    self.report()
                    # RuntimeError statt `return True`: der Rückgabewert hätte
                    # diesen Abbruch nicht nach außen getragen. main() rechnet
                    # `rc = 0 if night_ok or not pack.autoland else 4` — für jeden
                    # Pack ohne autoland (die Mehrheit) wäre selbst ein
                    # `return False` noch Exit 0 geworden, und die Unit hätte
                    # Erfolg gemeldet, während der Planner zweimal hintereinander
                    # seinen Statuskontrakt gebrochen hat.
                    #
                    # Die Verify-Phase macht es nebenan bereits richtig
                    # (fail-closed Revert); nur die Plan-Phase endete grün. Über
                    # RuntimeError greift der vorhandene ABBRUCH-Pfad in main()
                    # und liefert Exit 3 — vorbei am autoland-Rewrite, der genau
                    # dafür nicht gedacht ist (Befund 2026-07-28).
                    raise RuntimeError(msg)
                # Retry hat Pläne geliefert → normal weiter in cmd_run.
        # Nicht-DRY-Ende (Pläne vorhanden / skip_plan / Build-Pfad) → Streak nullen.
        # Sweep-DRY in cmd_run bleibt unberührt (kein _note_dry_end dort).
        self._reset_dry_streak()
        self.cmd_run(fresh=fresh)
        if self.autoland_active and self._autoland_pending():
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
        if self.autoland_active and self.manual_land_marker.exists():
            landing = "Landung: manuell — UI-Phasenvertrag weicht vom Auto-Land-Vertrag ab."
        elif self.autoland_active:
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
        return _land_gates(repo, base, self.pack.land_gates)

    def _push(self, repo: Path) -> tuple[bool, str]:
        """Push NUR piet-fork, nur ff (kein --force). Seam für Tests."""
        res = self.git("push", self.pack.land_remote, self.pack.base_branch, cwd=repo)
        return res.returncode == 0, (res.stderr.strip() or res.stdout.strip())

    def _safe_land_rollback(
        self, repo: Path, base: str, expected_head: str
    ) -> tuple[bool, str]:
        """Rollt nur unseren unveraenderten ff-Merge zurueck.

        Ein fremder Commit auf main zwischen Merge/Gates/Push darf niemals durch
        den Loop-Fehlerpfad verworfen werden. In diesem Fall bleibt alles stehen
        und die Landung wird als manuell zu klaeren markiert.
        """
        current = self.git("rev-parse", self.pack.base_branch, cwd=repo).stdout.strip()
        if current != expected_head:
            return False, (
                f"main ist parallel weitergelaufen ({current[:9]} statt "
                f"{expected_head[:9]}); kein automatischer Reset"
            )
        reset = self.git("reset", "--keep", base, cwd=repo)
        if reset.returncode != 0:
            return False, f"reset --keep fehlgeschlagen: {reset.stderr.strip()}"
        return True, f"rollback auf {base[:9]}"

    def _auto_rebase(
        self, repo: Path, *, collapse_net_zero: bool = False
    ) -> tuple[bool, str]:
        """Loop-Branch im Pack-Worktree auf main rebasen — nur wenn sicher.

        Sicher heißt: Worktree existiert, steht auf dem Loop-Branch, ist clean,
        und der Rebase läuft konfliktfrei durch. Sonst (False, Grund) und der
        Branch bleibt unverändert (rebase --abort). Der alte Tip bleibt bei
        Erfolg als Tag loop-rebase/<pack>/<ts> erreichbar (Rollback-Anker,
        gleiche Konvention wie loop-land/…). NIEMALS ensure_wt(fresh=True)
        hier — das würde den Branch auf main resetten.

        Beim BASE-REFRESH darf ``collapse_net_zero`` zusätzlich eine reine
        Build+Revert-Historie auf die aktuelle Basis kollabieren. Das ist nur
        erlaubt, wenn keine laufende oder verifizierte Queue-Arbeit existiert
        und ``base...branch`` wirklich leer ist. Der Anker-Tag erhält den alten
        Tip; ``reset --keep`` ist nach dem Clean-Guard ausreichend und vermeidet
        einen destruktiven Hard-Reset.
        """
        if not self.wt.is_dir():
            return False, f"Pack-Worktree fehlt ({self.wt}) — manuell rebasen"
        head = self.git("rev-parse", "--abbrev-ref", "HEAD", cwd=self.wt).stdout.strip()
        if head != self.pack.branch:
            return False, f"Worktree steht auf {head!r}, nicht {self.pack.branch!r}"
        if self.git("status", "--porcelain", cwd=self.wt).stdout.strip():
            return False, "Pack-Worktree ist dirty — manuell klären"
        net_zero = False
        if (
            collapse_net_zero
            and self.qcount("10-building") == 0
            and self.qcount("20-verified") == 0
        ):
            ahead = self.git(
                "rev-list", "--count",
                f"{self.pack.base_branch}..{self.pack.branch}", cwd=repo,
            )
            try:
                has_branch_commits = int(ahead.stdout.strip()) > 0
            except ValueError:
                has_branch_commits = False
            if ahead.returncode == 0 and has_branch_commits:
                net = self.git(
                    "diff", "--quiet",
                    f"{self.pack.base_branch}...{self.pack.branch}", cwd=repo,
                )
                net_zero = net.returncode == 0
        anchor = f"loop-rebase/{self.pack.name}/{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        if self.git("tag", anchor, self.pack.branch, cwd=repo).returncode != 0:
            return False, "Rebase-Anker-Tag ließ sich nicht setzen"
        if net_zero:
            reset = self.git("reset", "--keep", self.pack.base_branch, cwd=self.wt)
            if reset.returncode != 0:
                self.git("tag", "-d", anchor, cwd=repo)
                return False, (
                    "Netto-null-Kollaps fehlgeschlagen — Branch unverändert: "
                    f"{reset.stderr.strip()}"
                )
            return True, (
                f"netto-null kollabiert auf {self.pack.base_branch} "
                f"(Anker {anchor})"
            )
        res = self.git("rebase", self.pack.base_branch, cwd=self.wt)
        if res.returncode != 0:
            self.git("rebase", "--abort", cwd=self.wt)
            self.git("tag", "-d", anchor, cwd=repo)
            tail = "\n".join(((res.stdout or "") + (res.stderr or "")).splitlines()[-5:])
            return False, f"Auto-Rebase-Konflikt — manuell rebasen:\n{tail}"
        return True, f"auto-rebase auf main (Anker {anchor})"

    def cmd_land(self, push: bool = True, require_push: bool = False) -> bool:
        """Landung — läuft komplett unter dem Repo-Lock (siehe ``repo_locked``):
        der geteilte Live-Checkout ``self.pack.repo`` wird von der
        Sauberkeits-Prüfung bis zu Gates/Push/Rollback exklusiv gebraucht.

        Der Lock wird WARTEND genommen. Nicht-blockierend war er ein
        Nachtbetriebs-Defekt: eine fremde Landung hält ihn minutenlang (die
        Landungs-Gates laufen im Live-Checkout), der Einmal-Retry nach 1 s
        greift dagegen nie, und der RuntimeError lief ungefangen bis
        ``main()`` durch — Exit 3, ohne Ledger-Zeile und ohne Notify. Der
        verifizierte Commit blieb bis zur nächsten Nacht liegen, und morgens
        stand nichts darüber im Ledger.
        """
        try:
            with self.repo_locked(blocking=True, timeout=LAND_LOCK_WAIT_S):
                return self._cmd_land_locked(push=push, require_push=require_push)
        except RuntimeError as exc:
            # Fail-closed, aber laut: nichts wurde angefasst (der Lock wurde nie
            # genommen), und der Operator sieht den Grund im Ledger.
            self.say(f"ABBRUCH: Repo-Lock nicht freigeworden — {exc}")
            self.ledger(f"LAND verschoben: {exc} (fremde Landung läuft; nichts verändert)")
            self.notify(
                f"{self.pack.name}: Landung verschoben — Repo-Lock nach "
                f"{LAND_LOCK_WAIT_S / 60:.0f} min noch belegt."
            )
            self.ledger_event(phase="land", verdict="blocked", fail_kind="repo_lock_timeout",
                               reason=str(exc))
            return False

    def _cmd_land_locked(self, push: bool = True, require_push: bool = False) -> bool:
        repo = self.pack.repo
        ahead = self.git(
            "rev-list", "--count", f"{self.pack.base_branch}..{self.pack.branch}", cwd=repo
        ).stdout.strip()
        if not ahead or ahead == "0":
            self.say("Nichts zu landen (Branch ist nicht vor main).")
            return True
        if self.qcount("10-building") > 0:
            self.say("ABBRUCH: 10-building/ ist nicht leer — UNVERIFIZIERTE Arbeit zuerst klären.")
            return False
        cur = self.git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo).stdout.strip()
        if cur != self.pack.base_branch:
            self.say(f"ABBRUCH: Live-Checkout steht auf {cur!r}, nicht auf main.")
            return False
        dirty = self.git("status", "--porcelain", cwd=repo).stdout.strip()
        if dirty:
            self.say("ABBRUCH: Live-Checkout ist dirty (parallele Arbeit?) — Landung braucht einen sauberen Baum:\n"
                     + "\n".join(dirty.splitlines()[:10]))
            return False
        base = self.git("rev-parse", self.pack.base_branch, cwd=repo).stdout.strip()
        tag = f"loop-land/{self.pack.name}/{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        if self.git("tag", tag, self.pack.base_branch, cwd=repo).returncode != 0:
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
        merged_head = self.git("rev-parse", self.pack.base_branch, cwd=repo).stdout.strip()
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
        for line in report.splitlines():
            if line.startswith("Lastartefakt:"):
                self.ledger(f"LAND {line}")
        pushed = ""
        if push and self.pack.land_push:
            current = self.git("rev-parse", self.pack.base_branch, cwd=repo).stdout.strip()
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
            current = self.git("rev-parse", self.pack.base_branch, cwd=repo).stdout.strip()
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
        new_main = self.git(
            "rev-parse", "--short", self.pack.base_branch, cwd=repo
        ).stdout.strip()
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


# ── systemd-Cgroup-Eskalation (Nachtlauf-Vorfall 2026-07-13) ────────────────
# Ein Loop-Lauf, als Kind des Codex-Remote-Control-Daemons gestartet, erbte
# dessen Härtung (codex-remote.service: TasksMax=512/MemoryMax=6G/
# OOMPolicy=kill — bewusst, NICHT ändern) für alles, was der Loop spawnt
# (next dev, headless chromium, vitest-Worker) → `pthread_create: resource
# temporarily unavailable`, Worker-Forks starben, Prozesse wurden gekillt.
# `hermes-loop@<pack>.service`-Units haben TasksMax=18633 und keinen
# Memory-Cap. Fix: beim echten CLI-Start in einen eigenen transienten
# systemd-Scope re-execen, wenn wir in einem FREMDEN *.service-Cgroup laufen.
LOOP_SCOPE_MARKER_ENV = "HERMES_LOOP_SCOPED"
LOOP_SCOPE_OPT_OUT_ENV = "HERMES_LOOP_NO_SCOPE"


def _cgroup_leaf(cgroup_text: str) -> str:
    """Letztes Pfadsegment der `0::`-Zeile (cgroup v2, single hierarchy)."""
    for line in cgroup_text.splitlines():
        line = line.strip()
        if line.startswith("0::"):
            path = line[len("0::"):]
            return path.rstrip("/").rsplit("/", 1)[-1]
    return ""


def user_bus_socket_path(environ: Mapping[str, str], *, euid: int) -> str | None:
    """Socket-Pfad des User-Bus, den `systemd-run --user` zum Re-Exec braucht.

    Reihenfolge: `DBUS_SESSION_BUS_ADDRESS` (`unix:path=…`), dann
    `$XDG_RUNTIME_DIR/bus`, dann der systemd-Default `/run/user/<euid>/bus`
    (genau der Fall, wenn ein fremder Service ohne Bus-Umgebung startet).
    `unix:abstract=` ist auf dem Dateisystem nicht prüfbar und liefert
    bewusst None — der Caller bleibt dann lieber inline, statt blind zu
    exec'en (Vorfall 2026-07-27: land unter hermes-dashboard.service starb
    mit `Failed to connect to bus: No medium found`, weil os.execvp den
    Prozess bereits ersetzt hatte).
    """
    address = environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    for part in address.split(";"):
        if part.startswith("unix:path="):
            # Key/Value-Paare einer Adresse sind kommagetrennt (`…,guid=…`).
            return part[len("unix:path="):].split(",", 1)[0]
    if address:
        # Adresse gesetzt, aber ohne prüfbaren unix:path (z. B. abstract) —
        # nicht verifizierbar, Caller bleibt konservativ inline.
        return None
    runtime_dir = environ.get("XDG_RUNTIME_DIR")
    if runtime_dir:
        return os.path.join(runtime_dir, "bus")
    return os.path.join("/run/user", str(euid), "bus")


def should_reexec_into_scope(
    cgroup_text: str,
    *,
    marker_set: bool,
    systemd_run_available: bool,
    bus_socket_available: bool,
) -> bool:
    """Reine Entscheidung — kein systemd-Aufruf, kein echtes /proc, testbar.

    Inline (False) bleibt es bei: Marker schon gesetzt (Rekursionsschutz),
    kein `systemd-run` auf dem PATH, Cgroup-Blatt ist ein `.scope`
    (Terminal/tmux) oder die Slice-Wurzel, das Blatt IST bereits
    `hermes-loop@*.service`, oder die User-Bus-Socket ist nicht verifizierbar
    (systemd-run würde nach dem unumkehrbaren os.execvp erst scheitern).
    Nur ein FREMDES `*.service`-Blatt mit erreichbarem Bus löst den
    Re-Exec aus.
    """
    if marker_set or not systemd_run_available or not bus_socket_available:
        return False
    leaf = _cgroup_leaf(cgroup_text)
    if not leaf.endswith(".service"):
        return False
    if leaf.startswith("hermes-loop@"):
        return False
    return True


def build_scope_command(systemd_run: str, orig_argv: list[str]) -> list[str]:
    """Baut den systemd-run-Aufruf aus `sys.orig_argv` — NICHT aus `sys.argv`.

    `sys.argv[0]` ist bei `python -m loops.runner` der aufgelöste Dateipfad
    (…/loops/runner.py). Ein Re-Exec als Script setzt `sys.path[0]` auf
    …/loops statt aufs Repo-Root — `from loops import engines` (Zeile 49)
    stirbt dann mit ModuleNotFoundError, weil `loops/` bewusst nicht
    paketiert ist (siehe loops/shim.sh). `sys.orig_argv` erhält die
    ursprüngliche `-m`-Form und damit den Modul-Suchpfad.
    """
    return [
        systemd_run, "--user", "--scope", "--quiet", "--collect",
        f"--setenv={LOOP_SCOPE_MARKER_ENV}=1",
        *orig_argv,
    ]


def _reexec_into_own_scope() -> None:
    """CLI-Entrypoint-Hook (NUR aus dem `__main__`-Guard aufrufen — niemals
    beim Import, das würde `main()` aus Tests heraus kapern: Tests rufen
    `main(argv)` direkt in-process auf, ohne über diesen Guard zu laufen).

    Escaped ein fremdes Service-Cgroup via `systemd-run --user --scope`
    (Umgebung + Exitcode + stdout/stderr bleiben erhalten, os.execvp ersetzt
    den aktuellen Prozess); sonst No-Op. Ohne verifizierbare User-Bus-Socket
    bleibt der Lauf bewusst INLINE mit Warnung: nach dem unumkehrbaren
    os.execvp würde ein bus-loses `systemd-run` erst scheitern und den
    gesamten Loop-Aufruf mitnehmen (land-Vorfall 2026-07-27 unter
    hermes-dashboard.service: `Failed to connect to bus: No medium found`).
    """
    if os.environ.get(LOOP_SCOPE_OPT_OUT_ENV):
        return
    marker_set = bool(os.environ.get(LOOP_SCOPE_MARKER_ENV))
    systemd_run = shutil.which("systemd-run")
    try:
        cgroup_text = Path("/proc/self/cgroup").read_text(encoding="utf-8")
    except OSError:
        return
    bus_socket = user_bus_socket_path(os.environ, euid=os.geteuid())
    if bus_socket is not None and not os.path.exists(bus_socket):
        # Gesetzte, aber tote Bus-Adresse (stale env, z. B. alter
        # DBUS_SESSION_BUS_ADDRESS aus einem Vorgänger-Service): auf den
        # systemd-Default durchfallen, wenn dessen Socket lebt.
        default_socket = os.path.join("/run/user", str(os.geteuid()), "bus")
        if bus_socket != default_socket and os.path.exists(default_socket):
            bus_socket = default_socket
    bus_socket_available = bus_socket is not None and os.path.exists(bus_socket)
    if not should_reexec_into_scope(
        cgroup_text,
        marker_set=marker_set,
        systemd_run_available=bool(systemd_run),
        bus_socket_available=bus_socket_available,
    ):
        if should_reexec_into_scope(
            cgroup_text,
            marker_set=marker_set,
            systemd_run_available=bool(systemd_run),
            bus_socket_available=True,
        ):
            logger.warning(
                "[loops] foreign service cgroup %s, aber keine User-Bus-Socket "
                "(socket=%s) — bleibe inline statt fehlgeschlagenem "
                "systemd-run-Re-Exec; Task-/Memory-Caps des fremden Services "
                "gelten weiter (HERMES_LOOP_NO_SCOPE=1 zum Quittieren)",
                _cgroup_leaf(cgroup_text),
                bus_socket,
            )
        return
    leaf = _cgroup_leaf(cgroup_text)
    logger.warning(
        "[loops] re-exec into own systemd scope (escaping %s cgroup: TasksMax/Memory-Cap)",
        leaf,
    )
    # Bus-Umgebung für den Child-Prozess reparieren: fremde Services starten
    # oft ohne DBUS_SESSION_BUS_ADDRESS/XDG_RUNTIME_DIR, oder mit einer
    # STALEN Adresse, die nicht auf die verifizierte Socket zeigt. Der Child
    # erbt os.environ — XDG_RUNTIME_DIR auf das Socket-Verzeichnis setzen
    # und eine nicht passende DBUS-Adresse entfernen, damit `systemd-run
    # --user` die verifizierte Socket benutzt.
    if bus_socket is not None:
        if os.environ.get("XDG_RUNTIME_DIR") != os.path.dirname(bus_socket):
            os.environ["XDG_RUNTIME_DIR"] = os.path.dirname(bus_socket)
        address = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
        if address:
            # Exakter Pfadvergleich statt Substring: eine Adresse wie
            # unix:path=/run/user/1000/bus_1234 enthält die verifizierte
            # Socket als Präfix und würde sonst fälschlich behalten.
            env_socket = user_bus_socket_path(
                {"DBUS_SESSION_BUS_ADDRESS": address}, euid=os.geteuid()
            )
            if env_socket != bus_socket:
                os.environ.pop("DBUS_SESSION_BUS_ADDRESS", None)
    cmd = build_scope_command(systemd_run, sys.orig_argv)
    os.execvp(cmd[0], cmd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Loop-Runner (pipeline|sweep|deterministic Packs)"
    )
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
        deterministic = pack.type == "deterministic"
        lock_timeout = 0.0
        if deterministic:
            run_cfg = pack.phases["run"]
            if not isinstance(run_cfg, DeterministicPhaseCfg):
                raise RuntimeError(
                    f"Pack {pack.name}: deterministische run-Phase fehlt"
                )
            lock_timeout = float(run_cfg.timeout)
        with runner.locked(blocking=deterministic, timeout=lock_timeout):
            runner.say(f"START cmd={args.cmd} {datetime.now().strftime('%F %H:%M:%S')}")
            rc = 0
            if args.cmd == "plan":
                # Rückgabewert NICHT verwerfen: `cmd_plan` meldet mit False einen
                # abgebrochenen Planner (z.B. Usage-Limit). Der Wert wurde hier
                # bisher weggeworfen, `rc` blieb 0 — ein gescheiterter Planlauf
                # meldete Erfolg. Gleiche Konvention wie `land` darunter.
                rc = 0 if runner.cmd_plan(fresh=args.fresh) else 4
            elif args.cmd == "run":
                rc = runner.cmd_run(fresh=args.fresh)
            elif args.cmd == "land":
                rc = 0 if runner.cmd_land(push=not args.no_push) else 4
            elif deterministic:
                rc = runner.cmd_run(fresh=args.fresh)
            else:
                night_ok = runner.cmd_night(fresh=args.fresh, skip_plan=args.skip_plan)
                # Jeder explizite Fehlausgang erreicht die Unit — unabhängig von
                # Autoland. Erfolgreiche Review- und Autoland-Packs bleiben Exit 0;
                # Autoland-Fehlausgänge behalten ihren bisherigen Exit 4.
                rc = 0 if night_ok else 4
            runner.say(f"ENDE cmd={args.cmd}")
            if rc:
                return rc
    except RuntimeError as exc:
        print(f"ABBRUCH: {exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    _reexec_into_own_scope()
    sys.exit(main())
