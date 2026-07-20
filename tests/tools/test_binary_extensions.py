"""Tests for tools/binary_extensions (pure string classifier, no I/O).

Covers the last-dot suffix rule, case-insensitivity, the deliberately-excluded
.pdf case, dotfiles/edge inputs, and representative membership of the
BINARY_EXTENSIONS frozenset.
"""

from __future__ import annotations

import pytest

from tools.binary_extensions import BINARY_EXTENSIONS, has_binary_extension


class TestHasBinaryExtension:
    @pytest.mark.parametrize(
        "path",
        [
            "image.png",
            "photo.JPG",            # case-insensitive
            "dir/sub/photo.Jpeg",   # path + mixed case
            "archive.tar.gz",       # last dot wins -> .gz
            "song.mp3",
            "program.exe",
            "module.wasm",
            "data.sqlite3",
        ],
    )
    def test_known_binary_extensions_detected(self, path):
        assert has_binary_extension(path) is True

    @pytest.mark.parametrize(
        "path",
        [
            "notes.txt",
            "script.py",
            "README.md",
            "data.tar.txt",   # only the LAST segment counts -> .txt
            "document.pdf",   # explicitly excluded (text-inspectable)
        ],
    )
    def test_text_extensions_not_binary(self, path):
        assert has_binary_extension(path) is False

    @pytest.mark.parametrize(
        "path",
        [
            "noextension",   # no dot at all
            "",              # empty string
            "trailing.",     # dot but empty suffix
            ".gitignore",    # dotfile: suffix is the whole name, not a known ext
        ],
    )
    def test_no_dot_or_unknown_suffix_is_not_binary(self, path):
        assert has_binary_extension(path) is False


class TestExtensionSet:
    def test_is_an_immutable_frozenset(self):
        assert isinstance(BINARY_EXTENSIONS, frozenset)

    def test_representative_categories_present(self):
        for ext in (".png", ".mp4", ".mp3", ".zip", ".exe", ".wasm", ".sqlite", ".ttf"):
            assert ext in BINARY_EXTENSIONS, ext

    def test_pdf_and_text_deliberately_absent(self):
        assert ".pdf" not in BINARY_EXTENSIONS
        assert ".txt" not in BINARY_EXTENSIONS

    def test_all_entries_are_lowercase_dot_prefixed(self):
        for ext in BINARY_EXTENSIONS:
            assert ext.startswith("."), ext
            assert ext == ext.lower(), ext
