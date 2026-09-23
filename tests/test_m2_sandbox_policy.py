"""M2-1 (S2): shell injection surface.

Two layers:
1. Policy.scan_command structurally rejects newline / carriage-return
   and command substitution ($(...) and backticks) — these let payload
   hide from the deny-list regexes.
2. When no POSIX shell is available, execute() refuses to run rather
   than falling back to ``shell=True`` (cmd.exe), which re-parses the
   raw string under different metachar rules.
"""
from __future__ import annotations

import pytest

from mini_cc.sandbox import SubprocessSandbox
from mini_cc.sandbox.base import CommandBlockedError
from mini_cc.sandbox.policy import Policy


def _rules(violations):
    return {v.rule for v in violations}


# ── structural metachar rejection ───────────────────────────────────

def test_policy_rejects_newline_in_command():
    v = Policy().scan_command("echo hi\nrm -rf /")
    assert "newline_in_command" in _rules(v)


def test_policy_rejects_carriage_return_in_command():
    v = Policy().scan_command("echo hi\r\ndel /s C:\\")
    assert "newline_in_command" in _rules(v)


def test_policy_rejects_dollar_substitution():
    v = Policy().scan_command("rm -rf $(echo /)")
    assert "command_substitution" in _rules(v)


def test_policy_rejects_backtick_substitution():
    v = Policy().scan_command("echo `rm -rf /`")
    assert "command_substitution" in _rules(v)


def test_policy_structural_rules_not_disableable_via_custom_patterns():
    """Custom blocked_patterns must not remove the structural checks —
    they close deny-list bypasses, they are not style preferences."""
    pol = Policy(blocked_patterns=[])
    assert "command_substitution" in _rules(pol.scan_command("echo `x`"))
    assert "newline_in_command" in _rules(pol.scan_command("a\nb"))


def test_policy_clean_command_has_no_structural_violations():
    v = Policy().scan_command("ls -la | grep foo && echo done")
    assert "newline_in_command" not in _rules(v)
    assert "command_substitution" not in _rules(v)


# ── cmd.exe fallback refusal ────────────────────────────────────────

def test_execute_refuses_when_no_posix_shell(tmp_path, monkeypatch):
    import mini_cc.sandbox.subprocess_sandbox as mod
    monkeypatch.setattr(mod, "_find_posix_shell", lambda: None)
    sandbox = SubprocessSandbox(project_id="p", project_root=tmp_path)
    with pytest.raises(CommandBlockedError) as exc:
        sandbox.execute("echo hello")
    assert "no_posix_shell" in _rules(exc.value.violations)
