# Contract → Taskgraph Draft v1.1

`hermes-plan-compile` compiles a Vault Markdown plan into local planning artifacts only.

## Inputs

- Markdown plan with required YAML frontmatter and required sections.
- Optional `taskgraph_hints`:
  - `candidate_tasks`
  - `dependencies`
  - `recommended_roles`
  - `binding` (default `false`) — when `true`, requires `subtasks` and switches the output to a binding taskgraph (see below)
  - `subtasks` — required when `binding: true`

## Outputs

For each compiled plan, the compiler writes:

- `source.md` — source copy
- `contract.yaml` — validated contract
- `taskgraph.draft.yaml` — non-binding taskgraph draft (or a binding taskgraph if `taskgraph_hints.binding: true`)
- `contract.receipt.md` — local compiler receipt
- `contract.schema.json` — exported schema in the templates root

## Non-binding rule (default)

When `taskgraph_hints.binding` is unset or `false` (the default), `taskgraph.draft.yaml` is a planning aid only. It includes:

- `schema_version: taskgraph.draft.v1.1`
- `non_binding: true`
- `binding: non-binding`
- a `NON-BINDING DRAFT` disclaimer

It must not be treated as approval to dispatch, mutate runtime state, create Mission Control tasks, or restart services.

## Binding taskgraph (opt-in)

When `taskgraph_hints.binding: true` (with at least one `subtasks` entry), the compiler emits a binding taskgraph instead:

- `schema_version: taskgraph.binding.v1`
- `non_binding: false`
- `binding: true`
- `children` derived from `subtasks`

See `hermes_cli/plan_compiler.py::build_taskgraph_draft` and the binding PlanSpec ingest path (`hermes_cli/subcommands/plan.py`, `hermes_cli/planspecs.py`) for how this feeds `hermes plan ingest`.
