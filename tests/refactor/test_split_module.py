import textwrap

from scripts.refactor import split_module


def test_analyze_flags_import_time_backward_reference(tmp_path):
    src = tmp_path / "m.py"
    src.write_text(textwrap.dedent("""
        # ---------------------------------------------------------------------------
        # Alpha
        # ---------------------------------------------------------------------------
        FIRST = SECOND + 1

        # ---------------------------------------------------------------------------
        # Beta
        # ---------------------------------------------------------------------------
        SECOND = 2
    """))
    report = split_module.analyze(str(src))
    assert report["import_time_backward"] == [("FIRST", "SECOND")]


def test_analyze_reports_runtime_backward_without_flagging_it_fatal(tmp_path):
    src = tmp_path / "m2.py"
    src.write_text(textwrap.dedent("""
        # ---------------------------------------------------------------------------
        # Alpha
        # ---------------------------------------------------------------------------
        def early():
            return late()

        # ---------------------------------------------------------------------------
        # Beta
        # ---------------------------------------------------------------------------
        def late():
            return 1
    """))
    report = split_module.analyze(str(src))
    assert report["import_time_backward"] == []
    assert report["runtime_backward"] == [("early", "late")]
    assert report["fatal"] is False


def test_analyze_marks_import_time_backward_as_fatal(tmp_path):
    src = tmp_path / "m3.py"
    src.write_text(textwrap.dedent("""
        # ---------------------------------------------------------------------------
        # Alpha
        # ---------------------------------------------------------------------------
        FIRST = SECOND

        # ---------------------------------------------------------------------------
        # Beta
        # ---------------------------------------------------------------------------
        SECOND = 2
    """))
    assert split_module.analyze(str(src))["fatal"] is True


def test_ownership_separates_the_three_buckets(tmp_path, monkeypatch):
    import subprocess

    repo = tmp_path / "r"
    repo.mkdir()
    monkeypatch.chdir(repo)
    subprocess.run(["git", "init", "-q"], check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "config", "user.name", "t"], check=True)

    (repo / "m.py").write_text(
        "SHARED_SAME = 1\n\n\ndef shared_changed():\n    return 1\n")
    subprocess.run(["git", "add", "m.py"], check=True)
    subprocess.run(["git", "commit", "-qm", "up"], check=True)
    subprocess.run(["git", "branch", "-M", "upstream"], check=True)

    (repo / "m.py").write_text(
        "SHARED_SAME = 1\n\n\ndef shared_changed():\n    return 2\n\n\n"
        "def fork_only():\n    return 3\n")

    rep = split_module.ownership("m.py", upstream_ref="upstream")
    assert rep["fork_only"] == ["fork_only"]
    assert rep["upstream_identical"] == ["SHARED_SAME"]
    assert rep["upstream_diverged"] == ["shared_changed"]
