#!/usr/bin/env python3
"""Create validated Autoresearch run-request JSON files without executing them."""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from hermes_constants import get_hermes_home

REPO = Path(__file__).resolve().parents[1]
AUDIT = REPO / ".hermes" / "skill-audit"
REQUESTS_DIR = AUDIT / "run-requests"
DEFAULT_HERMES_HOME = get_hermes_home()
SCHEMA = "autoresearch-run-request-v1"
ALLOWED_MODES = {"skills", "tests", "code", "docs", "research_qa"}
MAX_ITERATIONS = 5
AREA_ROOTS = {
    "all": ("skills",),
    "github": ("skills/github",),
    "hermes-kanban": (
        "skills/hermes-kanban",
        "skills/devops/kanban-reviewer",
        "skills/devops/kanban-worker",
        "skills/devops/kanban-orchestrator",
        "skills/devops/kanban-critic",
        "skills/devops/kanban-execution-worker-readiness",
        "skills/devops/hermes-kanban-worker-scope-control",
    ),
    "devops": ("skills/devops",),
    "research": ("skills/research",),
    "productivity": ("skills/productivity",),
    "software-development": ("skills/software-development",),
    "mlops": ("skills/mlops",),
    "creative": ("skills/creative",),
    "firecrawl": ("skills/firecrawl",),
    "dashboard": ("hermes-agent/scripts", "hermes-agent/tests"),
}
SAFE_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def forbidden_paths(hermes_home: Path) -> list[str]:
    return [
        str(hermes_home / ".env"),
        str(hermes_home / "auth.json"),
        str(hermes_home / "config.yaml"),
        str(hermes_home / "kanban.db"),
    ]


def allowed_paths_for_area(area: str, *, repo_root: Path, hermes_home: Path) -> list[str]:
    if area not in AREA_ROOTS:
        raise ValueError(f"invalid area: must be one of {sorted(AREA_ROOTS)}")
    out: list[str] = []
    for rel in AREA_ROOTS[area]:
        if rel == "skills":
            out.append(str(hermes_home / "skills"))
            out.append(str(repo_root / "skills"))
        elif rel.startswith("skills/"):
            out.append(str(hermes_home / rel))
            out.append(str(repo_root / rel))
        elif rel.startswith("hermes-agent/"):
            out.append(str(repo_root / rel.removeprefix("hermes-agent/")))
        else:
            raise ValueError(f"unsupported area root: {rel}")
    return out


def validate_allowed_paths(area: str, paths: list[str], *, repo_root: Path = REPO, hermes_home: Path = DEFAULT_HERMES_HOME) -> list[str]:
    roots = [Path(p).resolve(strict=False) for p in allowed_paths_for_area(area, repo_root=repo_root, hermes_home=hermes_home)]
    forbidden = [Path(p).resolve(strict=False) for p in forbidden_paths(hermes_home)]
    validated: list[str] = []
    for raw in paths:
        raw_path = Path(str(raw))
        if ".." in raw_path.parts:
            raise ValueError("allowed path is outside allowed roots: traversal segments are forbidden")
        path = raw_path.resolve(strict=False)
        if any(path == f or _under(path, f) for f in forbidden):
            raise ValueError("allowed_paths cannot include secrets/auth/config/db surfaces")
        if not any(path == root or _under(path, root) for root in roots):
            raise ValueError("allowed path is outside the selected area roots")
        validated.append(str(path))
    return validated


def build_request(
    *,
    mode: str = "skills",
    area: str,
    focus: str,
    max_iterations: int = 1,
    mutation_policy: str = "requires_operator_go",
    repo_root: Path = REPO,
    hermes_home: Path = DEFAULT_HERMES_HOME,
) -> dict[str, object]:
    if mode not in ALLOWED_MODES:
        raise ValueError(f"invalid mode: must be one of {sorted(ALLOWED_MODES)}")
    if area not in AREA_ROOTS:
        raise ValueError(f"invalid area: must be one of {sorted(AREA_ROOTS)}")
    if not SAFE_TOKEN_RE.match(focus):
        raise ValueError("focus must be a lowercase token with dashes/underscores only")
    if not 1 <= int(max_iterations) <= MAX_ITERATIONS:
        raise ValueError(f"max_iterations must be between 1 and {MAX_ITERATIONS}")
    if mutation_policy != "requires_operator_go":
        raise ValueError("mutation_policy must be requires_operator_go for MVP")

    allowed = validate_allowed_paths(
        area,
        allowed_paths_for_area(area, repo_root=repo_root, hermes_home=hermes_home),
        repo_root=repo_root,
        hermes_home=hermes_home,
    )
    next_command = (
        f"/autoresearch mode={mode} area={area} focus={focus} "
        f"max_iterations={int(max_iterations)} mutation_policy={mutation_policy}"
    )
    return {
        "schema": SCHEMA,
        "created_at": utc_now(),
        "created_by": "autoresearch-dashboard",
        "mode": mode,
        "area": area,
        "focus": focus,
        "objective": f"Prepare a bounded Autoresearch {mode} campaign for {area}:{focus}.",
        "allowed_paths": allowed,
        "forbidden_paths": forbidden_paths(hermes_home),
        "model_preference": "MiniMax-M2.7-highspeed",
        "model_route_status": "unverified",
        "max_iterations": int(max_iterations),
        "require_backup": True,
        "require_eval": True,
        "mutation_policy": mutation_policy,
        "status": "planned",
        "next_command": next_command,
    }


def validate_request(data: dict[str, object], *, hermes_home: Path = DEFAULT_HERMES_HOME, repo_root: Path = REPO) -> None:
    if data.get("schema") != SCHEMA:
        raise ValueError(f"schema must be {SCHEMA}")
    mode = data.get("mode")
    if mode not in ALLOWED_MODES:
        raise ValueError("mode is invalid")
    area = data.get("area")
    if area not in AREA_ROOTS:
        raise ValueError("area is invalid")
    focus = data.get("focus")
    if not isinstance(focus, str) or not SAFE_TOKEN_RE.match(focus):
        raise ValueError("focus is invalid")
    max_iterations = data.get("max_iterations")
    if not isinstance(max_iterations, int) or not 1 <= max_iterations <= MAX_ITERATIONS:
        raise ValueError(f"max_iterations must be between 1 and {MAX_ITERATIONS}")
    if data.get("mutation_policy") != "requires_operator_go":
        raise ValueError("mutation_policy must require operator Go")
    if data.get("status") != "planned":
        raise ValueError("new requests must start as planned")

    forbidden = [Path(str(p)).resolve() for p in data.get("forbidden_paths", [])]
    required_forbidden = [Path(p).resolve() for p in forbidden_paths(hermes_home)]
    for required in required_forbidden:
        if required not in forbidden:
            raise ValueError(f"forbidden_paths missing required path: {required}")

    allowed_paths = data.get("allowed_paths")
    if not isinstance(allowed_paths, list) or not allowed_paths:
        raise ValueError("allowed_paths must be a non-empty list")
    for raw in allowed_paths:
        path = Path(str(raw)).resolve()
        if any(path == f or _under(path, f) for f in forbidden):
            raise ValueError("allowed_paths cannot include secrets/auth/config/db surfaces")
        if ".." in Path(str(raw)).parts:
            raise ValueError("allowed_paths cannot contain traversal segments")
    validate_allowed_paths(str(area), [str(path) for path in allowed_paths], repo_root=repo_root, hermes_home=hermes_home)


def create_request(
    *,
    mode: str,
    area: str,
    focus: str,
    max_iterations: int = 1,
    mutation_policy: str = "requires_operator_go",
    requests_dir: Path = REQUESTS_DIR,
    repo_root: Path = REPO,
    hermes_home: Path = DEFAULT_HERMES_HOME,
) -> Path:
    data = build_request(
        mode=mode,
        area=area,
        focus=focus,
        max_iterations=max_iterations,
        mutation_policy=mutation_policy,
        repo_root=repo_root,
        hermes_home=hermes_home,
    )
    validate_request(data, hermes_home=hermes_home)
    requests_dir.mkdir(parents=True, exist_ok=True)
    request_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
    path = requests_dir / f"{request_id}.json"
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create", help="create a planned run-request JSON file")
    create.add_argument("--mode", default="skills", choices=sorted(ALLOWED_MODES))
    create.add_argument("--area", required=True, choices=sorted(AREA_ROOTS))
    create.add_argument("--focus", required=True)
    create.add_argument("--max-iterations", type=int, default=1)
    create.add_argument("--mutation-policy", default="requires_operator_go")
    create.add_argument("--requests-dir", "--request-dir", dest="requests_dir", type=Path, default=REQUESTS_DIR)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.command == "create":
        path = create_request(
            mode=args.mode,
            area=args.area,
            focus=args.focus,
            max_iterations=args.max_iterations,
            mutation_policy=args.mutation_policy,
            requests_dir=args.requests_dir,
        )
        print(path)
        request = json.loads(path.read_text(encoding="utf-8"))
        print(request["next_command"])
        return 0
    raise ValueError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
