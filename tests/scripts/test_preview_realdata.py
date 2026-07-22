from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "preview-realdata.sh"


def test_terminal_bridge_scenario_is_explicitly_isolated():
    text = SCRIPT.read_text()
    assert "--scenario" in text and "terminal_bridge" in text
    assert "HERMES_SANDBOX_MODE=1" in text
    assert "HERMES_KANBAN_DB" in text and "TMUX_TMPDIR" in text
    assert "env -u HERMES_KANBAN_TASK" in text
    assert "-u HERMES_KANBAN_WORKSPACE" in text


def test_terminal_bridge_uses_fixture_not_live_database_copy():
    text = SCRIPT.read_text()
    assert 'if [ "$SCENARIO" = "terminal_bridge" ]' in text
    assert "SEED_FIXTURE_DB=1" in text
    assert 'copy_db kanban.db' in text


def test_terminal_bridge_builds_branch_frontend_into_isolated_dist():
    text = SCRIPT.read_text()
    assert 'WEB_DIST="$SEED_HOME/web-dist"' in text
    assert 'HERMES_WEB_DIST="$WEB_DIST" npm run build' in text
    assert 'HERMES_WEB_DIST="$WEB_DIST"' in text


def test_terminal_bridge_seeds_owned_tmux_and_held_candidate_fixture():
    text = SCRIPT.read_text()
    assert 'TMUX_TMPDIR="$SEED_HOME/tmux"' in text
    assert '@hermes_terminal_run_id' in text
    assert 'terminal-runs' in text and 'manifest.json' in text
    assert 'held candidate' in text.lower()
    assert 'tmux kill-server' in text
