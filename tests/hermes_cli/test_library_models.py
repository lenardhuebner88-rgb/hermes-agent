"""Bibliothek — Modelle: /api/library/models{,/guide}.

Fixtures are verbatim copies of live llm-wiki artefacts (never synthetic):
``fixtures/model-landscape.md`` (real cron output, 45 rows: 9 providers x 5,
copied 2026-07-09), ``fixtures/benchmarks.json`` (real S2 output, 6 scored
models incl. 2 that overlap the landscape: google/gemini-3.5-flash and
moonshotai/kimi-k2-thinking) and two real S3 guide files
(``fixtures/prompting/gpt5-codex.md``, ``claude-opus.md``) — deliberately NOT
all 8, so a landscape family with no copied guide (e.g. claude-fable) proves
the "no match" path stays graceful (``guide_family: null``), not an error.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hermes_cli import library_models as lm
from hermes_cli import web_server

FIXTURES_DIR = Path(__file__).parent / "fixtures"
_LANDSCAPE_MD = (FIXTURES_DIR / "model-landscape.md").read_text(encoding="utf-8")
_BENCHMARKS_JSON = (FIXTURES_DIR / "benchmarks.json").read_text(encoding="utf-8")


@pytest.fixture
def wiki_home(tmp_path, monkeypatch):
    """Real fixture files laid out under a fake ``Path.home()`` — mirrors
    ``test_library_knowledge.py``'s ``kb_home`` pattern (monkeypatch
    ``Path.home`` globally so every ``_llm_wiki_root()`` call, wherever it's
    imported from, resolves under tmp_path)."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    models_dir = tmp_path / "llm-wiki" / "wiki" / "models"
    data_dir = models_dir / "data"
    prompting_dir = tmp_path / "llm-wiki" / "wiki" / "prompting"
    data_dir.mkdir(parents=True)
    prompting_dir.mkdir(parents=True)
    (models_dir / "model-landscape.md").write_text(_LANDSCAPE_MD, encoding="utf-8")
    (data_dir / "benchmarks.json").write_text(_BENCHMARKS_JSON, encoding="utf-8")
    for guide in ("gpt5-codex", "claude-opus"):
        (prompting_dir / f"{guide}.md").write_text(
            (FIXTURES_DIR / "prompting" / f"{guide}.md").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    return tmp_path


@pytest.fixture
def client():
    """Loopback-TestClient against the real app stack (route wiring + gate)."""
    prev_host = getattr(web_server.app.state, "bound_host", None)
    prev_required = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.bound_host = "127.0.0.1"
    web_server.app.state.auth_required = False
    test_client = TestClient(web_server.app, base_url="http://127.0.0.1:9119")
    yield test_client
    web_server.app.state.bound_host = prev_host
    web_server.app.state.auth_required = prev_required


HEADERS = {"X-Hermes-Session-Token": web_server._SESSION_TOKEN}


# ---------------------------------------------------------------------------
# (a) landscape parser returns every model row, provider/context/price parsed.
# Hand-count from the real fixture: 9 provider sections x 5 rows = 45.
# ---------------------------------------------------------------------------

def test_parse_landscape_returns_every_real_row():
    models = lm.parse_landscape(_strip_frontmatter(_LANDSCAPE_MD))
    assert len(models) == 45
    ids = [m["id"] for m in models]
    assert len(ids) == len(set(ids))  # no duplicates

    sonnet = next(m for m in models if m["id"] == "anthropic/claude-sonnet-5")
    assert sonnet["provider"] == "anthropic"
    assert sonnet["context"] == "1M"
    assert sonnet["price_in"] == 2.00
    assert sonnet["price_out"] == 10.00
    assert sonnet["created"] == "2026-06-30"

    grok = next(m for m in models if m["id"] == "x-ai/grok-4.20-multi-agent")
    assert grok["provider"] == "x-ai"
    assert grok["context"] == "2M"
    assert grok["price_in"] == 1.25
    assert grok["price_out"] == 2.50

    free_llama = next(m for m in models if m["id"] == "meta-llama/llama-3.3-70b-instruct:free")
    assert free_llama["price_in"] == 0.0
    assert free_llama["price_out"] == 0.0


def _strip_frontmatter(raw: str) -> str:
    lines = raw.splitlines()
    assert lines[0].strip() == "---"
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            return "\n".join(lines[idx + 1:])
    raise AssertionError("fixture has no closing frontmatter fence")


# ---------------------------------------------------------------------------
# (b) unknown/renamed table columns -> loud error, never a silent empty list.
# ---------------------------------------------------------------------------

def test_parse_landscape_raises_on_renamed_header():
    broken = (
        "## anthropic\n\n"
        "| Modell-ID | Erstellt | Context-Window | Preis |\n"
        "|---|---|---|---|\n"
        "| `anthropic/claude-sonnet-5` | 2026-06-30 | 1M | $2.00 / $10.00 |\n"
    )
    with pytest.raises(lm.LandscapeParseError):
        lm.parse_landscape(broken)


def test_parse_landscape_raises_on_malformed_price_cell():
    broken = (
        "## anthropic\n\n"
        "| Modell-ID | Erstellt | Kontext | Prompt/Completion pro 1M |\n"
        "|---|---|---|---|\n"
        "| `anthropic/claude-sonnet-5` | 2026-06-30 | 1M | 2.00 EUR / 10.00 EUR |\n"
    )
    with pytest.raises(lm.LandscapeParseError):
        lm.parse_landscape(broken)


def test_parse_landscape_ignores_watchlist_bullets():
    # The real fixture's trailing "## Watchlist" section is bullets, not a
    # table -> must not raise and must not be mistaken for a model row.
    lm.parse_landscape(_strip_frontmatter(_LANDSCAPE_MD))  # no raise


def test_parse_landscape_tolerates_writer_dash_fallback():
    # model-return-watch.py's _fmt_price/_fmt_date fall back to "-" on bad
    # input (real, documented writer behaviour) -> must parse as None, not
    # raise (this is NOT a schema drift, just a legitimate sparse row).
    sparse = (
        "## anthropic\n\n"
        "| Modell-ID | Erstellt | Kontext | Prompt/Completion pro 1M |\n"
        "|---|---|---|---|\n"
        "| `anthropic/claude-mystery` | - | 1M | - / - |\n"
    )
    models = lm.parse_landscape(sparse)
    assert models == [{
        "id": "anthropic/claude-mystery",
        "provider": "anthropic",
        "family": "other",
        "context": "1M",
        "price_in": None,
        "price_out": None,
        "created": None,
    }]


# ---------------------------------------------------------------------------
# family_for_id / provider_for_id (duplicated mapping, benchmark-sync.py parity)
# ---------------------------------------------------------------------------

def test_family_for_id_matches_real_id_shapes():
    assert lm.family_for_id("anthropic/claude-fable-5") == "claude-fable"
    assert lm.family_for_id("anthropic/claude-opus-4.8") == "claude-opus"
    assert lm.family_for_id("anthropic/claude-sonnet-5") == "claude-sonnet-haiku"
    assert lm.family_for_id("openai/gpt-5.1-codex-max") == "gpt5-codex"
    # GPT-5.6 ids map to gpt5-codex via a precise variant prefix.
    assert lm.family_for_id("openai/gpt-5.6-sol") == "gpt5-codex"
    assert lm.family_for_id("openai/gpt-5.6-luna") == "gpt5-codex"
    assert lm.family_for_id("openai/gpt-5.6-terra-pro") == "gpt5-codex"
    assert lm.family_for_id("google/gemini-3.5-flash") == "gemini"
    assert lm.family_for_id("x-ai/grok-4.5") == "other"


# ---------------------------------------------------------------------------
# (c) merge: scores attach by model id, guide_family via S3 exact-match-first.
# ---------------------------------------------------------------------------

def test_build_payload_merges_scores_by_id(wiki_home):
    payload = lm.build_models_payload()
    assert len(payload["models"]) == 45

    gemini = next(m for m in payload["models"] if m["id"] == "google/gemini-3.5-flash")
    assert {s["suite"] for s in gemini["scores"]} == {"osworld-verified", "swe-bench-pro"}
    assert all(s["claimed_by_provider"] is True for s in gemini["scores"])
    assert all(s["source_name"] == "Provider-Angabe (kuratiert)" for s in gemini["scores"])

    kimi = next(m for m in payload["models"] if m["id"] == "moonshotai/kimi-k2-thinking")
    assert len(kimi["scores"]) == 1
    assert kimi["scores"][0]["score"] == 71.3

    # A landscape model with no corresponding benchmarks.json entry still
    # comes back with an empty (not missing) scores list.
    grok = next(m for m in payload["models"] if m["id"] == "x-ai/grok-4.5")
    assert grok["scores"] == []


def test_build_payload_resolves_guide_family_exact_match_first(wiki_home):
    payload = lm.build_models_payload()
    by_id = {m["id"]: m for m in payload["models"]}

    # GPT-5.6 ids now map to gpt5-codex via prefix, and the guide's
    # model_ids list confirms the match (both paths agree).
    for luna_id in ("openai/gpt-5.6-luna", "openai/gpt-5.6-luna-pro"):
        assert by_id[luna_id]["family"] == "gpt5-codex"
        assert by_id[luna_id]["guide_family"] == "gpt5-codex"

    # Normal case: family bucket + guide both say "claude-opus".
    assert by_id["anthropic/claude-opus-4.8"]["family"] == "claude-opus"
    assert by_id["anthropic/claude-opus-4.8"]["guide_family"] == "claude-opus"

    # claude-fable-5's family bucket is "claude-fable", but that guide file
    # was deliberately NOT copied into this fixture -> graceful None, no error.
    assert by_id["anthropic/claude-fable-5"]["family"] == "claude-fable"
    assert by_id["anthropic/claude-fable-5"]["guide_family"] is None

    # x-ai/mistralai/qwen/meta-llama have no guide at all -> always None.
    assert by_id["x-ai/grok-4.5"]["guide_family"] is None


def test_build_payload_includes_guides_list(wiki_home):
    payload = lm.build_models_payload()
    families = {g["family"] for g in payload["guides"]}
    assert families == {"gpt5-codex", "claude-opus"}
    gpt5 = next(g for g in payload["guides"] if g["family"] == "gpt5-codex")
    assert gpt5["maturity"] == "curated"
    assert gpt5["updated"] == "2026-07-09"
    assert gpt5["title"] == "Prompting-Guide: GPT-5.x & Codex"


def test_build_payload_pulse_and_updated_present(wiki_home):
    payload = lm.build_models_payload()
    assert payload["pulse"] == []  # no model-log.md in this fixture -> graceful empty
    assert payload["updated"] == "2026-07-09T18:54:36Z"  # real trailing stamp line


# ---------------------------------------------------------------------------
# (d) guide endpoint: frontmatter + body split for the real guide file.
# ---------------------------------------------------------------------------

def test_guide_detail_splits_frontmatter_and_body(wiki_home):
    detail = lm.get_guide_detail("gpt5-codex")
    assert detail["family"] == "gpt5-codex"
    fm = detail["frontmatter"]
    assert fm["family"] == "gpt5-codex"
    assert "openai/gpt-5.6-luna" in fm["model_ids"]
    assert fm["maturity"] == "curated"
    assert isinstance(fm["sources"], list) and fm["sources"]
    assert detail["body_md"].startswith("# Prompting-Guide: GPT-5.x & Codex")
    assert "<persistence>" in detail["body_md"]


def test_guide_detail_unknown_family_is_none(wiki_home):
    assert lm.get_guide_detail("does-not-exist") is None


def test_guide_detail_invalid_slug_raises_value_error(wiki_home):
    with pytest.raises(ValueError):
        lm.get_guide_detail("../../etc/passwd")


# ---------------------------------------------------------------------------
# (e) missing benchmarks.json -> models still returned with scores: [].
# ---------------------------------------------------------------------------

def test_missing_benchmarks_json_still_returns_all_models(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    models_dir = tmp_path / "llm-wiki" / "wiki" / "models"
    models_dir.mkdir(parents=True)
    (models_dir / "model-landscape.md").write_text(_LANDSCAPE_MD, encoding="utf-8")
    # deliberately no data/benchmarks.json and no prompting/ dir at all.

    payload = lm.build_models_payload()
    assert len(payload["models"]) == 45
    assert all(m["scores"] == [] for m in payload["models"])
    assert payload["guides"] == []


# ---------------------------------------------------------------------------
# Route wiring: session gate + endpoint shape.
# ---------------------------------------------------------------------------

def test_models_endpoint_requires_session_token(client, wiki_home):
    assert client.get("/api/library/models").status_code == 401


def test_models_endpoint_returns_real_payload_shape(client, wiki_home):
    res = client.get("/api/library/models", headers=HEADERS)
    assert res.status_code == 200
    payload = res.json()
    assert len(payload["models"]) == 45
    assert payload["updated"]
    ids = {m["id"] for m in payload["models"]}
    assert "google/gemini-3.5-flash" in ids


def test_guide_endpoint_requires_session_token(client, wiki_home):
    assert client.get("/api/library/models/guide", params={"family": "gpt5-codex"}).status_code == 401


def test_guide_endpoint_returns_body(client, wiki_home):
    res = client.get(
        "/api/library/models/guide", params={"family": "gpt5-codex"}, headers=HEADERS,
    )
    assert res.status_code == 200
    payload = res.json()
    assert payload["family"] == "gpt5-codex"
    assert "Beispiel-Snippets" in payload["body_md"]


def test_guide_endpoint_unknown_family_is_404(client, wiki_home):
    res = client.get(
        "/api/library/models/guide", params={"family": "does-not-exist"}, headers=HEADERS,
    )
    assert res.status_code == 404


def test_guide_endpoint_traversal_family_is_400(client, wiki_home):
    res = client.get(
        "/api/library/models/guide", params={"family": "../../etc/passwd"}, headers=HEADERS,
    )
    assert res.status_code == 400


# ---------------------------------------------------------------------------
# SLICE 2a: GPT-5.6 models map to gpt5-codex family (prefix-based)
# ---------------------------------------------------------------------------

def test_gpt56_ids_map_to_gpt5_codex_family():
    """All 5 GPT-5.6 OpenRouter ids must map to gpt5-codex via the new prefix
    rule. Covers luna/sol/terra variants with and without -pro suffix."""
    gpt56_ids = [
        "openai/gpt-5.6-luna-pro",
        "openai/gpt-5.6-luna",
        "openai/gpt-5.6-terra-pro",
        "openai/gpt-5.6-terra",
        "openai/gpt-5.6-sol-pro",
    ]
    for mid in gpt56_ids:
        assert lm.family_for_id(mid) == "gpt5-codex", \
            f"{mid} should map to gpt5-codex"


def test_gpt5_prefix_does_not_break_existing_families():
    """The GPT-5.6 rule must not classify general GPT-5 models as Codex."""
    # Existing codex-branded id (substring match, not prefix)
    assert lm.family_for_id("openai/gpt-5.1-codex-max") == "gpt5-codex"
    # Claude families
    assert lm.family_for_id("anthropic/claude-fable-5") == "claude-fable"
    assert lm.family_for_id("anthropic/claude-opus-4.8") == "claude-opus"
    assert lm.family_for_id("anthropic/claude-sonnet-5") == "claude-sonnet-haiku"
    # Gemini
    assert lm.family_for_id("google/gemini-3.5-flash") == "gemini"
    # Kimi
    assert lm.family_for_id("moonshotai/kimi-k2-thinking") == "kimi"
    # Non-gpt-5 openai (e.g., gpt-4) stays "other"
    assert lm.family_for_id("openai/gpt-4-turbo") == "other"
    assert lm.family_for_id("openai/gpt-4o") == "other"
    assert lm.family_for_id("openai/gpt-5-chat") == "other"
    assert lm.family_for_id("openai/gpt-5-mini") == "other"
    assert lm.family_for_id("openai/gpt-5.1") == "other"
    assert lm.family_for_id("openai/gpt-5.2") == "other"
    assert lm.family_for_id("openai/gpt-5.4") == "other"
    assert lm.family_for_id("openai/gpt-5.5") == "other"
    # Unrelated providers
    assert lm.family_for_id("x-ai/grok-4.5") == "other"
    assert lm.family_for_id("meta-llama/llama-3.3-70b-instruct:free") == "other"
