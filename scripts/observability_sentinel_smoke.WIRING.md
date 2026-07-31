# Observability sentinel wiring

`observability_sentinel_smoke.py` is a bounded, non-agentic weekly probe. It
appends two deterministic, content-free start/end facts to the dedicated
Execution-Facts ledger, retries them to prove durable dedupe, rebuilds the
projection, and verifies the outcome plus the invariant that an absent cost
population never renders as zero.

It does not create Kanban work, start an agent, inspect prompts or outputs, or
write any source runtime. Its only live writes are the dedicated Execution-Facts
database, lock, and content-free status receipt below
`/mnt/data/hermes-observability/`.

The service and timer files in `scripts/systemd/` are hardened **systemd user
unit** deployment templates for user `piet`. They are intentionally not
installed or enabled by repository changes; do not install them below
`/etc/systemd/system`.

## Preflight

From the live checkout:

```bash
.venv/bin/python scripts/observability_sentinel_smoke.py --dry-run
.venv/bin/python scripts/observability_sentinel_smoke.py \
  --db /tmp/execution-facts-sentinel.db \
  --status-path /tmp/hermes-sentinel-status.json \
  --lock-path /tmp/hermes-sentinel.lock
```

The first command writes nothing. The second writes only the three explicit
temporary paths.

## Install after operator approval

Copy the two templates to `~/.config/systemd/user/`, run
`systemctl --user daemon-reload`, execute the service once, inspect its
content-free receipt, and only then enable the timer.

The timer runs weekly with `Persistent=true` and a randomized 15-minute delay.
The ISO-week idempotency key and non-blocking lock prevent duplicate facts and
overlapping probes.
