# watermark_check.py — operator wiring (CAP-S4)

`scripts/watermark_check.py` is the tested watermark logic; it lives in **this** repo
(`hermes-agent`) so it is reviewed and versioned. The **existing nightly-audit /
operator health report** it folds into — `operator-morning-digest.py` — lives in a
**different** repo (`~/.hermes/scripts/`, its own git root) that a caged Kanban worker
cannot commit to. This file is the exact, minimal wiring for the operator/integrator
to apply there after review.

**No new service, no new timer, no new channel** (AC-1): this is a *fragment* the
existing report appends. Below every threshold the script emits **nothing**, so the
report is byte-for-byte unchanged (no spam).

## Recommended host — `~/.hermes/scripts/operator-morning-digest.py`

That script is the operator-facing Hermes health digest (no-agent cron, daily,
`deliver discord`; `stdout` **is** the Discord message). It already carries the
resource-health sections `section_statedb()` and `section_systemd()`, so a
memory/disk watermark belongs beside them.

Add a section that returns `""` when nothing is breached, and only append it when
non-empty — this is what keeps the report unchanged below the thresholds. Make the
repo importable and add this thin wrapper next to the other `section_*` functions (it
reuses the tested `collect_metrics` + `evaluate`, so the digest carries no threshold
logic of its own):

```python
# near the top of operator-morning-digest.py
import sys
sys.path.insert(0, "/home/piet/.hermes/hermes-agent")

def section_watermark() -> str:
    # Empty string below the thresholds → the caller skips it → report unchanged.
    from scripts.watermark_check import collect_metrics, evaluate
    line = evaluate(collect_metrics())
    return line or ""
```

Then, in `main()`, append it **only when non-empty** (right after the state.db /
systemd sections):

```python
    wm = _run_section(section_watermark, "Watermark")
    if wm:                      # breach → one extra line; all-clear → nothing added
        parts.append("")
        parts.append(wm)
```

`_run_section` already fences the call in try/except, so a dead source degrades to an
honest "nicht lesbar" line instead of taking the digest down — but note that on
failure `_run_section` returns a non-empty string, so a genuinely broken collector
*would* surface a line (that is the intended fail-loud behaviour for a capacity guard).

## Alternative host — any existing report that concatenates stdout

Because the script prints exactly one line (or nothing) and exits 0, it can be folded
into any existing report by appending its stdout — no code change needed:

```bash
/home/piet/.hermes/hermes-agent/.venv/bin/python3 \
  /home/piet/.hermes/hermes-agent/scripts/watermark_check.py
```

Do **not** add a dedicated cron/timer for it — the PlanSpec forbids a new service.

## Manual verification (operator)

```bash
# Against the live host (prints one line only if a threshold is breached):
/home/piet/.hermes/hermes-agent/.venv/bin/python3 \
  /home/piet/.hermes/hermes-agent/scripts/watermark_check.py
# all-clear → no output, exit 0

# Force a breach to see the line shape (low thresholds):
… scripts/watermark_check.py --swap-pct 0 --disk-pct 0 --rss-gib 0
```

## Thresholds (strictly greater-than — a value exactly at the line does not alert)

| Signal            | Alert when            | Note                                                        |
|-------------------|-----------------------|-------------------------------------------------------------|
| Swap used         | `> 50 %`              | no swap configured → never alerts                           |
| Disk used (`/`)   | `> 88 %`              | `--disk-path` to check another filesystem                   |
| Process RSS       | `> 2 GiB` **and** uncapped | "außerhalb Cap" = cgroup-v2 `memory.max == max`; a capped process is left to its own cgroup OOM and never counted |

All three are overridable via `--swap-pct` / `--disk-pct` / `--rss-gib` for testing or
future tuning. `psutil` (7.2.2) is present in the live `.venv`; cgroup v2 (unified) is
in use on this host.
