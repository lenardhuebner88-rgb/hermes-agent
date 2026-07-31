# Execution Facts shadow runbook

This backend remains read-only relative to every source and has no ranking or
product activation mode. The optional Shadow unit creates or updates only the
dedicated Execution-Facts database and content-free evidence directory. It
does not change Kanban/Cron databases, Loop ledgers, tmux options, source
systemd units, Crontab, or product behavior.

## Modes

`HERMES_EXECUTION_FACTS_MODE` accepts:

- `off` (default): publication returns immediately and drops as disabled.
- `shadow`: publication uses a bounded in-memory queue and daemon writer.

There is deliberately no production/ranking mode in V1. `try_emit()` performs
only counter updates and `put_nowait`; storage, retries, and reconciliation
never run on the source hot path. `publish_after_commit()` is the future
post-commit seam and is fail-open. Source work is authoritative and cannot be
reversed by telemetry.

## Create an isolated shadow database

Always name the target explicitly while V1 is shadow-only:

```bash
python scripts/execution_facts.py init \
  --db /tmp/execution-facts-shadow.db
```

The command creates only the named database. It does not touch source stores.

## Reconcile sources

Primary lifecycle adapter order is Kanban, Hermes Cron, Loops, tmux, then
systemd/Crontab. Usage Facts is an orthogonal cost/token bridge.

```bash
python scripts/execution_facts.py reconcile \
  --db /tmp/execution-facts-shadow.db \
  --kanban-db /path/to/read-only-kanban.db \
  --cron-db /path/to/read-only-cron.db \
  --loop-ledger /path/to/ledger.jsonl \
  --usage-db /path/to/usage_facts.db \
  --subscription-fees tests/fixtures/execution_facts/subscription-fees.example.json \
  --fee-version operator-fees-YYYY-MM
```

All SQLite sources are opened with `mode=ro`. A fee file is a versioned,
non-secret provider/origin-to-monthly-USD mapping. Keys may be scoped as
`YYYY-MM:provider`, `YYYY-MM:origin`, or `YYYY-MM:*`; those take precedence
over the unscoped provider/origin/`*` fallback.

Usage rows may improve after an initial write, for example when a streaming
run receives its final token counts. Usage and cost events therefore include
a content-derived revision token in their idempotency key. An unchanged row
dedupes; a changed row appends a new immutable revision, and the projection
selects the latest fact at the strongest validity.

For the fixed source order and universal eligible denominator, use the census
collector instead of assembling per-source fixture arguments:

```bash
/home/piet/.hermes/hermes-agent/.venv/bin/python \
  scripts/execution_facts.py collect-shadow \
  --db /tmp/execution-facts-shadow.db \
  --hermes-home /home/piet/.hermes \
  --usage-db /mnt/data/hermes-observability/usage_facts.db \
  --evidence-dir /tmp/execution-facts-shadow-evidence
```

The JSON response and evidence report contain no commands, pane contents,
journal messages, Cron errors, Loop plans, or source payloads. They contain
only counts, bounded classifications, timestamps, validity, and digests.
The Crontab adapter queries `SYSLOG_IDENTIFIER=CRON`, requests only boot ID,
PID, and realtime timestamp, and reads the complete configured journal window.
A parse error makes the denominator unknown instead of silently shrinking it.
The systemd adapter requests `InvocationID`, result/exit fields, and direct
`ExecMainStartTimestamp`/`ExecMainExitTimestamp` values with
`--timestamp=us+utc`. It does not derive wall time from `/proc` boot time.
Missing properties, malformed realtime values, or an ended invocation without
result and exit status fail closed to an unknown denominator.

For tmux, collect only neutral locator fields:

```bash
tmux list-panes -a -F \
  '#{session_id}\037#{window_id}\037#{pane_id}\037#{pane_pid}\037#{pane_dead}\037#{@hermes_terminal_run_id}' \
  > /tmp/execution-facts-panes.txt

python scripts/execution_facts.py reconcile \
  --db /tmp/execution-facts-shadow.db \
  --tmux-panes /tmp/execution-facts-panes.txt \
  --tmux-server-id default
```

Never add `pane_current_command`, title, path, captured content, or argv to
that format. Unmanaged panes receive an identity in the isolated identity
store from first observation. This command does not stamp live tmux options.
A separately authorized one-time adoption may set a pane-local
`@hermes_terminal_run_id`; record the exact panes and previous option values
in the coordination claim so the change is reversible.
The collector performs two neutral-field reads to prove behavior parity, but
only the first read becomes source facts. If fewer than 20 real panes exist,
later timestamped scans must accumulate the remaining real observations.
The separate idempotent writer probe may repeat events to obtain 20 timing
samples; those probe attempts never increase a source's event count or
minimum-sample gate.

Content-free systemd/Crontab milestone fixtures use:

```bash
python scripts/execution_facts.py reconcile \
  --db /tmp/execution-facts-shadow.db \
  --system-observations \
  tests/fixtures/execution_facts/system-observations.jsonl
```

## Read and diagnose

```bash
python scripts/execution_facts.py show \
  --db /tmp/execution-facts-shadow.db

python scripts/execution_facts.py validate \
  --db /tmp/execution-facts-shadow.db
```

`show` and `validate` are read-only. Diagnose these fields first:

- `database`: raw/projected counts and rebuild digest.
- `collector`: submitted, accepted, dropped, write errors, dedupe and queue.
- `sources`: per-source freshness, queue lag and reconciliation lag.
- `p0`: explicit numerator/denominator/validity/unknown state.
- `shadow_gates`: per-source checks and thresholds.
- `alert_gate`: missing/stale/unpriced source reasons.

Collector failure is represented as `unknown`; it is never a successful zero.

## Rebuild

```bash
python scripts/execution_facts.py rebuild \
  --db /tmp/execution-facts-shadow.db

python scripts/execution_facts.py validate \
  --db /tmp/execution-facts-shadow.db
```

Rebuild atomically replaces every derived table from raw events. Repeating it
without raw changes must produce the same digest and row counts.

## Retention

```bash
python scripts/execution_facts.py retention \
  --db /tmp/execution-facts-shadow.db \
  --raw-days 90
```

The transaction first adds old events to permanent monthly aggregates and then
deletes those raw rows. A later reconciliation of another old row increments,
rather than replaces, its monthly aggregate.
The permanent content-free dedupe registry is not pruned, so old retries remain
idempotent and conflicting key reuse remains fail-closed. The command rebuilds
all projections after pruning before it returns; a separate manual rebuild is
not required.

## Source activation gate

Each source remains shadowed until its own
`execution-facts-shadow-gates.v1` row passes:

- at least 20 events;
- source freshness within 24 hours;
- at least one execution identity plus a fresh exact identity-coverage proof;
- a fresh exact metric-coverage proof;
- no `unknown` events in the sample;
- maximum queue lag at most 1,000 ms;
- reconciliation lag within the freshness window;
- measured capture p95 strictly below 1,000 microseconds;
- dedupe/reconciliation parity;
- current shadow collector health with zero drops;
- zero source read/parse errors;
- unchanged-work/behavior-equivalence plus a named static read-only proof.

Passing a data gate has `activation_effect: none`; an operator must still
authorize any callpoint or timer. Minimum samples and limits are versioned.
Validation events use the separate `execution_facts_validation` source, so
recording a proof cannot make stale source data appear fresh.
For systemd, every requested property key must be present. Missing invocation
or direct realtime timestamp metadata fails closed to an unknown denominator;
missing values never become exit code or timestamp zero. Repeated reads of one
`InvocationID` reuse phase-stable idempotency keys; a new invocation is a new
execution rather than a duplicate.

After independently producing the referenced evidence, record its content-free
verdict explicitly:

```bash
python scripts/execution_facts.py record-shadow-proof \
  --db /tmp/execution-facts-shadow.db \
  --source kanban_timeline \
  --sample-size 20 \
  --capture-p95-us 421 \
  --identity-coverage-passed \
  --metric-coverage-passed \
  --dedupe-reconciled \
  --behavior-equivalent \
  --evidence-sha256 <64-hex-digest-of-the-proof-artifact>
```

Omitting any boolean records a blocking proof rather than silently assuming
success. The source is a closed choice and the evidence reference is built from
that source plus the validated digest; arbitrary paths, URLs, and free text are
not accepted.

## Optional periodic Shadow census

The repository templates are:

```text
scripts/systemd/hermes-execution-facts-shadow.service
scripts/systemd/hermes-execution-facts-shadow.timer
```

Before installation, run `collect-shadow` once with `/tmp` targets and inspect
its JSON. Then copy the exact templates to
`~/.config/systemd/user/`, run `systemctl --user daemon-reload`, start the
service once, inspect its exit status and evidence, and only then enable the
timer. The unit uses `ProtectSystem=strict`, `ProtectHome=read-only`, permits
writes only below `/mnt/data/hermes-observability`, and restricts sockets to
`AF_UNIX`. `PrivateTmp` is intentionally not enabled because the default tmux
server socket lives below the user's shared `/tmp`.

Stopping and disabling this timer is sufficient to stop all new Shadow writes:

```bash
systemctl --user disable --now hermes-execution-facts-shadow.timer
```

This does not delete the database or evidence and does not affect source
services.

## Non-agentic Sentinel

The weekly Sentinel is deliberately not a Kanban or model task. It writes two
deterministic start/end facts directly to the dedicated Execution-Facts ledger,
retries them to prove durable dedupe, rebuilds the projection, checks the
outcome, and verifies that a missing cost population is not rendered as zero.
The source is operational/non-work: its weekly timestamp cannot enter work
outcomes, source Shadow gates, or freshness alerts. The probe verifies this
isolation itself. It cannot inspect or modify source runtimes and its unit can
write only below `/mnt/data/hermes-observability`.

Deployment and rollback are documented in
`scripts/observability_sentinel_smoke.WIRING.md`.

## Rollback and removal

Because no production callpoint is enabled, rollback is:

1. keep `HERMES_EXECUTION_FACTS_MODE=off` or unset it;
2. disable and stop `hermes-execution-facts-shadow.timer`, if installed;
3. stop any explicitly launched offline reconciliation process;
4. preserve the shadow database for evidence, or move the exact isolated file
   to archival storage;
5. remove the read route/CLI and fork-owned modules in a later reviewed diff.

Do not delete or disable existing synchronous Kanban Runtime Facts yet. They
are the comparison bridge. Removal is a separate change only after each source
has passed equality, drop/lag, performance, reconciliation, and unchanged-work
gates.
