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
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

from .base import CommandBlockedError, PathEscapeError
from .policy import Policy, Violation

# Cached POSIX shell probe. ``None`` until probed; the resolved path (or
# ``""`` sentinel meaning "probed, none found") afterwards.
_POSIX_SHELL: str | None = None


def _terminate_proc(proc, *, grace: float = 2.0) -> None:
    """Best-effort terminate a Popen. SIGTERM first, SIGKILL after grace.

    On Windows, ``terminate()`` == ``TerminateProcess`` (forceful) but
    only kills the direct child — bash's children (e.g. ``sleep``) keep
    their inherited stdout/stderr pipe write-ends open, which would
    hang ``communicate()`` forever. We use ``taskkill /T /F /PID`` to
    tear down the whole tree. On POSIX, ``killpg`` does the equivalent.
    """
    if proc.poll() is not None:
        return
    if os.name == "nt":
        # Windows: tree kill via taskkill. /T = include children, /F = force.
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True, timeout=5)
        except Exception:
            pass
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:
                pass
        return
    # POSIX path: SIGTERM → grace → SIGKILL on the whole group.
    try:
        proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=grace)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        try:
            os.killpg(os.getpgid(proc.pid), 9)
        except (ProcessLookupError, PermissionError):
            pass
        proc.kill()
    except Exception:
        pass


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
    def _open_validated(self, path, flags: int) -> tuple[int, Path]:
        """Validate-then-open atomically enough to close the classic
        TOCTOU (S1): open the VALIDATED resolved path by fd, then
        re-stat that path without following symlinks. If the final
        component became a symlink, or the opened file's identity no
        longer matches what the path now holds, the open raced an
        attacker swap — refuse with PathEscapeError."""
        resolved = (Path(path) if Path(path).is_absolute()
                    else self.project_root / path).resolve()
        self.validate_path(resolved)
        fd = os.open(str(resolved), flags)
        try:
            fst = os.fstat(fd)
            rst = os.stat(str(resolved), follow_symlinks=False)
            if stat.S_ISLNK(rst.st_mode):
                raise PathEscapeError(
                    f"final component became a symlink after validation: "
                    f"{resolved}")
            if not stat.S_ISREG(fst.st_mode):
                raise PathEscapeError(
                    f"not a regular file: {resolved}")
            if (rst.st_dev, rst.st_ino) != (fst.st_dev, fst.st_ino):
                raise PathEscapeError(
                    f"file identity changed between validation and open "
                    f"(possible symlink swap): {resolved}")
        except Exception:
            os.close(fd)
            raise
        return fd, resolved

    def read(self, path, *, limit=None, offset=0):
        fd, _fp = self._open_validated(path, os.O_RDONLY)
        with os.fdopen(fd, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
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
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd, _fp = self._open_validated(path, flags)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)

    def edit(self, path, old, new, replace_all=False):
        fd, _fp = self._open_validated(path, os.O_RDONLY)
        with os.fdopen(fd, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
        if old not in text:
            return f"Error: text not found in {path}"
        if replace_all:
            text = text.replace(old, new)
        else:
            text = text.replace(old, new, 1)
        flags = os.O_WRONLY | os.O_TRUNC
        fd, _fp = self._open_validated(path, flags)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
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

    def execute(self, command, *, timeout=120, env=None, cwd=None,
                cancel_event=None):
        violations = self.policy.scan_command(command)
        if violations:
            raise CommandBlockedError(violations)
        # cwd may be a relative subpath of project_root (validated) or
        # None (default = project_root). We never honor absolute paths
        # here — agents that want to escape must do so explicitly via
        # a tool that surfaces the risk to the user.
        run_cwd = self._resolve_run_cwd(cwd)
        run_env = self._filtered_env(env)
        # Force UTF-8 I/O regardless of the host codepage. On Windows the
        # default locale encoding is often GBK; subprocess.Popen(text=True)
        # uses the locale encoding for its reader threads, which raises
        # UnicodeDecodeError when a child emits a byte sequence that
        # isn't valid GBK (very common with UTF-8-emitting tools, e.g.
        # git log on a repo with non-ASCII commit messages, or any
        # Python child that prints unicode to stdout). errors="replace"
        # keeps the stream decodable even if the child mixes encodings.
        popen_kwargs = dict(
            cwd=str(run_cwd), env=run_env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
        # Prefer a real bash so Unix flags (mkdir -p), pipes, &&, and
        # $VAR expansion behave the same on every platform. Without a
        # POSIX shell we REFUSE to execute (S2): shell=True routes
        # through cmd.exe, which re-parses the raw string under
        # different metachar rules — an unscannable injection surface.
        shell_path = _find_posix_shell()
        if shell_path:
            argv = [shell_path, "-c", command]
        else:
            from .policy import Violation
            raise CommandBlockedError([Violation(
                rule="no_posix_shell",
                match="no bash/sh on PATH; cmd.exe fallback disabled")])

        # Foreground path: cancel_event is None — use subprocess.run,
        # which handles its own timeout via wait(). No cancel needed.
        if cancel_event is None:
            return subprocess.run(argv, timeout=timeout, **popen_kwargs)

        # Background path: own the Popen so cancel_event can kill it
        # mid-flight. Polling at 0.1s keeps cancel latency low without
        # burning CPU. On Windows TerminateProcess is forceful (no
        # SIGTERM); on POSIX we use SIGTERM then SIGKILL after grace.
        return self._run_cancellable(argv, popen_kwargs, timeout, cancel_event)

    @staticmethod
    def _run_cancellable(argv, popen_kwargs, timeout, cancel_event):
        # start_new_session=True on POSIX makes the child a process
        # group leader so we can killpg() the whole tree (bash spawns
        # grand-children that would otherwise survive proc.kill()).
        if os.name == "posix":
            popen_kwargs = dict(popen_kwargs, start_new_session=True)
        proc = subprocess.Popen(argv, **popen_kwargs)
        deadline = time.monotonic() + timeout
        cancelled = False
        try:
            while True:
                if cancel_event.is_set():
                    cancelled = True
                    _terminate_proc(proc)
                    break
                try:
                    proc.wait(timeout=0.1)
                    break
                except subprocess.TimeoutExpired:
                    if time.monotonic() >= deadline:
                        _terminate_proc(proc)
                        raise
        except Exception:
            try:
                _terminate_proc(proc)
            finally:
                pass

        # Cancelled tasks: don't wait on communicate() — child FDs may
        # still be inherited by grand-children on Windows even after
        # tree-kill races. Just close our pipe handles and move on; the
        # cancelled marker in stdout is enough signal.
        if cancelled:
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass
            return subprocess.CompletedProcess(
                args=argv, returncode=-1,
                stdout="[cancelled by task_stop]\n", stderr="")

        try:
            stdout, stderr = proc.communicate(timeout=5)
        except Exception:
            try:
                _terminate_proc(proc)
            finally:
                pass
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except Exception:
                stdout, stderr = "", ""
        return subprocess.CompletedProcess(
            args=argv, returncode=proc.returncode,
            stdout=stdout or "", stderr=stderr or "")

    def _resolve_run_cwd(self, cwd):
        """Resolve a caller-supplied cwd against project_root.

        None → project_root. Relative paths are joined under root and
        validated to stay inside. Absolute paths must already be inside
        root or they're rejected — same rule as read/write.
        """
        if cwd is None:
            return self.project_root
        p = Path(cwd)
        if not p.is_absolute():
            p = self.project_root / p
        p = p.resolve()
        try:
            p.relative_to(self.project_root)
        except ValueError:
            raise PathEscapeError(
                f"cwd escapes project root: {p} (root={self.project_root})")
        return p

    def git(self, args, *, timeout=60):
        v = self.policy.check_git_args(args)
        if v:
            raise CommandBlockedError([v])
        run_env = self._filtered_env(None)
        return subprocess.run(
            ["git"] + [str(a) for a in args],
            cwd=str(self.project_root), env=run_env,
            capture_output=True,
            encoding="utf-8", errors="replace",
            timeout=timeout)
