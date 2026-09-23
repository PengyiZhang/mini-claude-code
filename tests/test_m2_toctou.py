"""M2-4 (S1): TOCTOU defense for sandbox fs operations.

``read``/``write``/``edit`` must not just validate-then-open: an
attacker can swap the validated final component for a symlink in the
window between validation and open. The open is fd-based and re-verify
that the opened file is a regular file matching the post-open stat of
the validated path — a swapped symlink raises PathEscapeError.
"""
from __future__ import annotations

import os

import pytest

from mini_cc.sandbox import SubprocessSandbox
from mini_cc.sandbox.base import PathEscapeError


def _sandbox(tmp_path):
    return SubprocessSandbox(project_id="p", project_root=tmp_path)


def _maybe_symlink(target, link_path):
    try:
        os.symlink(str(target), str(link_path))
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"symlink unsupported here: {e}")


def _swap_after_validate(sandbox, monkeypatch, victim: str, secret):
    """Simulate the race: validation passes on the real file, then the
    attacker replaces it with a symlink before open."""
    real_validate = sandbox.validate_path
    victim_path = sandbox.project_root / victim

    def racing_validate(path):
        real_validate(path)
        victim_path.unlink()
        _maybe_symlink(secret, victim_path)
    monkeypatch.setattr(sandbox, "validate_path", racing_validate)


def test_read_detects_final_component_swap(tmp_path, monkeypatch):
    secret = tmp_path / "secret.txt"
    secret.write_text("HOST SECRET", encoding="utf-8")
    root = tmp_path / "proj"
    root.mkdir()
    victim = root / "foo.txt"
    victim.write_text("innocuous", encoding="utf-8")
    sandbox = _sandbox(root)
    _swap_after_validate(sandbox, monkeypatch, victim, secret)
    with pytest.raises(PathEscapeError):
        sandbox.read("foo.txt")


def test_write_detects_final_component_swap(tmp_path, monkeypatch):
    secret = tmp_path / "secret.txt"
    secret.write_text("HOST SECRET", encoding="utf-8")
    root = tmp_path / "proj"
    root.mkdir()
    victim = root / "foo.txt"
    victim.write_text("innocuous", encoding="utf-8")
    sandbox = _sandbox(root)
    _swap_after_validate(sandbox, monkeypatch, victim, secret)
    with pytest.raises(PathEscapeError):
        sandbox.write("foo.txt", "overwrite attempt")


def test_edit_detects_final_component_swap(tmp_path, monkeypatch):
    secret = tmp_path / "secret.txt"
    secret.write_text("HOST SECRET", encoding="utf-8")
    root = tmp_path / "proj"
    root.mkdir()
    victim = root / "foo.txt"
    victim.write_text("innocuous", encoding="utf-8")
    sandbox = _sandbox(root)
    _swap_after_validate(sandbox, monkeypatch, victim, secret)
    with pytest.raises(PathEscapeError):
        sandbox.edit("foo.txt", "innocuous", "evil")


def test_plain_file_operations_still_work(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    sandbox = _sandbox(root)
    sandbox.write("a.txt", "line1\nline2")
    assert "line1" in sandbox.read("a.txt")
    assert sandbox.edit("a.txt", "line1", "LINE1") == "Edited a.txt"
    assert sandbox.read("a.txt").startswith("LINE1")


def test_outright_outside_symlink_still_rejected(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("HOST SECRET", encoding="utf-8")
    root = tmp_path / "proj"
    root.mkdir()
    _maybe_symlink(secret, root / "lnk.txt")
    sandbox = _sandbox(root)
    with pytest.raises(PathEscapeError):
        sandbox.read("lnk.txt")
