"""Sandbox policy: dangerous command patterns and path rules.

This is a SOFT sandbox: it blocks the most common escape vectors but
cannot resist a determined adversary. For hard isolation, run each
project in a container (planned P5).
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class Violation:
    rule: str
    match: str


# Patterns that are almost always unintended in an agent's bash invocation.
# Kept conservative to avoid false positives; project can extend via Policy().
DEFAULT_BLOCKED_PATTERNS: list[tuple[str, str]] = [
    ("absolute_cd_root", r"(^|[\s;&|])cd\s+/(?:\s|$|;)"),
    ("cd_to_dotdot_root", r"(^|[\s;&|])cd\s+(?:\.\./)+\.\.(?:\s|$|;)"),
    ("rm_rf_root", r"\brm\s+-[^\s]*r[^\s]*f?\s+/(?:\s|$)"),
    ("rm_rf_home", r"\brm\s+-[^\s]*r[^\s]*f?\s+\$HOME\b"),
    ("rm_rf_glob", r"\brm\s+-[^\s]*r[^\s]*f?\s+\*"),
    ("shell_history_clear", r"\bhistory\s+-c\b"),
    ("chmod_777_root", r"\bchmod\s+-R\s*777\s+/(?:\s|$)"),
    ("mkfs", r"\bmkfs(?:\.\w+)?\b"),
    ("dd_to_device", r"\bdd\b.*\bof=/dev/"),
    ("shutdown_reboot", r"\b(?:shutdown|reboot|halt|poweroff)\b"),
    ("sudo", r"(^|[\s;&|])sudo\b"),
    ("fork_bomb", r":\(\)\s*\{\s*:\|:\s*&\s*\}\s*;:"),
]

# Git subcommands allowed in the sandboxed git() entrypoint.
ALLOWED_GIT_SUBCOMMANDS = frozenset({
    "status", "log", "diff", "show", "branch", "worktree",
    "add", "commit", "restore", "stash", "clean", "checkout",
    "ls-files", "rev-parse", "config", "--version",
})

# Environment variables forwarded to subprocesses. Anything else is dropped
# to prevent tenant leakage (e.g. other tenants' tokens).
ALLOWED_ENV_VARS = frozenset({
    "PATH", "LANG", "LC_ALL", "TERM", "SYSTEMROOT", "WINDIR",
    "NUMBER_OF_PROCESSORS", "OS", "PROCESSOR_ARCHITECTURE", "TEMP", "TMP",
})


class Policy:
    def __init__(self,
                 blocked_patterns: list[tuple[str, str]] | None = None,
                 allowed_git: frozenset[str] | None = None,
                 allowed_env: frozenset[str] | None = None):
        self.blocked = blocked_patterns or DEFAULT_BLOCKED_PATTERNS
        self.allowed_git = allowed_git or ALLOWED_GIT_SUBCOMMANDS
        self.allowed_env = allowed_env or ALLOWED_ENV_VARS
        self._compiled = [(n, re.compile(p)) for n, p in self.blocked]

    def scan_command(self, command: str) -> list[Violation]:
        out: list[Violation] = []
        for name, rx in self._compiled:
            m = rx.search(command)
            if m:
                out.append(Violation(rule=name, match=m.group(0)))
        return out

    def check_git_args(self, args: list[str]) -> Violation | None:
        if not args:
            return Violation(rule="git_no_args", match="")
        # Skip leading global flags. -C / --git-dir / --work-tree take a value
        # that would otherwise look like the subcommand.
        value_flags = {"-C", "--git-dir", "--work-tree", "--namespace"}
        i = 0
        while i < len(args) and args[i].startswith("-"):
            if args[i] in value_flags:
                i += 2  # consume the value
            else:
                i += 1
        sub = args[i] if i < len(args) else ""
        if sub not in self.allowed_git:
            return Violation(rule="git_subcommand_not_allowed", match=sub)
        return None

    def filter_env(self, env: dict) -> dict:
        return {k: v for k, v in env.items() if k in self.allowed_env}
