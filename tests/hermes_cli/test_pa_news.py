"""Tests for hermes_cli/pa_news.py (Slice B2, KI-News).

Fixtures mirror the REAL research-cron output format (verified live):
a Frontier Desk AM file with a fenced Script Output block and a ``## Response``
section, a ``[SILENT]`` Flash file, a gate-skip file (``wakeAgent=false``, no
Response section), and a Flash single-bullet breaking item. Covers response
extraction, SILENT/gate-skip skipping, newest-first ordering across both jobs,
the 8 KB markdown cap, empty + missing-dir degraded states, the limit clamp,
the filename ts fallback, and the 60 s cache TTL.
"""

import time
from datetime import datetime
from pathlib import Path

import pytest
from fastapi import FastAPI

from hermes_cli import pa_news as mod

DESK_JOB = "5a2a54ac3dae"
FLASH_JOB = "4c88cd4449a6"


# ---------------------------------------------------------------------------
# Real-format fixtures
# ---------------------------------------------------------------------------

# Frontier Desk AM: header + fenced Script Output (with a '## ' line INSIDE the
# fence) + the agent answer under '## Response'. Proves the fence-aware parser
# neither leaks grounding data nor mis-detects the Response start.
AM_FILE = """# Cron Job: Frontier Desk AM

**Job ID:** 5a2a54ac3dae
**Run Time:** 2026-07-20 20:44:21
**Schedule:** 0 8 * * *

## Prompt

[IMPORTANT: You are running as a scheduled cron job. SILENT: If there is
genuinely nothing new to report, respond with exactly "[SILENT]" (nothing else)
to suppress delivery.]

## Script Output
The following data was collected by a pre-run script. Use it as context.

```
GROUNDING-DATEN (Stand 2026-07-20, Lookback 30h).
## Frische Meldungen (offiziell zuerst)
- [community] 2026-07-20 · Example: Testmeldung
```

## Response

[FYI] **🧠 Frontier Desk — 2026-07-20**
`Tag: aktiv` · `Top: Qwen` · `Konfidenz: Medium`

### ⚡ In 30 Sekunden
1. Qwen aktualisiert sein Max-Preview und koppelt es an neue Token-Plan-Tarife; Leistungsclaims sind bisher nicht unabhängig belegt.
2. Kimi bremst wegen GPU-Kapazität Neuabschlüsse. Für bestehende Kunden laut Anbieter kein unmittelbarer Einschnitt.

### 🏗 Was ist neu
- **Qwen — Max-Preview/Token Plan** · [A · Medium]
  Was: Qwen meldet breite Verbesserungen, besonders für Web-Frontend.
"""

SILENT_FILE = """# Cron Job: Frontier Flash

**Job ID:** 4c88cd4449a6
**Run Time:** 2026-07-11 14:01:07
**Schedule:** 0 14 * * *

## Prompt

[IMPORTANT: respond with exactly "[SILENT]" if nothing new.]

## Response

[SILENT]
"""

# Gate-skip: 'wakeAgent=false', NO '## Response' section at all.
GATE_SKIP_FILE = """# Cron Job: KI Modell Breaking-Watch (Mittag)

**Job ID:** 4c88cd4449a6
**Run Time:** 2026-07-20 14:00:17

Script gate returned `wakeAgent=false` — agent skipped.
"""

FLASH_FILE = """# Cron Job: KI Modell Breaking-Watch (Mittag)

**Job ID:** 4c88cd4449a6
**Run Time:** 2026-07-18 14:01:46
**Schedule:** 0 14 * * *

## Response

**⚡ KI-Modell Breaking - 2026-07-18 14:00**
- **Anthropic – Claude Fable 5 Zugangsänderung** [A, 2026-07-18]: Ab **20.07.** ist Fable 5 in allen **Max- und Team-Premium-Plänen** enthalten, jedoch nur bis **50 % der Limits**. Pro-Nutzer erhalten einmalig **100 US-Dollar Guthaben**.
"""


def _am_file(run_time: str, headline: str) -> str:
    return f"""# Cron Job: Frontier Desk AM

**Job ID:** {DESK_JOB}
**Run Time:** {run_time}
**Schedule:** 0 8 * * *

## Response

[FYI] **🧠 {headline}**

### ⚡ In 30 Sekunden
1. Erster Punkt für {headline} mit etwas Fließtext.
"""


def _flash_file(run_time: str, headline: str) -> str:
    return f"""# Cron Job: KI Modell Breaking-Watch (Mittag)

**Job ID:** {FLASH_JOB}
**Run Time:** {run_time}
**Schedule:** 0 14 * * *

## Response

**⚡ {headline}**
- **Lab — Modell** [A]: Eine kurze Breaking-Meldung als Fließtext.
"""


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_cache():
    mod._reset_cache()
    yield
    mod._reset_cache()


@pytest.fixture
def root(tmp_path, monkeypatch):
    """Point the module at a temp output root with both job dirs present."""
    monkeypatch.setenv("HERMES_RESEARCH_CRON_OUTPUT", str(tmp_path))
    (tmp_path / DESK_JOB).mkdir(parents=True, exist_ok=True)
    (tmp_path / FLASH_JOB).mkdir(parents=True, exist_ok=True)
    return tmp_path


def _write(root: Path, job_id: str, filename: str, content: str) -> Path:
    path = root / job_id / filename
    path.write_text(content, encoding="utf-8")
    return path


def _client():
    from starlette.testclient import TestClient

    app = FastAPI()
    mod.register_pa_news_routes(app)
    return TestClient(app)


def _ts(y, mo, d, h, mi, s) -> int:
    return int(datetime(y, mo, d, h, mi, s).timestamp())


# ---------------------------------------------------------------------------
# Extraction / shaping
# ---------------------------------------------------------------------------


def test_extracts_response_from_real_am_format(root):
    _write(root, DESK_JOB, "2026-07-20_20-44-21.md", AM_FILE)
    resp = _client().get("/api/pa/news")
    assert resp.status_code == 200
    data = resp.json()
    assert data["version"] == "pa-news/v1"
    assert data["errors"] == []
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert item["title"] == "🧠 Frontier Desk — 2026-07-20"
    assert item["tag"] == "Frontier Desk"
    assert item["ts"] == _ts(2026, 7, 20, 20, 44, 21)
    # Metadata tuple + headline skipped; first prose (In 30 Sekunden) wins.
    assert item["summary"].startswith("Qwen aktualisiert sein Max-Preview")
    assert len(item["summary"]) <= 200
    # Response body present; fenced Script Output must NOT leak into markdown.
    assert "### ⚡ In 30 Sekunden" in item["markdown"]
    assert "GROUNDING-DATEN" not in item["markdown"]


def test_flash_single_bullet_format(root):
    _write(root, FLASH_JOB, "2026-07-18_14-01-46.md", FLASH_FILE)
    item = _client().get("/api/pa/news").json()["items"][0]
    assert item["title"] == "⚡ KI-Modell Breaking - 2026-07-18 14:00"
    assert item["tag"] == "Frontier Flash"
    assert item["summary"].startswith("Anthropic – Claude Fable 5 Zugangsänderung")


def test_silent_and_gate_skip_yield_no_item(root):
    _write(root, FLASH_JOB, "2026-07-11_14-01-07.md", SILENT_FILE)
    _write(root, FLASH_JOB, "2026-07-20_14-00-17.md", GATE_SKIP_FILE)
    data = _client().get("/api/pa/news").json()
    assert data["items"] == []
    assert data["errors"] == []  # silent skips are NOT errors


def test_newest_first_across_both_jobs(root):
    _write(root, DESK_JOB, "2026-07-20_08-00-00.md", _am_file("2026-07-20 08:00:00", "DeskAlt"))
    _write(root, FLASH_JOB, "2026-07-20_14-00-00.md", _flash_file("2026-07-20 14:00:00", "FlashMid"))
    _write(root, DESK_JOB, "2026-07-21_08-00-00.md", _am_file("2026-07-21 08:00:00", "DeskNeu"))
    items = _client().get("/api/pa/news").json()["items"]
    assert [it["title"] for it in items] == ["🧠 DeskNeu", "⚡ FlashMid", "🧠 DeskAlt"]
    assert items[0]["ts"] > items[1]["ts"] > items[2]["ts"]
    assert [it["tag"] for it in items] == ["Frontier Desk", "Frontier Flash", "Frontier Desk"]


def test_markdown_capped_at_8kb(root):
    big = "Zeile mit etwas Inhalt. " * 800  # ~20 KB
    content = f"""# Cron Job: Frontier Desk AM

**Job ID:** {DESK_JOB}
**Run Time:** 2026-07-20 08:00:00

## Response

[FYI] **🧠 Big Digest**

{big}
"""
    _write(root, DESK_JOB, "2026-07-20_08-00-00.md", content)
    item = _client().get("/api/pa/news").json()["items"][0]
    assert len(item["markdown"].encode("utf-8")) <= mod.MAX_MARKDOWN_BYTES
    assert item["markdown"].endswith(mod._TRUNCATION_MARKER)


def test_ts_falls_back_to_filename(root):
    content = f"""# Cron Job: Frontier Desk AM

**Job ID:** {DESK_JOB}

## Response

[FYI] **🧠 No RunTime**

### ⚡ In 30 Sekunden
1. Inhalt.
"""
    _write(root, DESK_JOB, "2026-07-19_09-30-00.md", content)
    item = _client().get("/api/pa/news").json()["items"][0]
    assert item["ts"] == _ts(2026, 7, 19, 9, 30, 0)


# ---------------------------------------------------------------------------
# Degraded states + contract
# ---------------------------------------------------------------------------


def test_empty_state_returns_200_empty_items(root):
    resp = _client().get("/api/pa/news")  # both job dirs exist but are empty
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []
    assert data["errors"] == []


def test_missing_output_dir_returns_200_with_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_RESEARCH_CRON_OUTPUT", str(tmp_path / "does-not-exist"))
    resp = _client().get("/api/pa/news")
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []
    assert len(data["errors"]) == 2  # both job dirs missing
    assert all("Cron-Output-Verzeichnis fehlt" in e for e in data["errors"])


def test_limit_clamp(root):
    for day, label in [("2026-07-18", "D0"), ("2026-07-19", "D1"), ("2026-07-20", "D2")]:
        _write(root, DESK_JOB, f"{day}_08-00-00.md", _am_file(f"{day} 08:00:00", label))
    client = _client()
    assert len(client.get("/api/pa/news?limit=1").json()["items"]) == 1
    assert len(client.get("/api/pa/news?limit=0").json()["items"]) == 1  # clamped up
    assert len(client.get("/api/pa/news?limit=99").json()["items"]) == 3  # only 3 exist


def test_cache_ttl_second_call_does_not_reread(monkeypatch):
    calls = {"n": 0}

    def fake_collect():
        calls["n"] += 1
        return ([{"title": "x", "ts": 1, "tag": "t", "summary": "", "markdown": ""}], [])

    monkeypatch.setattr(mod, "_collect_items", fake_collect)
    client = _client()
    client.get("/api/pa/news")
    client.get("/api/pa/news")
    assert calls["n"] == 1  # 2nd call within 60 s served from cache
    mod._cache_at = time.monotonic() - mod.CACHE_TTL_S - 1  # expire
    client.get("/api/pa/news")
    assert calls["n"] == 2
