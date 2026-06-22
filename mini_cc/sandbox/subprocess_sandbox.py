"""Default subprocess-based Sandbox implementation.

Behaviour:
- read/write/edit/glob/grep: resolve paths under project_root; reject escapes
  (including via .. or absolute paths).
- execute: scan command against Policy; reject if any violation. Otherwise run
  with cwd=project_root and an env whitelist (HOME=project_root).
- git: cwd=project_root; only allowed subcommands.
"""
from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .base import CommandBlockedError, PathEscapeError
from .policy import Policy, Violation

# Cached POSIX shell probe. ``None`` until probed; the resolved path (or
# ``""`` sentinel meaning "probed, none found") afterwards.
_POSIX_SHELL: str | None = None


def _find_posix_shell() -> str | None:
    """Locate a real bash/sh on PATH (cached).

    ``subprocess.run(..., shell=True)`` routes through ``cmd.exe`` on
    Windows, which rejects Unix flags (``mkdir -p`` creates a directory
    literally named ``-p``) and doesn't expand ``$VAR``/``&&``/pipes the
    way the bash tool's callers expect. The tool is named "bash" and most
    Windows dev boxes ship Git Bash, so we prefer a real bash whenever one
    is on PATH and only fall back to the platform default shell otherwise.
    """
    global _POSIX_SHELL
    if _POSIX_SHELL is not None:
        # "" is our "probed but not found" sentinel — turn it back into None.
        return _POSIX_SHELL or None
    found = shutil.which("bash") or shutil.which("sh")
    _POSIX_SHELL = found if found else ""
    return found


class SubprocessSandbox:
    def __init__(self, project_id: str, project_root: Path,
                 policy: Policy | None = None):
        self.project_id = project_id
        self.project_root = Path(project_root).resolve()
        self.project_root.mkdir(parents=True, exist_ok=True)
        self.policy = policy or Policy()

    # ── Path helpers ────────────────────────────────────────────────────
    def resolve_path(self, rel: str) -> Path:
        p = Path(rel)
        if not p.is_absolute():
            p = self.project_root / p
        return p.resolve()

    def validate_path(self, path: Path) -> None:
        path = path.resolve() if path.is_absolute() else (self.project_root / path).resolve()
        try:
            path.relative_to(self.project_root)
        except ValueError:
            raise PathEscapeError(
                f"Path escapes project root: {path} (root={self.project_root})")

    # ── File operations ─────────────────────────────────────────────────
    def read(self, path, *, limit=None, offset=0):
        fp = self.resolve_path(path)
        self.validate_path(fp)
        text = fp.read_text(encoding="utf-8")
        lines = text.splitlines()
        offset = max(int(offset or 0), 0)
        lines = lines[offset:]
        if limit is not None:
            limit = int(limit)
            if limit < len(lines):
                lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)

    def write(self, path, content):
        fp = self.resolve_path(path)
        self.validate_path(fp)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")

    def edit(self, path, old, new, replace_all=False):
        fp = self.resolve_path(path)
        self.validate_path(fp)
        text = fp.read_text(encoding="utf-8")
        if old not in text:
            return f"Error: text not found in {path}"
        if replace_all:
            text = text.replace(old, new)
        else:
            text = text.replace(old, new, 1)
        fp.write_text(text, encoding="utf-8")
        return f"Edited {path}"

    def glob(self, pattern):
        root = self.project_root
        # Use rglob-style recursive matching for ** patterns.
        matches: list[str] = []
        if "**" in pattern:
            for p in root.rglob(pattern.replace("**/", "").replace("**", "*") or "*"):
                try:
                    rel = p.relative_to(root)
                except ValueError:
                    continue
                matches.append(str(rel))
        else:
            for p in root.glob(pattern):
                try:
                    rel = p.relative_to(root)
                except ValueError:
                    continue
                matches.append(str(rel))
        return sorted(matches)

    def grep(self, pattern, *, output_mode="files_with_matches",
             glob=None, head_limit=250):
        rx = re.compile(pattern)
        results: list[dict] = []
        for fp in self.project_root.rglob("*"):
            if not fp.is_file():
                continue
            if glob and not fnmatch.fnmatch(fp.name, glob):
                continue
            try:
                text = fp.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if output_mode == "files_with_matches":
                if rx.search(text):
                    results.append({"file": str(fp.relative_to(self.project_root))})
            elif output_mode == "content":
                for i, line in enumerate(text.splitlines(), 1):
                    if rx.search(line):
                        results.append({
                            "file": str(fp.relative_to(self.project_root)),
                            "line": i, "text": line[:300]})
                        if len(results) >= head_limit:
                            return results
            elif output_mode == "count":
                results.append({
                    "file": str(fp.relative_to(self.project_root)),
                    "count": len(rx.findall(text))})
            if len(results) >= head_limit:
                break
        return results

    # ── Process execution ───────────────────────────────────────────────
    def _filtered_env(self, extra: dict | None) -> dict:
        env = {k: v for k, v in os.environ.items() if k in self.policy.allowed_env}
        env["HOME"] = str(self.project_root)
        env["USERPROFILE"] = str(self.project_root)  # Windows
        if extra:
            env.update(extra)
        return env

    def execute(self, command, *, timeout=120, env=None):
        violations = self.policy.scan_command(command)
        if violations:
            raise CommandBlockedError(violations)
        run_env = self._filtered_env(env)
        # Prefer a real bash so Unix flags (mkdir -p), pipes, &&, and
        # $VAR expansion behave the same on every platform. Only fall
        # back to shell=True (cmd.exe on Windows) when no bash is found.
        shell_path = _find_posix_shell()
        if shell_path:
            return subprocess.run(
                [shell_path, "-c", command],
                cwd=str(self.project_root), env=run_env,
                capture_output=True, text=True, timeout=timeout)
        return subprocess.run(
            command, shell=True, cwd=str(self.project_root),
            env=run_env, capture_output=True, text=True, timeout=timeout)

    def git(self, args, *, timeout=60):
        v = self.policy.check_git_args(args)
        if v:
            raise CommandBlockedError([v])
        run_env = self._filtered_env(None)
        return subprocess.run(
            ["git"] + [str(a) for a in args],
            cwd=str(self.project_root), env=run_env,
            capture_output=True, text=True, timeout=timeout)
