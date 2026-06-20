"""Sandbox abstraction.

All file/process operations by agent tools must go through a Sandbox.
The default SubprocessSandbox locks every operation inside a per-project
root directory and runs shell commands with cwd=pwd_root + an env whitelist.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .policy import Policy, Violation


class PathEscapeError(PermissionError):
    pass


class CommandBlockedError(PermissionError):
    def __init__(self, violations: list[Violation]):
        self.violations = violations
        super().__init__(
            "Command blocked by sandbox policy: "
            + ", ".join(v.rule for v in violations))


class Sandbox(Protocol):
    project_id: str
    project_root: Path
    policy: Policy

    def resolve_path(self, rel: str) -> Path: ...
    def validate_path(self, path: Path) -> None: ...
    def read(self, path: str, *, limit: int | None = None, offset: int = 0) -> str: ...
    def write(self, path: str, content: str) -> None: ...
    def edit(self, path: str, old: str, new: str, replace_all: bool = False) -> str: ...
    def glob(self, pattern: str) -> list[str]: ...
    def grep(self, pattern: str, *, output_mode: str = "files_with_matches",
             glob: str | None = None, head_limit: int = 250) -> list[dict]: ...
    def execute(self, command: str, *, timeout: int = 120,
                env: dict | None = None) -> subprocess.CompletedProcess: ...
    def git(self, args: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess: ...
