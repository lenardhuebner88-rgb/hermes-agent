"""Tests for the shared path-validation helpers in tools/path_security.

These guard against path-traversal escapes in the six production tools that
import this module (credential_files, file_tools, skill_manager_tool,
skills_tool, cronjob_tools, tts_tool). The symlink cases matter most:
``validate_within_dir`` must follow symlinks via ``Path.resolve()`` so a link
placed *inside* the allowed root cannot point *outside* it.
"""

import os
from pathlib import Path

from tools.path_security import has_traversal_component, validate_within_dir


class TestValidateWithinDir:
    def test_file_directly_inside_root_is_safe(self, tmp_path: Path):
        root = tmp_path / "root"
        root.mkdir()
        target = root / "file.txt"
        target.write_text("x", encoding="utf-8")
        assert validate_within_dir(target, root) is None

    def test_nested_subdir_is_safe(self, tmp_path: Path):
        root = tmp_path / "root"
        nested = root / "a" / "b" / "c"
        nested.mkdir(parents=True)
        assert validate_within_dir(nested, root) is None

    def test_root_itself_is_safe(self, tmp_path: Path):
        root = tmp_path / "root"
        root.mkdir()
        # A path equal to the root resolves relative to itself.
        assert validate_within_dir(root, root) is None

    def test_parent_escape_returns_error(self, tmp_path: Path):
        root = tmp_path / "root"
        root.mkdir()
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        err = validate_within_dir(outside, root)
        assert err is not None
        assert "escapes allowed directory" in err

    def test_dotdot_escape_returns_error(self, tmp_path: Path):
        root = tmp_path / "root"
        root.mkdir()
        sneaky = root / ".." / "elsewhere"
        err = validate_within_dir(sneaky, root)
        assert err is not None
        assert "escapes allowed directory" in err

    def test_sibling_dir_escape_returns_error(self, tmp_path: Path):
        root = tmp_path / "root"
        sibling = tmp_path / "sibling"
        root.mkdir()
        sibling.mkdir()
        assert validate_within_dir(sibling / "f.txt", root) is not None

    def test_symlink_pointing_outside_root_is_caught(self, tmp_path: Path):
        """Security-critical: a link inside root must not reach outside it."""
        root = tmp_path / "root"
        root.mkdir()
        outside = tmp_path / "secret"
        outside.mkdir()
        secret_file = outside / "creds.txt"
        secret_file.write_text("s3cret", encoding="utf-8")

        link = root / "link"
        os.symlink(secret_file, link)

        err = validate_within_dir(link, root)
        assert err is not None, "symlink escaping root must be rejected"
        assert "escapes allowed directory" in err

    def test_symlink_staying_inside_root_is_safe(self, tmp_path: Path):
        root = tmp_path / "root"
        real = root / "real.txt"
        root.mkdir()
        real.write_text("ok", encoding="utf-8")

        link = root / "alias"
        os.symlink(real, link)

        # Resolves to root/real.txt, still within root.
        assert validate_within_dir(link, root) is None


class TestHasTraversalComponent:
    def test_mid_path_dotdot_is_traversal(self):
        assert has_traversal_component("a/../b") is True

    def test_leading_dotdot_is_traversal(self):
        assert has_traversal_component("../etc/passwd") is True

    def test_plain_path_is_not_traversal(self):
        assert has_traversal_component("a/b/c") is False

    def test_absolute_path_without_dotdot_is_not_traversal(self):
        assert has_traversal_component("/etc/passwd") is False

    def test_dotdot_as_substring_of_name_is_not_traversal(self):
        # 'b..c' is a single filename component, NOT a '..' traversal step.
        assert has_traversal_component("a/b..c/d") is False

    def test_single_dot_is_not_traversal(self):
        assert has_traversal_component("a/./b") is False
