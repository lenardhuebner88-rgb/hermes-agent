"""Engine: official Grok Build CLI bound to the SuperGrok subscription.

The subscription CLI exposes the product slot ``grok-build`` rather than the
API model id ``grok-4.5``.  xAI documents Grok Build as powered by Grok 4.5, so
the adapter keeps the operator-facing model id and makes that alias explicit at
the transport boundary.  This path does not use an API key or OpenRouter.
"""
from __future__ import annotations

import os
import json
import subprocess
from pathlib import Path
from urllib.parse import quote

from . import EngineResult, detect_usage_limit, register

GROK_BIN = os.environ.get("GROK_BIN", "/home/piet/.npm-global/bin/grok")
GROK_HOME = Path(os.environ.get("GROK_HOME", Path.home() / ".grok"))

_CLI_MODEL_ALIASES = {"grok-4.5": "grok-build"}


@register("xai")
def run(model: str, prompt: str, cwd: Path, timeout_s: int) -> EngineResult:
    cli_model = _CLI_MODEL_ALIASES.get(model, model)
    cmd = [
        GROK_BIN,
        "--no-memory",
        "--no-subagents",
        "--disable-web-search",
        "--always-approve",
        "--model",
        cli_model,
        "--single",
        prompt,
        "--output-format",
        "plain",
    ]
    env = dict(os.environ)
    env["HERMES_SANDBOX_MODE"] = "1"  # a loop builder must never write to the live board
    sessions_before = _session_ids(cwd)
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), env=env, capture_output=True,
            encoding="utf-8", errors="replace", timeout=timeout_s, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        out = _decode(exc.stdout) + _decode(exc.stderr)
        return EngineResult(rc=124, output=out, usage_limit=detect_usage_limit(out), timed_out=True)
    out = (proc.stdout or "") + (proc.stderr or "")
    usage = _new_session_usage(cwd, sessions_before)
    return EngineResult(
        rc=proc.returncode,
        output=out,
        usage_limit=detect_usage_limit(out),
        **usage,
    )


def _session_ids(cwd: Path) -> set[str]:
    root = GROK_HOME / "sessions" / quote(str(cwd), safe="")
    try:
        return {entry.name for entry in root.iterdir() if entry.is_dir()}
    except OSError:
        return set()


def _new_session_usage(cwd: Path, before: set[str]) -> dict[str, int | str | None]:
    new_ids = _session_ids(cwd) - before
    totals = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
    }
    found = False
    try:
        lines = (GROK_HOME / "logs" / "unified.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            continue
        if event.get("sid") not in new_ids or event.get("msg") != "shell.turn.inference_done":
            continue
        ctx = event.get("ctx") if isinstance(event.get("ctx"), dict) else {}
        for source, target in (
            ("prompt_tokens", "input_tokens"),
            ("cached_prompt_tokens", "cached_input_tokens"),
            ("completion_tokens", "output_tokens"),
            ("reasoning_tokens", "reasoning_tokens"),
        ):
            value = ctx.get(source)
            if isinstance(value, int) and value >= 0:
                totals[target] += value
                found = True
    provenance_path = None
    if len(new_ids) == 1:
        sid = next(iter(new_ids))
        provenance_path = str(
            GROK_HOME / "sessions" / quote(str(cwd), safe="") / sid / "updates.jsonl"
        )
    if not found:
        return {
            **{key: None for key in (*totals, "total_tokens")},
            "provenance_path": provenance_path,
        }
    return {
        **totals,
        "total_tokens": totals["input_tokens"] + totals["output_tokens"],
        "provenance_path": provenance_path,
    }


def _decode(raw: bytes | str | None) -> str:
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return raw
