"""Host-specific gating in ``hermes_cli.gateway._all_platforms()``.

Some messaging platforms can't function on every host. The gate lives
in one place — ``_all_platforms()`` — so the setup wizard, the curses
gateway-config menu, and any future picker all see the same filtered
list.

Currently:
- Matrix is hidden on Windows. The ``[matrix]`` extra pulls
  ``mautrix[encryption]`` -> ``python-olm``, which has no Windows wheel
  and needs ``make`` + libolm to build from sdist. There's no native
  Windows path that works.
"""



class TestMatrixHiddenOnWindows:
    def test_matrix_present_on_linux(self, monkeypatch):
        """Sanity: matrix is still in the picker on Linux/macOS."""
        import hermes_cli.gateway as gateway_mod

        monkeypatch.setattr(gateway_mod.sys, "platform", "linux")
        platforms = gateway_mod._all_platforms()
        keys = {p["key"] for p in platforms}
        assert "matrix" in keys, "matrix must be available on Linux"

    def test_matrix_present_on_macos(self, monkeypatch):
        import hermes_cli.gateway as gateway_mod

        monkeypatch.setattr(gateway_mod.sys, "platform", "darwin")
        platforms = gateway_mod._all_platforms()
        keys = {p["key"] for p in platforms}
        assert "matrix" in keys, "matrix must be available on macOS"

    def test_matrix_hidden_on_windows(self, monkeypatch):
        """The actual gate: matrix must NOT appear on Windows."""
        import hermes_cli.gateway as gateway_mod

        monkeypatch.setattr(gateway_mod.sys, "platform", "win32")
        platforms = gateway_mod._all_platforms()
        keys = {p["key"] for p in platforms}
        assert "matrix" not in keys, (
            "matrix must be hidden on Windows — python-olm has no "
            "Windows wheel and no native build path"
        )

    def test_other_platforms_unaffected_on_windows(self, monkeypatch):
        """Gating must only drop matrix, not collateral damage."""
        import hermes_cli.gateway as gateway_mod

        monkeypatch.setattr(gateway_mod.sys, "platform", "win32")
        platforms = gateway_mod._all_platforms()
        keys = {p["key"] for p in platforms}
        # A representative sample of platforms that have no Windows
        # blockers — picker should still surface them.
        for must_have in ("telegram", "discord", "slack", "mattermost"):
            assert must_have in keys, (
                f"{must_have} disappeared from Windows picker — gate is "
                "over-filtering"
            )

    def test_mattermost_uses_plugin_setup_and_connection_authority(self, monkeypatch):
        """The stale built-in entry must not mask Mattermost's plugin hooks."""
        import hermes_cli.gateway as gateway_mod

        monkeypatch.setattr(gateway_mod.sys, "platform", "linux")
        platforms = gateway_mod._all_platforms()
        mattermost = next(p for p in platforms if p["key"] == "mattermost")

        entry = mattermost.get("_registry_entry")
        assert entry is not None
        assert entry.setup_fn is not None
        assert entry.is_connected is not None

        monkeypatch.setattr(
            gateway_mod,
            "get_env_value",
            lambda key: "token-only" if key == "MATTERMOST_TOKEN" else None,
        )
        assert gateway_mod._platform_status(mattermost) == "not configured"
