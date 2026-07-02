"""Loop-Runner — führt Loop-Packs aus (Archetypen: pipeline | sweep).

CLI:
    python -m loops.runner --pack <name> --cmd plan|run|night|status
                           [--state-root PFAD] [--fresh] [--skip-plan]

Ein Pack (loops/packs/<name>/) beschreibt in pack.yaml WAS läuft (Phasen mit
Engine/Modell/Timeout/Prompt, Stop-Kriterien); der Runner liefert das WIE:
Worktree-Isolation, Datei-Queue, Ledger, deterministische Disposition
(Retry/Revert/Bounce), Locks, Usage-Limit-Stop, Discord-Notify.

Laufzeit-State: ~/.hermes/loops/<pack>/ (Override: --state-root, für Tests).
Der Runner pusht/deployt/merged NIE — Landung ist ein bewusster Morgen-Schritt.

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
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

from loops import engines

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKS_DIR = REPO_ROOT / "loops" / "packs"
# Werkstatt-Substrat (v2.1): vom Operator/Dashboard angelegte Packs leben im State,
# nie im Repo — Browser-Edits dürfen den Live-Checkout nicht dirty machen.
CUSTOM_PACKS_DIR = Path("~/.hermes/loops/packs-custom").expanduser()
DEFAULT_STATE_ROOT = Path("~/.hermes/loops").expanduser()
NOTIFY_SCRIPT = Path("~/.hermes/scripts/discord-notify.py").expanduser()

QUEUE_STAGES = ("00-planned", "10-building", "20-verified", "30-landed", "90-bounced")
DEFAULT_STOP = {"max_rounds": 12, "max_hours": 7, "fail_streak": 2, "dry_rounds": 2}

PHASES_BY_TYPE = {"pipeline": ("plan", "build", "verify"), "sweep": ("round",)}

RETRY_RE = re.compile(r"^retry:\s*(\d+)", re.MULTILINE)


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
    autoland: bool = False  # v1: hart False (Design-Entscheid #8)

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

    autoland = bool(raw.get("autoland", False))
    if autoland:
        # Design-Entscheid #8: v1 ist Review-only; Auto-Land kommt in v2 mit Schutzschienen.
        print(f"[{name}] WARN: autoland ist in v1 deaktiviert — erzwinge false", file=sys.stderr)
        autoland = False

    params = {str(k): str(v) for k, v in (raw.get("params") or {}).items()}
    notify = {str(k): str(v) for k, v in (raw.get("notify") or {}).items()}

    return Pack(
        name=name, type=ptype, repo=Path(repo).expanduser(), pack_dir=pack_dir,
        phases=phases, stop=stop, description=str(raw.get("description", "")),
        stability=str(raw.get("stability", "experimental")), notify=notify,
        params=params, autoland=autoland,
    )


# ── reine Helfer (test-direkt) ───────────────────────────────────────────────

def parse_retry(plan_text: str) -> int:
    m = RETRY_RE.search(plan_text)
    return int(m.group(1)) if m else 0


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
        self.overrides = parse_overrides(self.state / "overrides.env")
        self.phase_secs: dict[str, int] = {}

    # ── Infrastruktur ──
    def say(self, msg: str) -> None:
        print(f"[{self.pack.name}] {msg}", flush=True)

    def ledger(self, msg: str) -> None:
        stamp = datetime.now().strftime("%F %H:%M")
        with self.ledger_path.open("a", encoding="utf-8") as fh:
            fh.write(f"- {stamp} {msg}\n")

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
        except Exception:  # noqa: BLE001 — Notify ist nie lauf-kritisch
            pass

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

    def run_phase(self, phase: str, **extra: str) -> engines.EngineResult:
        cfg = self.phase_cfg(phase)
        self.say(f"── Phase {phase} (engine={cfg.engine}, model={cfg.model}, timeout={cfg.timeout}s)")
        self.status_path.write_text("", encoding="utf-8")
        prompt = self.render_prompt(phase, **extra)
        started = time.time()
        started_iso = datetime.now().strftime("%FT%T")
        self._heartbeat({"phase": phase, "engine": cfg.engine, "model": cfg.model,
                         "started_at": started_iso, "timeout": cfg.timeout})
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

    # ── Queue-Disposition (pipeline) ──
    def qcount(self, stage: str) -> int:
        stage_dir = self.queue / stage
        return len(list(stage_dir.glob("*.md"))) if stage_dir.is_dir() else 0

    def pick_plan(self) -> Path | None:
        plans = sorted((self.queue / "00-planned").glob("*.md"))
        return plans[0] if plans else None

    def handle_fail(self, plan: Path, reason: str) -> str:
        """1 Retry (mit Feedback in der Plan-Datei), danach 90-bounced."""
        append_section(plan, "Loop-Fail", reason)
        if parse_retry(plan.read_text(encoding="utf-8")) >= 1:
            target = self.queue / "90-bounced" / plan.name
            if target.exists():  # Namens-Wiederverwendung: alte Evidenz nicht überschreiben
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                target = target.with_name(f"{target.stem}.{stamp}.md")
            plan.rename(target)
            self.ledger(f"bounced: {target.name} ({reason})")
            return "bounced"
        bump_retry(plan)
        plan.rename(self.queue / "00-planned" / plan.name)
        return "retry"

    # ── Kommandos ──
    def cmd_plan(self, fresh: bool = False) -> bool:
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
        n = self.qcount("00-planned")
        status = "TIMEOUT" if result.timed_out else self.last_status()
        self.say(f"Planner fertig: status=[{status}], {n} Pläne in der Queue")
        self.ledger(f"PLAN: {n} Pläne (status={status})")
        self.notify(f"🌀 {self.pack.name} PLAN: {n} Pläne in der Queue (status={status})")
        return True

    def cmd_run(self, fresh: bool = False) -> None:
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

            build = self.run_phase("build", PLAN_PATH=str(building))
            if build.usage_limit:
                # Invariante „Branch = nur verified-oder-reverted" auch hier halten:
                # existiert schon ein Commit, MUSS er als UNVERIFIED ausgewiesen werden
                # (Plan bleibt in 10-building); ohne Commit zurück in die Queue.
                if self.rev_parse() != prehead:
                    self.say("Usage-Limit im Build — Commit vorhanden, bleibt UNVERIFIZIERT (Plan in 10-building/).")
                    self.ledger(f"R{rnd} ⚠️ {building.name} Commit vorhanden aber UNVERIFIED (usage-limit im Build)")
                    self.notify(f"{self.pack.name}: Usage-Limit im Build — {building.name} unverifiziert, gestoppt.")
                else:
                    building.rename(self.queue / "00-planned" / building.name)
                    self.say("Usage-Limit — Stop.")
                    self.ledger(f"R{rnd} ⏸ {building.name} zurück in die Queue (usage-limit, kein Commit)")
                    self.notify(f"{self.pack.name}: Usage-Limit in Runde {rnd} — gestoppt ({verified} verified).")
                break
            status = "TIMEOUT" if build.timed_out else self.last_status()
            if self.rev_parse() == prehead or not status.startswith("BUILT"):
                self.say(f"BUILD_FAIL [{status}]")
                if not self.guard_clean():
                    break
                if self.rev_parse() != prehead and not self.revert_range(prehead):
                    break
                self.handle_fail(building, f"build: {status or 'kein Status'}")
                self.ledger(f"R{rnd} ❌ {building.name} build-fail: {status or '?'}")
                fails += 1
                if fails >= self.stop_cfg("fail_streak"):
                    self.say("Fail-Streak — Stop für Human-Review.")
                    self.notify(f"{self.pack.name}: {fails}× Fail in Folge — gestoppt.")
                    break
                continue

            verify = self.run_phase("verify", PLAN_PATH=str(building), RANGE=f"{prehead}..HEAD")
            if verify.usage_limit:
                self.say("Usage-Limit im Verifier — Commit bleibt UNVERIFIZIERT (Plan in 10-building/).")
                self.ledger(f"R{rnd} ⚠️ {building.name} BUILT aber UNVERIFIED (usage-limit)")
                self.notify(f"{self.pack.name}: Usage-Limit im Verifier — {building.name} unverifiziert, gestoppt.")
                break
            status = "TIMEOUT" if verify.timed_out else self.last_status()
            if not self.guard_clean():
                break
            if status.startswith("PASS"):
                building.rename(self.queue / "20-verified" / building.name)
                verified += 1
                fails = 0
                sha = self.rev_parse()[:9]
                self.ledger(f"R{rnd} ✅ {building.name} verified ({sha}) [{self._secs('build', 'verify')}]")
                self.notify(f"✅ {self.pack.name} R{rnd}: {building.name} verified ({sha}) — {verified} gesamt")
            else:
                self.say(f"VERIFY_FAIL [{status}] — revert + retry/bounce")
                if not self.revert_range(prehead):
                    self.notify(f"{self.pack.name}: Revert fehlgeschlagen bei {building.name} — gestoppt.")
                    break
                self.handle_fail(building, f"verify: {status}")
                self.ledger(f"R{rnd} ❌ {building.name} verify-fail: {status} (reverted)")
                self.notify(f"❌ {self.pack.name} R{rnd}: {building.name} verify-fail — {status}")
                fails += 1
                if fails >= self.stop_cfg("fail_streak"):
                    self.say("Fail-Streak — Stop für Human-Review.")
                    self.notify(f"{self.pack.name}: {fails}× Fail in Folge — gestoppt.")
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
                break
            status = "TIMEOUT" if result.timed_out else self.last_status()
            self.ledger(f"R{rnd} sweep status={status or '?'} [{self._secs('round')}]")
            if status.startswith("DRY"):
                dry, blocked = dry + 1, 0
            elif status.startswith("BLOCKED") or status == "TIMEOUT":
                blocked, dry = blocked + 1, 0
            else:
                dry = blocked = 0
            if dry >= self.stop_cfg("dry_rounds"):
                self.say("DRY-Konvergenz — Stop.")
                break
            if blocked >= self.stop_cfg("fail_streak"):
                self.say("Blocked-Streak — Stop für Human-Review.")
                self.notify(f"{self.pack.name}: {blocked}× BLOCKED in Folge — gestoppt.")
                break

    def cmd_night(self, fresh: bool = False, skip_plan: bool = False) -> None:
        if self.pack.type == "pipeline" and not skip_plan:
            if not self.cmd_plan(fresh=fresh):
                return
            fresh = False  # Worktree steht jetzt
            if self.qcount("00-planned") == 0:
                self.say("Keine Pläne — nichts zu bauen.")
                self.report()
                return
        self.cmd_run(fresh=fresh)

    def report(self) -> None:
        commits = self.git("log", "--oneline", f"main..{self.pack.branch}").stdout.strip()
        counts = " · ".join(f"{self.qcount(s)} {s[3:]}" for s in QUEUE_STAGES) \
            if self.pack.type == "pipeline" else "(sweep)"
        msg = (
            f"🌙 {self.pack.name} Bilanz: {counts}\n"
            f"Commits (main..{self.pack.branch}):\n{commits or '—'}\n"
            f"Landung: Morgen-Review (Design-Doc → Landung)."
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

    def cmd_land(self, push: bool = True) -> bool:
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
        merge = self.git("merge", "--ff-only", self.pack.branch, cwd=repo)
        if merge.returncode != 0:
            self.git("tag", "-d", tag, cwd=repo)
            self.say(f"ABBRUCH: kein ff-Merge möglich (main ist weitergelaufen) — KEIN Auto-Rebase.\n{merge.stderr.strip()}")
            self.ledger(f"LAND abgebrochen: nicht ff-fähig (base {base[:9]})")
            return False
        ok, report = self._land_gates(repo, base)
        if not ok:
            # Baum war sauber, Merge war reiner ff → --keep rollt den Ref zurück,
            # ohne irgendetwas zu verwerfen (verweigert sonst; bewusst NICHT --hard).
            self.git("reset", "--keep", base, cwd=repo)
            self.say(f"LAND zurückgerollt auf {base[:9]} — Gates rot:\n{report}")
            self.ledger(f"LAND rollback (Anker {tag}): {report.splitlines()[0]}")
            self.notify(f"⛔ {self.pack.name} LAND: Gates rot → rollback auf {base[:9]} (Anker {tag}).")
            return False
        pushed = ""
        if push:
            p_ok, p_msg = self._push(repo)
            pushed = " · piet-fork gepusht" if p_ok else f" · PUSH FEHLGESCHLAGEN (Merge bleibt lokal): {p_msg}"
        # Verdaute Pläne archivieren + Pack frisch von neuem main ziehen
        landed_dir = self.queue / "30-landed"
        landed_dir.mkdir(parents=True, exist_ok=True)
        moved = 0
        for plan in sorted((self.queue / "20-verified").glob("*.md")):
            plan.rename(landed_dir / plan.name)
            moved += 1
        self.ensure_wt(fresh=True)
        new_main = self.git("rev-parse", "--short", "main", cwd=repo).stdout.strip()
        self.ledger(f"LAND ✅ {ahead} Commits → main {new_main} (Anker {tag}, {moved} Pläne archiviert){pushed}")
        self.say(f"LAND ✅ main={new_main} · Gates: {report}{pushed}")
        self.notify(f"🛬 {self.pack.name} LAND: {ahead} Commits auf main ({new_main}); {report}{pushed}")
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
                runner.cmd_night(fresh=args.fresh, skip_plan=args.skip_plan)
            runner.say(f"ENDE cmd={args.cmd}")
            if rc:
                return rc
    except RuntimeError as exc:
        print(f"ABBRUCH: {exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
