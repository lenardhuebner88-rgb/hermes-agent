"""Engine: OpenAI Codex CLI headless (ChatGPT-Abo, `codex exec`).

Belegzeilen `codex exec --help` (codex-cli 0.142.5, 2026-07-02 live geprüft):
    -m, --model <MODEL>
            Model the agent should use
    -s, --sandbox <SANDBOX_MODE>
            Select the sandbox policy to use when executing model-generated shell commands
            [possible values: read-only, workspace-write, danger-full-access]
        --dangerously-bypass-approvals-and-sandbox
            Skip all confirmation prompts and execute commands without sandboxing. EXTREMELY
            DANGEROUS. Intended solely for running in environments that are externally sandboxed

Kein `--full-auto` — Source-Beleg (`codex-rs/exec/src/cli.rs`, opensrc-Cache
`openai/codex@main`): das Flag ist ein "Legacy compatibility trap for the removed
`--full-auto` flag" und gibt beim Parsen die Warnung
    "warning: `--full-auto` is deprecated; use `--sandbox workspace-write` instead."
aus. Deshalb hier direkt `--sandbox workspace-write` statt des toten Flags.

Approval-Policy: `codex exec` (Source-Beleg `codex-rs/exec/src/lib.rs:411`, Kommentar
"Default to never ask for approvals in headless mode") setzt intern immer
`approval_policy = Never` — headless fragt nie nach Zustimmung; der Sandbox-Modus ist
der einzige verbleibende Freiheitsgrad. `--sandbox workspace-write` ist damit das
Äquivalent zu Claudes `--permission-mode bypassPermissions`, ohne auf
`danger-full-access` (Netzwerk + Dateisystem uneingeschränkt) zurückzugreifen.

2026-07-05 live geprüft (Nachtlauf builder-reviewer): `--sandbox workspace-write`
begrenzte Schreibzugriffe auf cwd (+/tmp) und produzierte drei reale Fehlermodi:
    (a) `git add`/`git commit` schlug fehl — der gitdir eines Git-Worktrees liegt
        unter `.git/worktrees/…` des Haupt-Repos, außerhalb von cwd
        (`index.lock`: EROFS).
    (b) der prompt-vorgeschriebene Status-Write nach `{{STATE_DIR}}/last-status`
        liegt außerhalb von cwd → der Runner sah einen leeren Status
        ("build-fail: ?").
    (c) Gate-Tests brauchen tmux-Sockets und Loopback-Binds — blockiert.
Fix: `--sandbox danger-full-access`. Das ist Parität zu Claudes
`--permission-mode bypassPermissions` (`loops/engines/claude_cli.py`) — die
Loop-Isolation kommt vom dedizierten Worktree des Runners plus den
Revert-/Land-Rails, nicht vom Engine-Sandbox.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from . import EngineResult, detect_usage_limit, register

CODEX_BIN = os.environ.get("CODEX_BIN", "codex")
_TOKENS_USED_RE = re.compile(r"tokens used\s*\n\s*([\d,]+)", re.IGNORECASE)

# `codex exec` hat KEIN eigenes --effort-Flag; der Reasoning-Effort geht über den
# generischen Config-Override `-c key=value`.
#
# Das Set stammt vom ENDPOINT, nicht aus dem Rust-Enum. Das Enum in
# codex-rs/protocol/src/openai_models.rs:40-64 führt zusätzlich `minimal` —
# aber der Codex-Endpoint lehnt das für die aktuellen Modelle ab (live geprüft
# 2026-07-28, `-c model_reasoning_effort=minimal` → HTTP 400):
#   "Unsupported value: 'minimal' is not supported with the 'gpt-5.6-sol-…'
#    model. Supported values are: 'none', 'low', 'medium', 'high', and 'xhigh'."
# Das Enum beschreibt, was der Client SERIALISIEREN kann; maßgeblich ist, was
# der Endpoint ANNIMMT. Wer `minimal` wieder aufnimmt, baut ein UI-Angebot,
# das zur Laufzeit mit 400 stirbt.
#
# Dieser Override ist überhaupt nötig, weil der Host in ~/.codex/config.toml
# global `model_reasoning_effort = "xhigh"` setzt: OHNE das Flag lief JEDE
# Codex-Phase auf xhigh — unabhängig davon, was Pack oder Dashboard anzeigten.
CODEX_EFFORT_LEVELS = ("none", "low", "medium", "high", "xhigh")


@register("codex", effort_levels=CODEX_EFFORT_LEVELS)
def run(
    model: str,
    prompt: str,
    cwd: Path,
    timeout_s: int,
    effort: str | None = None,
) -> EngineResult:
    cmd = [
        CODEX_BIN,
        "exec",
        "--model",
        model,
        "--sandbox",
        "danger-full-access",
    ]
    if effort:
        cmd += ["-c", f"model_reasoning_effort={effort}"]
    cmd.append(prompt)
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        out = _decode(exc.stdout) + _decode(exc.stderr)
        return EngineResult(
            rc=124, output=out, usage_limit=detect_usage_limit(out), timed_out=True
        )
    out = (proc.stdout or "") + (proc.stderr or "")
    token_match = _TOKENS_USED_RE.search(out)
    total_tokens = int(token_match.group(1).replace(",", "")) if token_match else None
    return EngineResult(
        rc=proc.returncode, output=out, usage_limit=detect_usage_limit(out), total_tokens=total_tokens
    )


def _decode(raw: bytes | str | None) -> str:
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return raw
