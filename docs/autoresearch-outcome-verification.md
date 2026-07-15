# Autoresearch outcome verification

Autoresearch and Strategist expose one shared outcome vocabulary while keeping
their existing delivery stores authoritative. The verifier is evidence-only:
it does not rank proposals, calibrate Strategist levers, dispatch work, or turn
an integration into a benefit claim.

## Canonical dimensions

Every projected record carries four independent fields:

| Field | Values | Meaning |
|---|---|---|
| `outcome_applicability` | `applicable`, `not_applicable` | Whether a benefit can meaningfully be measured. |
| `measurement_status` | `not_started`, `pending`, `measuring`, `measured`, `retryable_failure`, `exhausted` | State of measurement, not delivery. |
| `outcome_verdict` | `null`, `improved`, `neutral`, `worsened`, `unmeasurable`, `confounded` | Result supported by the observation. |
| `evidence_grade` | `legacy_observational`, `contract_verified` | Strength and origin of the evidence. |

`delivery_state=integrated` never implies `outcome_verdict=improved`. Historical
facts retain their verdicts and timestamps, but are marked
`legacy_observational`. Explicit terminal no-delivery records are
`not_applicable`; they are not silently turned into neutral outcomes.

Autoresearch rows always carry `calibration_eligible=false`. They may appear in
the shared operator read model, but never influence Strategist ranking or
calibration.

## Source hierarchy and ownership

The proposal JSON and existing Kanban `task_events` remain delivery truth. The
existing Strategist `lever-outcomes.json` remains its compatibility projection.
The verifier adds only two ownership tables to the same Kanban SQLite database:

- `outcome_contracts`: immutable proposal/task/contract/baseline linkage;
- `outcome_attempts`: leased, deduplicated observations, verdict, full delivery
  SHA, and measurement cost.

Contract registration and its `outcome_contract_registered` task event commit
in one SQLite transaction. A measurement claim and
`outcome_measurement_started` event also commit together; final status, cost,
SHA and `outcome_measurement_completed` event share the final transaction.
The deterministic dedupe key is
`sha256(task_id, contract_hash, phase, attempt_no)`. Cross-process JSON writers
use advisory locks and unique temporary files followed by `fsync` and rename.
An active-attempt partial unique index also prevents attempt N+1 from starting
while attempt N still owns its lease.

## Baseline-before-dispatch

Reconciliation materializes a versioned allowlisted contract and captures its
baseline before creating releasable work. The new task is born blocked. Only
after the immutable contract, baseline, release fingerprint, task event and
proposal link exist is the task released to `ready`. A retry can recover that
same contracted blocked task, but cannot replace its baseline.

Supported probes are deliberately narrow:

- `source_pattern.v1`: reviewed source-pattern counters below allowlisted repo
  roots;
- `pytest_target.v1`: at most four repository-local `tests/` targets, invoked
  without a shell and with bounded output, memory and timeout; it may carry
  reviewed pytest or source-pattern counter probes whose violation wins over a
  primary improvement;
- `delivery_evidence.v1`: records delivery without making a benefit claim;
- `vision_metric_snapshot.v1`: Strategist metrics with a reviewed direction.

Every contract hashes the claim, typed probe/args, success template and complete
parameters/rule, stable `outcome_class`, counter probes/rules, sampling plan,
observation window, trigger, environment requirements and complete measurement
budget. A recomputed allowlisted template must match byte-for-byte; merely
supplying a plausible hash is rejected.

There is no arbitrary shell, SQL, network, absolute-path, or free-form command
surface. Code probes remain pending until both `integration_merged` and
`INTEGRATOR_VERIFIED` contain the same real integrator `merge_commit` SHA.
Runtime probes require `deployed_sha == running_sha`, retain the Strategist's
three-day maturity window, and reject stale source snapshots. Same-class
runtime windows that overlap, an expired observation window, target-SHA drift,
source-schema drift or relevant environment drift are `confounded`, never
`improved`.

## Shadow execution

Both existing nightly entrypoints call the verifier after reconciliation. It is
a strict no-op unless this profile-local marker exists:

```text
~/.hermes/state/autoresearch-outcome-shadow.enabled
```

Shadow mode reads delivery evidence and writes only the additive outcome tables
and measurement events. It does not enforce, rank, calibrate, reopen, dispatch,
or deploy anything. Each call is capped; each contract is also bounded by its
immutable attempt and timeout budget.

Registration alone does not grant `contract_verified`: a pending contract is
still unconfirmed. Only a terminal common-verifier attempt with its immutable
baseline, observation, evidence references, exact delivery SHA and additive
cost breakdown earns that evidence grade. Consequently
`contract_verified + improved` is the only “benefit confirmed” state.

## Migration and rollback

Use `migrate_shared_state` from `hermes_cli.outcome_verification` with the live
proposal directory, Strategist projection, and Kanban database paths. First run
with `apply=False`. Apply is permitted only after independent review and green
gates.

Apply creates one timestamped backup containing the full proposal directory,
the Strategist projection, and a transactionally consistent SQLite backup made
through SQLite's backup API. It then adds canonical JSON fields and missing
SQLite tables/indexes. A second apply must report zero proposal, Strategist and
schema changes and create no backup.

Rollback order:

1. remove the shadow marker so the nightly hooks become no-ops;
2. stop the affected runtime before restoring state;
3. restore proposal JSON, Strategist projection, and Kanban database from the
   same timestamped backup;
4. restart and verify the existing dashboard/nightly health.

Do not restore only one member of the backup set. Do not enable the marker to
repair a failed migration.

## Required evidence before live enablement

The release evidence must include:

- the historical parent/target flood replay with two real OS processes, one
  owner, a hard global cap of five, zero loser mutation and an idempotent second
  wave;
- the historical lifecycle replay proving explicit `delivery_state=none`
  overrides legacy inference while legacy records remain compatible;
- a clone of the exact candidate SHA plus a separate-process, post-discovery
  E2E canary over reconciliation, real dispatcher-provisioned worktree,
  controlled worker lifecycle, canonical worker gate, real chain integrator,
  separate verifier and API projection. It must include `improved`, a
  counter-won `worsened`, delivery-only `unmeasurable`, and a second no-op
  reconcile/verifier pass. The harness never inserts integration events;
- targeted/affected/backend/frontend/pre-release gates and an independent
  read-only `APPROVE` review of the exact commit;
- authenticated desktop (1440 px) and mobile (390×844) candidate and live
  screenshots;
- dry-run, backed-up apply, idempotent second migration, reversible deployment,
  and one normal budgeted forward run without quota or cooldown bypass.

A synthetic canary proves wiring, not product benefit. Completion still needs a
natural forward record with a reproducible observation. If the normal run
yields no eligible delivery, the correct status is pending—not improved.
