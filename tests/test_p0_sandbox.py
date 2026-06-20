"""P1 sandbox tests — path escape + dangerous command blocking.

These exercise the SubprocessSandbox directly, without an LLM.
"""
from __future__ import annotations

import pytest

from mini_cc.sandbox import (CommandBlockedError, PathEscapeError,
                             SubprocessSandbox)


@pytest.fixture
def sandbox(tmp_path):
    return SubprocessSandbox("test-proj", tmp_path / "ws")


def test_read_write_within_root(sandbox):
    sandbox.write("hello.txt", "line1\nline2\nline3\n")
    assert sandbox.read("hello.txt") == "line1\nline2\nline3"
    assert sandbox.read("hello.txt", offset=1, limit=1) == "line2\n... (1 more lines)"


def test_write_escape_rejected(sandbox):
    with pytest.raises(PathEscapeError):
        sandbox.write("../escape.txt", "boom")
    with pytest.raises(PathEscapeError):
        sandbox.write("/etc/passwd", "boom")


def test_read_escape_rejected(sandbox):
    with pytest.raises(PathEscapeError):
        sandbox.read("../escape.txt")
    with pytest.raises(PathEscapeError):
        sandbox.read("/etc/passwd")


def test_edit_replace(sandbox):
    sandbox.write("f.txt", "foo bar foo")
    sandbox.edit("f.txt", "foo", "qux")
    assert sandbox.read("f.txt") == "qux bar foo"
    sandbox.edit("f.txt", "foo", "qux", replace_all=True)
    assert sandbox.read("f.txt") == "qux bar qux"


def test_glob(sandbox):
    sandbox.write("a.py", "x")
    sandbox.write("b.txt", "y")
    sandbox.write("sub/c.py", "z")
    matches = sandbox.glob("*.py")
    assert "a.py" in matches
    assert "b.txt" not in matches


def test_grep_files_with_matches(sandbox):
    sandbox.write("a.py", "hello world")
    sandbox.write("b.py", "goodbye")
    res = sandbox.grep("hello", output_mode="files_with_matches")
    assert any(r["file"] == "a.py" for r in res)
    assert not any(r["file"] == "b.py" for r in res)


def test_grep_content(sandbox):
    sandbox.write("a.py", "alpha\nbeta\n")
    res = sandbox.grep("beta", output_mode="content")
    assert any(r["line"] == 2 and r["text"] == "beta" for r in res)


def test_bash_executes_in_root(sandbox):
    sandbox.execute("echo hello > out.txt")
    assert sandbox.read("out.txt").strip() == "hello"


def test_bash_cannot_escape_via_cd(sandbox):
    # cd / should be blocked by policy
    with pytest.raises(CommandBlockedError):
        sandbox.execute("cd / && ls")


def test_bash_cannot_rm_rf_root(sandbox):
    with pytest.raises(CommandBlockedError):
        sandbox.execute("rm -rf /")


def test_bash_cannot_sudo(sandbox):
    with pytest.raises(CommandBlockedError):
        sandbox.execute("sudo ls")


def test_bash_env_home_is_project_root(sandbox):
    # HOME must point at the sandbox root, not the real user home.
    # Use python so this works on both bash (POSIX) and cmd.exe (Windows).
    r = sandbox.execute('python -c "import os; print(os.environ.get(\'HOME\'))"')
    assert str(sandbox.project_root) in r.stdout


def test_git_subcommand_whitelist(sandbox):
    # Initialize a repo so git status works
    sandbox.execute("git init")
    sandbox.execute("git config user.email t@t.t")
    sandbox.execute("git config user.name t")
    r = sandbox.git(["status", "--short"])
    assert r.returncode == 0


def test_git_blocks_disallowed_subcommand(sandbox):
    with pytest.raises(CommandBlockedError):
        sandbox.git(["push", "origin", "main"])
