"""Phase C: permissions.toml config loader."""
from __future__ import annotations

from pathlib import Path

from mini_cc.projects.permissions_config import (
    DEFAULT_TIMEOUT_SECONDS, load_permissions_config)


def test_missing_file_returns_none(tmp_path):
    assert load_permissions_config(tmp_path) is None


def test_empty_file_returns_empty_config(tmp_path):
    (tmp_path / ".mini_cc").mkdir()
    (tmp_path / ".mini_cc" / "permissions.toml").write_text("")
    cfg = load_permissions_config(tmp_path)
    assert cfg is not None
    assert cfg.prompt_tools == set()
    assert cfg.timeout_seconds == DEFAULT_TIMEOUT_SECONDS


def test_valid_file_parses(tmp_path):
    (tmp_path / ".mini_cc").mkdir()
    (tmp_path / ".mini_cc" / "permissions.toml").write_text(
        'prompt_tools = ["bash", "fs_write", "fs_edit"]\ntimeout_seconds = 120\n')
    cfg = load_permissions_config(tmp_path)
    assert cfg is not None
    assert cfg.prompt_tools == {"bash", "fs_write", "fs_edit"}
    assert cfg.timeout_seconds == 120


def test_default_timeout_when_unspecified(tmp_path):
    (tmp_path / ".mini_cc").mkdir()
    (tmp_path / ".mini_cc" / "permissions.toml").write_text(
        'prompt_tools = ["bash"]\n')
    cfg = load_permissions_config(tmp_path)
    assert cfg.timeout_seconds == DEFAULT_TIMEOUT_SECONDS


def test_unknown_tools_silently_kept(tmp_path):
    """Per design, unknown tool names are kept in the set — the loop
    just never matches them. (We don't validate against the tool registry.)
    But we DO drop non-string / empty entries."""
    (tmp_path / ".mini_cc").mkdir()
    (tmp_path / ".mini_cc" / "permissions.toml").write_text(
        'prompt_tools = ["bash", "", 42, "fs_write"]\n')
    cfg = load_permissions_config(tmp_path)
    assert cfg is not None
    # Non-strings dropped, empty string dropped; the rest kept.
    assert cfg.prompt_tools == {"bash", "fs_write"}


def test_malformed_toml_returns_none(tmp_path, caplog):
    (tmp_path / ".mini_cc").mkdir()
    (tmp_path / ".mini_cc" / "permissions.toml").write_text(
        'this is not = valid = toml\n')
    cfg = load_permissions_config(tmp_path)
    assert cfg is None


def test_bad_timeout_falls_back_to_default(tmp_path):
    (tmp_path / ".mini_cc").mkdir()
    (tmp_path / ".mini_cc" / "permissions.toml").write_text(
        'prompt_tools = ["bash"]\ntimeout_seconds = -5\n')
    cfg = load_permissions_config(tmp_path)
    assert cfg is not None
    assert cfg.timeout_seconds == DEFAULT_TIMEOUT_SECONDS
