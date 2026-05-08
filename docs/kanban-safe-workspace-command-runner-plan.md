# Kanban Safe Workspace Command Runner Plan

> **For Hermes:** Phase A spec only. Do not implement, restart, dispatch, or change runtime config from this document without a separate approval gate.

**Goal:** Add a narrow worker command runner that lets scoped Kanban workers execute allowlisted commands inside their assigned workspace without exposing the broad `terminal` tool.

**Architecture:** Introduce one concrete model-native tool, tentatively `kanban_run_workspace_command`, available only to dispatcher-spawned Kanban workers. The tool resolves the current task workspace from `HERMES_KANBAN_WORKSPACE` / persisted task workspace, enforces workspace containment, rejects shell execution and unsafe commands, captures bounded output, and returns structured JSON for task completion metadata.

**Tech Stack:** Python, Hermes tool registry, Kanban dispatcher preflight, pytest.

## Phase B implementation status

Implemented on 2026-05-08 without gateway restart:

- New tool module: `tools/kanban_workspace_runner.py`.
- New tests: `tests/tools/test_kanban_workspace_runner.py`.
- Dispatcher preflight allowlist updated in `hermes_cli/kanban_db.py`.
- Toolset exposure updated in `toolsets.py`.
- Worker visibility expectation updated in `tests/tools/test_kanban_tools.py`.

Verification:

```text
pytest tests/tools/test_kanban_workspace_runner.py \
  tests/hermes_cli/test_kanban_db.py::test_dispatch_preflight_blocks_toolset_like_allowed_tool_names \
  tests/hermes_cli/test_kanban_db.py::test_dispatch_preflight_blocks_mixed_concrete_and_toolset_like_allowed_tools \
  tests/hermes_cli/test_kanban_db.py::test_dispatch_preflight_accepts_safe_workspace_runner_tool \
  tests/tools/test_kanban_tools.py::test_kanban_tools_visible_with_env_var -q
# 18 passed

pytest tests/hermes_cli/test_kanban_db.py tests/tools/test_kanban_tools.py tests/tools/test_kanban_workspace_runner.py -q
# 148 passed, 4 warnings
```

Live gateway pickup remains Phase C and requires separate restart approval.

## Phase B.1 hardening status

Implemented on 2026-05-08 without gateway restart:

- Runner no longer resolves `python`/`python3` through worker-controlled `PATH`; it executes via Hermes' current `sys.executable` and returns the requested display argv in JSON.
- Runner uses fixed minimal `PATH=/usr/local/bin:/usr/bin:/bin` in child env.
- Dispatcher now exports `HERMES_KANBAN_WORKSPACE_KIND`.
- Runner enforces scratch-root containment only for `workspace_kind=scratch`; `dir`/`worktree` workspaces may live outside scratch root.
- Runner rejects unknown workspace kinds.
- Tool schema now declares `argv.minItems=2` and `argv.maxItems=2`.
- Tests prove the runner stays hidden without `HERMES_KANBAN_TASK` even when explicitly registered.

Verification:

```text
pytest tests/tools/test_kanban_workspace_runner.py \
  tests/hermes_cli/test_kanban_db.py::TestPathResolutionAndWorkerEnv::test_dispatcher_spawn_injects_kanban_db_and_workspaces_root \
  tests/tools/test_kanban_tools.py::test_kanban_tools_hidden_without_env_var \
  tests/tools/test_kanban_tools.py::test_kanban_tools_visible_with_env_var -q
# 24 passed

pytest tests/hermes_cli/test_kanban_db.py tests/tools/test_kanban_tools.py tests/tools/test_kanban_workspace_runner.py -q
# 155 passed, 4 warnings
```

Phase C remains separate: running gateway code still needs an approved restart and live autodispatch smoke.

## Phase D pytest expansion status

Implemented on 2026-05-08 without gateway restart:

- `kanban_run_workspace_command` now allows exactly `['pytest', '<workspace-relative .py test file>']`.
- `pytest` execution uses Hermes' current Python interpreter via `sys.executable -m pytest`, not worker-controlled `PATH`.
- Still rejected: pytest flags/options, extra args, directory-wide pytest, path escapes, non-`.py` targets, shell strings, `python -m`, and generic `terminal`.
- Timeout clamp expanded from 30s to 60s for small pytest file checks.
- `kanban-worker` skill guidance patched to include the new pytest-safe shape.

Verification:

```text
pytest tests/tools/test_kanban_workspace_runner.py -q
# 30 passed

pytest tests/hermes_cli/test_kanban_db.py tests/tools/test_kanban_tools.py tests/tools/test_kanban_workspace_runner.py -q
# 164 passed, 4 warnings
```

Live gateway was not restarted for Phase D. New worker subprocesses should load runner code from disk, but an end-to-end autodispatch pytest smoke remains a separate approval gate.

---

## Current evidence

Live autonomy smoke on 2026-05-08 showed:

- Gateway autodispatch works for scoped workers.
- `coder` can write/read files in a scratch workspace via `write_file`/`read_file`.
- A scope contract declaring `terminal` fails closed before spawn:
  - `dispatch_preflight_invalid_allowed_tools`
  - `allowed_tool is too broad: terminal`
- A narrow file-only contract passed and completed with `claim_lock=huebners:1137291`.

Relevant current code paths:

- `hermes_cli/kanban_db.py`
  - `_KNOWN_SCOPE_ALLOWED_TOOLS` includes concrete tool names.
  - `_BROAD_SCOPE_ALLOWED_TOOL_MARKERS` includes broad markers including `terminal`.
  - `_validate_scope_allowed_tools(...)` fails closed for broad/unknown tools.
  - `dispatch_once(...)` records `dispatch_preflight_passed` with `effective_toolsets`.
  - `_default_spawn(...)` exports `HERMES_KANBAN_EFFECTIVE_TOOLSETS` for workers.
- `tools/kanban_tools.py`
  - Kanban model-native tools are worker-aware via `HERMES_KANBAN_TASK`.
  - Existing handlers return JSON and use `tool_error(...)` for structured failure.
- `tools/file_tools.py`
  - File tools already resolve relative paths against live task cwd / `TERMINAL_CWD`.
- `tests/hermes_cli/test_kanban_db.py`
  - Existing tests prove `terminal` remains blocked as too broad.
  - Existing tests prove concrete tool allowlists pass preflight and must match completion metadata.

## Non-goals

- Do not enable generic `terminal` in scoped worker contracts.
- Do not allow shell strings, pipes, redirects, glob expansion, or `bash -c`.
- Do not allow arbitrary cwd.
- Do not read or expose secrets/env.
- Do not allow network tooling by default.
- Do not run outside `scratch`/approved worktree workspace roots.
- Do not change gateway config or restart gateway during implementation phases unless separately approved.

## Proposed tool

Name:

```text
kanban_run_workspace_command
```

Toolset:

```text
kanban
```

Availability:

- Only when `HERMES_KANBAN_TASK` is set.
- Optional: also require `HERMES_KANBAN_WORKSPACE` or task `workspace_path` to be set and under the approved Kanban workspaces root for `scratch` tasks.

Input schema:

```json
{
  "argv": ["python3", "autonomy_probe.py"],
  "timeout_seconds": 10,
  "max_output_bytes": 20000
}
```

Optional later fields, not Phase 1:

```json
{
  "expected_exit_code": 0,
  "stdin": null
}
```

Output schema:

```json
{
  "ok": true,
  "exit_code": 0,
  "stdout": "autonomy-ok\n",
  "stderr": "",
  "duration_ms": 42,
  "timed_out": false,
  "truncated": false,
  "cwd": "/home/piet/.hermes/kanban/workspaces/t_...",
  "argv": ["python3", "autonomy_probe.py"]
}
```

Failure output uses `tool_error(...)` or an `ok:false` JSON result depending on convention chosen during implementation. Prefer structured `ok:false` for command non-zero exits and `tool_error(...)` for policy violations.

## Security policy

### Workspace containment

The runner must:

1. Resolve workspace from current worker context.
2. Refuse if no active `HERMES_KANBAN_TASK`.
3. Refuse if no workspace path can be resolved.
4. Resolve realpath for cwd and target path checks.
5. Require cwd to be the task workspace.
6. For `scratch`, require cwd under `kb.workspaces_root()`.
7. Never follow requested cwd outside workspace; initial version has no user-supplied cwd.

### Command shape

Require `argv` as a list of strings. Reject:

- string command input
- empty argv
- arguments containing NUL bytes
- shell metacharacter-only execution paths
- `bash`, `sh`, `zsh`, `fish`, `dash`, `powershell`, `cmd`, `python -c`, `node -e`
- pipes/redirection only insofar as they imply shell usage; since `shell=False`, metacharacters are inert but should still be rejected for clear policy.

### Initial command allowlist

Phase 1 should be intentionally tiny:

```python
_ALLOWED_WORKSPACE_COMMANDS = {
    "python3": {
        "allowed_extensions": [".py"],
        "max_args": 2,
        "deny_flags": ["-c", "-m"],
    },
    "python": {
        "allowed_extensions": [".py"],
        "max_args": 2,
        "deny_flags": ["-c", "-m"],
    },
}
```

Allowed Phase-1 examples:

```json
["python3", "autonomy_probe.py"]
["python", "script.py"]
```

Rejected Phase-1 examples:

```json
["python3", "-c", "print('x')"]
["python3", "../../secret.py"]
["bash", "-c", "python3 autonomy_probe.py"]
["env"]
["printenv"]
["curl", "https://example.com"]
["pytest"]
```

`pytest` should not be added in Phase 1. Add it only after separate policy discussion because it can import arbitrary repo code and trigger plugin/network/env behavior.

### Environment

Default environment should be minimal:

- Start from `os.environ` only if required for Python runtime, then strip sensitive keys.
- Remove keys matching `*KEY*`, `*TOKEN*`, `*SECRET*`, `*PASSWORD*`, `OPENAI*`, `ANTHROPIC*`, `MINIMAX*`, `DISCORD*`, `OPENROUTER*`, etc.
- Prefer an explicit tiny env:

```python
{
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "HOME": workspace,
    "PYTHONNOUSERSITE": "1",
}
```

Do not pass `.env` contents.

### Output limits

- `timeout_seconds`: default 10, max 30.
- `max_output_bytes`: default 20_000, max 100_000.
- Capture stdout/stderr separately.
- Truncate by bytes, with `truncated:true` and suffix marker.
- Do not stream output live to model.

## Implementation plan

### Task 1: Add tests for policy-only command validation

**Objective:** Define the security contract before adding code.

**Files:**
- Create or modify: `tests/tools/test_kanban_workspace_runner.py`

**Tests:**

- `test_runner_requires_kanban_task_env`
- `test_runner_rejects_string_command`
- `test_runner_rejects_empty_argv`
- `test_runner_rejects_shells_and_inline_code`
- `test_runner_rejects_path_escape_argument`
- `test_runner_accepts_python_file_in_workspace`

Expected initial result: fail because tool does not exist.

### Task 2: Implement minimal tool module

**Objective:** Add `tools/kanban_workspace_runner.py` with policy validation and command execution.

**Files:**
- Create: `tools/kanban_workspace_runner.py`

**Implementation notes:**

- Use `subprocess.run(..., shell=False, capture_output=True, text=False, timeout=...)`.
- Use `Path(...).resolve()` and `relative_to(workspace)` for containment.
- Return JSON string.
- Use `registry.register(name="kanban_run_workspace_command", toolset="kanban", ...)`.
- `check_fn` should require `HERMES_KANBAN_TASK` and a resolvable workspace.

### Task 3: Add tool to scoped preflight allowlist

**Objective:** Make `scope_contract.allowed_tools: [kanban_run_workspace_command]` pass as a concrete allowed tool.

**Files:**
- Modify: `hermes_cli/kanban_db.py`

**Change:**

- Add `kanban_run_workspace_command` to `_KNOWN_SCOPE_ALLOWED_TOOLS`.
- Do **not** remove `terminal` from `_BROAD_SCOPE_ALLOWED_TOOL_MARKERS`.

**Tests:**

- Add test: `test_dispatch_preflight_accepts_safe_workspace_runner_tool`.
- Keep existing `terminal` broad tests green.

### Task 4: Add model schema/toolset exposure

**Objective:** Ensure worker schema can include the new tool when effective toolsets request it.

**Files:**
- Likely modify: `toolsets.py` if needed for `_HERMES_CORE_TOOLS` / kanban toolset definitions.
- Verify auto-discovery if module registration is sufficient.

**Tests:**

- Add or update tool registry/toolset test to assert `kanban_run_workspace_command` is visible when `HERMES_KANBAN_EFFECTIVE_TOOLSETS` includes it.
- Add absence proof that it is not visible in non-Kanban contexts unless explicitly enabled by policy.

### Task 5: Add integration test with fake workspace

**Objective:** Prove command executes inside workspace and returns structured output.

**Test:**

1. Create temp workspace.
2. Write `autonomy_probe.py`.
3. Set `HERMES_KANBAN_TASK` and `HERMES_KANBAN_WORKSPACE`.
4. Call handler directly or via registry.
5. Assert:
   - `ok:true`
   - `exit_code:0`
   - `stdout:"autonomy-ok\n"`
   - `cwd` equals workspace

### Task 6: Add docs/guidance

**Objective:** Teach orchestrators/workers to use the narrow runner instead of `terminal` for scoped tests.

**Files:**
- Modify: `agent/prompt_builder.py` guidance if needed.
- Patch skill: `kanban-worker` after implementation if current skill lacks runner guidance.
- Add repo doc: `docs/kanban-safe-workspace-command-runner.md`.

### Task 7: Regression gates

Run focused tests first:

```bash
pytest tests/tools/test_kanban_workspace_runner.py -q
pytest tests/hermes_cli/test_kanban_db.py::test_dispatch_preflight_blocks_toolset_like_allowed_tool_names -q
pytest tests/hermes_cli/test_kanban_db.py::test_dispatch_preflight_blocks_mixed_concrete_and_toolset_like_allowed_tools -q
pytest tests/hermes_cli/test_kanban_db.py::test_dispatch_preflight_accepts_safe_workspace_runner_tool -q
```

Then run cluster:

```bash
pytest tests/hermes_cli/test_kanban_db.py tests/tools/test_kanban_tools.py tests/tools/test_kanban_workspace_runner.py -q
```

## Live pickup plan after implementation

Requires separate approval for gateway restart.

1. Precheck git status and focused tests.
2. Restart gateway using approved delayed systemd pattern if needed.
3. Verify tool registration in running gateway schema or by focused worker smoke.
4. Create `coder` scratch task with allowed tools:

```yaml
allowed_tools:
  - kanban_show
  - write_file
  - read_file
  - kanban_run_workspace_command
  - kanban_complete
  - kanban_block
```

5. Let gateway autodispatch pick it up.
6. Worker writes `autonomy_probe.py`.
7. Worker calls:

```json
{
  "argv": ["python3", "autonomy_probe.py"],
  "timeout_seconds": 10,
  "max_output_bytes": 20000
}
```

8. Worker completes with `stdout:"autonomy-ok\n"` evidence.
9. Verify task events/logs and write Vault receipt.

## Acceptance criteria

Green only if:

- `terminal` remains blocked in scope contracts.
- `kanban_run_workspace_command` passes dispatcher preflight as a concrete tool.
- Runner refuses shell strings and path escapes.
- Runner executes only inside the task workspace.
- Runner returns bounded structured output.
- Completion attestation requires and records exact `effective_toolsets`.
- Live smoke proves gateway-autonomous pickup and command execution after approved restart.

## Open design decisions before Phase B

1. Should Phase 1 allow only `python/python3 <file.py>`, or also `pytest <specific_test.py>`?
   - Recommendation: start with Python file only.
2. Should the runner use a new toolset name or stay in `kanban`?
   - Recommendation: stay in `kanban`, because it is only meaningful for Kanban workers.
3. Should non-zero exit be `ok:false` or `tool_error`?
   - Recommendation: `ok:false` for command result, `tool_error` for policy violation.
4. Should output redaction happen inside the runner even if global redaction is off?
   - Recommendation: yes for obvious env/secret patterns, but do not rely on redaction as the main safety boundary.
