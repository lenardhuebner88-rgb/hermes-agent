#!/usr/bin/env python3
"""Generate a standalone read-only HTML dashboard from Autoresearch audit files."""
from __future__ import annotations

import csv
import html
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
AUDIT = REPO / ".hermes" / "skill-audit"
RUBRIC = AUDIT / "skill_quality_rubric.md"
RESULTS = AUDIT / "autoresearch_results.tsv"
INVENTORY = AUDIT / "skills_inventory.md"
OUTPUT = AUDIT / "dashboard.html"
MODEL_PREFERENCE = "MiniMax-M2.7-highspeed"
AREAS = [
    "all",
    "devops",
    "github",
    "software-development",
    "research",
    "productivity",
    "mlops",
    "creative",
    "firecrawl",
    "hermes-kanban",
]
WEAKNESSES = {
    "safety_gates": "missing/weak safety gates",
    "output_contract": "missing/weak output contract",
    "activation_criteria": "missing activation criteria",
    "workflow": "weak workflow",
    "evalability": "weak evalability",
    "maintainability": "maintainability risk",
}


def parse_int(value: str, default: int = 999) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def parse_rubric() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    if not RUBRIC.exists():
        return rows
    for line in RUBRIC.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("|") or "---" in line or line.lower().startswith("| skill "):
            continue
        parts = [p.strip() for p in line.strip("|").split("|")]
        if len(parts) != 10:
            continue
        rows.append(
            {
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
            }
        )
    return rows


def parse_results() -> list[dict[str, str]]:
    if not RESULTS.exists() or RESULTS.stat().st_size == 0:
        return []
    with RESULTS.open("r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def extract_inventory() -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    summary: dict[str, str] = {}
    inventory: dict[str, dict[str, str]] = {}
    if not INVENTORY.exists():
        return summary, inventory
    current: dict[str, str] | None = None
    for line in INVENTORY.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("- ") and current is None:
            match = re.match(r"- ([^:]+):\s*(.+)", line)
            if match:
                summary[match.group(1)] = match.group(2)
            continue
        heading = re.match(r"###\s+\d+\.\s+(.+)", line)
        if heading:
            current = {"skill": heading.group(1).strip()}
            inventory[current["skill"]] = current
            continue
        if current and line.startswith("- "):
            key, _, value = line[2:].partition(":")
            if value:
                current[key.strip().lower()] = value.strip().strip("`")
    return summary, inventory


def area_from_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    if "/skills/hermes-kanban/" in normalized or "/devops/kanban-" in normalized or "/devops/hermes-kanban-" in normalized:
        return "hermes-kanban"
    for area in AREAS:
        if area != "all" and f"/{area}/" in normalized:
            return area
    if "/firecrawl-" in normalized or "/firecrawl_" in normalized:
        return "firecrawl"
    if "/mlops/" in normalized or "/mlops-" in normalized:
        return "mlops"
    return "all"


def weakness_keys(row: dict[str, str]) -> list[str]:
    keys: list[str] = []
    if parse_int(row.get("safety", "3"), 3) < 3:
        keys.append("safety_gates")
    if parse_int(row.get("output", "3"), 3) < 3:
        keys.append("output_contract")
    if parse_int(row.get("activation", "3"), 3) < 3:
        keys.append("activation_criteria")
    if parse_int(row.get("workflow", "3"), 3) < 3:
        keys.append("workflow")
    if parse_int(row.get("eval", "3"), 3) < 3:
        keys.append("evalability")
    if parse_int(row.get("maintain", "3"), 3) < 3:
        keys.append("maintainability")
    return keys


def enrich_rows(rubric_rows: list[dict[str, str]], inventory: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for row in rubric_rows:
        item = dict(row)
        inv = inventory.get(row["skill"], {})
        path = inv.get("pfad", "")
        item["path"] = path
        item["area"] = area_from_path(path)
        item["weaknesses"] = weakness_keys(row)
        item["weakness_labels"] = [WEAKNESSES[key] for key in item["weaknesses"]]
        enriched.append(item)
    return enriched


def campaign_cards(results: list[dict[str, str]]) -> list[dict[str, str]]:
    cards: list[dict[str, str]] = []
    for row in reversed(results[-12:]):
        cards.append(
            {
                "timestamp": row.get("timestamp", ""),
                "mode": row.get("mode", ""),
                "target": row.get("target", ""),
                "hypothesis": row.get("hypothesis", ""),
                "decision": row.get("decision", ""),
                "risk": row.get("risk", ""),
                "evidence": row.get("evidence", ""),
            }
        )
    return cards


def recommended_actions(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    candidates = sorted(rows, key=lambda row: (parse_int(row.get("total", "999")), row.get("skill", "")))
    actions: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for row in candidates:
        for weakness in row.get("weaknesses", []):
            key = (row.get("area", "all"), weakness)
            if key in seen:
                continue
            seen.add(key)
            area = row.get("area", "all") if row.get("area") != "all" else "all"
            command = (
                "python3 scripts/autoresearch_request.py create "
                f"--mode skills --area {area} --focus {weakness} --max-iterations 3 "
                "--mutation-policy requires_operator_go"
            )
            actions.append(
                {
                    "area": area,
                    "focus": weakness,
                    "label": f"Improve {WEAKNESSES[weakness]} in {area} skills",
                    "example_skill": row.get("skill", ""),
                    "command": command,
                }
            )
            break
        if len(actions) >= 8:
            break
    return actions


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


def html_table(rows: list[dict[str, Any]], columns: list[str], limit: int | None = None) -> str:
    rows = rows[:limit] if limit else rows
    if not rows:
        return '<div class="empty">No rows yet</div>'
    head = "".join(f"<th>{html.escape(col)}</th>" for col in columns)
    body_rows = []
    for row in rows:
        cells = "".join(f"<td>{html.escape(str(row.get(col, '')))}</td>" for col in columns)
        attrs = []
        if "area" in row:
            attrs.append(f'data-area="{html.escape(str(row.get("area", "all")))}"')
        if "weaknesses" in row:
            attrs.append(f'data-weaknesses="{html.escape(" ".join(row.get("weaknesses", [])))}"')
        body_rows.append(f"<tr {' '.join(attrs)}>{cells}</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>"


def numeric_scores_from_results(results: list[dict[str, str]]) -> list[float]:
    numeric_scores = []
    for row in results:
        for key in ("score_after", "score", "total"):
            try:
                numeric_scores.append(float(row.get(key, "")))
                break
            except (TypeError, ValueError):
                continue
    return numeric_scores


def build_dashboard_data(enriched_rows: list[dict[str, Any]], results: list[dict[str, str]], summary: dict[str, str]) -> dict[str, Any]:
    counts = {"high": 0, "medium": 0, "low": 0}
    area_counts = {area: 0 for area in AREAS}
    area_counts["all"] = len(enriched_rows)
    weakness_counts = {key: 0 for key in WEAKNESSES}
    for row in enriched_rows:
        pri = str(row.get("priority", "")).lower()
        if pri in counts:
            counts[pri] += 1
        area = row.get("area", "all")
        if area != "all":
            area_counts[area] = area_counts.get(area, 0) + 1
        for weakness in row.get("weaknesses", []):
            weakness_counts[weakness] += 1
    decisions: dict[str, int] = {}
    for row in results:
        decision = (row.get("decision") or "unknown").lower()
        decisions[decision] = decisions.get(decision, 0) + 1
    return {
        "schema": "autoresearch-dashboard-data-v1",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "audit_folder": str(AUDIT),
        "model_preference": MODEL_PREFERENCE,
        "model_route_status": "unverified",
        "inventory_summary": summary,
        "priority_counts": counts,
        "area_counts": area_counts,
        "weakness_counts": weakness_counts,
        "decision_counts": decisions,
        "results_count": len(results),
        "recommended_actions": recommended_actions(enriched_rows),
        "campaign_cards": campaign_cards(results),
        "safety_gate": "read-only; requires operator Go; no mutation endpoints; no provider routing or secrets changes",
    }


def main() -> int:
    AUDIT.mkdir(parents=True, exist_ok=True)
    rubric_rows = parse_rubric()
    results = parse_results()
    summary, inventory = extract_inventory()
    enriched_rows = enrich_rows(rubric_rows, inventory)
    data = build_dashboard_data(enriched_rows, results, summary)
    counts = data["priority_counts"]
    decisions = data["decision_counts"]
    last = results[-1] if results else {}
    generated = data["generated_at"]
    cards = [
        ("Inventoried skills", summary.get("Total SKILL.md files inventoried", str(len(rubric_rows)))),
        ("High priority", str(counts["high"])),
        ("Medium priority", str(counts["medium"])),
        ("Low priority", str(counts["low"])),
        ("Iterations logged", str(len(results))),
        ("Kept", str(decisions.get("keep", 0))),
        ("Blocked", str(decisions.get("blocked", 0))),
        ("Discarded", str(decisions.get("discard", 0))),
    ]
    card_html = "".join(
        f'<section class="metric"><span>{html.escape(label)}</span><strong>{html.escape(value)}</strong></section>'
        for label, value in cards
    )
    result_columns = ["timestamp", "mode", "target", "hypothesis", "decision", "risk", "evidence"]
    risk_columns = ["skill", "area", "safety", "output", "activation", "workflow", "eval", "total", "priority"]
    top_risks = sorted(enriched_rows, key=lambda r: parse_int(str(r.get("total", "999"))))[:24]
    actions_html = "".join(
        "<article class=\"action\">"
        f"<b>{html.escape(action['label'])}</b>"
        f"<p>Example: {html.escape(action['example_skill'])}</p>"
        f"<code>{html.escape(action['command'])}</code>"
        "</article>"
        for action in data["recommended_actions"]
    ) or '<div class="empty">No recommended actions yet</div>'
    campaigns_html = "".join(
        "<article class=\"campaign\">"
        f"<span>{html.escape(card['timestamp'])} · {html.escape(card['decision'])}</span>"
        f"<h3>{html.escape(card['target'])}</h3>"
        f"<p>{html.escape(card['hypothesis'])}</p>"
        f"<small>Risk: {html.escape(card['risk'])} · Evidence: {html.escape(card['evidence'])}</small>"
        "</article>"
        for card in data["campaign_cards"]
    ) or '<div class="empty">No campaign cards yet</div>'
    data_json = json.dumps(data, ensure_ascii=False, sort_keys=True).replace("</", "<\\/")
    area_options = "".join(f'<option value="{html.escape(area)}">{html.escape(area)}</option>' for area in AREAS)
    weakness_options = "".join(
        f'<option value="{html.escape(key)}">{html.escape(label)}</option>' for key, label in WEAKNESSES.items()
    )
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
.metric, .panel, .action, .campaign {{ border:1px solid var(--line); background:#fff; border-radius:8px; }}
.metric {{ padding:14px 14px 12px; min-height:88px; }}
.metric span {{ display:block; color:var(--muted); font-size:13px; margin-bottom:12px; }}
.metric strong {{ font-size:26px; line-height:1; }}
.panel {{ padding:16px; overflow:auto; }}
h2 {{ margin:0 0 12px; font-size:18px; letter-spacing:0; }}
h3 {{ margin:4px 0 8px; font-size:15px; }}
.meta, .controls {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:10px; color:var(--muted); }}
.meta b {{ color:var(--ink); }}
.banner {{ border:1px solid #efcf9d; background:#fff8e9; color:#5d3b00; border-radius:8px; padding:12px 14px; }}
select, input, button {{ width:100%; border:1px solid var(--line); border-radius:8px; padding:9px 10px; background:#fff; color:var(--ink); }}
button {{ cursor:pointer; background:#e8f1ec; color:var(--accent); font-weight:700; }}
table {{ width:100%; border-collapse:collapse; font-size:13px; }}
th, td {{ text-align:left; border-bottom:1px solid var(--line); padding:9px 8px; vertical-align:top; }}
th {{ color:var(--muted); font-weight:650; background:var(--panel); }}
.chart {{ width:100%; max-width:760px; height:210px; }}
.chart polyline {{ fill:none; stroke:var(--accent); stroke-width:3; stroke-linecap:round; stroke-linejoin:round; }}
.chart circle {{ fill:#fff; stroke:var(--accent); stroke-width:2; }}
.empty {{ color:var(--muted); border:1px dashed var(--line); border-radius:8px; padding:16px; background:var(--panel); }}
.badge {{ display:inline-block; padding:3px 8px; border-radius:999px; background:#e8f1ec; color:var(--accent); font-size:12px; }}
.action-grid, .campaign-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:10px; }}
.action, .campaign {{ padding:12px; }}
.action p, .campaign p {{ color:var(--muted); margin:6px 0; }}
.action code, #commandPreview {{ display:block; white-space:pre-wrap; overflow-wrap:anywhere; border:1px solid var(--line); border-radius:8px; padding:10px; background:#0f1a14; color:#e8f7ec; }}
.hidden {{ display:none; }}
@media (max-width: 720px) {{ header, main {{ padding-left:16px; padding-right:16px; }} h1 {{ font-size:23px; }} .metric strong {{ font-size:22px; }} }}
</style>
<script type="application/json" id="data-autoresearch">{data_json}</script>
</head>
<body data-autoresearch="dashboard-v1">
<header>
  <h1>Hermes Autoresearch Dashboard</h1>
  <p>Read-only view over local skill audit artifacts. Model route preference: <b>{MODEL_PREFERENCE}</b> for bounded runner loops when already configured; requires operator Go before any mutation.</p>
</header>
<main>
  <section class="banner"><b>Safety:</b> read-only dashboard; No secrets, no provider routing change, no push/merge, no runtime mutation, no POST execution. Any run request requires operator Go.</section>
  <section class="metrics">{card_html}</section>
  <section class="panel">
    <h2>Run State</h2>
    <div class="meta">
      <div><b>Generated:</b> {html.escape(generated)}</div>
      <div><b>Audit folder:</b> {html.escape(str(AUDIT))}</div>
      <div><b>Model route:</b> {MODEL_PREFERENCE} / unverified</div>
      <div><b>Last target:</b> {html.escape(last.get('target', 'none'))}</div>
      <div><b>Last decision:</b> <span class="badge">{html.escape(last.get('decision', 'none'))}</span></div>
      <div><b>Safety gate:</b> read-only, requires operator Go, no mutation endpoints</div>
      <div><b>Stop reason:</b> {html.escape(last.get('eval_result', 'no logged result yet'))}</div>
    </div>
  </section>
  <section class="panel">
    <h2>Filters and request command preview</h2>
    <div class="controls">
      <label>Area filter<select id="areaFilter"><option value="all">all</option>{''.join(f'<option value="{html.escape(area)}">{html.escape(area)}</option>' for area in AREAS if area != 'all')}</select></label>
      <label>Weakness filter<select id="weaknessFilter"><option value="all">all</option>{weakness_options}</select></label>
      <label>Iterations<input id="iterationCap" type="number" min="1" max="5" value="3"></label>
      <label>Generate<button id="generateCommand" type="button">Generate Request Command</button></label>
    </div>
    <p class="empty" id="visibleCount">Filter not applied yet</p>
    <code id="commandPreview">Select filters, then generate a copy-paste command. This does not execute anything.</code>
  </section>
  <section class="panel">
    <h2>Recommended next actions</h2>
    <div class="action-grid">{actions_html}</div>
  </section>
  <section class="panel">
    <h2>Campaign cards</h2>
    <div class="campaign-grid">{campaigns_html}</div>
  </section>
  <section class="panel">
    <h2>Score Trend</h2>
    {trend_svg(numeric_scores_from_results(results))}
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
<script>
const rows = Array.from(document.querySelectorAll('tr[data-area]'));
const areaFilter = document.getElementById('areaFilter');
const weaknessFilter = document.getElementById('weaknessFilter');
const iterationCap = document.getElementById('iterationCap');
const visibleCount = document.getElementById('visibleCount');
const commandPreview = document.getElementById('commandPreview');
function selectedArea() {{ return areaFilter.value || 'all'; }}
function selectedWeakness() {{ return weaknessFilter.value || 'safety_gates'; }}
function applyFilters() {{
  let count = 0;
  for (const row of rows) {{
    const areaOk = selectedArea() === 'all' || row.dataset.area === selectedArea();
    const weaknessOk = weaknessFilter.value === 'all' || (row.dataset.weaknesses || '').split(' ').includes(weaknessFilter.value);
    const show = areaOk && weaknessOk;
    row.classList.toggle('hidden', !show);
    if (show) count++;
  }}
  visibleCount.textContent = `${{count}} rubric rows match current read-only filters.`;
}}
function generateCommand() {{
  const area = selectedArea();
  const focus = selectedWeakness() === 'all' ? 'safety_gates_and_output_contracts' : selectedWeakness();
  const iterations = Math.min(5, Math.max(1, Number(iterationCap.value || 1)));
  commandPreview.textContent = `cd /home/piet/.hermes/hermes-agent && python3 scripts/autoresearch_request.py create --mode skills --area ${{area}} --focus ${{focus}} --max-iterations ${{iterations}} --mutation-policy requires_operator_go`;
}}
areaFilter.addEventListener('change', applyFilters);
weaknessFilter.addEventListener('change', applyFilters);
document.getElementById('generateCommand').addEventListener('click', generateCommand);
applyFilters();
</script>
</body>
</html>
"""
    OUTPUT.write_text(page, encoding="utf-8")
    print(f"Dashboard written: {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
