"""Characterization tests for pure pet sprite-geometry / state helpers.

``agent/pet/constants.py`` is a leaf module (constants + pure functions, no
I/O, no globals mutation). These tests pin the exact geometry math and the
state→spritesheet-row resolution so the module can be refactored safely.

Expected values are hard-coded (not recomputed from the constants) so a change
to a constant or to the clamping logic surfaces as a real failure.
"""

from agent.pet.constants import (
    CODEX_STATE_ROWS,
    LEGACY_STATE_ROWS,
    MAX_SCALE,
    MIN_SCALE,
    UNICODE_MIN_COLS,
    PetState,
    clamp_scale,
    cols_for_scale,
    resolve_cols,
    state_aliases_for,
    state_row_index,
    state_rows_for_grid,
)

# ─── clamp_scale ────────────────────────────────────────────────────────────


def test_clamp_scale_passes_through_in_range_values():
    assert clamp_scale(0.33) == 0.33
    assert clamp_scale(1.0) == 1.0
    assert clamp_scale(2.5) == 2.5


def test_clamp_scale_clamps_to_floor_and_ceiling():
    assert clamp_scale(0.0) == MIN_SCALE
    assert clamp_scale(-5.0) == MIN_SCALE
    assert clamp_scale(0.05) == MIN_SCALE
    assert clamp_scale(10.0) == MAX_SCALE
    assert clamp_scale(3.5) == MAX_SCALE


def test_clamp_scale_boundary_values_are_inclusive():
    assert clamp_scale(MIN_SCALE) == MIN_SCALE
    assert clamp_scale(MAX_SCALE) == MAX_SCALE


# ─── cols_for_scale ─────────────────────────────────────────────────────────


def test_cols_for_scale_tracks_kitty_cell_box_above_floor():
    # BASE_UNICODE_COLS == 192 // 8 == 24 cells at scale 1.0.
    assert cols_for_scale(1.0) == 24
    assert cols_for_scale(2.0) == 48


def test_cols_for_scale_clamps_to_legibility_floor():
    # 24 * 0.33 ≈ 7.92 → rounds to 8, below UNICODE_MIN_COLS (16) → floor.
    assert cols_for_scale(0.33) == UNICODE_MIN_COLS
    assert cols_for_scale(0.5) == UNICODE_MIN_COLS


def test_cols_for_scale_treats_falsy_scale_as_default():
    # ``scale or DEFAULT_SCALE`` (0.33) → floor for 0, None, and empty.
    assert cols_for_scale(0) == UNICODE_MIN_COLS
    assert cols_for_scale(None) == UNICODE_MIN_COLS  # type: ignore[arg-type]


# ─── resolve_cols ───────────────────────────────────────────────────────────


def test_resolve_cols_prefers_positive_explicit_override():
    assert resolve_cols(1.0, unicode_cols=40) == 40
    assert resolve_cols(1.0, unicode_cols=1) == 1


def test_resolve_cols_coerces_string_override():
    assert resolve_cols(1.0, unicode_cols="30") == 30


def test_resolve_cols_falls_back_to_scale_when_override_absent():
    assert resolve_cols(1.0) == 24
    assert resolve_cols(1.0, unicode_cols=0) == 24
    assert resolve_cols(1.0, unicode_cols=-5) == 24


# ─── state_aliases_for ──────────────────────────────────────────────────────


def test_state_aliases_for_canonical_states():
    assert state_aliases_for(PetState.WAVE) == ("wave", "waving")
    assert state_aliases_for(PetState.JUMP) == ("jump", "jumping")
    assert state_aliases_for(PetState.RUN) == ("run", "running")


def test_state_aliases_for_accepts_string_state():
    assert state_aliases_for("wave") == ("wave", "waving")


def test_state_aliases_for_single_alias_states():
    assert state_aliases_for(PetState.IDLE) == ("idle",)
    assert state_aliases_for(PetState.FAILED) == ("failed",)
    assert state_aliases_for(PetState.REVIEW) == ("review",)
    assert state_aliases_for(PetState.WAITING) == ("waiting",)


def test_state_aliases_for_unknown_state_falls_back_to_self():
    # Always non-empty: unknown names map to a 1-tuple of themselves.
    assert state_aliases_for("nonexistent") == ("nonexistent",)


# ─── state_rows_for_grid ────────────────────────────────────────────────────


def test_state_rows_for_grid_codex_when_enough_rows():
    assert state_rows_for_grid(9) is CODEX_STATE_ROWS
    assert state_rows_for_grid(10) is CODEX_STATE_ROWS
    assert state_rows_for_grid(12) is CODEX_STATE_ROWS


def test_state_rows_for_grid_legacy_below_codex_threshold():
    assert state_rows_for_grid(8) is LEGACY_STATE_ROWS
    assert state_rows_for_grid(1) is LEGACY_STATE_ROWS
    assert state_rows_for_grid(0) is LEGACY_STATE_ROWS
    assert state_rows_for_grid(None) is LEGACY_STATE_ROWS


def test_state_rows_for_grid_coerces_and_guards_input():
    assert state_rows_for_grid("9") is CODEX_STATE_ROWS
    assert state_rows_for_grid("8") is LEGACY_STATE_ROWS
    # Non-numeric → treated as 0 → legacy.
    assert state_rows_for_grid("abc") is LEGACY_STATE_ROWS  # type: ignore[arg-type]


# ─── state_row_index ────────────────────────────────────────────────────────


def test_state_row_index_idle_is_row_zero_in_both_grids():
    assert state_row_index(PetState.IDLE, 9) == 0
    assert state_row_index(PetState.IDLE, 8) == 0
    assert state_row_index(PetState.IDLE) == 0  # default → legacy


def test_state_row_index_resolves_through_aliases():
    # Canonical "wave" is not a Codex row name; its alias "waving" is (index 3).
    assert state_row_index(PetState.WAVE, 9) == 3
    # In the legacy grid "wave" is a direct row (index 1).
    assert state_row_index(PetState.WAVE, 8) == 1


def test_state_row_index_direct_matches():
    assert state_row_index(PetState.FAILED, 9) == 5
    assert state_row_index(PetState.REVIEW, 9) == 8
    assert state_row_index(PetState.FAILED, 8) == 3


def test_state_row_index_unknown_state_falls_back_to_idle_row():
    assert state_row_index("nonexistent", 9) == 0
    assert state_row_index("nonexistent") == 0
