# Contract → Taskgraph Draft v1.1

`hermes-plan-compile` compiles a Vault Markdown plan into local planning artifacts only.

## Inputs

- Markdown plan with required YAML frontmatter and required sections.
- Optional `taskgraph_hints`:
  - `candidate_tasks`
  - `dependencies`
  - `recommended_roles`

## Outputs

For each compiled plan, the compiler writes:

- `source.md` — source copy
- `contract.yaml` — validated contract
- `taskgraph.draft.yaml` — non-binding taskgraph draft
- `contract.receipt.md` — local compiler receipt
- `contract.schema.json` — exported schema in the templates root

## Non-binding rule

`taskgraph.draft.yaml` is always a planning aid. It includes:

- `schema_version: taskgraph.draft.v1.1`
- `non_binding: true`
- `binding: non-binding`
- a `NON-BINDING DRAFT` disclaimer

It must not be treated as approval to dispatch, mutate runtime state, create Mission Control tasks, or restart services.
