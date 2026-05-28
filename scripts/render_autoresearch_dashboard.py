#!/usr/bin/env python3
"""Generate a read-only HTML dashboard from Hermes autoresearch audit files."""
from __future__ import annotations

import csv
import html
import re
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
AUDIT = REPO / ".hermes" / "skill-audit"
RUBRIC = AUDIT / "skill_quality_rubric.md"
RESULTS = AUDIT / "autoresearch_results.tsv"
INVENTORY = AUDIT / "skills_inventory.md"
OUTPUT = AUDIT / "dashboard.html"


def parse_rubric() -> list[dict[str, str]]:
    rows = []
    if not RUBRIC.exists():
        return rows
    for line in RUBRIC.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("|") or "---" in line or line.lower().startswith("| skill "):
            continue
        parts = [p.strip() for p in line.strip("|").split("|")]
        if len(parts) != 10:
            continue
        rows.append({
            "skill": parts[0],
            "purpose": parts[1],
            "activation": parts[2],
            "workflow": parts[3],
            "safety": parts[4],
            "output": parts[5],
            "eval": parts[6],
            "maintain": parts[7],
            "total": parts[8],
            "priority": parts[9],
        })
    return rows


def parse_results() -> list[dict[str, str]]:
    if not RESULTS.exists() or RESULTS.stat().st_size == 0:
        return []
    with RESULTS.open("r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def extract_inventory_summary() -> dict[str, str]:
    summary = {}
    if not INVENTORY.exists():
        return summary
    for line in INVENTORY.read_text(encoding="utf-8", errors="replace").splitlines()[:40]:
        match = re.match(r"- ([^:]+):\s*(.+)", line)
        if match:
            summary[match.group(1)] = match.group(2)
    return summary


def trend_svg(values: list[float]) -> str:
    if len(values) < 2:
        return '<div class="empty">No numeric score trend yet</div>'
    width, height, pad = 520, 160, 18
    lo, hi = min(values), max(values)
    span = hi - lo or 1.0
    pts = []
    for idx, value in enumerate(values):
        x = pad + idx * ((width - 2 * pad) / max(1, len(values) - 1))
        y = height - pad - ((value - lo) / span) * (height - 2 * pad)
        pts.append(f"{x:.1f},{y:.1f}")
    circles = "".join(f'<circle cx="{p.split(",")[0]}" cy="{p.split(",")[1]}" r="3" />' for p in pts)
    return f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="Score trend"><polyline points="{" ".join(pts)}" />{circles}</svg>'


def html_table(rows: list[dict[str, str]], columns: list[str], limit: int | None = None) -> str:
    rows = rows[:limit] if limit else rows
    if not rows:
        return '<div class="empty">No rows yet</div>'
    head = "".join(f"<th>{html.escape(col)}</th>" for col in columns)
    body_rows = []
    for row in rows:
        cells = "".join(f"<td>{html.escape(str(row.get(col, '')))}</td>" for col in columns)
        body_rows.append(f"<tr>{cells}</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>"


def main() -> int:
    AUDIT.mkdir(parents=True, exist_ok=True)
    rubric_rows = parse_rubric()
    results = parse_results()
    summary = extract_inventory_summary()
    counts = {"high": 0, "medium": 0, "low": 0}
    for row in rubric_rows:
        pri = row.get("priority", "").lower()
        if pri in counts:
            counts[pri] += 1
    decision_counts: dict[str, int] = {}
    for row in results:
        decision = (row.get("decision") or "unknown").lower()
        decision_counts[decision] = decision_counts.get(decision, 0) + 1
    numeric_scores = []
    for row in results:
        for key in ("score_after", "score", "total"):
            try:
                numeric_scores.append(float(row.get(key, "")))
                break
            except (TypeError, ValueError):
                continue
    top_risks = sorted(rubric_rows, key=lambda r: int(r.get("total") or 999))[:12]
    last = results[-1] if results else {}
    generated = datetime.now().astimezone().isoformat(timespec="seconds")
    cards = [
        ("Inventoried skills", summary.get("Total SKILL.md files inventoried", str(len(rubric_rows)))),
        ("High priority", str(counts["high"])),
        ("Medium priority", str(counts["medium"])),
        ("Low priority", str(counts["low"])),
        ("Iterations logged", str(len(results))),
        ("Kept", str(decision_counts.get("keep", 0))),
        ("Blocked", str(decision_counts.get("blocked", 0))),
        ("Discarded", str(decision_counts.get("discard", 0))),
    ]
    card_html = "".join(f'<section class="metric"><span>{html.escape(label)}</span><strong>{html.escape(value)}</strong></section>' for label, value in cards)
    result_columns = ["timestamp", "mode", "target", "hypothesis", "decision", "risk", "evidence"]
    risk_columns = ["skill", "safety", "output", "activation", "total", "priority"]
    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hermes Autoresearch Dashboard</title>
<style>
:root {{ color-scheme: light; --ink:#162016; --muted:#5f6f63; --line:#d8ded3; --panel:#f6f8f2; --accent:#126b54; --warn:#a9501a; --bad:#9b2635; --bg:#fbfcf8; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; font-family:Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background:var(--bg); color:var(--ink); }}
header {{ padding:24px 28px 16px; border-bottom:1px solid var(--line); background:#ffffff; }}
h1 {{ margin:0 0 8px; font-size:28px; line-height:1.15; letter-spacing:0; }}
header p {{ margin:0; color:var(--muted); max-width:980px; }}
main {{ padding:22px 28px 36px; display:grid; gap:22px; }}
.metrics {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(160px,1fr)); gap:10px; }}
.metric {{ border:1px solid var(--line); background:#fff; border-radius:8px; padding:14px 14px 12px; min-height:88px; }}
.metric span {{ display:block; color:var(--muted); font-size:13px; margin-bottom:12px; }}
.metric strong {{ font-size:26px; line-height:1; }}
.panel {{ border:1px solid var(--line); border-radius:8px; background:#fff; padding:16px; overflow:auto; }}
h2 {{ margin:0 0 12px; font-size:18px; letter-spacing:0; }}
.meta {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); gap:10px; color:var(--muted); }}
.meta b {{ color:var(--ink); }}
table {{ width:100%; border-collapse:collapse; font-size:13px; }}
th, td {{ text-align:left; border-bottom:1px solid var(--line); padding:9px 8px; vertical-align:top; }}
th {{ color:var(--muted); font-weight:650; background:var(--panel); }}
.chart {{ width:100%; max-width:760px; height:210px; }}
.chart polyline {{ fill:none; stroke:var(--accent); stroke-width:3; stroke-linecap:round; stroke-linejoin:round; }}
.chart circle {{ fill:#fff; stroke:var(--accent); stroke-width:2; }}
.empty {{ color:var(--muted); border:1px dashed var(--line); border-radius:8px; padding:16px; background:var(--panel); }}
.badge {{ display:inline-block; padding:3px 8px; border-radius:999px; background:#e8f1ec; color:var(--accent); font-size:12px; }}
@media (max-width: 720px) {{ header, main {{ padding-left:16px; padding-right:16px; }} h1 {{ font-size:23px; }} .metric strong {{ font-size:22px; }} }}
</style>
</head>
<body>
<header>
  <h1>Hermes Autoresearch Dashboard</h1>
  <p>Read-only view over local skill audit artifacts. Model route preference: <b>MiniMax-M2.7-highspeed</b> for bounded runner loops when already configured; no provider routing or secret changes are performed by this dashboard.</p>
</header>
<main>
  <section class="metrics">{card_html}</section>
  <section class="panel">
    <h2>Run State</h2>
    <div class="meta">
      <div><b>Generated:</b> {html.escape(generated)}</div>
      <div><b>Audit folder:</b> {html.escape(str(AUDIT))}</div>
      <div><b>Last target:</b> {html.escape(last.get('target', 'none'))}</div>
      <div><b>Last decision:</b> <span class="badge">{html.escape(last.get('decision', 'none'))}</span></div>
      <div><b>Safety gate:</b> read-only dashboard, no mutation endpoints</div>
      <div><b>Stop reason:</b> {html.escape(last.get('eval_result', 'no logged result yet'))}</div>
    </div>
  </section>
  <section class="panel">
    <h2>Score Trend</h2>
    {trend_svg(numeric_scores)}
  </section>
  <section class="panel">
    <h2>Lowest Rubric Scores</h2>
    {html_table(top_risks, risk_columns)}
  </section>
  <section class="panel">
    <h2>Recent Results</h2>
    {html_table(list(reversed(results)), result_columns, limit=20)}
  </section>
</main>
</body>
</html>
"""
    OUTPUT.write_text(page, encoding="utf-8")
    print(f"Dashboard written: {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
